#!/usr/bin/env python2
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Functional test for pack_firmware.py.

This runs a basic scenario and checks the output by running the update script
with a few fake tools.
"""

from __future__ import print_function

import glob
import gzip
import os
import re
import shutil
import tarfile

from chromite.lib import cros_test_lib
from chromite.lib import cros_build_lib
from chromite.lib import osutils
from chromite.lib import path_util

import pack_firmware
from pack_firmware import FirmwarePacker
import pack_firmware_utils

# We need to poke around in internal members of PackFirmware.
# pylint: disable=W0212

REEF_HWID = 'Reef A12-B3C-D5E-F6G-H7I'
REEF_MODEL = 'reef'
REEF_RO_MAIN_VERSION = 'Google_Reef.9042.43.0'
UPDATER = 'updater4.sh'
MODELS_DIR = 'models'

# Firmware update runs on the device using the dash shell. Try to use this if
# available.
HAVE_DASH = os.path.exists('/bin/dash')
SHELL = '/bin/dash' if HAVE_DASH else '/bin/sh'


class TestFunctional(cros_test_lib.TempDirTestCase):
  """Functional test for firmware packer script.

  If a test needs additional writable paths, they can use self.tempdir.  Just
  make sure to not pick a path already used by one of the other members (see
  the code below for actual paths).

  Members:
    indir: Directory which contains the input firmware files (e.g. image.bin).
    basedir: Directory containing this script.
    outdir: Directory to place output shellball.
    tempdir: Temporary directory to use as our base for other temp dirs.
    unpackdir: Directory used to unpack shellball into.
  """

  def setUp(self):
    # These are the named paths we use under self.tempdir.
    self.indir = os.path.join(self.tempdir, 'indir')
    self.outdir = os.path.join(self.tempdir, 'outdir')
    self.unpackdir = os.path.join(self.tempdir, 'unpackdir')

    osutils.SafeMakedirs(self.indir)
    osutils.SafeMakedirs(self.outdir)
    osutils.SafeMakedirs(self.unpackdir)

    pack_firmware_utils.MakeTestFiles()
    self.packer = FirmwarePacker('test')
    with tarfile.open('functest/Reef.9042.50.0.tbz2') as tar:
      tar.extractall(self.indir)
    with tarfile.open('functest/Reef_EC.9042.50.0.tbz2') as tar:
      tar.extractall(self.indir)
    self.basedir = os.path.realpath(os.path.dirname(__file__))
    self.chroot = path_util.ToChrootPath('/')
    self.packer._force_dash = HAVE_DASH

  @staticmethod
  def _ExpectedFiles(extra_files, models=None):
    """Get a sorted list of files that we expect to see in the shellball.

    Args:
      extra_files: A list of extra files to include.
      models: A list of models whose files need to be included.

    Returns:
      A sorted list of files to expect.
    """
    expected_files = [
        'common.sh',
        'crosfw.sh',
        'crossystem',
        'crosutil.sh',
        'flashrom',
        'futility',
        'mosys',
        'shflags',
        'VERSION',
        'VERSION.md5',
        'vpd',
        UPDATER,
    ] + extra_files
    if models:
      for model in models:
        expected_files.append(os.path.join(MODELS_DIR, model, 'bios.bin'))
        expected_files.append(os.path.join(MODELS_DIR, model, 'ec.bin'))
        expected_files.append(os.path.join(MODELS_DIR, model, 'setvars.sh'))
    return sorted(expected_files)

  def _RunScript(self, outfile, hwid, model, ro_main_version,
                 mode='autoupdate', customization_id=''):
    """Run an autoupdate with the shellball and check that it works.

    This relies on fake tools, principally crossystem which is controlled by
    environment variables set here.

    Args:
      outfile: Shellball output file to test.
      hwid: Hardware ID to provide when the script asks for the hardware ID
      model: Model name to provide when the script asks for it.
      ro_main_version: Value to return when RO_FWID is requested.
      mode: Execution mode (can be 'autoupdate' or 'output')
      customization_id: Value to return for customization_id from the fake vpd

    Returns:
      List of lines output from the script
    """
    # These are used by our fake crossystem script (see functest/ directory).
    os.environ['FAKE_BIOS_BIN'] = os.path.join(self.indir, 'image.bin')
    os.environ['FAKE_GBB_BIN'] = os.path.join(self.basedir, 'test', 'gbb.bin')
    os.environ['FAKE_NVRAM_ROM'] = os.path.join(self.basedir, 'test',
                                                'nvram.rom')
    os.environ['FAKE_FWID'] = ro_main_version
    os.environ['FAKE_FW_VBOOT2'] = '0'
    os.environ['FAKE_HWID'] = hwid
    os.environ['FAKE_MODEL'] = model
    os.environ['FAKE_MAINFW_ACT'] = 'A'
    os.environ['FAKE_RO_FWID'] = ro_main_version
    os.environ['FAKE_TPM_FWVER'] = '1'
    os.environ['FAKE_WPSW_CUR'] = '1'    # RO firmware is write-protected.
    os.environ['FAKE_VDAT_FLAGS'] = '0'  # Not using RO normal.
    os.environ['FAKE_CUSTOMIZATION_ID'] = customization_id
    cmd = [SHELL, outfile]
    cmd += ['--mode', mode]
    if mode == 'output':
      cmd += ['--model', model, '--output_dir', self.outdir]
    cmd += ['--verbose', '--debug']
    result = cros_build_lib.RunCommand(cmd, capture_output=True,
                                       print_cmd=False)

    # We expect debugging output but should not get anything else.
    errors = [line for line in result.error.splitlines()
              if not line.startswith(' (DEBUG')]
    self.assertEqual(errors, [])
    return result.output.splitlines()

  def _RunPackFirmware(self, extra_args):
    """Run the FirmwarePacker process and read the resulting shellball.

    Args:
      extra_args: Extra arguments to pass to FirmwarePacker.

    Returns:
      Tuple containing:
        Path to output shellball.
        Sorted list of files in the shellball.
        Dict containing the version information, with each entry being:
          key: shell variable (e.g. TARGET_FWID).
          value: value of that variable.
    """
    tool_path = [
        os.path.join(self.basedir, 'functest'),
        os.path.join(self.chroot, 'usr/sbin'),
        os.path.join(self.chroot, 'usr/bin'),
    ]
    outfile = os.path.join(self.outdir, 'output.sh')
    argv = extra_args + [
        '-o', outfile, '-q',
        '--script', UPDATER,
        '--tool_base', ':'.join(tool_path),
    ]

    # Create the shellball, extract it, and get a list of files it contains.
    os.environ['SYSROOT'] = 'test'
    os.environ['FILESDIR'] = 'test'
    self.packer.Start(argv)
    cros_build_lib.RunCommand([outfile, '--sb_extract', self.unpackdir],
                              quiet=True, mute_output=True)
    files = []
    for dirpath, _, fnames in os.walk(self.unpackdir):
      for fname in fnames:
        rel_path = os.path.join(dirpath, fname)[len(self.unpackdir) + 1:]
        files.append(rel_path)

    versions = pack_firmware_utils.ReadVersions(outfile)
    return outfile, sorted(files), versions

  def testFirmwareUpdate(self):
    """Run the firmware packer, unpack the result and check it."""
    extra_args = ['-b', os.path.join(self.indir, 'image.bin'), '-L',
                  '--script', UPDATER]
    outfile, files, versions = self._RunPackFirmware(extra_args)

    # Check that we got the right files.
    self.assertEqual(13, len(files))
    self.assertEqual(self._ExpectedFiles(['bios.bin']), files)

    # Comb through the VERSION file and check that everything is as expected.
    lines = osutils.ReadFile(
        os.path.join(self.unpackdir, 'VERSION')).splitlines()
    self.assertEqual(8, len(lines))
    self.assertEqual(
        'flashrom(8): 7404a736f0c1a92b75cddc7722f21b4d '
        '*%s/functest/flashrom' % self.basedir, lines[1])
    self.assertEqual('             data', lines[2])
    self.assertEqual('             0.9.4  : 1bb61e1 : Feb 07 2017 18:29:17 UTC',
                     lines[3])
    self.assertEqual('', lines[4])
    self.assertEqual(
        'BIOS image:   99a6fc64e45596aa2c1a9911cddce952 *%s/image.bin' %
        self.indir, lines[5])
    self.assertEqual('BIOS version: Google_Reef.9042.50.0', lines[6])

    self.assertEqual('Google_Reef.9042.50.0', versions['TARGET_RO_FWID'])
    self.assertEqual('Google_Reef.9042.50.0', versions['TARGET_FWID'])
    self.assertEqual('', versions['TARGET_ECID'])
    self.assertEqual('', versions['TARGET_PDID'])
    self.assertEqual('Google_Reef', versions['TARGET_PLATFORM'])
    self.assertEqual(UPDATER, versions['TARGET_SCRIPT'])
    self.assertEqual('', versions['UNIBUILD'])
    self.assertEqual(8, len(lines))

    # Run the shellball to make sure we can do a fake autoupdate.
    self._RunScript(outfile, REEF_HWID, REEF_MODEL, REEF_RO_MAIN_VERSION)

  def _CheckVersionsReef(self, versions):
    """Check the versions match expectations for reef.

    Args:
      versions: Dict of version information:
        key: Shell variable name..
        value: Value of that variable.
    """
    self.assertEqual('Google_Reef.9042.50.0', versions['TARGET_FWID'])
    self.assertEqual('Google_Reef.9042.50.0', versions['TARGET_RO_FWID'])
    self.assertEqual('reef_v1.1.5857-77f6ed7', versions['TARGET_ECID'])

    self.assertEqual('', versions['TARGET_PDID'])
    self.assertEqual('Google_Reef', versions['TARGET_PLATFORM'])

  def _CheckVars(self, versions, base, signature_id):
    """Check the versions match expectations.

    Args:
      versions: Dict of version information:
        key: Shell variable name..
        value: Value of that variable.
      base: Base directory format, e.g. 'models/reef/%s' for location of
          firmware files.
      signature_id: Expected signature ID.
    """
    self.assertEqual(base % 'bios.bin', versions['IMAGE_MAIN'])
    self.assertEqual(base % 'bios_rw.bin', versions['IMAGE_MAIN_RW'])
    self.assertEqual(base % 'ec.bin', versions['IMAGE_EC'])
    self.assertEqual(base % 'pd.bin', versions['IMAGE_PD'])
    self.assertEqual(signature_id, versions['SIGNATURE_ID'])

  def _SetupArgs(self, extra_models=None):
    """Set up the arguments to execute a functional test

    This is a convenience function to hold common code.

    Args:
      extra_models: List of extra models to generate firmware for, or None

    Returns:
      Tuple:
        List of extra arguments to pass to the firmware updater
        List of files we expect to see in the firmware update
    """
    models = ['reef', 'pyro', 'sand']
    all_models = models + ['electro', 'basking']  # Share others firmware
    if extra_models:
      all_models += extra_models
    extra_args = []
    for model in all_models:
      extra_args += ['-m', model]
    extra_args += ['-c', 'test/config.dtb', '-i', 'functest']
    expected_files = self._ExpectedFiles(
        ['fake_extra_tool',
         'ectool',
         'image.bin',
         'models/electro/setvars.sh',
         'models/basking/setvars.sh',
         'signer_config.csv'],
        models)
    return extra_args, expected_files

  def testFirmwareUpdateUnibuild(self):
    """Run the firmware packer, unpack the result and check it."""
    extra_args, expected_files = self._SetupArgs()
    outfile, files, versions = self._RunPackFirmware(extra_args)

    self.assertEqual(27, len(files))
    self.assertEqual(expected_files, files)
    version_lines = osutils.ReadFile(
        os.path.join(self.unpackdir, 'VERSION')).splitlines()

    # Each assertion matches to a line:
    assertions = [
        None,  # Skip testing first line.
        lambda(x): self.assertEqual(
            'flashrom(8): 7404a736f0c1a92b75cddc7722f21b4d '
            '*%s/functest/flashrom' % self.basedir, x),
        lambda(x): self.assertEqual('             data', x),
        lambda(x): self.assertEqual(
            '             0.9.4  : 1bb61e1 : Feb 07 2017 18:29:17 UTC', x),
        lambda(x): self.assertEqual('', x),
        lambda(x): self.assertIn('reef', x),
        lambda(x): self.assertIn('reef/image.bin', x),
        lambda(x): self.assertEqual('BIOS version: Google_Reef.9042.50.0', x),
        lambda(x): self.assertIn('/reef/ec.bin', x),
        lambda(x): self.assertEqual('EC version:   reef_v1.1.5857-77f6ed7', x),
        lambda(x): self.assertEqual('Extra files from folder: test/extra', x),
        lambda(x): self.assertEqual('Extra file: test/usr/sbin/ectool', x),
        lambda(x): self.assertIn(
            'Extra BCS file: bcs://Reef.9000.0.0.tbz2:', x),
        lambda(x): self.assertEqual('', x),
        lambda(x): self.assertIn('pyro', x),
        lambda(x): self.assertIn('pyro/image.bin', x),
        lambda(x): self.assertEqual('BIOS version: Google_Pyro.9042.41.0', x),
        lambda(x): self.assertIn('/pyro/ec.bin', x),
        lambda(x): self.assertEqual('EC version:   pyro_v1.1.5840-f0d7761', x),
        lambda(x): self.assertEqual('', x),
        lambda(x): self.assertIn('sand', x),
        lambda(x): self.assertIn('sand/image.bin', x),
        lambda(x): self.assertEqual('BIOS version: Google_Reef.9000.0.0', x),
        lambda(x): self.assertIn('/sand/ec.bin', x),
        lambda(x): self.assertEqual('EC version:   reef_v1.1.5857-77f6ed7', x),
        lambda(x): self.assertEqual('', x),
        lambda(x): self.assertIn('electro', x),
        lambda(x): self.assertIn('reef/image.bin', x),
        lambda(x): self.assertEqual('BIOS version: Google_Reef.9042.50.0', x),
        lambda(x): self.assertIn('/reef/ec.bin', x),
        lambda(x): self.assertEqual('EC version:   reef_v1.1.5857-77f6ed7', x),
        lambda(x): self.assertEqual('Extra files from folder: test/extra', x),
        lambda(x): self.assertEqual('Extra file: test/usr/sbin/ectool', x),
        lambda(x): self.assertIn(
            'Extra BCS file: bcs://Reef.9000.0.0.tbz2:', x),
        lambda(x): self.assertEqual('', x),
        lambda(x): self.assertIn('basking', x),
        lambda(x): self.assertIn('sand/image.bin', x),
        lambda(x): self.assertEqual('BIOS version: Google_Reef.9000.0.0', x),
        lambda(x): self.assertIn('/sand/ec.bin', x),
        lambda(x): self.assertEqual('EC version:   reef_v1.1.5857-77f6ed7', x),
        lambda(x): self.assertEqual('', x),
    ]
    self.assertEqual(len(assertions), len(version_lines))
    for i, assertion in enumerate(assertions):
      if assertion:
        assertion(version_lines[i])

    self.assertEqual('yes', versions['UNIBUILD'])
    for var in pack_firmware.REPLACE_VARS:
      self.assertEqual('<unused with unified builds>', versions[var])

    versions = pack_firmware_utils.ReadVersions(
        os.path.join(self.unpackdir, MODELS_DIR, 'reef/setvars.sh'))
    self.assertEqual(10, len(versions))
    self._CheckVersionsReef(versions)
    self._CheckVars(versions, 'models/reef/%s', 'reef')

    versions = pack_firmware_utils.ReadVersions(
        os.path.join(self.unpackdir, MODELS_DIR, 'electro/setvars.sh'))
    self._CheckVersionsReef(versions)
    self._CheckVars(versions, 'models/reef/%s', 'electro')

    versions = pack_firmware_utils.ReadVersions(
        os.path.join(self.unpackdir, MODELS_DIR, 'pyro/setvars.sh'))
    self.assertEqual('Google_Pyro.9042.41.0', versions['TARGET_FWID'])
    self._CheckVars(versions, '${MODEL_DIR}/%s', 'pyro')

    lines = osutils.ReadFile(
        os.path.join(self.unpackdir, 'signer_config.csv')).splitlines()
    self.assertEqual('model_name,firmware_image,key_id,ec_image', lines[0])
    self.assertEqual('reef,models/reef/bios.bin,REEF,models/reef/ec.bin',
                     lines[1])
    self.assertEqual('pyro,models/pyro/bios.bin,PYRO,models/pyro/ec.bin',
                     lines[2])
    self.assertEqual('sand,models/sand/bios.bin,SAND,models/sand/ec.bin',
                     lines[3])
    self.assertEqual('electro,models/reef/bios.bin,ELECTRO,models/reef/ec.bin',
                     lines[4])
    self.assertEqual('basking,models/sand/bios.bin,BASKING,models/sand/ec.bin',
                     lines[5])

    # Run the firmware update for Reef and Pyro.
    self._RunScript(outfile, REEF_HWID, REEF_MODEL, REEF_RO_MAIN_VERSION)
    self._RunScript(outfile, 'Pyro C25-R45-W23-H63-M33', 'pyro',
                    'Google_Pyro.9021.00.0')
    self._RunScript(outfile, REEF_HWID, 'electro', REEF_RO_MAIN_VERSION)

  def testVersionOutput(self):
    """Check that the -V option shows version information as expected"""
    extra_args, expected_files = self._SetupArgs()
    outfile, files, _ = self._RunPackFirmware(extra_args)

    cmd = [SHELL, outfile, '-V']
    result = cros_build_lib.RunCommand(cmd, capture_output=True,
                                       print_cmd=False)
    check_version_lines = result.output.splitlines()

    version_lines = osutils.ReadFile(
        os.path.join(self.unpackdir, 'VERSION')).splitlines()
    self.assertEqual(version_lines, check_version_lines[:len(version_lines)])

    # Now check the second part of the -V output, which is the contents of
    # the VERSIONS.md5 file. Note that VERSION and VERSION.md5 do not have
    # entries in this output.
    first_md5_line, remaining_md5_lines = (
        check_version_lines[len(version_lines)],
        check_version_lines[len(version_lines) + 1:])
    self.assertEqual('Package Content:', first_md5_line)

    # We want to get the filename, The line format is:
    #
    # ab911c951ccf0dddf023defee22dea83 *./crosfw.sh
    #
    # so split into two words and strip the *./ off the second word
    re_file = re.compile('.* ...(.*)$')
    files = [re_file.match(line).group(1) for line in remaining_md5_lines]
    expected_files.remove('VERSION')
    expected_files.remove('VERSION.md5')
    self.assertEqual(set(expected_files), set(files))

  def _MakeImage(self, outfile, ro_id, rw_id=''):
    """Create a new firmware image with the given IDs."

    This uses the existing flashmap defined in functest/base.fmd. It has two
    256-byte ID sections followed by an FMAP section. We can easily create a
    file that conforms to this map by padding our ID strings to 256 bytes.

    Note fmap.bin can be created with:
      $ fmaptool functest/base.fmd functtest.fmap.bin

    The binary file is checked in since fmaptool is not installed by the
    coreboot-utils ebuild.

    Args:
      outfile: Destination file (within the unpack directory) for output image.
      ro_id: Read-only firmware ID to use.
      rw_id: Read-write firmware ID to use, empty string if none.
    """
    with open(os.path.join(self.unpackdir, outfile), 'wb') as fd:
      fd.write(ro_id + chr(0) * (256 - len(ro_id)))
      fd.write(rw_id + chr(0) * (256 - len(rw_id)))
      fd.write(osutils.ReadFile('functest/fmap.bin'))

  def testRepack(self):
    """Repacking the shellball with new images should update versions."""
    extra_args = ['-b', os.path.join(self.indir, 'image.bin'), '-L']
    outfile, _, versions = self._RunPackFirmware(extra_args)
    self.assertEqual('Google_Reef.9042.50.0', versions['TARGET_RO_FWID'])
    self.assertEqual('Google_Reef.9042.50.0', versions['TARGET_FWID'])
    self.assertEqual('', versions['TARGET_ECID'])
    self.assertEqual('', versions['TARGET_PDID'])

    # Extract the file into a directory, then overwrite various files with new
    # images with a different IDs.
    cros_build_lib.RunCommand([outfile, '--sb_extract', self.unpackdir],
                              quiet=True, mute_output=True)
    ro_id = 'Google_Veyron_Mickey.6588.197.0'
    rw_id = 'Google_Veyron_Mickey.6588.197.1'
    ec_id = 'GoogleEC_Veyron_Mickey.6588.197.0'
    pd_id = 'GooglePD_Veyron_Mickey.6588.197.0'

    self._MakeImage('bios.bin', ro_id, rw_id)
    self._MakeImage('ec.bin', ec_id)
    self._MakeImage('pd.bin', pd_id)

    # Repack the file and make sure that the versions update.
    cros_build_lib.RunCommand([outfile, '--sb_repack', self.unpackdir],
                              quiet=True, mute_output=True)
    versions = pack_firmware_utils.ReadVersions(outfile)
    self.assertEqual(ro_id, versions['TARGET_RO_FWID'])
    self.assertEqual(rw_id, versions['TARGET_FWID'])
    self.assertEqual(ec_id, versions['TARGET_ECID'])
    self.assertEqual(pd_id, versions['TARGET_PDID'])

  def testFilesSorted(self):
    """Files in the shellball should be sorted by filename."""
    extra_args = ['-b', os.path.join(self.indir, 'image.bin'), '-L']
    outfile, files, _ = self._RunPackFirmware(extra_args)

    # The shellball shows files in a comment with this format:
    # 16777216 -rw-r--r-- bios.bin
    re_files = re.compile(r'^# +[0-9]+ [-rwx]+ \(.*\)$')
    for line in osutils.ReadFile(outfile).splitlines():
      m = re_files.match(line)
      if m:
        files.append(m.group(1))
    self.assertEqual(files, sorted(files))

  def _AssertFileEqual(self, expect_fname, fname):
    """Check that two files have the same contents.

    This causes a test failure if the file contents do not match.

    Args:
      expect_fname: File containing expected contents
      fname: File containing contents to check
    """
    expect = osutils.ReadFile(expect_fname)
    data = osutils.ReadFile(fname)
    if expect != data:
      self.fail("Contents of '%s' does not match '%s'" % (fname, expect_fname))

  def _GetUnequalRegions(self, expect_fname, fname, sig_id=None,
                         expect_root_sum=None):
    """Get a list of firmware regions which are not the same

    This is used to compare two firmware images. It checks the files regions
    by region. Any regions which do not match are added to the returned list.
    For vblock regions the contents are checked against the corresponding
    vblock file in functest rather than the contents of expect_fname. For
    GBB regions the root key is checked against expect_root_sum.

    The goal of this function is to check that the firmware the updater would
    write has the correct keys inside it for the model being written. Note that
    'SECTION' regions (include 'WP_RO' are ignored since they cover other
    regions (all of which we check).

    Args:
      expect_fname: File containing expected contents
      fname: File containing contents to check
      sig_id: Signature ID for vblock, or None
      expect_root_sum: Expected SHA1 sum of the root key (as a string), or None

    Returns:
      List of file regions that differ  (e.g. ['GBB']) excluding any
      'section' regions
    """
    try:
      expect_dir = os.path.join(self.tempdir, 'expect')
      osutils.SafeMakedirs(expect_dir)
      cros_build_lib.RunCommand(['dump_fmap', '-x', expect_fname], quiet=True,
                                cwd=expect_dir)
      actual_dir = os.path.join(self.tempdir, 'actual')
      osutils.SafeMakedirs(actual_dir)
      cros_build_lib.RunCommand(['dump_fmap', '-x', fname], quiet=True,
                                cwd=actual_dir)
      differ = []
      for expect_fname in glob.glob('%s/*' % expect_dir):
        expect = osutils.ReadFile(expect_fname)
        basename = os.path.basename(expect_fname)
        data = osutils.ReadFile(os.path.join(actual_dir, basename))
        if sig_id and basename in ['VBLOCK_A', 'VBLOCK_B']:
          expect = self._GetVblock(sig_id, basename[-1])
        elif expect_root_sum and basename == 'GBB':
          result = cros_build_lib.RunCommand(
              ['futility', 'show', fname], quiet=True, mute_output=True,
              error_code_ok=True)
          # Relevant output is:
          #   Root Key:
          #     Vboot API:           1.0
          #     Algorithm:           11 RSA8192 SHA512
          #     Key Version:         1
          #     Key sha1sum:         ac7c01b1bea84da486f30a52bba5eb67ff45f50f
          lines = result.output.splitlines()
          filtered_lines = [line for line in lines if 'sha1sum' in line]
          data = re.match(' *Key sha1sum: *(.*)$', filtered_lines[0]).group(1)
          expect = expect_root_sum

        if 'SECTION' not in basename and basename != 'WP_RO' and expect != data:
          differ.append(basename)

    finally:
      osutils.RmDir(expect_dir)
      osutils.RmDir(actual_dir)
    return differ

  def testFirmwareOutput(self):
    """Check the --output feature."""
    extra_args, _ = self._SetupArgs()
    outfile, files, _ = self._RunPackFirmware(extra_args)
    lines = self._RunScript(outfile, REEF_HWID, REEF_MODEL,
                            REEF_RO_MAIN_VERSION, 'output')
    self.assertIn('Firmware update (output) completed.', lines)
    self.assertIn('Firmware images generated to: %s' % self.outdir, lines)

    # Check that the files were written correctly
    files = glob.glob('%s/*' % self.outdir)
    ec_file = '%s/ec.bin' % self.outdir
    bios_file = '%s/bios.bin' % self.outdir
    self.assertIn(ec_file, files)
    self.assertIn(bios_file, files)

    # Now check that the file contents match. We allow a few sections to have
    # differences due to the actions of the firmware packer. Specifically it
    # changes some fields in the GBB.
    regions = self._GetUnequalRegions(bios_file, '%s/image.bin' % self.indir)
    self.assertEqual(['GBB'], regions)
    self._AssertFileEqual(ec_file, '%s/ec.bin' % self.indir)

    # Electro should be the same as reef
    self._RunScript(outfile, REEF_HWID, 'electro', REEF_RO_MAIN_VERSION,
                    'output')
    regions = self._GetUnequalRegions(bios_file, '%s/image.bin' % self.indir)
    self.assertEqual(['GBB'], regions)

  def _GetVblock(self, sig_id, a_or_b):
    """Get the contents of an A or B vblock for a given signature ID

    Args:
      sig_id: Signature ID for vblock (this is key_id in the update script)
      a_or_b: Either 'A' or 'B' to select which vblock to use

    Returns:
      Contents of the vblock file as a string
    """
    self.assertIn(a_or_b, ['A', 'B'])
    with gzip.GzipFile('functest/vblock_%s.%s.gz' % (a_or_b, sig_id)) as fd:
      return fd.read()

  def _CopyVblock(self, keydir, sig_id, a_or_b):
    """Copy a vblock file into the firmware-update key directory

    Args:
      keydir: Destination directory to add files into
      sig_id: Signature ID for vblock
      a_or_b: Either 'A' or 'B' to select which vblock to use
    """
    path = os.path.join(keydir, 'vblock_%s.%s' % (a_or_b, sig_id))
    osutils.WriteFile(path, self._GetVblock(sig_id, a_or_b), mode='wb')

  def _AddKeys(self, keydir, sig_id):
    """Add root key and vblock information for testing.

    Args:
      keydir: Destination directory to add files into
      sig_id: Signature ID used to identify files in functest/

    Returns:
      SHA1 hash of the root key (as a string)
    """
    rootkey = 'functest/rootkey.%s' % sig_id
    shutil.copy(rootkey, keydir)
    self._CopyVblock(keydir, sig_id, 'A')
    self._CopyVblock(keydir, sig_id, 'B')
    result = cros_build_lib.RunCommand([
        'futility', 'show', rootkey], quiet=True, mute_output=True)

    # The last output line has the following format:
    #   Key sha1sum:         ac7c01b1bea84da486f30a52bba5eb67ff45f50f
    return result.output.splitlines()[-1].split()[2]

  def _CheckOutput(self, outfile, model, rootkey_sum, customization_id=''):
    """Check that the firmware updater can generate the correct output.

    This runs the firmware in 'output' mode with the given model and checks
    that the resulting firmware image is correctly signed for that model.

    Args:
      outfile: Firmware update shellball filename
      model: Name of model to generate firmware for
      rootkey_sum: SHA1 sum of the root key (as a string)
      customization_id: Value to return for customization_id from the fake vpd
    """
    lines = self._RunScript(outfile, REEF_HWID, model, REEF_RO_MAIN_VERSION,
                            'output', customization_id)
    expected_sig_id = customization_id if customization_id else model
    self.assertIn('Firmware keys changed to set [%s].' % expected_sig_id, lines)
    bios_file = '%s/bios.bin' % self.outdir
    regions = self._GetUnequalRegions('%s/image.bin' % self.indir, bios_file,
                                      expected_sig_id, rootkey_sum)
    self.assertEqual([], regions)

  def AddFakeKeys(self, outfile, key_ids):
    """Add fake keys to an existing firmware update to allow testing

    Args:
      outfile: Firmware update file to modify
      key_ids: List of key IDs to add

    Returns:
      List of sums for each key in key_ids
    """
    cros_build_lib.RunCommand([outfile, '--sb_extract', self.unpackdir],
                              quiet=True, mute_output=True)
    keydir = os.path.join(self.unpackdir, 'keyset')
    osutils.SafeMakedirs(keydir)
    key_sums = []
    for key_id in key_ids:
      key_sums.append(self._AddKeys(keydir, key_id))
    cros_build_lib.RunCommand([outfile, '--sb_repack', self.unpackdir],
                              quiet=True, mute_output=True)
    return key_sums

  def testSignedFirmwareOutput(self):
    """Check the --output feature with signed firmware."""
    extra_args, _ = self._SetupArgs()
    outfile, _, _ = self._RunPackFirmware(extra_args)

    # Add some fake root keys and vblocks for some of the models, to simulate
    # the action of the signer.
    reef_sum, electro_sum = self.AddFakeKeys(outfile, ['reef', 'electro'])

    self._CheckOutput(outfile, REEF_MODEL, reef_sum)
    self._CheckOutput(outfile, 'electro', electro_sum)

  def testHelp(self):
    """Check that the -h option shows help"""
    extra_args, _ = self._SetupArgs()
    outfile, _, _ = self._RunPackFirmware(extra_args)

    cmd = [outfile, '-h']
    result = cros_build_lib.RunCommand(cmd, capture_output=True, quiet=True,
                                       mute_output=True, error_code_ok=True)
    self.assertEqual(1, result.returncode)
    self.assertIsNone(result.error)
    lines = result.output.splitlines()
    self.assertIn('USAGE: %s [bundle_option|--] [updater_options]' % outfile,
                  lines)
    self.assertIn('USAGE: ./updater4.sh [flags] args', lines)
    self.assertIn('  --[no]force:  Try to force update. (default: false)',
                  lines)

  def testWhitelabel(self):
    """Test generation of firmware for a whitelabel model"""
    extra_args, _ = self._SetupArgs(['whitelabel-test'])
    self._RunPackFirmware(extra_args)
    version_lines = osutils.ReadFile(
        os.path.join(self.unpackdir, 'VERSION')).splitlines()

    # Check that the whitelabel version output matches sand.
    found = None
    for linenum, line in enumerate(version_lines):
      if 'whitelabel-test' in line:
        found = linenum
        break
    self.assertTrue(found)
    lines = version_lines[found:]
    assertions = [
        lambda(x): self.assertIn('whitelabel-test', x),
        lambda(x): self.assertIn('sand/image.bin', x),
        lambda(x): self.assertEqual('BIOS version: Google_Reef.9000.0.0', x),
        lambda(x): self.assertIn('/sand/ec.bin', x),
        lambda(x): self.assertEqual('EC version:   reef_v1.1.5857-77f6ed7', x),
        lambda(x): self.assertEqual('', x),
    ]
    self.assertEqual(len(assertions), len(lines))
    for i, assertion in enumerate(assertions):
      if assertion:
        assertion(lines[i])

    # Check that the setvars.sh file is correct (points to sand).
    versions = pack_firmware_utils.ReadVersions(
        os.path.join(self.unpackdir, MODELS_DIR, 'whitelabel-test/setvars.sh'))
    self.assertEqual('Google_Reef.9000.0.0', versions['TARGET_FWID'])
    self._CheckVars(versions, 'models/sand/%s', 'whitelabel-test')

    # Check the signer instructions - the last line should be for whitelabel.
    lines = osutils.ReadFile(
        os.path.join(self.unpackdir, 'signer_config.csv')).splitlines()
    self.assertEqual(
        'whitelabel-test,models/sand/bios.bin,WHITELABEL,models/sand/ec.bin',
        lines[-1])

  def _CheckZeroTouchWhitelabel(self, model, wl_models, wl_tags):
    """Test generation of firmware for a 'zero-touch' whitelabel model

    This is one where the device cannot tell its model name by hardware
    detection, but must use the model name stored in the VPD
    customization_id.

    Args:
      model: Main whitelabel model to generate firmware for
      wl_models: List of whitelabel models to generate firmware for
      wl_tags: List of whitelabel tags to generate firmware for
    """
    extra_args, _ = self._SetupArgs([model] + wl_models + wl_tags)

    # For our purposes we can treat the tags like models from here on.
    if not wl_models:
      wl_models = wl_tags
    outfile, _, _ = self._RunPackFirmware(extra_args)
    version_lines = osutils.ReadFile(
        os.path.join(self.unpackdir, 'VERSION')).splitlines()

    # Check that the whitelabel version output matches sand.
    found = None
    for linenum, line in enumerate(version_lines):
      if model in line:
        found = linenum
        break
    self.assertTrue(found)
    lines = version_lines[found:]
    assertions = [
        lambda(x): self.assertIn(model, x),
        lambda(x): self.assertIn('reef/image.bin', x),
        lambda(x): self.assertEqual('BIOS version: Google_Reef.9042.50.0', x),
        lambda(x): self.assertIn('/reef/ec.bin', x),
        lambda(x): self.assertEqual('EC version:   reef_v1.1.5857-77f6ed7', x),
        lambda(x): self.assertEqual('Extra files from folder: test/extra', x),
        lambda(x): self.assertEqual('Extra file: test/usr/sbin/ectool', x),
        lambda(x): self.assertIn(
            'Extra BCS file: bcs://Reef.9000.0.0.tbz2:', x),
        lambda(x): self.assertEqual('', x),
    ]
    self.assertEqual(len(assertions), len(lines))
    for i, assertion in enumerate(assertions):
      if assertion:
        assertion(lines[i])

    # We should not create setvars.sh scripts for zero-touch whitelabels.
    for wl_model in wl_models:
      self.assertNotExists(os.path.join(self.unpackdir, MODELS_DIR, wl_model))

    # Check that the setvars.sh file is correct (points to sand).
    versions = pack_firmware_utils.ReadVersions(
        os.path.join(self.unpackdir, MODELS_DIR, '%s/setvars.sh' % model))
    self.assertEqual('Google_Reef.9042.50.0', versions['TARGET_FWID'])
    self._CheckVars(versions, 'models/reef/%s', 'sig-id-in-customization-id')

    # Check the signer instructions - we should get keys for the two
    # zero-touch whitelabels.
    lines = osutils.ReadFile(
        os.path.join(self.unpackdir, 'signer_config.csv')).splitlines()
    self.assertEqual('basking,models/sand/bios.bin,BASKING,models/sand/ec.bin',
                     lines[-1 - len(wl_models)])
    for i, wl_model in enumerate(wl_models):
      expect = ('%s,models/reef/bios.bin,%s,models/reef/ec.bin' %
                (wl_model, wl_model.upper()))
      self.assertEqual(expect, lines[i - len(wl_models)])

    sums = self.AddFakeKeys(outfile, wl_models)

    for i, wl_model in enumerate(wl_models):
      self._CheckOutput(outfile, model, sums[i], wl_model)

  def testZeroTouchWhitelabel(self):
    """Test generation of firmware for a 'zero-touch' whitelabel model

    This uses the original schema, where each whitelabel is a model.
    """
    self._CheckZeroTouchWhitelabel('zero-touch-whitelabel', ['zt1', 'zt2'], [])

  def testZeroTouchWhitelabelAlternate(self):
    """Test generation of firmware for a 'zero-touch' whitelabel model

    This uses the alternate schema, where whitelabels use a single model with
    multiple 'whitelabel' tags associated with it.
    """
    self._CheckZeroTouchWhitelabel('whitelabel-alt', [], ['wltag1', 'wltag2'])


if __name__ == '__main__':
  cros_test_lib.main(module=__name__)
