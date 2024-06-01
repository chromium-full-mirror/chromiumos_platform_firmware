#!/usr/bin/env python3
# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Unit tests for pack_firmware.py.

This mocks out all tools so it can run fairly quickly.
"""

import contextlib
import glob
import io
import os
import pprint
import shutil
import struct
import sys
from unittest import mock

import pack_firmware
import pack_firmware_utils

from chromite.lib import cros_test_lib
from chromite.lib import osutils
from chromite.lib import partial_mock
from chromite.signing.lib import firmware_unittest


# We need to poke around in internal members of PackFirmware.
# pylint: disable=W0212

# Pre-set ID expected for test/image.bin. Note the 'R' in the first is a 'W'
# in the second. It is confusing but this is how the firmware images are
# currently created.
RO_FRID = "Google_Reef.9264.0.0_d2017_02_09_1240"
RW_FWID = "Google_Reef.9264.0.0_d2017_02_09_1250"
NO_TIMESTAMP_RO_FRID = "Google_Reef.9264.0.1"
EC_RO_FRID = "reef_v1.1.5857-77f6ed7"
EC_RW_FWID = "reef_v1.1.5858-77f6ed7"


# Expected output from 'futility dump_fmap -p' for main image.
FMAP_OUTPUT = """WP_RO 0 4194304
SI_DESC 0 4096
IFWI 4096 2093056
RO_VPD 2097152 16384
RO_SECTION 2113536 2080768
FMAP 2113536 2048
RO_FRID 2115584 64
RO_FRID_PAD 2115648 1984
COREBOOT 2117632 1552384
GBB 3670016 262144
RO_UNUSED 3932160 262144
MISC_RW 4194304 196608
UNIFIED_MRC_CACHE 4194304 135168
RECOVERY_MRC_CACHE 4194304 65536
RW_MRC_CACHE 4259840 65536
RW_VAR_MRC_CACHE 4325376 4096
RW_ELOG 4329472 12288
RW_SHARED 4341760 16384
SHARED_DATA 4341760 8192
VBLOCK_DEV 4349952 8192
RW_VPD 4358144 8192
RW_NVRAM 4366336 24576
RW_SECTION_A 4390912 4718592
VBLOCK_A 4390912 65536
FW_MAIN_A 4456448 4652992
RW_FWID_A 9109440 64
RW_SECTION_B 9109504 4718592
VBLOCK_B 9109504 65536
FW_MAIN_B 9175040 4652992
RW_FWID_B 13828032 64
RW_LEGACY 13828096 2097152
BIOS_UNUSABLE 15925248 323584
DEVICE_EXTENSION 16248832 524288
UNUSED_HOLE 16773120 4096
"""

# Expected output from 'futility dump_fmap -p' for alternate main image.
# This FMAP contains EC_MAIN_A sections, which will alter
# the behavior of repack. A similar FMAP is used on some legacy Chrome OS
# devices.
FMAP_OUTPUT_LEGACY = """SI_ALL 0 2097152
SI_DESC 0 4096
SI_ME 4096 2093056
SI_BIOS 2097152 6291456
RW_SECTION_A 2097152 983040
VBLOCK_A 2097152 65536
FW_MAIN_A 2162688 720896
EC_MAIN_A 2949120 131008
RW_FWID_A 3080128 64
RW_SECTION_B 3080192 983040
VBLOCK_B 3080192 65536
FW_MAIN_B 3145728 720896
EC_MAIN_B 3932160 131008
RW_FWID_B 4063168 64
RW_MRC_CACHE 4063232 65536
RW_ELOG 4128768 16384
RW_SHARED 4145152 16384
SHARED_DATA 4145152 8192
VBLOCK_DEV 4153344 8192
RW_VPD 4161536 8192
RW_UNUSED 4169728 24576
RW_LEGACY 4194304 2097152
WP_RO 6291456 2097152
RO_VPD 6291456 16384
RO_UNUSED 6307840 49152
RO_SECTION 6356992 2031616
FMAP 6356992 2048
RO_FRID 6359040 64
RO_FRID_PAD 6359104 1984
GBB 6361088 978944
BOOT_STUB 7340032 1048576
"""

# Size of dummy 'ecrw' file.
ECRW_SIZE = 0x38000

# Expected output from 'futility dump_fmap -p' for EC image.
FMAP_OUTPUT_EC = """EC_RO 64 229376
FR_MAIN 64 229376
RO_FRID 388 32
FMAP 135232 350
WP_RO 0 262144
EC_RW 262144 229376
RW_FWID 262468 32
"""


# Use this to suppress stdout/stderr output:
# with capture_sys_output() as (stdout, stderr)
#   ...do something...
@contextlib.contextmanager
def capture_sys_output():
    capture_out, capture_err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = capture_out, capture_err
        yield capture_out, capture_err
    finally:
        sys.stdout, sys.stderr = old_out, old_err


class TestUnit(cros_test_lib.MockTempDirTestCase):
    """Test cases for common program flows."""

    IMAGE_DIR = "functest/images"

    @classmethod
    def setUpClass(cls):
        pack_firmware_utils.MakeTestFiles()

    def setUp(self):
        self.output = os.path.join(self.tempdir, "out")
        self.image_output = os.path.join(self.tempdir, "images")
        self.ec_comp_output = os.path.join(self.tempdir, "ec_comp_out")
        self.common_flags = [
            "-b",
            "test/image.bin",
            "-q",
            "-o",
            self.output,
            "--image_output",
            self.image_output,
        ]

        self.packer = pack_firmware.FirmwarePacker(".")
        self._ExtractFrid = pack_firmware.FirmwarePacker._ExtractFrid

    def tearDown(self):
        pack_firmware.FirmwarePacker._ExtractFrid = self._ExtractFrid

    def _AssertVersionsEqual(self, versions, expected_versions):
        """Verify the content of `versions`."""
        versions = versions.strip().splitlines()
        n = len(expected_versions)
        if len(versions) != n:
            raise AssertionError(
                f"Wrong list length {len(versions)}, expected {n}\n"
                f"Versions: \n{pprint.pformat(versions)}\n"
                f"Expected versions: \n{pprint.pformat(expected_versions)}"
            )
        self.assertEqual(len(versions), len(expected_versions))
        for i, line in enumerate(versions):
            self.assertRegex(line, expected_versions[i])

    def testBadStartup(self):
        """Test various bad start-up conditions."""
        # Starting up in another directory (without required files) should fail.
        with self.assertRaises(pack_firmware.PackError) as e:
            pack_firmware.main(["/"])
        self.assertIn("'/pack/sfx2.sh'", str(e.exception))

        # Should check for 'zip' tool.
        with mock.patch.object(osutils, "Which", return_value=None):
            with self.assertRaises(pack_firmware.PackError) as e:
                pack_firmware.main(["."])
            self.assertIn("'zip'", str(e.exception))

        # Should complain if we don't provide at least one image.
        with self.assertRaises(pack_firmware.PackError) as e:
            args = [".", "-L", "-o", "out"]
            pack_firmware.main(args)
        self.assertIn("Must assign at least one", str(e.exception))

    def testArgParse(self):
        """Test some basic argument parsing as a validity check."""
        with self.assertRaises(SystemExit):
            with capture_sys_output():
                self.assertIsNone(self.packer.ParseArgs(["--invalid"]))

        self.assertIsNone(self.packer.ParseArgs([]).bios_image)
        self.assertEqual(
            "/bios.bin", self.packer.ParseArgs(["-b", "/bios.bin"]).bios_image
        )

    def testEnsureCommand(self):
        """Check that we detect a missing command."""
        self.packer._EnsureCommand("ls", "sample-package")
        with self.assertRaises(pack_firmware.PackError) as e:
            self.packer._EnsureCommand("does-not-exist", "sample-package")
        self.assertIn("You need 'does-not-exist'", str(e.exception))

    def testTmpdirs(self):
        """Check creation and removal of temporary directories."""
        dir1 = self.packer._CreateTmpDir()
        dir2 = self.packer._CreateTmpDir()
        self.assertExists(dir1)
        self.assertExists(dir2)
        self.packer._RemoveTmpdirs()
        self.assertNotExists(dir1)
        self.assertNotExists(dir2)

    def testAddVersionInfoMissingFile(self):
        """Trying to add version info for a missing file should be detected."""
        with self.assertRaises(IOError) as e:
            self.packer._AddVersionInfo("BIOS", "missing-file", "v123")
        self.assertIn("'missing-file'", str(e.exception))

    def testAddVersionInfoNoFile(self):
        """Check adding version info with no filename."""
        self.packer._AddVersionInfo("BIOS", "", "v123")
        self._AssertVersionsEqual(
            self.packer._versions.getvalue(),
            ["BIOS version: v123"],
        )

    def testAddVersionNoVersion(self):
        """Check adding version info with no version."""
        self.packer._AddVersionInfo("BIOS", "test/image.bin", "")
        self._AssertVersionsEqual(
            self.packer._versions.getvalue(),
            [
                "BIOS image:   8ce05b02847603aef6cfa01f1bab73d0 "
                r"\*test/image.bin"
            ],
        )

    def testAddVersionInfo(self):
        """Check adding version info with both a filename and version."""
        self.packer._AddVersionInfo("BIOS", "test/image.bin", "v123")
        self._AssertVersionsEqual(
            self.packer._versions.getvalue(),
            [
                "BIOS image:   8ce05b02847603aef6cfa01f1bab73d0 "
                r"\*test/image.bin",
                "BIOS version: v123",
            ],
        )

    def testExtractFrid(self):
        """Check extracting the firmware ID from a bios image."""
        self.packer._tmpdir = "test"
        self.packer._args = self.packer.ParseArgs(
            ["--bios_image", "image.bin", "-t"]
        )
        with cros_test_lib.RunCommandMock() as rc:
            rc.AddCmdResult(
                partial_mock.ListRegex("futility dump_fmap"), returncode=0
            )
            self.assertEqual(RO_FRID, self.packer._ExtractFrid("image.bin"))

    def testExtractFridTrailingSpace(self):
        """Check extracting a firmware ID with a trailing space."""

        def _SetupImage(_, **kwargs):
            destdir = kwargs["cwd"]
            osutils.WriteFile(
                os.path.join(destdir, "RO_FRID"), b"TESTING \0\0\0", mode="wb"
            )

        self.packer._tmpdir = self.packer._CreateTmpDir()
        self.packer._args = self.packer.ParseArgs(
            ["--bios_image", "image.bin", "-t"]
        )
        with cros_test_lib.RunCommandMock() as rc:
            rc.AddCmdResult(
                partial_mock.ListRegex("futility dump_fmap"),
                returncode=0,
                side_effect=_SetupImage,
            )
            self.assertEqual("TESTING ", self.packer._ExtractFrid("image.bin"))
        self.packer._RemoveTmpdirs()

    def testExtractEcVersion(self):
        """Check extracting version from firmware ID."""
        # Old format with single hash.
        self.assertEqual(
            self.packer._ExtractEcVersion("reef_v1.1.5857-77f6ed7"),
            pack_firmware.FirmwareVersion(1, 1, 5857),
        )
        # Old format with multiple hashes.
        self.assertEqual(
            self.packer._ExtractEcVersion("geralt_v3.5.119161-ec:dc7985,os"),
            pack_firmware.FirmwareVersion(3, 5, 119161),
        )
        # New format.
        self.assertEqual(
            self.packer._ExtractEcVersion("reef-9264.0.0"),
            pack_firmware.FirmwareVersion(9264, 0, 0),
        )
        self.assertEqual(
            self.packer._ExtractEcVersion("rex-ish-ec-15709.72.0"),
            pack_firmware.FirmwareVersion(15709, 72, 0),
        )
        # Malformed FWID.
        with self.assertRaises(pack_firmware.PackError) as e:
            self.packer._ExtractEcVersion("a_bad_id")
        self.assertIn("Malformed EC firmware ID", str(e.exception))

    def testExtractMainVersion(self):
        """Check extracting version from firmware ID."""
        # Local build.
        self.assertEqual(
            self.packer._ExtractMainVersion(
                "Google_Reef.9264.0.0_d2017_02_09_1240"
            ),
            pack_firmware.FirmwareVersion(9264, 0, 0),
        )
        # Official build.
        self.assertEqual(
            self.packer._ExtractMainVersion("Google_Reef.9264.0.1"),
            pack_firmware.FirmwareVersion(9264, 0, 1),
        )
        # Official build for Volteer2.
        self.assertEqual(
            self.packer._ExtractMainVersion("Google_Volteer2.13672.328.0"),
            pack_firmware.FirmwareVersion(13672, 328, 0),
        )
        # Malformed FWID.
        with self.assertRaises(pack_firmware.PackError) as e:
            self.packer._ExtractMainVersion("a_bad_id")
        self.assertIn("Malformed BIOS firmware ID", str(e.exception))

    def testFirmwareImageOutput(self):
        """Check firmware image output name."""
        self.packer._args = self.packer.ParseArgs([])
        self.packer._tmpdir = self.packer._CreateTmpDir()

        tag = "ec"
        target = "reef"
        fw_ids = pack_firmware.FirmwareIds(EC_RO_FRID, EC_RW_FWID)
        bad_fw_ids = pack_firmware.FirmwareIds(EC_RO_FRID, None)
        version_fn = (
            lambda fwid: pack_firmware.FirmwareVersion(1, 1, 5857)
            if fwid == EC_RO_FRID
            else pack_firmware.FirmwareVersion(1, 1, 5858)
        )
        self.assertEqual(self.packer._FirmwareImageOutput(tag), "")
        self.assertEqual(
            self.packer._FirmwareImageOutput(
                tag, fw_ids=fw_ids, extract_version=version_fn
            ),
            "",
        )
        self.assertEqual(
            self.packer._FirmwareImageOutput(tag, target, fw_ids), ""
        )
        self.assertEqual(
            self.packer._FirmwareImageOutput(
                tag, target, extract_version=version_fn
            ),
            "",
        )
        self.assertEqual(
            self.packer._FirmwareImageOutput(
                tag, target, bad_fw_ids, version_fn
            ),
            "",
        )

        expected_fname = "ec-reef.ro-1-1-5857.rw-1-1-5858.bin"
        self.assertEqual(
            self.packer._FirmwareImageOutput(tag, target, fw_ids, version_fn),
            expected_fname,
        )
        self.assertEqual(
            self.packer._FirmwareImageOutput(
                tag,
                target,
                fw_ids,
                version_fn,
                None,
                target_dir=self.packer._tmpdir,
            ),
            os.path.join(self.packer._tmpdir, expected_fname),
        )

        self.assertEqual(
            self.packer._FirmwareImageOutput(
                "bios",
                "rex",
                pack_firmware.FirmwareIds(
                    "Google_Rex.15555.55.0", "Google_Rex.15555.56.0"
                ),
                self.packer._ExtractMainVersion,
                bios_ecrw_id="rex-16666.66.0",
            ),
            "bios-rex.ro-15555-55-0.rw-15555-56-0.ecrw-16666-66-0.bin",
        )

        self.packer._RemoveTmpdirs()

    def testFirmwareImageOutputLegacy(self):
        """Check firmware image output name when legacy is set."""
        self.packer._args = self.packer.ParseArgs(["-L"])
        self.packer._tmpdir = self.packer._CreateTmpDir()
        self.assertEqual(self.packer._FirmwareImageOutput("bios"), "bios.bin")
        self.assertEqual(
            self.packer._FirmwareImageOutput("bios", "reef"), "bios.bin"
        )
        self.assertEqual(
            self.packer._FirmwareImageOutput(
                tag="bios", target_dir=self.packer._tmpdir
            ),
            os.path.join(self.packer._tmpdir, "bios.bin"),
        )
        self.packer._RemoveTmpdirs()

    def testUntarFile(self):
        """Test operation of the tar file unpacker."""
        self.packer._tmpdir = self.packer._CreateTmpDir()
        dirname = self.packer._CreateTmpDir()
        fname = self.packer._UntarFile(
            f"{self.IMAGE_DIR}/Reef.9042.50.0.tbz2", dirname
        )
        self.assertEqual(os.path.basename(fname), "image.bin")

        # Unpack again with a different suffix. We should get a different
        # filename and the file contents should be different.
        fname2 = self.packer._UntarFile(
            f"{self.IMAGE_DIR}/Reef.9000.0.0.tbz2", dirname, "-rw"
        )
        self.assertEqual(os.path.basename(fname2), "image.bin-rw")
        self.assertNotEqual(fname, fname2)
        data = osutils.ReadFile(fname, mode="rb")
        data2 = osutils.ReadFile(fname2, mode="rb")
        self.assertNotEqual(data, data2)

        dirname = self.packer._CreateTmpDir()
        fname = self.packer._UntarFile(
            f"{self.IMAGE_DIR}/Reef.9042.50.0.tbz2", dirname
        )
        self.assertEqual(os.path.basename(fname), "image.bin")

        # This tar file has two files in it.
        # -rw-r----- sjg/eng          64 2017-03-03 16:12 RO_FRID
        # -rw-r----- sjg/eng          64 2017-03-15 13:38 RW_FRID
        with self.assertRaises(pack_firmware.PackError) as e:
            fname = self.packer._UntarFile("test/two_files.tbz2", dirname)
        self.assertIn("Expected 1 member", str(e.exception))

        # Extract one file from a tar file with two files.
        fname = self.packer._UntarFile(
            "test/two_files.tbz2", dirname, fname="RW_FRID"
        )
        self.assertEqual(os.path.basename(fname), "RW_FRID")

        # Extract a non-existing file.
        with self.assertRaises(pack_firmware.PackError) as e:
            self.packer._UntarFile(
                "test/two_files.tbz2", dirname, fname="NO_SUCH"
            )
        self.assertIn("No matched member", str(e.exception))

        # Extract a non-existing file, but with required=False.
        fname = self.packer._UntarFile(
            "test/two_files.tbz2", dirname, fname="NO_SUCH", required=False
        )
        self.assertIsNone(fname)

        # This tar file has as directory name in its member's filename.
        # -rw-r----- sjg/eng          64 2017-03-03 16:12 test/RO_FRID
        with self.assertRaises(pack_firmware.PackError) as e:
            fname = self.packer._UntarFile("test/path.tbz2", dirname)
        self.assertIn("should be a simple name", str(e.exception))

        self.packer._RemoveTmpdirs()

    @staticmethod
    def _FilesInDir(dirname):
        """Get a list of files in a directory.

        Args:
            dirname: Directory name to check.

        Returns:
            List of files in that directory (basename only). Any subdirectories
                are ignored.
        """
        return sorted(
            [
                os.path.basename(fname)
                for fname in glob.glob(os.path.join(dirname, "*"))
                if not os.path.isdir(fname)
            ]
        )

    def testBaseDirPath(self):
        """Check that _BaseDirPath() works as expected."""
        self.packer._basedir = "base"
        self.assertEqual("base/fred", self.packer._BaseDirPath("fred"))

    def testExtractFileBcs(self):
        """Test handling file extraction."""
        self.packer._tmpdir = self.packer._CreateTmpDir()
        self.packer._args = self.packer.ParseArgs(
            ["--imagedir", self.IMAGE_DIR]
        )
        dirname = self.packer._CreateTmpDir()

        # This should find the tar file in self.IMAGE_DIR, copy it to dirname
        # and return its filename.
        image = self.packer._ExtractFile(
            None, None, "bcs://Reef.9042.50.0.tbz2", dirname
        )
        self.assertEqual(image.file, os.path.join(dirname, "image.bin"))

        # Specify the file name to be extracted.
        image = self.packer._ExtractFile(
            None, None, "bcs://Reef_EC.9042.50.0.tbz2", dirname, fname="ec.bin"
        )
        self.assertEqual(image.file, os.path.join(dirname, "ec.bin"))

        # Try to extract a non-existing file.
        image = self.packer._ExtractFile(
            None,
            None,
            "bcs://Reef_EC.9042.50.0.tbz2",
            dirname,
            fname="NO_SUCH",
            required=False,
            extract_fwid=False,
        )
        self.assertIsNone(image)

        # Extract a file from a tar ball with two files.
        image = self.packer._ExtractFile(
            None,
            None,
            "bcs://Ciri_EC.15705.0.0.tbz2",
            dirname,
            fname="component_manifest.json",
            extract_fwid=False,
        )
        self.assertEqual(
            image.file, os.path.join(dirname, "component_manifest.json")
        )

        # Cleanup
        self.packer._RemoveTmpdirs()

    @staticmethod
    def _CopySections(_, **kwargs):
        destdir = kwargs["cwd"]
        for fname in ["RO_FRID", "RW_FWID"]:
            shutil.copy2(os.path.join("test", fname), destdir)

    @staticmethod
    def _CopyEcSections(_, **kwargs):
        destdir = kwargs["cwd"]
        for fname, section in [
            ("EC_RO_FRID", "RO_FRID"),
            ("EC_RW_FWID", "RW_FWID"),
        ]:
            shutil.copy2(
                os.path.join("test", fname), os.path.join(destdir, section)
            )

    @classmethod
    def _AddMocks(cls, rc):
        rc.AddCmdResult(
            partial_mock.ListRegex(r"(?:^|\s)file(?:$|\s)"),
            returncode=0,
            stdout="ELF 64-bit LSB executable, etc.\n",
        )
        fwid_pattern = "RO_FRID|RW_FWID|RW_FWID_A|RW_FWID_B"
        rc.AddCmdResult(
            partial_mock.ListRegex(
                rf"futility dump_fmap -x .*image\.bin (?:{fwid_pattern})$"
            ),
            side_effect=cls._CopySections,
            returncode=0,
        )
        rc.AddCmdResult(
            partial_mock.ListRegex("futility gbb"),
            returncode=0,
            stdout=" - exported root_key to file: rootkey.bin",
        )
        rc.AddCmdResult(partial_mock.ListRegex("--repack"), returncode=0)
        rc.AddCmdResult(
            partial_mock.ListRegex(r"futility dump_fmap -x .*ec\.bin"),
            side_effect=cls._CopyEcSections,
            returncode=0,
        )

    @staticmethod
    def _CreateCbfstoolFile(cmd, **_kwargs):
        """Called as a side effect to emulate the effect of cbfstool.

        This handles the 'cbfstool...extract' command which is supposed to
        extract a particular 'file' from inside the CBFS archive. We deal with
        this by creating a zero-filled file with the correct name and size.
        See _ExtractEcRwUsingCbfs() for where this command is generated.

        Args:
            cmd: Arguments, of the form:
                ['cbfstool.sh', ..., '-f', <filename>, ...]
                See _SetPreambleFlags() for where this is generated.
        """
        file_arg = cmd.index("-f")
        fname = cmd[file_arg + 1]
        with open(fname, "wb") as fd:
            fd.truncate(ECRW_SIZE)

    @staticmethod
    def _CreateDumpfmapFile(cmd, **_kwargs):
        """Called as a side effect to emulate the effect of dump_fmap.

        This handles the 'dump_fmap -x' command which is supposed to
        extract a particular region from a file with an FMAP descriptor.
        See _ExtractEcRwUsingFMAP() for where this command is generated.

        Args:
            cmd: Arguments, of the form:
                ['dump_fmap', '-x', <filename>, <region>]
                <region> specifies the region to extract from <filename>, which
                will be extracted to cwd in a file named <region>.
                See _ExtractEcRwUsingFMAP() for where this is generated.
        """
        fname = os.path.join(_kwargs["cwd"], cmd.pop())
        with open(fname, "wb") as fd:
            # Write a dummy header with image size = ECRW_SIZE, and payload of
            # zeros
            fd.write(struct.pack("<III", 1, 12, ECRW_SIZE))
            fd.truncate(ECRW_SIZE + 12)

    @classmethod
    def _AddMergeMocks(cls, rc, mocked_dump_fmap_output):
        fwid_pattern = "RO_FRID|RW_FWID|RW_FWID_A|RW_FWID_B"
        rc.AddCmdResult(
            partial_mock.ListRegex(
                rf"futility dump_fmap -x .*image_rw\.bin (?:{fwid_pattern})$"
            ),
            side_effect=cls._CopySections,
            returncode=0,
        )
        rc.AddCmdResult(
            partial_mock.ListRegex(
                r"futility dump_fmap -p .*/images-merged/.*image_rw\.bin$"
            ),
            returncode=0,
            stdout=mocked_dump_fmap_output,
        )
        rc.AddCmdResult(
            partial_mock.ListRegex(r"futility dump_fmap -p .*image\.binrw$"),
            returncode=0,
            stdout=mocked_dump_fmap_output,
        )
        rc.AddCmdResult(
            partial_mock.ListRegex(r"futility dump_fmap -p .*image\.bin$"),
            returncode=0,
            stdout=mocked_dump_fmap_output,
        )
        rc.AddCmdResult(partial_mock.Regex("extract_ecrw"), returncode=0)
        rc.AddCmdResult(
            partial_mock.ListRegex(r"futility dump_fmap -p .*ec\.bin$"),
            returncode=0,
            stdout=FMAP_OUTPUT_EC,
        )
        rc.AddCmdResult(
            partial_mock.ListRegex("cbfstool"),
            returncode=0,
            side_effect=cls._CreateCbfstoolFile,
        )
        rc.AddCmdResult(
            partial_mock.ListRegex(
                r"futility dump_fmap -x .*image(?:_rw)?\.bin \w+_MAIN_A$"
            ),
            returncode=0,
            side_effect=cls._CreateDumpfmapFile,
        )

    def testMockedRun(self):
        """Start up with a valid updater script and BIOS."""
        args = [".", "-L", "-e", "test/ec.bin"] + self.common_flags
        with cros_test_lib.RunCommandMock() as rc:
            self._AddMocks(rc)
            firmware_unittest.MockFirmwareSigner(rc)
            pack_firmware.main(args)
            pack_firmware.packer._versions.getvalue().splitlines()

    def _testMockedRunWithMerge(
        self, mocked_dump_fmap_output, expected_versions
    ):
        args = [
            "--bios_rw_image",
            "test/image_rw.bin",
            "-L",
            "-e",
            "test/ec.bin",
        ] + self.common_flags
        with cros_test_lib.RunCommandMock() as rc:
            self._AddMocks(rc)
            self._AddMergeMocks(rc, mocked_dump_fmap_output)
            self.packer.Start(args, remove_tmpdirs=False)
        self._AssertVersionsEqual(
            self.packer._versions.getvalue(), expected_versions
        )
        rw_fname = self.packer._BaseDirPath("ec.bin")
        self.assertAlmostEqual(
            os.stat(rw_fname).st_mtime, os.stat("test/image_rw.bin").st_mtime
        )
        ec_fname = self.packer._BaseDirPath("ec.bin")
        self.assertAlmostEqual(
            os.stat(ec_fname).st_mtime, os.stat("test/image_rw.bin").st_mtime
        )
        self.packer._RemoveTmpdirs()

    def testMockedRunWithMerge(self):
        """Start up with a valid updater script and merge the RW BIOS."""
        expected_versions = [
            r"BIOS image: +\w+ \*/images-merged/.*/image.bin",
            f"BIOS version: +{RO_FRID}",
            rf"BIOS \(RW\) version: +{RW_FWID}",
            r"EC image: +\w+ \*/images-merged/.*/ec.bin",
            f"EC version: +{EC_RO_FRID}",
            rf"EC \(RW\) version: +{EC_RW_FWID}",
        ]
        self._testMockedRunWithMerge(FMAP_OUTPUT, expected_versions)

    def testMockedRunWithMergeLegacyFmap(self):
        """Repeat merge test with alternate FMAP used on legacy devices."""
        expected_versions = [
            r"BIOS image: +\w+ \*/images-merged/.*/image.bin",
            f"BIOS version: +{RO_FRID}",
            rf"BIOS \(RW\) version: +{RW_FWID}",
            r"EC image: +\w+ \*/images-merged/.*/ec.bin",
            f"EC version: +{EC_RO_FRID}",
            rf"EC \(RW\) version: +{EC_RW_FWID}",
        ]
        self._testMockedRunWithMerge(FMAP_OUTPUT_LEGACY, expected_versions)

    def testMockedRunUnibuildBad(self):
        """Try unified build options with invalid arguments."""

        args = [".", "-m", "reef", "-o", "output"]
        with self.assertRaises(pack_firmware.PackError) as e:
            pack_firmware.main(args)
        self.assertIn("Missing model configuration file", str(e.exception))

    def SetupRunWithUnibuild(self, config_fname):
        """Set up to run with a valid updater script and BIOS.

        Args:
            config_fname: Model configuration file to use.

        Returns:
            List of arguments to pass to pack_firmware.main()
        """

        args = [
            ".",
            "-m",
            "reef",
            "-m",
            "pyro",
            "-q",
            "-o",
            self.output,
            "--image_output",
            self.image_output,
            "--ec_component_manifest_output",
            self.ec_comp_output,
            "-c",
            "test/%s" % config_fname,
            "-i",
            "functest",
            "-t",
        ]

        os.environ["SYSROOT"] = "test"
        os.environ["FILESDIR"] = "test"
        return args

    def testMockedRunWithUnibuild(self):
        """Start up with a valid updater script and BIOS."""
        args = self.SetupRunWithUnibuild("config.yaml")
        with cros_test_lib.RunCommandMock() as rc:
            self._AddMocks(rc)
            pack_firmware.main(args)
            pack_firmware.packer._versions.getvalue().splitlines()

    def testMockedRunWithUnibuildMixedUris(self):
        """Start up with config where a subset of devices have firmware uris."""
        args = self.SetupRunWithUnibuild("config_mixed_uri.yaml")
        with cros_test_lib.RunCommandMock() as rc:
            self._AddMocks(rc)
            pack_firmware.main(args)
            # Only reef is in the output, configs without firmware uris are
            # skipped.
            self._AssertVersionsEqual(
                pack_firmware.packer._versions.getvalue(),
                [
                    "Model: +reef",
                    "BIOS image: +99a6fc64e45596aa2c1a9911cddce952"
                    r" \*.*/reef/image.bin",
                    f"BIOS version: +{RO_FRID}",
                    rf"BIOS \(RW\) version: +{RW_FWID}",
                    "EC image: +60c08e5aefa3a660687c7027d1358df0"
                    r" \*.*/reef/ec.bin",
                    f"EC version: +{EC_RO_FRID}",
                    rf"EC \(RW\) version: +{EC_RW_FWID}",
                ],
            )

    def testMockedRunWithUnibuildMerge(self):
        """Start up with a valid updater script and both RO and RW BIOS."""

        # Due to a bug in the DT impl, the RW firmware image was never actually
        # present.
        # TODO(sjg): Change this to config_rw.json once the RW merge case if
        # correctly supported.
        args = self.SetupRunWithUnibuild("config.yaml")
        with cros_test_lib.RunCommandMock() as rc:
            self._AddMocks(rc)
            self._AddMergeMocks(rc, FMAP_OUTPUT)
            pack_firmware.main(args)
            pack_firmware.packer._versions.getvalue().splitlines()

    def _FindLineInList(self, lines, start_text):
        """Find a single line starting with the given text and return it.

        Args:
            lines: List of lines to check.
            start_text: Text to find.

        Returns:
            Line found, as a string (or assertion failure if exactly one
                matching line was not found).
        """
        found = [line for line in lines if line.strip().startswith(start_text)]
        self.assertEqual(len(found), 1)
        return found[0]

    def testNoECFirmware(self):
        """Simple test of creating firmware without an EC image."""
        args = self.common_flags + ["-L"]
        with cros_test_lib.RunCommandMock() as rc:
            self._AddMocks(rc)
            self.packer.Start(args)

        # There should be only BIOS version in the VERSION file.
        expected_versions = [
            r"BIOS image: +\w+ \*/.*/image.bin",
            f"BIOS version: +{RO_FRID}",
            rf"BIOS \(RW\) version: +{RW_FWID}",
        ]
        self._AssertVersionsEqual(
            self.packer._versions.getvalue(), expected_versions
        )


if __name__ == "__main__":
    cros_test_lib.main(module=__name__)
