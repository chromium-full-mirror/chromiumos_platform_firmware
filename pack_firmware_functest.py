# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
#
"""Functional test for pack_firmware.py.

This runs a basic scenario and checks the output by running the update script
with a few fake tools.
"""

from __future__ import print_function

import os
import re
import shutil
import tarfile
import tempfile
import unittest

from chromite.lib import cros_build_lib

from pack_firmware import FirmwarePacker
import pack_firmware_utils

# We need to poke around in internal members of PackFirmware.
# pylint: disable=W0212

REEF_HWID = 'Reef A12-B3C-D5E-F6G-H7I'
REEF_MODEL = 'reef'
REEF_STABLE_MAIN_VERSION = 'Google_Reef.9042.43.0'
UPDATER = 'updater4.sh'
MODELS_DIR = 'models'

# Firmware update runs on the device using the dash shell. Try to use this if
# available.
HAVE_DASH = os.path.exists('/bin/dash')
SHELL = '/bin/dash' if HAVE_DASH else '/bin/sh'


class TestFunctional(unittest.TestCase):
  """Functional test for firmware packer script.

  Members:
    indir: Directory which contains the input firmware files (e,g. image.bin).
    basedir: Directory containing this script.
    outdir: Directory to place output shellball.
    unpackdir: Directory used to unpack shellball into.
  """

  def setUp(self):
    pack_firmware_utils.MakeTestFiles()
    self.packer = FirmwarePacker('test')
    tmp_base = 'pack_firmwaretest-%d' % os.getpid()
    self.indir = tempfile.mkdtemp(tmp_base)
    with tarfile.open('functest/Reef.9042.50.0.tbz2') as tar:
      tar.extractall(self.indir)
    self.basedir = os.path.realpath(os.path.dirname(__file__))
    if os.path.exists('/etc/cros_chroot_version'):
      self.chroot = '/'
    else:
      self.chroot = os.path.join(self.basedir, '../../../chroot')
    self.outdir = tempfile.mkdtemp(tmp_base)
    self.unpackdir = tempfile.mkdtemp(tmp_base)
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
    expected_files = (
        'crosfw.sh crosutil.sh flashrom mosys VERSION.md5 common.sh crossystem '
        'dump_fmap gbb_utility shflags VERSION vpd').split(' ')
    expected_files.append(UPDATER)
    if extra_files:
      expected_files += extra_files
    if models:
      for model in models:
        expected_files.append(os.path.join(MODELS_DIR, model, 'bios.bin'))
        expected_files.append(os.path.join(MODELS_DIR, model, 'ec.bin'))
        expected_files.append(os.path.join(MODELS_DIR, model, 'setvars.sh'))
    return sorted(expected_files)

  def _RunScript(self, outfile, hwid, model, stable_main_version,
                 unstable=False):
    """Run an autoupdate with the shellball and check that it works.

    This relies on fake tools, principally crossystem which is controlled by
    environment variables set here.

    Args:
      outfile: Shellball output file to test.
      hwid: Hardware ID to provide when the script asks for the hardware ID
      model: Model name to provide when the script asks for it.
      stable_main_version: Value to return when RO_FWID is requested.
      unstable: True to allow an error about unstable EC firmware.
    """
    # These are used by our fake crossystem script (see functest/ directory).
    os.environ['FAKE_BIOS_BIN'] = os.path.join(self.indir, 'image.bin')
    os.environ['FAKE_GBB_BIN'] = os.path.join(self.basedir, 'test', 'gbb.bin')
    os.environ['FAKE_NVRAM_ROM'] = os.path.join(self.basedir, 'test',
                                                'nvram.rom')
    os.environ['FAKE_FWID'] = stable_main_version
    os.environ['FAKE_FW_VBOOT2'] = '0'
    os.environ['FAKE_HWID'] = hwid
    os.environ['FAKE_MODEL'] = model
    os.environ['FAKE_MAINFW_ACT'] = 'A'
    os.environ['FAKE_RO_FWID'] = stable_main_version
    os.environ['FAKE_TPM_FWVER'] = '1'
    os.environ['FAKE_WPSW_CUR'] = '1'    # RO firmware is write-protected.
    os.environ['FAKE_VDAT_FLAGS'] = '0'  # Not using RO normal.
    result = cros_build_lib.RunCommand(
        [SHELL, outfile, '--mode', 'autoupdate', '--verbose', '--debug'],
        capture_output=True)

    # We expect debugging output but should not get anything else.
    errors = [line for line in result.error.splitlines()
              if not line.startswith(' (DEBUG')]
    if unstable:
      self.assertEqual(errors,
                       ['One-time RO+RW update from unstable EC firmware.'])
    else:
      self.assertEqual(errors, [])

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
    extra_args = ['-b', os.path.join(self.indir, 'image.bin'),
                  '--stable_main_version', REEF_STABLE_MAIN_VERSION,
                  '--script', UPDATER]
    outfile, files, versions = self._RunPackFirmware(extra_args)

    # Check that we got the right files.
    self.assertEqual(14, len(files))
    self.assertEqual(self._ExpectedFiles(['bios.bin']), files)

    # Comb through the VERSION file and check that everything is as expected.
    with open(os.path.join(self.unpackdir, 'VERSION')) as fd:
      lines = fd.read().splitlines()
    self.assertEqual(8, len(lines))
    self.assertEqual(
        'flashrom(8): 97e2e0a5685515808cee43002df52861 '
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
    self.assertEqual(REEF_STABLE_MAIN_VERSION, versions['STABLE_FWID'])
    self.assertEqual('', versions['STABLE_ECID'])
    self.assertEqual('', versions['STABLE_PDID'])
    self.assertEqual('', versions['UNIBUILD'])
    self.assertEqual(8, len(lines))

    # Run the shellball to make sure we can do a fake autoupdate.
    self._RunScript(outfile, REEF_HWID, REEF_MODEL, REEF_STABLE_MAIN_VERSION)

  def testFirmwareUpdateUnibuild(self):
    """Run the firmware packer, unpack the result and check it."""
    models = ['reef', 'pyro']
    extra_args = [
        '-m', 'reef', '-m', 'pyro', '-c', 'test/config.dtb', '-i', 'functest']
    outfile, files, versions = self._RunPackFirmware(extra_args)

    self.assertEqual(22, len(files))
    self.assertEqual(self._ExpectedFiles(
        ['fake_extra_tool', 'ectool', 'image.bin'], models), files)
    with open(os.path.join(self.unpackdir, 'VERSION')) as fd:
      lines = fd.read().splitlines()

    # Each assertion matches to a line:
    assertions = [
        None,  # Skip testing first line.
        lambda(x): self.assertEqual(
            'flashrom(8): 97e2e0a5685515808cee43002df52861 '
            '*%s/functest/flashrom' % self.basedir, x),
        lambda(x): self.assertEqual('             data', x),
        lambda(x): self.assertEqual(
            '             0.9.4  : 1bb61e1 : Feb 07 2017 18:29:17 UTC', x),
        lambda(x): self.assertEqual('', x),
        lambda(x): self.assertIn('reef/image.bin', x),
        lambda(x): self.assertEqual('BIOS version: Google_Reef.9042.50.0', x),
        lambda(x): self.assertIn('/reef/ec.bin', x),
        lambda(x): self.assertEqual('EC version:   reef_v1.1.5857-77f6ed7', x),
        lambda(x): self.assertEqual('Extra files from folder: test/extra', x),
        lambda(x): self.assertEqual('Extra file: test/usr/sbin/ectool', x),
        lambda(x): self.assertIn('Extra BCS file: bcs://Reef.9000.0.0.tbz2: '
                                 '/tmp', x),
        lambda(x): self.assertEqual('', x),
        lambda(x): self.assertIn('pyro/image.bin', x),
        lambda(x): self.assertEqual('BIOS version: Google_Pyro.9042.41.0', x),
        lambda(x): self.assertIn('/pyro/ec.bin', x),
        lambda(x): self.assertEqual('EC version:   pyro_v1.1.5840-f0d7761', x),
        lambda(x): self.assertEqual('', x),
    ]
    self.assertEqual(len(assertions), len(lines))
    for i, assertion in enumerate(assertions):
      if assertion:
        assertion(lines[i])

    self.assertEqual('yes', versions['UNIBUILD'])

    versions = pack_firmware_utils.ReadVersions(
        os.path.join(self.unpackdir, MODELS_DIR, 'reef/setvars.sh'))
    self.assertEqual(13, len(versions))
    self.assertEqual('Google_Reef.9042.50.0', versions['TARGET_FWID'])
    self.assertEqual('Google_Reef.9042.50.0', versions['TARGET_RO_FWID'])
    self.assertEqual('reef_v1.1.5857-77f6ed7', versions['TARGET_ECID'])

    self.assertEqual('', versions['TARGET_PDID'])
    self.assertEqual('Google_Reef.9042.43.0', versions['STABLE_FWID'])
    self.assertEqual('reef-v1.1.5840-f0d7761', versions['STABLE_ECID'])
    self.assertEqual('', versions['STABLE_PDID'])
    self.assertEqual('Google_Reef', versions['TARGET_PLATFORM'])
    base = '${MODEL_DIR}/%s'
    self.assertEqual(base % 'bios.bin', versions['IMAGE_MAIN'])
    self.assertEqual(base % 'bios_rw.bin', versions['IMAGE_MAIN_RW'])
    self.assertEqual(base % 'ec.bin', versions['IMAGE_EC'])
    self.assertEqual(base % 'pd.bin', versions['IMAGE_PD'])
    self.assertEqual(base % 'keyset', versions['KEYSET_DIR'])

    # Run the firmware update for Reef and Pyro.
    self._RunScript(outfile, REEF_HWID, REEF_MODEL, REEF_STABLE_MAIN_VERSION,
                    unstable=True)
    self._RunScript(outfile, 'Pyro C25-R45-W23-H63-M33', 'pyro',
                    'Google_Pyro.9021.00.0')

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
      fd.write(open('functest/fmap.bin').read())

  def testRepack(self):
    """Repacking the shellball with new images should update versions."""
    extra_args = ['-b', os.path.join(self.indir, 'image.bin')]
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
    extra_args = ['-b', os.path.join(self.indir, 'image.bin')]
    outfile, files, _ = self._RunPackFirmware(extra_args)

    # The shellball shows files in a comment with this format:
    # 16777216 -rw-r--r-- bios.bin
    re_files = re.compile(r'^# +[0-9]+ [-rwx]+ \(.*\)$')
    with open(outfile) as fd:
      for line in fd.read().splitlines():
        m = re_files.match(line)
        if m:
          files.append(m.group(1))
    self.assertEqual(files, sorted(files))

  def tearDown(self):
    """Remove temporary directories"""
    shutil.rmtree(self.indir)
    shutil.rmtree(self.outdir)
    shutil.rmtree(self.unpackdir)


if __name__ == '__main__':
  unittest.main()
