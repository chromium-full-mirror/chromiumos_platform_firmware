#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Packages firmware images into an executable "shell-ball".

It requires:
 - at least one firmware image (*.bin, should be AP or EC or ...)
 - pack/dist/updater?.sh main script
 - pack/sfx.sh as the self extraction (and installation) script
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

# pylint: disable=import-error
from cros_config_host import libcros_config_host
# pylint: enable=import-error

MAIN, MAIN_RW = 'BIOS', 'BIOS (RW)'
EC = 'EC'
PD = 'PD'
MODELS_DIR = 'models'
Section = namedtuple('Section', ['offset', 'size'])
ImageFile = namedtuple('ImageFile', ['filename', 'firmware_ids'])
FirmwareIds = namedtuple('FirmwareIds', ['ro_id', 'rw_id'])

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

# Variables that we replace in pack/sfx.sh and pack/setvars_template.
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
    _pack_dist: Path to `dist` directory.
    _script_base: Base directory with useful files (src/platform/firmware).
    _sfx_file: Path to `sfx.sh`.
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
    self._pack_dist = os.path.join(self._script_base, 'pack', 'dist')
    self._sfx_file = os.path.join(self._script_base, 'pack', 'sfx.sh')
    self._setvars_template_file = os.path.join(
        self._script_base, 'pack', 'setvars_template')
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
    parser.add_argument('--script', type=str, default='updater5.sh',
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

    parser.add_argument('--root', type='path',
                        help='The board sysroot to find ELF dependencies under')
    parser.add_argument('--tool_base', type=str, default='',
                        help='Deprecated (do not use this)')

    parser.add_argument('-q', '--quiet', action='store_true',
                        help='Avoid output except for warnings/errors')

    opts = parser.parse_args(argv)

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
      # any build and temp directies since they are not useful to the user.
      short_fname = fname
      if self._basedir:
        short_fname = short_fname.replace(
            self._basedir,
            os.path.join(os.path.dirname(self._basedir), 'tmp'))
      if self._tmpdir and short_fname.startswith(self._tmpdir):
        short_fname = short_fname[len(self._tmpdir):]
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

  def _ExtractFirmwareIds(self, image_file, rw_image_file=None):
    """Extracts the RO/RW firmware IDs from image files.

    Args:
      image_file: File to process.
      rw_image_file: RW image file, if separate.

    Returns:
      FirmwareIds object containing RO and RW firmware IDs.
      If RW ID not found, RO ID is used in its place.
    """
    ro_id = self._ExtractFrid(image_file)
    rw_id = (self._ExtractFrid(rw_image_file)
             if rw_image_file else self._ExtractFrid(image_file, 'RW_FWID'))
    return FirmwareIds(ro_id, rw_id or ro_id)

  def _BaseDirPath(self, basename):
    """Build a filename in the temporary base directory.

    Args:
      basename: Leafname (with no directory) of file to build.

    Returns:
      New filename within the self._basedir directory.
    """
    return os.path.join(self._basedir, basename)

  @staticmethod
  def _CopyTimestamp(reference_fname, fname):
    """Copy the timestamp from a reference file to another file.

    reference_fname: Reference file for timestamp.
    fname: File to copy timestamp to.
    """
    mtime = os.stat(reference_fname).st_mtime
    os.utime(fname, (mtime, mtime))

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

  @staticmethod
  def _MergeFile(large_path, small_path, large_offset, small_offset=0,
                 size=None):
    """Merges file in small_path to large_path in given offset.

    Args:
      large_path: A string for path of the file to merged to.
      small_path: A string for path of the file to merge from.
      large_offset: The offset to write in large_path.
      small_offset: The offset to read in small_path.
      size: Count of bytes to read/write. None to read whole small_path.
    """
    with open(large_path, 'r+') as output:
      output.seek(large_offset)
      with open(small_path) as source:
        source.seek(small_offset)
        output.write(source.read() if size is None else source.read(size))

  def _CloneFirmwareSection(self, dst, src, section, optional=False):
    """Clone a section in one file from another.

    Args:
      dst: Destination file (relative or absolute path).
      src: Source file (relative or absolute path).
      section: Section to clone.
      optional: Ignore if the section does not exist.
    """
    src_section = self._GetFMAP(src).get(section, None)
    dst_section = self._GetFMAP(dst).get(section, None)

    if src_section is None and dst_section is None and optional:
      if not self._args.quiet:
        print("Ignored nonexistent optional section '%s' in RW firmware image" %
              section)
      return
    if src_section is None or dst_section is None:
      raise PackError("Firmware section '%s' does not exist" % section)
    if not src_section.size:
      raise PackError("Firmware section '%s' is invalid" % section)
    if src_section.size != dst_section.size:
      raise PackError("Firmware section '%s' size is different, cannot clone" %
                      section)
    if src_section.offset != dst_section.offset:
      raise PackError("Firmware section '%s' is not in same location, cannot "
                      "clone" % section)
    self._MergeFile(dst, src, dst_section.offset, src_section.offset,
                    src_section.size)

  def _MergeRwFirmware(self, ro_fname, rw_fname):
    """Merge RW sections from RW firmware to RO firmware.

    Args:
      ro_fname: RO firmware image file (relative or absolute path).
      rw_fname: RW firmware image file (relative or absolute path).
    """
    self._CloneFirmwareSection(ro_fname, rw_fname, 'RW_SECTION_A')
    self._CloneFirmwareSection(ro_fname, rw_fname, 'RW_SECTION_B')
    self._CloneFirmwareSection(ro_fname, rw_fname, 'RW_LEGACY', optional=True)
    self._CloneFirmwareSection(ro_fname, rw_fname, 'RW_MISC', optional=True)
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
    # To make sure files to be merged are both prepared, _MergeFile will
    # only accept existing files, so we have to create ecrw now.
    osutils.Touch(ecrw_fname)
    self._MergeFile(ecrw_fname, ec_main_a, 0, offset, size)

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
    self._MergeFile(ec_fname, ecrw_fname, section.offset)
    self._CopyTimestamp(rw_fname, ec_fname)

  @staticmethod
  def _ExtractEcVersion(fw_id):
    """Extracts EC version from a firmware ID, swapping dots for dashes.

    Returns empty string if fw_id is empty or malformed.

    Letting V denote [0-9], the expected version regex is "V+.V+.V+"

    Expected format is "<board>_vV.V.VVVV-<hash>"
    For example, "reef_v1.1.5857-77f6ed7" becomes "1-1-5857"

    Args:
      fw_id: The firmware ID providing version information.

    Returns:
      The version number.
    """
    m = re.search(r'((\d+\.){2}\d+)(-\S+)', fw_id)
    return m.group(1).replace('.', '-') if m else ''

  @staticmethod
  def _ExtractMainVersion(fw_id):
    """Extracts main version from a firmware ID.

    Note this function also swaps periods for dashes, and removes
    timestamps from local builds. Returns empty string if fw_id is empty
    or malformed.

    Letting V denote [0-9], the expected version regex is...
      "V+.V+" for local builds
      "V+.V+.V+" for official builds

    Expected FW ID format is one of...
      "Google_<board>.<version>.yyyy_mm_dd_tttt" for local builds
      "Google_<board>.<version>" for official builds

    For example...
      "Google_Reef.9264.0.2017_02_09_1240" becomes "9264-0"
      "Google_Reef.9264.0.1" becomes "9264-0-1"

    Args:
      fw_id: The firmware ID providing version information.

    Returns:
      The version number.
    """
    m = re.search(r'(\d+\.\d+)(\.\d+$|\.\d{4}_)', fw_id)
    if not m:
      return ''
    version = m.group(1)
    if '_' not in m.group(2):
      version += m.group(2)
    return version.replace('.', '-')

  def _FirmwareImageOutput(self, tag, fw_ids=None, target_dir=None):
    """Creates output file name for some firmware image.

    Args:
      tag: Type of image (e.g, "bios").
      fw_ids: The image firmware RO/RW IDs (FirmwareIds object). (Not yet used.)
      target_dir: Target directory to be prepended to filename.

    Returns:
      Filename of the output image.
    """
    fname = '%s.bin' % tag
    return os.path.join(target_dir, fname) if target_dir else fname

  def _PdImageOutput(self, fw_ids=None, target_dir=None):
    """Creates output file name for given PD image.

    Args:
      fw_ids: The image firmware ID (FirmwareIds object). (Not yet used.)
      target_dir: Target directory to be prepended to filename.

    Returns:
      Filename of the output image.
    """
    return self._FirmwareImageOutput('pd', fw_ids, target_dir)

  def _EcImageOutput(self, fw_ids=None, target_dir=None):
    """Creates output file name for given EC image.

    Args:
      fw_ids: The image firmware ID (FirmwareIds object). (Not yet used.)
      target_dir: Target directory to be prepended to filename.

    Returns:
      Filename of the output image.
    """
    return self._FirmwareImageOutput('ec', fw_ids, target_dir)

  def _MainImageOutput(self, fw_ids=None, target_dir=None):
    """Creates output file name for given main image.

    Args:
      fw_ids: The image firmware ID (FirmwareIds object). (Not yet used.)
      target_dir: Target directory to be prepended to filename.

    Returns:
      Filename of the output image.
    """
    return self._FirmwareImageOutput('bios', fw_ids, target_dir)

  def _CopyFirmwareFiles(self, bios_image, bios_rw_image, ec_image, pd_image,
                         target_dir):
    """Process firmware files and copy them into the working directory.

    Args:
      bios_image: Input filename of main BIOS (main) image.
      bios_rw_image: Input filename of RW BIOS image, or None if none.
      ec_image: Input filename of EC (Embedded Controller) image, or None if
          none.
      pd_image: Input filename of PD (Power Delivery) image, or None if none.
      target_dir: Target directory for output images.

    Returns:
      Dict containing information on the output image files:
        key: Name, one of BIOS, BIOS (RW), EC, EC (RW), PD, PD (RW).
        value: ImageFile, containing filename and version.
    """
    image_files = {}

    # Merge firmware first so output can be named by version.
    if bios_rw_image:
      assert bios_image, 'Need main (RO) firmware image to merge RW image.'
      bios_image = self._CopyFile(bios_image, self._tmpdir, preserve_path=True)
      self._MergeRwFirmware(bios_image, bios_rw_image)
      if ec_image:
        ec_image = self._CopyFile(ec_image, self._tmpdir, preserve_path=True)
        self._MergeRwEcFirmware(ec_image, bios_image, 'EC_MAIN_A', 'ecrw')
      if pd_image:
        pd_image = self._CopyFile(pd_image, self._tmpdir, preserve_path=True)
        self._MergeRwEcFirmware(pd_image, bios_image, 'PD_MAIN_A', 'pdrw')

    if bios_image:
      bios_fw_id = self._ExtractFirmwareIds(bios_image, bios_rw_image)
      image_main = self._MainImageOutput(bios_fw_id, target_dir)
      shutil.copy2(bios_image, image_main)
      image_files[MAIN] = ImageFile(bios_image, bios_fw_id)

    if bios_rw_image:
      image_files[MAIN_RW] = ImageFile(
          bios_rw_image, self._ExtractFirmwareIds(bios_rw_image))

    if ec_image:
      ec_fw_id = self._ExtractFirmwareIds(ec_image)
      image_ec = self._EcImageOutput(ec_fw_id, target_dir)
      image_files[EC] = ImageFile(ec_image, ec_fw_id)
      shutil.copy2(ec_image, image_ec)

    if pd_image:
      pd_fw_id = self._ExtractFirmwareIds(pd_image)
      image_pd = self._PdImageOutput(pd_fw_id, target_dir)
      image_files[PD] = ImageFile(pd_image, pd_fw_id)
      shutil.copy2(pd_image, image_pd)
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
      filename = image_files[name].filename
      ro_id, rw_id = image_files[name].firmware_ids
      self._AddVersionInfo(name, filename, ro_id)
      # Only write the RW version if it differs from RO. Main has separate
      # RO/RW inputs, so check to avoid writing RW version twice.
      if ro_id != rw_id and name != MAIN:
        self._AddVersionInfo(name + ' (RW)', None, rw_id)

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
  def _CopyFile(src, dst, mode=CHMOD_ALL_READ, preserve_path=False):
    """Copy a file (to another file or into a directory) and set its mode.

    Args:
      src: Source filename (relative or absolute path).
      dst: Destination filename or directory (relative or absolute path).
      mode: File mode to OR with the existing mode.
      preserve_path: Whether or not to preserve src's path inside dst.

    Returns:
      Full pathname of the new file.
    """
    if os.path.isdir(dst):
      dst = os.path.join(
          dst, src.strip('/') if preserve_path else os.path.basename(src))
    osutils.SafeMakedirs(dst if os.path.isdir(dst) else os.path.dirname(dst))
    shutil.copy2(src, dst)
    os.chmod(dst, os.stat(dst).st_mode | mode)
    return dst

  @staticmethod
  def _CopyElfs(root, elfs, dst):
    """Copy an ELF (and its dependencies) to the destination directory.

    Args:
      root: The board sysroot to copy additional files from.
      elfs: A list of source ELFs to copy.
      dst: Destination directory to copy all files.
    """
    cros_build_lib.RunCommand(
        ['lddtree', '--copy-to-tree', dst, '--bindir=/bin', '--libdir=/lib',
         '--generate-wrappers', '--copy-non-elfs', '--no-auto-root',
         '--root', root] + elfs,
        quiet=True)

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

  def _CopyExtraFiles(self, extras):
    """Copy extra files into the base directory.

    extras: List of extra files / directories to copy. If the item is a
        directory, then the files in that directory are copied into the
        base directory. If the item starts with "bcs://" then this indicates a
        tar file: the prefix is stripped, the file is picked up from the
        image directory (--imagedir) and unpacked to obtain the (single-file)
        contents.
    """
    root = self._args.root if self._args else None
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
        if root is None:
          for fname in fnames:
            if not os.path.isdir(fname):
              self._CopyFile(fname, self._basedir)
        else:
          self._CopyElfs(root, [x for x in fnames if not os.path.isdir(x)],
                         self._basedir)
        print('Extra files from folder: %s' % extra,
              file=self._versions)
      else:
        # *.bin are extra firmware images that must live in top level folder.
        if root is None or extra.endswith('.bin'):
          self._CopyFile(extra, self._basedir)
        else:
          self._CopyElfs(root, [extra], self._basedir)
        print('Extra file: %s' % extra, file=self._versions)

  def _GetReplaceDict(self, image_files, shared_model=None):
    """Build a dictionary of string replacements for use with the update script.

    Args:
      image_files: Dict with:
          key: Image type (e.g. 'BIOS').
          value: ImageFile object containing filename and version.
      shared_model: Model with which the current model shares firmware.

    Returns:
      Dict with:
          key: String to replace.
          value: Value to replace with.
    """
    empty = ImageFile(None, FirmwareIds('', ''))
    bios_fw_id, ec_fw_id, pd_fw_id = [
        image_files.get(k, empty).firmware_ids for k in [MAIN, EC, PD]]
    dirname = 'models/%s' % shared_model if shared_model else '${MODEL_DIR}'
    return {
        'REPLACE_TARGET_IMAGE_RO_MAIN': self._MainImageOutput(
            bios_fw_id, dirname),
        # TODO(evanhernandez): The bios_rw is always merged, so remove this.
        'REPLACE_TARGET_IMAGE_RW_MAIN': os.path.join(dirname, 'bios_rw.bin'),
        'REPLACE_TARGET_IMAGE_EC': self._EcImageOutput(ec_fw_id, dirname),
        'REPLACE_TARGET_IMAGE_PD': self._PdImageOutput(pd_fw_id, dirname),
        'REPLACE_TARGET_RO_FWID': bios_fw_id.ro_id,
        'REPLACE_TARGET_FWID': bios_fw_id.rw_id,
        'REPLACE_TARGET_ECID': ec_fw_id.ro_id,
        'REPLACE_TARGET_PDID': pd_fw_id.ro_id,
        # Set platform to first field of firmware version
        # (ex: Google_Link.1234 -> Google_Link).
        'REPLACE_TARGET_PLATFORM': bios_fw_id.ro_id.split('.')[0],
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
      script: Filename of update script being used (e.g. 'updater5.sh').
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
    self._CreateFileFromTemplate(self._sfx_file, self._args.output,
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
                    extras, shared_image_files, target_dir):
    """Set up the firmware update for one model.

    Args:
      model: name of model to set up for (e.g. 'reef'). If this is not a
          unified build then this should be ''.
      bios_image: Input filename of main BIOS (main) image.
      bios_rw_image: Input filename of RW BIOS image, or None if none.
      ec_image: Input filename of EC (Embedded Controller) image, or None if
          none.
      pd_image: Input filename of PD (Power Delivery) image, or None if none.
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
          target_dir=target_dir)
    self._WriteVersions(model, image_files)
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

  def _GenerateOneModel(self, firmware, args, model_details):
    """Generate the output files for a single model.

    Create the firmware files and set up the 'setvars.sh' script for a single
    model. If this model uses shared firmware the files are reused from the
    shared model rather than being recreated.

    Args:
      firmware: Firmware information to process (FirmwareInfo object)
      args: Command-line arguments (argparse.Namespace object).
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

    image_files = self._ProcessModel(
        bios_image=bios_image,
        bios_rw_image=bios_rw_image,
        ec_image=ec_image,
        pd_image=pd_image,
        extras=extras,
        model=firmware.model,
        shared_image_files=shared_image_files,
        target_dir=image_dirname)
    setvars_dict = self._GetReplaceDict(image_files, firmware.shared_model)
    setvars_dict['REPLACE_MODEL'] = firmware.model

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
      for model, details in model_details.iteritems():
        if not details.key_id:
          # Some models will not have a key. At present this is normally
          # just the zero-touch whitelabel devices.
          continue
        # Sometimes, shared images are stored in their own directory instead
        # of with a particular model. (See coral.) For whitelabel devices
        # that use these images, we have to search for the corresponding
        # image_files dictionary in order to write out the correct file name.
        shared_model = details.shared_model
        if shared_model:
          if not shared_model in model_details:
            image_files = next(d for d in model_details.values()
                               if d.shared_model == shared_model
                               and d.image_files).image_files
          else:
            image_files = model_details[shared_model].image_files
        else:
          image_files = details.image_files
        dirname = os.path.join(MODELS_DIR, shared_model or model)
        image_fname = self._MainImageOutput(
            image_files[MAIN].firmware_ids, dirname)
        ec_fname = self._EcImageOutput(image_files[EC].firmware_ids, dirname)
        print(','.join((model, image_fname, details.key_id, ec_fname)), file=fd)

  def Start(self, argv, remove_tmpdirs=True):
    """Handle the creation of a firmware shell-ball.

    argv: List of arguments (excluding the program name/argv[0]).

    Raises:
      PackError if any error occurs.
    """
    args = self._args = self.ParseArgs(argv)

    self._EnsureCommand('shar', 'sharutils')
    if not os.path.exists(self._sfx_file):
      raise PackError("Cannot find required file '%s'" % self._sfx_file)
    try:
      if not args.output:
        raise PackError('Missing output file')
      self._basedir = self._CreateTmpDir()
      self._tmpdir = self._CreateTmpDir()
      model_details = OrderedDict()
      if not args.legacy:
        # Most of the arguments are meaningless with unified builds since we
        # get the information from the master configuration. Add checks here
        # to avoid confusion. We could possibly do some of this using
        # mutual exclusivity in the argparser, but I'm not sure it is better.
        image_args = [args.bios_image, args.ec_image, args.pd_image]
        if any(image_args) and not args.local:
          raise PackError('Cannot use -b/-p/-e with -m')
        elif args.local and not any(image_args):
          raise PackError('Must provide one of -b, -e, -p with -l')
        if not args.config:
          raise PackError('Missing master configuration file (use -c)')

        conf = libcros_config_host.CrosConfig(args.config)
        osutils.SafeMakedirs(self._BaseDirPath(MODELS_DIR))
        osutils.SafeMakedirs(os.path.join(self._tmpdir, MODELS_DIR))

        firmware_info = conf.GetFirmwareInfo()
        # TODO: Ignore the passed-in model list and use the list from
        # libcros_config_host always. For now the passed-in list is used for
        # testing.
        models = args.models
        if not args.models:
          models = []
          if firmware_info:
            models = firmware_info.keys()
        for model in models:
          model_details[model] = self._GenerateOneModel(
              firmware_info[model], args, model_details)
        replace_dict = {}
      else:
        image_files = self._ProcessModel(
            model='',
            bios_image=args.bios_image,
            bios_rw_image=args.bios_rw_image,
            ec_image=args.ec_image,
            pd_image=args.pd_image,
            extras=args.extra.split(':') if args.extra else [],
            shared_image_files=None,
            target_dir=self._basedir)
        replace_dict = self._GetReplaceDict(image_files)
      script = args.script
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
