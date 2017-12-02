#!/usr/bin/env python2
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Packages firmware images into an executable "shell-ball".

It requires:
 - at least one firmware image (*.bin, should be AP or EC or ...)
 - pack_dist/updater.sh main script
 - pack_stub as the template/stub script for output
 - any other additional files used by updater.sh in pack_dist folder
"""

from __future__ import print_function

from collections import namedtuple, OrderedDict
import glob
import md5
import os
import re
import shutil
import struct
import sys
from StringIO import StringIO
import tarfile
import tempfile

from chromite.lib import commandline
from chromite.lib import cros_build_lib
from chromite.lib import osutils

from libcros_config_host import libcros_config_host

sys.path.append('utils')
import merge_file

IMAGE_MAIN = 'bios.bin'
IMAGE_MAIN_RW = 'bios_rw.bin'
IMAGE_EC = 'ec.bin'
IMAGE_PD = 'pd.bin'
MODELS_DIR = 'models'
Section = namedtuple('Section', ['offset', 'size'])
ImageFile = namedtuple('ImageFile', ['filename', 'version'])

# image_files:  Dict containing information on the output image files for this
#     model:
#   key: Image name (e.g. 'BIOS (RW)' or 'EC').
#   value: ImageFile, containing filename and version.
# shared_model: shared model name if this model shares firmware with another
#     model (or None if it does not)
# key_id: key ID to use to sign the image for this model
ModelDetails = namedtuple('ModelDetails',
                          ['image_files', 'shared_model', 'key_id'])

# File execution permissions. We could use state.S_... but that's confusing.
CHMOD_ALL_READ = 0444
CHMOD_ALL_EXEC = 0555

# For testing
packer = None

# Variables that we replace in pack_stub and setvars_template.
REPLACE_VARS = [
    'TARGET_RO_FWID',
    'TARGET_FWID',
    'TARGET_ECID',
    'TARGET_PDID',
    'TARGET_PLATFORM',
]

class PackError(Exception):
  """Exception returned by FirmwarePacker when something goes wrong"""
  pass


class FirmwarePacker(object):
  """Handles building a shell-ball firmware update.

  Most member functions raise an exception on error. This can be
  RunCommandError if an executed tool fails, or PackError on some other error.

  Private members:
    _args: Parsed arguments.
    _pack_dist: Path to 'pack_dist' directory.
    _script_base: Base directory with useful files (src/platform/firmware).
    _stub_file: Path to 'pack_stub'.
    _shflags_file: Path to shflags script.
    _testing: True if running tests.
    _basedir: Base temporary directory.
    _tmpdir: Temporary directory for use for running tools.
    _tmp_dirs: List of temporary directories created.
    _versions: Collected version information (StringIO).
    _force_dash: Replace the /bin/sh shebang at the top of all scripts with
        /bin/dash. This is only used for testing.
  """

  def __init__(self, progname):
    # This may or may not provide the full path to the script, but in any case
    # we can access the script files using the same path as the script.
    self._script_base = os.path.dirname(progname)
    self._args = None
    self._pack_dist = os.path.join(self._script_base, 'pack_dist')
    self._stub_file = os.path.join(self._script_base, 'pack_stub')
    self._setvars_template_file = os.path.join(self._script_base,
                                               'setvars_template')
    self._shflags_file = os.path.join(self._script_base, 'lib/shflags/shflags')
    self._testing = False
    self._basedir = None
    self._tmpdir = None
    self._tmp_dirs = []
    self._versions = StringIO()
    self._force_dash = False

  @staticmethod
  def ParseArgs(argv):
    """Parse the available arguments.

    Invalid arguments or -h cause this function to print a message and exit.

    Args:
      argv: List of string arguments (excluding program name / argv[0])

    Returns:
      argparse.Namespace object containing the attributes.
    """
    parser = commandline.ArgumentParser(description=__doc__)
    parser.add_argument('-L', '--legacy', action='store_true',
                        help='Legacy mode for use with boards not using '
                        'unified builds')
    parser.add_argument('-m', '--model', type=str, dest='models',
                        action='append',
                        help='Model name to include in firmware update')
    parser.add_argument('-c', '--config', type='path',
                        help='Filename of master configuration .dtb file')
    parser.add_argument(
        '-l', '--local', action='store_true',
        help='Build a local firmware image. With this option you must provide '
             '-b, -e and -p flags to indicate where to find the images for '
             'each model. You can use MODEL in the filenames as a placeholder '
             'for the model name. For example '
             '-b "${root}/firmware/image-BUILD_TARGET.bin"')
    parser.add_argument('-i', '--imagedir', type='path', default='.',
                        help='Default locations for source images')
    parser.add_argument('-b', '--bios_image', type='path',
                        help='Path of input AP (BIOS) firmware image')
    parser.add_argument('-w', '--bios_rw_image', type='path',
                        help='Path of input BIOS RW firmware image')
    parser.add_argument('-e', '--ec_image', type='path',
                        help='Path of input Embedded Controller firmware image')
    parser.add_argument('-p', '--pd_image', type='path',
                        help='Path of input Power Delivery firmware image')
    parser.add_argument('--script', type=str, default='updater.sh',
                        help='File name of main script file')
    parser.add_argument('-o', '--output', type='path',
                        help='Path of output filename')
    parser.add_argument(
        '--extra', type=str,
        help='Directory list (separated by :) of files to be merged')

    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument(
        '--remove_inactive_updaters', default=True,
        action='store_true', help='Remove inactive updater scripts')
    group.add_argument(
        '--no-remove_inactive_updaters',
        action='store_false', dest='remove_inactive_updaters',
        help="Don't remove inactive updater scripts")

    parser.add_argument('--create_bios_rw_image', action='store_true',
                        help='Resign and generate a BIOS RW image')
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument(
        '--merge_bios_rw_image', default=True, action='store_true',
        help='Merge the --bios_rw_image into --bios_image RW sections')
    group.add_argument(
        '--no-merge_bios_rw_image', action='store_false',
        dest='merge_bios_rw_image',
        help="Don't Merge the --bios_rw_image into --bios_image RW sections")

    # embedded tools
    parser.add_argument(
        '--tools', action='split_extend',
        help='List of tool programs to be bundled into updater')

    # TODO(sjg@chromium.org: Consider making this accumulate rather than using
    # the ':' separator.
    parser.add_argument(
        '--tool_base', type=str, default='',
        help='Default source locations for tools programs (delimited by colon)')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='Avoid output except for warnings/errors')

    opts = parser.parse_args(argv)

    if opts.tools is None:
      opts.tools = ['flashrom', 'mosys', 'crossystem', 'futility', 'vpd']

    opts.Freeze()
    return opts

  @staticmethod
  def _EnsureCommand(cmd, package):
    """Ensure that a command is available, raising an exception if not.

    Args:
      cmd: Command to check (just the name, not the full path).
      package: Name of package to install to obtain this tool.
    """
    if not osutils.Which(cmd):
      raise PackError("You need '%s' (package '%s')" % (cmd, package))

  @staticmethod
  def _FindTool(tool_base, tool):
    """Find a tool in the tool_base path list, raising an exception if missing.

    Args:
      tool_base: List of directories to check.
      tool: Name of tool to find (just the name, not the full path).
    """
    for path in tool_base:
      fname = os.path.join(path, tool)
      if os.path.exists(fname):
        return os.path.realpath(fname)
    raise PackError("Cannot find tool program '%s' to bundle" % tool)

  def _CreateTmpDir(self):
    """Create a temporary directory, and remember it for later removal.

    Returns:
      Path name of temporary directory.
    """
    fname = tempfile.mkdtemp('.pack_firmware-%d' % os.getpid())
    self._tmp_dirs.append(fname)
    return fname

  def _RemoveTmpdirs(self):
    """Remove all the temporary directories."""
    for fname in self._tmp_dirs:
      shutil.rmtree(fname)
    self._tmp_dirs = []

  def _AddFlashromVersion(self, tool_base):
    """Add flashrom version info to the collection of version information.

    Args:
      tool_base: List of directories to check.
    """
    flashrom = self._FindTool(tool_base, 'flashrom')

    # Look for a string ending in UTC.
    data = osutils.ReadFile(flashrom, mode='rb')
    m = re.search(r'([0-9.]+ +: +[a-z0-9]+ +: +.+UTC)', data)
    if m:
      version = m.group(1)
    else:
      # As a fallback, look for a version without git hash / date.
      m = re.search(r'([0-9.]+ +:  +: +)', data)
      if not m:
        raise PackError('Could not find flashrom version number')

      # Don't show a partial version, it is not very useful and the
      # pack_firmware.sh script (which predates this script) did not show it.
      version = ''

    # crbug.com/695904: Can we use a SHA2-based algorithm?
    digest = md5.new()
    digest.update(data)
    result = cros_build_lib.RunCommand(['file', '-b', flashrom], quiet=True)
    print('\nflashrom(8): %s *%s\n             %s\n             %s' %
          (digest.hexdigest(), flashrom, result.output.strip(), version),
          file=self._versions)

  def _AddVersionInfo(self, name, fname, version):
    """Add version info for a single file.

    Calculates the MD5 hash of the file and adds this and other file details
    into the collection of version information.

    Args:
      name: User-readable name of the file (e.g. 'BIOS').
      fname: Filename to read.
      version: Version string (e.g. 'Google_Reef.9042.40.0').
    """
    if fname:
      digest = md5.new()
      digest.update(osutils.ReadFile(fname, mode='rb'))

      # Modify the filename to replace any use of our base directory with a
      # constant string, so we produce the same output on each run. Also drop
      # the build directory since it is not useful to the user.
      short_fname = fname
      if self._basedir:
        short_fname = short_fname.replace(
            self._basedir,
            os.path.join(os.path.dirname(self._basedir), 'tmp'))
      short_fname = re.sub(r'/build/.*/work/', '', short_fname)
      print('%s image:%s%s *%s' % (name, ' ' * max(3, 7 - len(name)),
                                   digest.hexdigest(), short_fname),
            file=self._versions)
    if version:
      print('%s version:%s%s' % (name, ' ' * max(1, 5 - len(name)), version),
            file=self._versions)

  def _ExtractFrid(self, image_file, section_name='RO_FRID'):
    """Extracts the firmware ID from an image file.

    Args:
      image_file: File to process.
      section_name: Name of the section of image_file which contains the
          firmware ID.

    Returns:
      Firmware ID as a string, if found, else ''
    """
    fname = os.path.join(self._tmpdir, section_name)

    # Remove any file that might be in the way (if not testing).
    if not self._testing and os.path.exists(fname):
      os.remove(fname)
    cros_build_lib.RunCommand(['futility', 'dump_fmap', '-x', image_file],
                              quiet=True, cwd=self._tmpdir, error_code_ok=True)
    if os.path.exists(fname):
      return osutils.ReadFile(fname).replace('\x00', '').strip()
    return ''

  def _BaseDirPath(self, basename):
    """Build a filename in the temporary base directory.

    Args:
      basename: Leafname (with no directory) of file to build.

    Returns:
      New filename within the self._basedir directory.
    """
    return os.path.join(self._basedir, basename)

  def _GetPreambleFlags(self, fname):
    """Get the preamble flags from an image.

    Args:
      fname: Image to check (relative or absolute path).

    Returns:
      Preamble flags as an integer. See VB2_FIRMWARE_PREAMBLE_... for available
      flags; the most common one is VB2_FIRMWARE_PREAMBLE_USE_RO_NORMAL.
    """
    cros_build_lib.RunCommand(['futility', 'dump_fmap', '-x', fname],
                              quiet=True, cwd=self._tmpdir)
    cros_build_lib.RunCommand(['futility', 'gbb', '--rootkey=rootkey.bin',
                               'GBB'],
                              quiet=True, cwd=self._tmpdir)
    result = cros_build_lib.RunCommand(
        ['futility', 'vbutil_firmware', '--verify', 'VBLOCK_A', '--signpubkey',
         'rootkey.bin', '--fv', 'FW_MAIN_A'], quiet=True, cwd=self._tmpdir)
    lines = ([line for line in result.output.splitlines()
              if 'Preamble flags' in line])
    if len(lines) != 1:
      raise PackError("vbutil_firmware returned %d 'Preamble flags' lines",
                      len(lines))
    return int(lines[0].split()[-1])

  def _SetPreambleFlags(self, infile, outfile, preamble_flags):
    """Set the preamble flags for an image.

    Args:
      infile: Input image file (relative or absolute path).
      outfile: Output image file (relative or absolute path).
      preamble_flags: Preamble flags as an integer.
    """
    keydir = '/usr/share/vboot/devkeys'
    cros_build_lib.RunCommand(
        ['resign_firmwarefd.sh', infile, outfile,
         os.path.join(keydir, 'firmware_data_key.vbprivk'),
         os.path.join(keydir, 'firmware.keyblock'),
         os.path.join(keydir, 'dev_firmware_data_key.vbprivk'),
         os.path.join(keydir, 'dev_firmware.keyblock'),
         os.path.join(keydir, 'kernel_subkey.vbpubk'),
         '0', str(preamble_flags)],
        quiet=True, cwd=self._tmpdir)

  @staticmethod
  def _CopyTimestamp(reference_fname, fname):
    """Copy the timestamp from a reference file to another file.

    reference_fname: Reference file for timestamp.
    fname: File to copy timestamp to.
    """
    mtime = os.stat(reference_fname).st_mtime
    os.utime(fname, (mtime, mtime))

  def _CreateRwFirmware(self, ro_fname, rw_fname):
    """Build a RW firmware file from an input RO file.

    This works by clearing bit 0 of the preamble flags, this indicating this is
    RW firmware.

    Args:
      ro_fname: Filename of RO firmware (relative or absolute path).
      rw_fname: Filename of RW firmware (relative or absolute path).
    """
    preamble_flags = self._GetPreambleFlags(ro_fname)
    if not preamble_flags & 1:
      raise PackError("Firmware image '%s' is NOT RO_NORMAL firmware" %
                      ro_fname)
    self._SetPreambleFlags(ro_fname, rw_fname, preamble_flags ^ 1)
    self._CopyTimestamp(ro_fname, rw_fname)
    if not self._args.quiet:
      print("RW firmware image '%s' created" % rw_fname)

  def _CheckRwFirmware(self, fname):
    """Check that the firmware file is RW firmware.

    Args:
      fname: Filename of firmware image to check.

    Raises:
      PackError or RunCommandError if the flags could not be read or indicate
        that the firmware file is not RW firmware.
    """
    if self._GetPreambleFlags(fname) & 1:
      raise PackError("Firmware image '%s' is NOT RW-firmware" % fname)

  def _GetFMAP(self, fname):
    """Get the FMAP (flash map) from a firmware image.

    Args:
      fname: Filename of firmware image.

    Returns:
      A dict comprising:
        key: Section name.
        value: Section() named tuple containing offset and size.
    """
    result = cros_build_lib.RunCommand(['futility', 'dump_fmap', '-p', fname],
                                       quiet=True, cwd=self._tmpdir)
    sections = {}
    for line in result.output.splitlines():
      name, offset, size = line.split()
      sections[name] = Section(int(offset), int(size))
    return sections

  def _CloneFirmwareSection(self, dst, src, section):
    """Clone a section in one file from another.

    Args:
      dst: Destination file (relative or absolute path).
      src: Source file (relative or absolute path).
      section: Section to clone.
    """
    src_section = self._GetFMAP(src)[section]
    dst_section = self._GetFMAP(dst)[section]
    if not src_section.size:
      raise PackError("Firmware section '%s' is invalid" % section)
    if src_section.size != dst_section.size:
      raise PackError("Firmware section '%s' size is different, cannot clone" %
                      section)
    if src_section.offset != dst_section.offset:
      raise PackError("Firmware section '%s' is not in same location, cannot "
                      "clone" % section)
    merge_file.merge_file(dst, src, dst_section.offset, src_section.offset,
                          src_section.size)

  def _MergeRwFirmware(self, ro_fname, rw_fname):
    """Merge RW sections from RW firmware to RO firmware.

    Args:
      ro_fname: RO firmware image file (relative or absolute path).
      rw_fname: RW firmware image file (relative or absolute path).
    """
    self._CloneFirmwareSection(ro_fname, rw_fname, 'RW_SECTION_A')
    self._CloneFirmwareSection(ro_fname, rw_fname, 'RW_SECTION_B')
    self._CopyTimestamp(rw_fname, ro_fname)

  def _ExtractEcRwUsingFMAP(self, fname, fmap_area_name, ecrw_fname):
    """Use the FMAP to extract fmap_area_name section containing an EC binary.

    Args:
      fname: Filename of firmware image (relative or absolute path).
      fmap_area_name: Name of the FMAP area which contains the EC binary.
      ecrw_fname: Filename to put EC binary into (relative or absolute path).
    """
    cros_build_lib.RunCommand(
        ['futility', 'dump_fmap', '-x', fname, fmap_area_name],
        quiet=True, cwd=self._tmpdir)
    ec_main_a = os.path.join(self._tmpdir, fmap_area_name)
    with open(ec_main_a) as fd:
      count, offset, size = struct.unpack('<III', fd.read(12))
    if count != 1 or offset != 12:
      raise PackError('Unexpected %s (%d, %d). Cannot merge EC RW' %
                      (fmap_area_name, count, offset))
    # To make sure files to be merged are both prepared, merge_file.py will
    # only accept existing files, so we have to create ecrw now.
    osutils.Touch(ecrw_fname)
    merge_file.merge_file(ecrw_fname, ec_main_a, 0, offset, size)

  def _ExtractEcRwUsingCBFS(self, fname, cbfs_name, ecrw_fname):
    """Extract an EC binary from a CBFS image.

    Args:
      fname: Filename of firmware image (relative or absolute path).
      cbfs_name: Name of file in CBFS which contains the EC binary.
      ecrw_fname: Filename to put EC binary into (relative or absolute path).
    """
    cros_build_lib.RunCommand(
        ['cbfstool', fname, 'extract', '-n', cbfs_name, '-f', ecrw_fname, '-r',
         'FW_MAIN_A'], quiet=True, cwd=self._tmpdir)

  def _ExtractEcRw(self, fname, fmap_area_name, cbfs_name, ecrw_fname):
    """Obtain the RW EC binary.

    Args:
      fname: Filename of firmware image (relative or absolute path).
      fmap_area_name: Name of the section in FMAP which contains the EC
          binary, will fall-back to CBFS if not present.
      cbfs_name: Name of file in CBFS which contains the EC binary, if the
          image does not have an fmap_area_name section.
      ecrw_fname: Filename to put EC binary into (relative or absolute path).

    Raises:
      PackError or RunCommandError if an error occurs.
    """
    if fmap_area_name in self._GetFMAP(fname):
      self._ExtractEcRwUsingFMAP(fname, fmap_area_name, ecrw_fname)
    else:
      self._ExtractEcRwUsingCBFS(fname, cbfs_name, ecrw_fname)

  def _MergeRwEcFirmware(self, ec_fname, rw_fname, fmap_area_name, cbfs_name):
    """Merge EC firmware from an image into the given file.

    Args:
      ec_fname: Filename to merge RW EC binary into (relative or absolute path).
      rw_fname: Filename of firmware image (relative or absolute path).
      fmap_area_name: Name of the section in FMAP which contains the EC
          binary, will fall-back to CBFS if not present.
      cbfs_name: Name of file in CBFS which contains the EC binary, if the
          image does not have an fmap_area_name section.
    """
    ecrw_fname = os.path.join(self._tmpdir, cbfs_name)
    self._ExtractEcRw(rw_fname, fmap_area_name, cbfs_name, ecrw_fname)
    section = self._GetFMAP(ec_fname)['EC_RW']
    ecrw_size = os.stat(ecrw_fname).st_size
    if section.size < ecrw_size:
      raise PackError('New RW payload size %#x is larger than preserved FMAP '
                      'section %#x, cannot merge' % (ecrw_size, section.size))
    merge_file.merge_file(ec_fname, ecrw_fname, section.offset)
    self._CopyTimestamp(rw_fname, ec_fname)

  def _CopyFirmwareFiles(self, bios_image, bios_rw_image, ec_image, pd_image,
                         create_bios_rw_image, target_dir):
    """Process firmware files and copy them into the working directory.

    Args:
      bios_image: Input filename of main BIOS (main) image.
      bios_rw_image: Input filename of RW BIOS image, or None if none.
      ec_image: Input filename of EC (Embedded Controller) image.
      pd_image: Input filename of PD (Power Delivery) image.
      create_bios_rw_image: True to create a RW BIOS image from the main one
          if not provided.
      target_dir: Target directory for output images.

    Returns:
      Dict containing information on the output image files:
        key: Name, one of BIOS, BIOS (RW), EC, EC (RW), PD, PD (RW).
        value: ImageFile, containing filename and version.
    """
    image_main = os.path.join(target_dir, IMAGE_MAIN)
    image_main_rw = os.path.join(target_dir, IMAGE_MAIN_RW)
    image_ec = os.path.join(target_dir, IMAGE_EC)
    image_pd = os.path.join(target_dir, IMAGE_PD)
    image_files = {}
    merge_bios_rw_image = self._args.merge_bios_rw_image
    if bios_image:
      bios_version = self._ExtractFrid(bios_image)
      bios_rw_version = bios_version
      shutil.copy2(bios_image, image_main)
      image_files['BIOS'] = ImageFile(bios_image, bios_version)
    else:
      merge_bios_rw_image = False

    if not bios_rw_image and create_bios_rw_image:
      bios_rw_image = image_main_rw
      self._CreateRwFirmware(bios_image, bios_rw_image)
      merge_bios_rw_image = False

    if bios_rw_image:
      self._CheckRwFirmware(bios_rw_image)
      bios_rw_version = self._ExtractFrid(bios_rw_image)
      if merge_bios_rw_image:
        self._MergeRwFirmware(image_main, bios_rw_image)
      elif bios_rw_image != image_main_rw:
        shutil.copy2(bios_rw_image, image_main_rw)
      image_files['BIOS (RW)'] = ImageFile(bios_rw_image, bios_rw_version)
    else:
      merge_bios_rw_image = False

    if ec_image:
      ec_version = self._ExtractFrid(ec_image)
      image_files['EC'] = ImageFile(ec_image, ec_version)
      shutil.copy2(ec_image, image_ec)
      if merge_bios_rw_image:
        self._MergeRwEcFirmware(image_ec, image_main, 'EC_MAIN_A', 'ecrw')
        ec_rw_version = self._ExtractFrid(image_ec, 'RW_FWID')
        image_files['EC (RW)'] = ImageFile(None, ec_rw_version)

    if pd_image:
      pd_version = self._ExtractFrid(pd_image)
      image_files['PD'] = ImageFile(pd_image, pd_version)
      shutil.copy2(pd_image, image_pd)
      if merge_bios_rw_image:
        self._MergeRwEcFirmware(image_pd, image_main, 'PD_MAIN_A', 'pdrw')
        pd_rw_version = self._ExtractFrid(image_pd, 'RW_FWID')
        image_files['PD (RW)'] = ImageFile(None, pd_rw_version)
    return image_files

  def _WriteVersions(self, model, image_files):
    """Write version information for all image files into the version file.

    Args:
      model: Name of model these image files are for (or '' if none).
      image_files: Dict with:
        key: Image type (e.g. 'BIOS').
        value: ImageFile object containing filename and version.
    """
    print(file=self._versions)  # Blank line.
    if model:
      print('Model:        %s' % model, file=self._versions)
    for name in sorted(image_files.keys()):
      image_file = image_files[name]
      self._AddVersionInfo(name, image_file.filename, image_file.version)

  def _UntarFile(self, pathname, dst_dirname, suffix=''):
    """Unpack the only item in a tar file.

    Read a file from a tar file. It must contain just a single member and its
    filename may not include a path.

    Args:
      pathname: Pathname of tar file to unpack.
      dst_dirname: Destination directory to place the unpacked file.
      suffix: String to append to output filename.

    Returns:
      Pathname of unpacked file.
    """
    with tarfile.open(pathname) as tar:
      members = tar.getmembers()
      if len(members) != 1:
        raise PackError("Expected 1 member in file '%s' but found %d" %
                        (pathname, len(members)))
      if '/' in members[0].name:
        raise PackError("Tar file '%s' member '%s' should be a simple name" %
                        (pathname, members[0].name))
      tar.extractall(self._tmpdir, members)
      fname = os.path.join(dst_dirname, members[0].name + suffix)
      os.rename(os.path.join(self._tmpdir, members[0].name), fname)
      return fname

  @staticmethod
  def _CopyFile(src, dst, mode=CHMOD_ALL_READ):
    """Copy a file (to another file or into a directory) and set its mode.

    Args:
      src: Source filename (relative or absolute path).
      dst: Destination filename or directory (relative or absolute path).
      mode: File mode to OR with the existing mode.

    Returns:
      Full pathname of the new file.
    """
    if os.path.isdir(dst):
      dst = os.path.join(dst, os.path.basename(src))
    shutil.copy2(src, dst)
    os.chmod(dst, os.stat(dst).st_mode | mode)
    return dst

  def _CopyUpdateScript(self, script):
    """Copy the update script into the base directory.

    The update script is the shell script executed to perform the update. It
    needs to be copied into the base directory so it will appear in the
    output file.

    Args:
      script: Update script being used (so that we can avoid copying the
          other update scripts over).
    """
    copied = False
    for fname in glob.glob(os.path.join(self._pack_dist, '*')):
      if (self._args.remove_inactive_updaters and 'updater' in fname and
          not script in fname):
        continue
      newfile = self._CopyFile(fname, self._basedir, CHMOD_ALL_EXEC)
      if not copied:
        copied = script in fname
      if self._force_dash and fname.endswith('.sh'):
        with open(newfile) as fd:
          fd.readline()
          rest = fd.read()
        osutils.WriteFile(newfile, '#!/bin/dash\n' + rest)
    if not copied:
      raise PackError("Cannot find required file '%s'" %
                      os.path.join(self._pack_dist, script))

  def _CopyBaseFiles(self, tool_base, tools):
    """Copy base files that every firmware update needs.

    Args:
      tool_base: List of directories to check.
      tools: List of tools to copy.
    """
    self._CopyFile(self._shflags_file, self._basedir)
    for tool in tools:
      tool_fname = self._FindTool(tool_base, tool)
      # Most tools are dynamically linked, but if there is a statically
      # linked version (denoted by a '_s' suffix) use that in preference.
      # This helps to reduce run-time dependencies for firmware update,
      # which is a critical process.
      if os.path.exists(tool_fname + '_s'):
        tool_fname += '_s'
      self._CopyFile(tool_fname, self._BaseDirPath(tool), CHMOD_ALL_EXEC)

  def _CopyExtraFiles(self, extras):
    """Copy extra files into the base directory.

    extras: List of extra files / directories to copy. If the item is a
        directory, then the files in that directory are copied into the
        base directory. If the item starts with "bcs://" then this indicates a
        tar file: the prefix is stripped, the file is picked up from the
        image directory (--imagedir) and unpacked to obtain the (single-file)
        contents.
    """
    for extra in extras:
      if extra.startswith('bcs://'):
        src = os.path.join(self._args.imagedir, extra.replace('bcs://', ''))
        fname = self._UntarFile(src, self._basedir)
        print("Extra BCS file: %s: %s" % (extra, fname), file=self._versions)
      elif os.path.isdir(extra):
        fnames = glob.glob(os.path.join(extra, '*'))
        if not fnames:
          raise PackError("cannot copy extra files from folder '%s'" %
                          extra)
        for fname in fnames:
          if not os.path.isdir(fname):
            self._CopyFile(fname, self._basedir)
        print('Extra files from folder: %s' % extra,
              file=self._versions)
      else:
        self._CopyFile(extra, self._basedir)
        print('Extra file: %s' % extra, file=self._versions)

  @staticmethod
  def _GetReplaceDict(image_files):
    """Build a dictionary of string replacements for use with the update script.

    Args:
      image_files: Dict with:
          key: Image type (e.g. 'BIOS').
          value: ImageFile object containing filename and version.

    Returns:
      Dict with:
          key: String to replace.
          value: Value to replace with.
    """
    empty = ImageFile(None, '')
    bios_rw_version = image_files.get('BIOS (RW)', empty).version
    if not bios_rw_version:
      bios_rw_version = image_files['BIOS'].version
    return {
        'REPLACE_TARGET_RO_FWID': image_files['BIOS'].version,
        'REPLACE_TARGET_FWID': bios_rw_version,
        'REPLACE_TARGET_ECID': image_files.get('EC', empty).version,
        'REPLACE_TARGET_PDID': image_files.get('PD', empty).version,
        # Set platform to first field of firmware version
        # (ex: Google_Link.1234 -> Google_Link).
        'REPLACE_TARGET_PLATFORM': image_files['BIOS'].version.split('.')[0],
    }

  @staticmethod
  def _CreateFileFromTemplate(infile, outfile, replace_dict):
    """Create a new file based on a template and some string replacements.

    Args:
      infile: Input file (the template).
      outfile: Output file.
      replace_dict: Dict with:
          key: String to replace.
          value: Value to replace with.
    """
    data = osutils.ReadFile(infile)
    rep = dict((re.escape(k), v) for k, v in replace_dict.iteritems())
    pattern = re.compile("|".join(rep.keys()))
    data = pattern.sub(lambda m: rep[re.escape(m.group(0))], data)

    osutils.WriteFile(outfile, data)
    os.chmod(outfile, os.stat(outfile).st_mode | 0555)

  def _WriteUpdateScript(self, script, replace_dict):
    """Create and write the update script which will run on the device.

    This generates the beginnings of the output file (shellball) based on the
    template. Normally all the required settings for firmware update are at the
    top of the script. With unified builds only TARGET_SCRIPT is used: the
    rest are unset and the model-specific setvars.sh files contain the rest
    of the settings.

    Args:
      script: Filename of update script being used (e.g. 'updater4.sh').
      replace_dict: Modified by this function to add the script. Dict with:
          key: String to replace.
          value: Value to replace with.
        Note: For unified builds this should be empty.
    """
    if replace_dict:
      unibuild = ''
    else:
      unibuild = '"yes"'
      for key in REPLACE_VARS:
        replace_dict['REPLACE_' + key] = '<unused with unified builds>'
    replace_dict['REPLACE_UNIBUILD'] = 'UNIBUILD=%s' % unibuild
    replace_dict['REPLACE_SCRIPT'] = script
    self._CreateFileFromTemplate(self._stub_file, self._args.output,
                                 replace_dict)

  def _WriteVersionFile(self):
    """Write out the VERSION file with our collected version information."""
    print(file=self._versions)
    osutils.WriteFile(self._BaseDirPath('VERSION'), self._versions.getvalue())

  def _BuildShellball(self):
    """Build a shell-ball containing the firmware update.

    Add our files to the shell-ball, and display all version information.
    """
    cros_build_lib.RunCommand(
        ['sh', self._args.output, '--sb_repack', self._basedir],
        quiet=self._args.quiet, mute_output=not self._args.quiet)
    if not self._args.quiet:
      for fname in glob.glob(self._BaseDirPath('VERSION*')):
        print(osutils.ReadFile(fname))

  def _ProcessModel(self, model, bios_image, bios_rw_image, ec_image, pd_image,
                    create_bios_rw_image, tools, tool_base, extras,
                    shared_image_files, target_dir):
    """Set up the firmware update for one model.

    Args:
      model: name of model to set up for (e.g. 'reef'). If this is not a
          unified build then this should be ''.
      bios_image: Input filename of main BIOS (main) image.
      bios_rw_image: Input filename of RW BIOS image, or None if none.
      ec_image: Input filename of EC (Embedded Controller) image.
      pd_image: Input filename of PD (Power Delivery) image.
      create_bios_rw_image: True to create a RW BIOS image from the main one
          if not provided.
      tools: List of tools to include in the update (e.g. ['mosys', 'ectool'].
      tool_base: List of directories to look in for tools.
      extras: List of extra files/directories to include.
      shared_image_files: Dict of image file information for the model whose
          images we want to share (or None if none):
        key: Image name (e.g. 'BIOS (RW)' or 'EC').
        value: ImageFile object for that image.
      target_dir: Target directory for image files.

    Returns:
      Dict containing information on the output image files:
        key: Name, one of "BIOS", "BIOS (RW)", "EC", "EC (RW)", "PD", "PD (RW)".
        value: ImageFile, containing filename and version.
    """
    for tool in tools:
      self._FindTool(tool_base, tool)
    if shared_image_files:
      image_files = shared_image_files
    else:
      if not bios_image and not ec_image and not pd_image:
        raise PackError("Model '%s': Must assign at least one of BIOS or EC or "
                        'PD image' % model)
      image_files = self._CopyFirmwareFiles(
          bios_image=bios_image,
          bios_rw_image=bios_rw_image,
          ec_image=ec_image,
          pd_image=pd_image,
          create_bios_rw_image=create_bios_rw_image,
          target_dir=target_dir)
    self._WriteVersions(model, image_files)
    self._CopyBaseFiles(tool_base, tools)
    if extras:
      self._CopyExtraFiles(extras)
    return image_files

  def _ExtractFile(
      self, build_target, fname_template, uri, dirname, suffix=''):
    """Obtain a file based on provided information.

    This obtains the file in one of two ways:
      - if we are building local firmware, it creates a filename using the
          provided template, with MODEL replaced with the current model. This
          is used to build a firmware update from the firmware ebuilds. The
          file is only used if there is a valid property value, since that is
          what indicates that the file is needed in the update.
      - otherwise it finds a tar file, unpacks it and returns the filename of
          the resulting unpacked file. There is only one file in the archive.
          This is used to build a firmware update from the configured firmware
          versions, downloaded from BCS.

    Args:
      build_target: The name of the build target uses, driving the file output.
      fname_template: Filename template to use in local model.
      uri: URI containing the file
      dirname: Output directory where the unpacked file should be placed.
      suffix: String to append to output filename.

    Returns:
      Filename of the resulting image file.
    """
    if not uri:
      return None
    if self._args.local:
      if not fname_template:
        return None
      # TODO(shapiroc): Remove MODEL after cros-firmware.eclass is updated
      return fname_template.replace('MODEL', build_target).replace(
          'BUILD_TARGET', build_target)
    fname = uri.replace('bcs://', '')
    return self._UntarFile(
        os.path.join(self._args.imagedir, fname), dirname, suffix)

  def _GenerateOneModel(self, firmware, args, tool_base, model_details):
    """Generate the output files for a single model.

    Create the firmware files and set up the 'setvars.sh' script for a single
    model. If this model uses shared firmware the files are reused from the
    shared model rather than being recreated.

    Args:
      firmware: Firmware information to process (FirmwareInfo object)
      args: Command-line arguments (argparse.Namespace object).
      tool_base: List of directories to check.
      model_details: Dict:
          key: Model name.
          value: ModelDetails object for that model.

    Returns:
      ModelDetails object for |model|.
    """
    shared_image_files = None
    if firmware.shared_model and firmware.shared_model in model_details:
      shared_image_files = model_details[firmware.shared_model].image_files
    if not firmware.have_image:
      return ModelDetails(None, firmware.shared_model, firmware.key_id)

    setvars_dirname = self._BaseDirPath(os.path.join(MODELS_DIR,
                                                     firmware.model))
    osutils.SafeMakedirs(setvars_dirname)
    if firmware.shared_model:
      image_dirname = self._BaseDirPath(os.path.join(MODELS_DIR,
                                                     firmware.shared_model))
      osutils.SafeMakedirs(image_dirname)
    else:
      image_dirname = setvars_dirname
    # Put all model files in a separate subdirectory.
    dirname = os.path.join(self._tmpdir, MODELS_DIR, firmware.model)
    osutils.SafeMakedirs(dirname)

    if shared_image_files:
      bios_image, bios_rw_image, ec_image, pd_image = None, None, None, None
    else:
      bios_image = self._ExtractFile(
          firmware.bios_build_target, args.bios_image, firmware.main_image_uri,
          dirname)
      bios_rw_image = self._ExtractFile(
          firmware.bios_build_target, None, firmware.main_rw_image_uri, dirname,
          'rw')

      ec_image = self._ExtractFile(
          firmware.ec_build_target, args.ec_image, firmware.ec_image_uri,
          dirname)
      pd_image = self._ExtractFile(
          firmware.ec_build_target, args.pd_image, firmware.pd_image_uri,
          dirname)

    extras = [os.path.expandvars(extra) for extra in firmware.extra]
    if args.local:
      firmware = firmware._replace(create_bios_rw_image=False)

    tools = firmware.tools + args.tools

    image_files = self._ProcessModel(
        bios_image=bios_image,
        bios_rw_image=bios_rw_image,
        ec_image=ec_image,
        pd_image=pd_image,
        create_bios_rw_image=firmware.create_bios_rw_image,
        tools=tools,
        tool_base=tool_base,
        extras=extras,
        model=firmware.model,
        shared_image_files=shared_image_files,
        target_dir=image_dirname)
    setvars_dict = self._GetReplaceDict(image_files)
    setvars_dict['REPLACE_MODEL'] = firmware.model
    if firmware.shared_model:
      setvars_dict['${MODEL_DIR}'] = 'models/%s' % firmware.shared_model

    # For zero-touch whitelabel devices tell the firmware updater to get its
    # signature ID from the VPD.
    # TODO(sjg): Update this to say that it comes from mosys / cros_config.
    setvars_dict['REPLACE_SIGNATURE_ID'] = firmware.sig_id
    fname = os.path.join(self._basedir, MODELS_DIR, firmware.model,
                         'setvars.sh')
    self._CreateFileFromTemplate(self._setvars_template_file, fname,
                                 setvars_dict)
    return ModelDetails(image_files, firmware.shared_model, firmware.key_id)

  def _WriteSignerInstructions(self, model_details):
    """Write the signer instructions file.

    This file tells the signer the mapping between models and their images and
    key IDs. The signer uses this to work out which images to sign, the key to
    use to sign each image and the model name to use in the vblock filename.

    Note that a change with the format can break the code which has already
    been released. Update all the consumers first so that they can handle
    old and new formats, then push the change here.

    Args:
      model_details: OrderedDict:
          key: Model name (in the order that information is wanted in the
              instruction file).
          value: ModelDetails object for that model.
    """
    with open(self._BaseDirPath('signer_config.csv'), 'w') as fd:
      print('model_name,firmware_image,key_id,ec_image', file=fd)
      for model in model_details.keys():
        details = model_details[model]
        if not details.key_id:
          # Some models will not have a key. At present this is normally
          # just the zero-touch whitelabel devices.
          continue
        image_fname = os.path.join(MODELS_DIR, details.shared_model or model,
                                   IMAGE_MAIN)
        ec_fname = os.path.join(MODELS_DIR, details.shared_model or model,
                                IMAGE_EC)
        print(','.join((model, image_fname, details.key_id, ec_fname)), file=fd)

  def Start(self, argv, remove_tmpdirs=True):
    """Handle the creation of a firmware shell-ball.

    argv: List of arguments (excluding the program name/argv[0]).

    Raises:
      PackError if any error occurs.
    """
    args = self._args = self.ParseArgs(argv)

    self._EnsureCommand('shar', 'sharutils')
    if not os.path.exists(self._stub_file):
      raise PackError("Cannot find required file '%s'" % self._stub_file)
    tool_base = args.tool_base.split(':')
    try:
      if not args.output:
        raise PackError('Missing output file')
      self._basedir = self._CreateTmpDir()
      self._tmpdir = self._CreateTmpDir()
      self._AddFlashromVersion(tool_base)
      model_details = OrderedDict()
      if not args.legacy:
        # Most of the arguments are meaningless with unified builds since we
        # get the information from the master configuration. Add checks here
        # to avoid confusion. We could possibly do some of this using
        # mutual exclusivity in the argparser, but I'm not sure it is better.
        image_args = [args.bios_image, args.ec_image, args.pd_image]
        if any(image_args) and not args.local:
          raise PackError('Cannot use -b/-p/-e with -m')
        elif args.local and not all(image_args):
          raise PackError('Must provide -b, -e, -p with -l')
        if args.create_bios_rw_image:
          raise PackError('Cannot use --create_bios_rw_image with -m')
        if not args.config:
          raise PackError('Missing master configuration file (use -c)')

        with open(args.config) as fd:
          conf = libcros_config_host.CrosConfig(fd)

        script = conf.GetFirmwareScript()

        osutils.SafeMakedirs(self._BaseDirPath(MODELS_DIR))
        osutils.SafeMakedirs(os.path.join(self._tmpdir, MODELS_DIR))

        firmware_info = conf.GetFirmwareInfo()
        # TODO: Ignore the passed-in model list and use the list from
        # libcros_config_host always. For now the passed-in list is used for
        # testing.
        models = args.models if args.models else firmware_info.keys()
        for model in models:
          model_details[model] = self._GenerateOneModel(
              firmware_info[model], args, tool_base, model_details)
        replace_dict = {}
      else:
        script = args.script
        image_files = self._ProcessModel(
            model='',
            bios_image=args.bios_image,
            bios_rw_image=args.bios_rw_image,
            ec_image=args.ec_image,
            pd_image=args.pd_image,
            create_bios_rw_image=args.create_bios_rw_image,
            tools=args.tools,
            tool_base=tool_base,
            extras=args.extra.split(':') if args.extra else [],
            shared_image_files=None,
            target_dir=self._basedir)
        replace_dict = self._GetReplaceDict(image_files)
      self._CopyUpdateScript(script)
      self._WriteUpdateScript(script, replace_dict)
      self._WriteVersionFile()
      if model_details:
        self._WriteSignerInstructions(model_details)
      self._BuildShellball()
      if not args.quiet:
        print('Packed output image is: %s' % args.output)
    finally:
      if remove_tmpdirs:
        self._RemoveTmpdirs()


# The style guide says that we cannot pass in sys.argv[0]. That makes testing
# a pain, so this is a full argv.
def main(argv):
  # pylint: disable=W0603
  global packer

  packer = FirmwarePacker(argv[0])
  packer.Start(argv[1:])

if __name__ == "__main__":
  main(sys.argv)
