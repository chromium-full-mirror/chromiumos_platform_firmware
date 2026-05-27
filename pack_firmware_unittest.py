#!/usr/bin/env python3
# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Unit tests for pack_firmware.py.

This mocks out all tools so it can run fairly quickly.
"""

import collections
import contextlib
import glob
import io
import os
import pprint
import shutil
import struct
import sys
from typing import Iterable, List
import unittest
from unittest import mock

import config_parser
import pack_firmware
import pack_firmware_utils

from chromite.lib import cros_test_lib
from chromite.lib import osutils
from chromite.lib import partial_mock


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


# Expected output from 'futility dump_fmap -p' for AP image.
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

# Size of placeholder 'ecrw' file.
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


class ConfigParserTestUnit(cros_test_lib.TestCase):
    """Test cases for config_parser."""

    def test_parse_textproto_config(self):
        """Test parse_textproto_config."""
        firmware_info = collections.OrderedDict()
        firmware_configs_by_device = collections.OrderedDict()

        firmware_info["first"] = config_parser.FirmwareUpdateInfo(
            device="first",
            base_device="first",
            key_id="DEFAULT",
            have_image=True,
            ap_build_target="first",
            ec_build_target="first_ec",
            ap_image_source=config_parser.ImageUriSource(
                "gs://path/First.12345.123.0.tbz2", "1" * 64
            ),
            ap_rw_image_source=config_parser.ImageUriSource(
                "gs://path/First.12345.123.0.tbz2", "1" * 64
            ),
            ec_image_source=config_parser.ImageUriSource(
                "gs://path/First_EC.12345.123.0.tbz2", "2" * 64
            ),
            ec_rw_image_source=config_parser.ImageUriSource("", ""),
            ap_image_for_ec_rw_source=config_parser.ImageUriSource("", ""),
            brand_code="ABCD",
        )
        firmware_configs_by_device["first"] = "first"

        firmware_info["first-variant"] = config_parser.FirmwareUpdateInfo(
            device="first-variant",
            base_device="first",
            key_id="DEFAULT",
            have_image=False,
            ap_build_target="first",
            ec_build_target="first_ec",
            ap_image_source=config_parser.ImageUriSource(
                "gs://path/First.12345.123.0.tbz2", "1" * 64
            ),
            ap_rw_image_source=config_parser.ImageUriSource(
                "gs://path/First.12345.123.0.tbz2", "1" * 64
            ),
            ec_image_source=config_parser.ImageUriSource(
                "gs://path/First_EC.12345.123.0.tbz2", "2" * 64
            ),
            ec_rw_image_source=config_parser.ImageUriSource("", ""),
            ap_image_for_ec_rw_source=config_parser.ImageUriSource("", ""),
            brand_code="ABCD",
        )
        firmware_configs_by_device["first-variant"] = "first"

        conf = config_parser.parse_textproto_config("test/config_textproto/")
        self.assertEqual(conf.get_firmware_info(), firmware_info)
        self.assertEqual(
            conf.get_firmware_configs_by_device(), firmware_configs_by_device
        )


class ModuleTestUnit(cros_test_lib.TestCase):
    """Test cases for module functions."""

    def test_uri_filename(self):
        """Test uri_filename."""
        bcs_scheme = "bcs://"
        gs_scheme = "gs://dir1/dir2/"
        filename = "brya.12345.123.0.tar.bz2"
        self.assertEqual(
            filename, pack_firmware.uri_filename(bcs_scheme + filename)
        )
        self.assertEqual(
            filename, pack_firmware.uri_filename(gs_scheme + filename)
        )

    def test_extract_uri_version(self):
        """Test extract_uri_version."""
        legacy_bcs_uri = "bcs://Brya.12345.67.tbz2"
        legacy_version = pack_firmware.FirmwareVersion(12345, 67, 0)

        bcs_uri = "bcs://Brya.12345.67.89.tbz2"
        gs_uri = "gs://path/Brya.12345.67.89.tar.bz2"
        gs_ec_uri = "gs://path/Brya.EC.12345.67.89.tar.bz2"
        version = pack_firmware.FirmwareVersion(12345, 67, 89)
        no_ver_in_filename = (
            "gs://firmware-image-archive/"
            "firmware-ec-R132-12345.67.B/12345.67.89/brox.EC.tar.bz2"
        )

        self.assertEqual(
            legacy_version, pack_firmware.extract_uri_version(legacy_bcs_uri)
        )
        self.assertEqual(version, pack_firmware.extract_uri_version(bcs_uri))
        self.assertEqual(version, pack_firmware.extract_uri_version(gs_uri))
        self.assertEqual(version, pack_firmware.extract_uri_version(gs_ec_uri))
        self.assertEqual(
            version, pack_firmware.extract_uri_version(no_ver_in_filename)
        )


class TestUnit(cros_test_lib.MockTempDirTestCase):
    """Test cases for common program flows."""

    IMAGE_DIR = "functest/images"

    @classmethod
    def setUpClass(cls):
        pack_firmware_utils.make_test_files()

    def setUp(self):
        self.output = os.path.join(self.tempdir, "out")
        self.image_output = os.path.join(self.tempdir, "images")
        self.ec_comp_output = os.path.join(self.tempdir, "ec_comp_out")
        self.packer = pack_firmware.FirmwarePacker(".")
        self._extract_frid = pack_firmware.FirmwarePacker._extract_frid

    def tearDown(self):
        pack_firmware.FirmwarePacker._extract_frid = self._extract_frid

    def _assert_versions_equal(self, versions, expected_versions):
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

    def test_bad_startup(self):
        """Test various bad start-up conditions."""
        # starting up in another directory (without required files) should fail.
        with self.assertRaises(pack_firmware.PackError) as e:
            pack_firmware.main(["/"])
        self.assertIn("'/pack/sfx2.sh'", str(e.exception))

        # Should check for 'zip' tool.
        with mock.patch.object(osutils, "Which", return_value=None):
            with self.assertRaises(pack_firmware.PackError) as e:
                pack_firmware.main(["."])
            self.assertIn("'zip'", str(e.exception))

    def test_arg_parse(self):
        """Test some basic argument parsing as a validity check."""
        with self.assertRaises(SystemExit):
            with capture_sys_output():
                self.assertIsNone(self.packer.parse_args(["--invalid"]))

        self.assertIsNone(self.packer.parse_args([]).ap_image)
        self.assertEqual(
            "/image.bin", self.packer.parse_args(["-b", "/image.bin"]).ap_image
        )

    def test_ensure_command(self):
        """Check that we detect a missing command."""
        self.packer._ensure_command("ls", "sample-package")
        with self.assertRaises(pack_firmware.PackError) as e:
            self.packer._ensure_command("does-not-exist", "sample-package")
        self.assertIn("You need 'does-not-exist'", str(e.exception))

    def test_tmpdirs(self):
        """Check creation and removal of temporary directories."""
        dir1 = self.packer._create_tmp_dir()
        dir2 = self.packer._create_tmp_dir()
        self.assertExists(dir1)
        self.assertExists(dir2)
        self.packer._remove_tmpdirs()
        self.assertNotExists(dir1)
        self.assertNotExists(dir2)

    def test_add_version_info_missing_file(self):
        """Trying to add version info for a missing file should be detected."""
        with self.assertRaises(IOError) as e:
            self.packer._add_version_info("AP", "missing-file", "v123")
        self.assertIn("'missing-file'", str(e.exception))

    def test_add_version_info_no_file(self):
        """Check adding version info with no filename."""
        self.packer._add_version_info("AP", "", "v123")
        self._assert_versions_equal(
            self.packer._versions.getvalue(),
            [r"AP version: +v123"],
        )

    def test_add_version_no_version(self):
        """Check adding version info with no version."""
        self.packer._add_version_info("AP", "test/image.bin", "")
        self._assert_versions_equal(
            self.packer._versions.getvalue(),
            [
                "AP image:     8ce05b02847603aef6cfa01f1bab73d0 "
                r"\*test/image.bin"
            ],
        )

    def test_add_version_info(self):
        """Check adding version info with both a filename and version."""
        self.packer._add_version_info("AP", "test/image.bin", "v123")
        self._assert_versions_equal(
            self.packer._versions.getvalue(),
            [
                "AP image:     8ce05b02847603aef6cfa01f1bab73d0 "
                r"\*test/image.bin",
                r"AP version: +v123",
            ],
        )

    def test_extract_frid(self):
        """Check extracting the firmware ID from an AP image."""
        self.packer._tmpdir = "test"
        self.packer._args = self.packer.parse_args(
            ["--bios_image", "image.bin", "-t"]
        )
        with cros_test_lib.RunCommandMock() as rc:
            rc.AddCmdResult(
                partial_mock.ListRegex("futility dump_fmap"), returncode=0
            )
            self.assertEqual(RO_FRID, self.packer._extract_frid("image.bin"))

    def test_extract_frid_trailing_space(self):
        """Check extracting a firmware ID with a trailing space."""

        def _setup_image(_, **kwargs):
            destdir = kwargs["cwd"]
            osutils.WriteFile(
                os.path.join(destdir, "RO_FRID"), b"TESTING \0\0\0", mode="wb"
            )

        self.packer._tmpdir = self.packer._create_tmp_dir()
        self.packer._args = self.packer.parse_args(
            ["--bios_image", "image.bin", "-t"]
        )
        with cros_test_lib.RunCommandMock() as rc:
            rc.AddCmdResult(
                partial_mock.ListRegex("futility dump_fmap"),
                returncode=0,
                side_effect=_setup_image,
            )
            self.assertEqual("TESTING ", self.packer._extract_frid("image.bin"))
        self.packer._remove_tmpdirs()

    def test_extract_ec_version(self):
        """Check extracting version from firmware ID."""
        # Old format with single hash.
        self.assertIsNone(
            self.packer._extract_ec_version("reef_v1.1.5857-77f6ed7"),
        )
        # Old format with multiple hashes.
        self.assertIsNone(
            self.packer._extract_ec_version("geralt_v3.5.119161-ec:dc7985,os"),
        )
        # New format.
        self.assertEqual(
            self.packer._extract_ec_version("reef-9264.0.0"),
            pack_firmware.FirmwareVersion(9264, 0, 0),
        )
        self.assertEqual(
            self.packer._extract_ec_version("rex-ish-ec-15709.72.0"),
            pack_firmware.FirmwareVersion(15709, 72, 0),
        )
        # Malformed FWID.
        with self.assertRaises(pack_firmware.PackError) as e:
            self.packer._extract_ec_version("a_bad_id")
        self.assertIn("Malformed EC firmware ID", str(e.exception))

    def test_extract_ap_version(self):
        """Check extracting version from firmware ID."""
        # Local build.
        self.assertEqual(
            self.packer._extract_ap_version(
                "Google_Reef.9264.0.0_d2017_02_09_1240"
            ),
            pack_firmware.FirmwareVersion(9264, 0, 0),
        )
        # Official build.
        self.assertEqual(
            self.packer._extract_ap_version("Google_Reef.9264.0.1"),
            pack_firmware.FirmwareVersion(9264, 0, 1),
        )
        # Official build for Volteer2.
        self.assertEqual(
            self.packer._extract_ap_version("Google_Volteer2.13672.328.0"),
            pack_firmware.FirmwareVersion(13672, 328, 0),
        )
        # Official build for Alder Lake Client.
        self.assertEqual(
            self.packer._extract_ap_version(
                "Google_Alder Lake Client.15920.0.0"
            ),
            pack_firmware.FirmwareVersion(15920, 0, 0),
        )
        # Malformed FWID.
        with self.assertRaises(pack_firmware.PackError) as e:
            self.packer._extract_ap_version("a_bad_id")
        self.assertIn("Malformed AP firmware ID", str(e.exception))

    def test_firmware_image_output(self):
        """Check firmware image output name."""
        self.packer._args = self.packer.parse_args([])
        self.packer._tmpdir = self.packer._create_tmp_dir()

        tag = "ec"
        target = "reef"
        fw_ids = pack_firmware.FirmwareIds(EC_RO_FRID, EC_RW_FWID)
        bad_fw_ids = pack_firmware.FirmwareIds(EC_RO_FRID, None)
        ro_uri_version = pack_firmware.FirmwareVersion(15555, 55, 0)
        rw_uri_version = pack_firmware.FirmwareVersion(15555, 56, 0)
        uri_versions = pack_firmware.FirmwareVersions(
            ro_uri_version, rw_uri_version
        )
        version_fn = lambda fwid: (
            pack_firmware.FirmwareVersion(1, 1, 5857)
            if fwid == EC_RO_FRID
            else pack_firmware.FirmwareVersion(1, 1, 5858)
        )

        # Missing required fields..
        self.assertEqual(self.packer._firmware_image_output(tag), "")
        self.assertEqual(
            self.packer._firmware_image_output(
                tag, fw_ids=fw_ids, extract_cros_version=version_fn
            ),
            "",
        )
        self.assertEqual(
            self.packer._firmware_image_output(tag, target, fw_ids), ""
        )
        self.assertEqual(
            self.packer._firmware_image_output(
                tag, target, extract_cros_version=version_fn
            ),
            "",
        )

        # Invalid FWIDs.
        self.assertEqual(
            self.packer._firmware_image_output(
                tag, target, fw_ids=bad_fw_ids, extract_cros_version=version_fn
            ),
            "",
        )

        # CrOS versions extracted by version_fn (uri_versions ignored).
        expected_fname = "ec-reef.ro-1-1-5857.rw-1-1-5858.bin"
        self.assertEqual(
            self.packer._firmware_image_output(
                "ec", "reef", fw_ids=fw_ids, extract_cros_version=version_fn
            ),
            expected_fname,
        )
        self.assertEqual(
            self.packer._firmware_image_output(
                "ec",
                "reef",
                fw_ids=fw_ids,
                uri_versions=uri_versions,
                extract_cros_version=version_fn,
                ap_ecrw_version=None,
                target_dir=self.packer._tmpdir,
            ),
            os.path.join(self.packer._tmpdir, expected_fname),
        )

        # CrOS versions extracted from uri_versions.
        self.assertEqual(
            self.packer._firmware_image_output(
                "ap",
                "rex",
                fw_ids=fw_ids,
                uri_versions=uri_versions,
                extract_cros_version=lambda fwid: None,
            ),
            "ap-rex.ro-15555-55-0.rw-15555-56-0.bin",
        )

        # CrOS versions extracted from FWIDs, with ap_ecrw_version specified.
        self.assertEqual(
            self.packer._firmware_image_output(
                "ap",
                "rex",
                fw_ids=pack_firmware.FirmwareIds(
                    "Google_Rex.15555.55.0", "Google_Rex.15555.56.0"
                ),
                extract_cros_version=self.packer._extract_ap_version,
                ap_ecrw_version=pack_firmware.FirmwareVersion(16666, 66, 0),
            ),
            "ap-rex.ro-15555-55-0.rw-15555-56-0.ecrw-16666-66-0.bin",
        )

        # Legacy format of ec version extracted from FWIDs, with
        # ap_ecrw_version specified.
        self.assertEqual(
            self.packer._firmware_image_output(
                "ap",
                "reef",
                fw_ids=pack_firmware.FirmwareIds(
                    "Google_Reef.15555.55.0", "Google_Reef.15555.56.0"
                ),
                extract_cros_version=self.packer._extract_ap_version,
                ap_ecrw_version="reef_v1.1.5857-77f",
            ),
            "ap-reef.ro-15555-55-0.rw-15555-56-0.ecrw-reef_v1.1.5857-77f.bin",
        )

        self.packer._remove_tmpdirs()

    def test_untar_file(self):
        """Test operation of the tar file unpacker."""
        self.packer._tmpdir = self.packer._create_tmp_dir()
        dirname = self.packer._create_tmp_dir()
        fname = self.packer._untar_file(
            f"{self.IMAGE_DIR}/Reef.9042.50.0.tbz2", dirname
        )
        self.assertEqual(os.path.basename(fname), "image.bin")

        # Unpack again with a different suffix. We should get a different
        # filename and the file contents should be different.
        fname2 = self.packer._untar_file(
            f"{self.IMAGE_DIR}/Reef.9000.0.0.tbz2", dirname, "-rw"
        )
        self.assertEqual(os.path.basename(fname2), "image.bin-rw")
        self.assertNotEqual(fname, fname2)
        data = osutils.ReadFile(fname, mode="rb")
        data2 = osutils.ReadFile(fname2, mode="rb")
        self.assertNotEqual(data, data2)

        dirname = self.packer._create_tmp_dir()
        fname = self.packer._untar_file(
            f"{self.IMAGE_DIR}/Reef.9042.50.0.tbz2", dirname
        )
        self.assertEqual(os.path.basename(fname), "image.bin")

        # This tar file has two files in it.
        # -rw-r----- sjg/eng          64 2017-03-03 16:12 RO_FRID
        # -rw-r----- sjg/eng          64 2017-03-15 13:38 RW_FRID
        with self.assertRaises(pack_firmware.PackError) as e:
            fname = self.packer._untar_file("test/two_files.tbz2", dirname)
        self.assertIn("Expected 1 member", str(e.exception))

        # Extract one file from a tar file with two files.
        fname = self.packer._untar_file(
            "test/two_files.tbz2", dirname, fname="RW_FRID"
        )
        self.assertEqual(os.path.basename(fname), "RW_FRID")

        # Extract a non-existing file.
        with self.assertRaises(pack_firmware.PackError) as e:
            self.packer._untar_file(
                "test/two_files.tbz2", dirname, fname="NO_SUCH"
            )
        self.assertIn("No matched member", str(e.exception))

        # Extract a non-existing file, but with required=False.
        fname = self.packer._untar_file(
            "test/two_files.tbz2", dirname, fname="NO_SUCH", required=False
        )
        self.assertIsNone(fname)

        # This tar file has as directory name in its member's filename.
        # -rw-r----- sjg/eng          64 2017-03-03 16:12 test/RO_FRID
        with self.assertRaises(pack_firmware.PackError) as e:
            fname = self.packer._untar_file("test/path.tbz2", dirname)
        self.assertIn("should be a simple name", str(e.exception))

        self.packer._remove_tmpdirs()

    @staticmethod
    def _files_in_dir(dirname):
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

    def test_base_dir_path(self):
        """Check that _base_dir_path() works as expected."""
        self.packer._basedir = "base"
        self.assertEqual("base/fred", self.packer._base_dir_path("fred"))

    def test_extract_file_bcs(self):
        """Test handling file extraction."""
        self.packer._tmpdir = self.packer._create_tmp_dir()
        self.packer._args = self.packer.parse_args(
            ["--imagedir", self.IMAGE_DIR]
        )
        dirname = self.packer._create_tmp_dir()

        # This should find the tar file in self.IMAGE_DIR, copy it to dirname
        # and return its filename.
        image = self.packer._extract_file(
            None,
            None,
            config_parser.ImageUriSource("bcs://Reef.9042.50.0.tbz2"),
            dirname,
        )
        self.assertEqual(image.file, os.path.join(dirname, "image.bin"))

        # Specify the file name to be extracted.
        image = self.packer._extract_file(
            None,
            None,
            config_parser.ImageUriSource("bcs://Reef_EC.9042.50.0.tbz2"),
            dirname,
            fname="ec.bin",
        )
        self.assertEqual(image.file, os.path.join(dirname, "ec.bin"))

        # Try to extract a non-existing file.
        image = self.packer._extract_file(
            None,
            None,
            config_parser.ImageUriSource("bcs://Reef_EC.9042.50.0.tbz2"),
            dirname,
            fname="NO_SUCH",
            required=False,
            extract_fwid=False,
        )
        self.assertIsNone(image)

        # Extract a file from a tar ball with two files.
        image = self.packer._extract_file(
            None,
            None,
            config_parser.ImageUriSource("bcs://Ciri_EC.15705.0.0.tbz2"),
            dirname,
            fname="component_manifest.json",
            extract_fwid=False,
        )
        self.assertEqual(
            image.file, os.path.join(dirname, "component_manifest.json")
        )

        # Cleanup
        self.packer._remove_tmpdirs()

    @staticmethod
    def _copy_sections(_, **kwargs):
        destdir = kwargs["cwd"]
        for fname in ["RO_FRID", "RW_FWID"]:
            shutil.copy2(os.path.join("test", fname), destdir)

    @staticmethod
    def _copy_ec_sections(_, **kwargs):
        destdir = kwargs["cwd"]
        for fname, section in [
            ("EC_RO_FRID", "RO_FRID"),
            ("EC_RW_FWID", "RW_FWID"),
        ]:
            shutil.copy2(
                os.path.join("test", fname), os.path.join(destdir, section)
            )

    @classmethod
    def _add_mocks(cls, rc):
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
            side_effect=cls._copy_sections,
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
            side_effect=cls._copy_ec_sections,
            returncode=0,
        )

    @staticmethod
    def _create_cbfstool_file(cmd, **_kwargs):
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
    def _create_dumpfmap_file(cmd, **_kwargs):
        """Called as a side effect to emulate the effect of dump_fmap.

        This handles the 'dump_fmap -x' command which is supposed to
        extract a particular region from a file with an FMAP descriptor.
        See _extract_ec_rw_using_fmap() for where this command is generated.

        Args:
            cmd: Arguments, of the form:
                ['dump_fmap', '-x', <filename>, <region>]
                <region> specifies the region to extract from <filename>, which
                will be extracted to cwd in a file named <region>.
                See _extract_ec_rw_using_fmap() for where this is generated.
        """
        fname = os.path.join(_kwargs["cwd"], cmd.pop())
        with open(fname, "wb") as fd:
            # Write a placeholder header with image size = ECRW_SIZE,
            # and payload of zeros.
            fd.write(struct.pack("<III", 1, 12, ECRW_SIZE))
            fd.truncate(ECRW_SIZE + 12)

    @classmethod
    def _add_merge_mocks(cls, rc, mocked_dump_fmap_output):
        fwid_pattern = "RO_FRID|RW_FWID|RW_FWID_A|RW_FWID_B"
        rc.AddCmdResult(
            partial_mock.ListRegex(
                rf"futility dump_fmap -x .*image_rw\.bin (?:{fwid_pattern})$"
            ),
            side_effect=cls._copy_sections,
            returncode=0,
        )
        rc.AddCmdResult(
            partial_mock.ListRegex(
                r"futility dump_fmap -p .*/.*image_rw\.bin$"
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
            side_effect=cls._create_cbfstool_file,
        )
        rc.AddCmdResult(
            partial_mock.ListRegex(
                r"futility dump_fmap -x .*image(?:_rw)?\.bin \w+_MAIN_A$"
            ),
            returncode=0,
            side_effect=cls._create_dumpfmap_file,
        )

    def test_mocked_run_bad(self):
        """Try invalid arguments."""
        args = [".", "-m", "reef", "-o", "output"]
        with self.assertRaises(pack_firmware.PackError) as e:
            pack_firmware.main(args)
        self.assertIn("Missing model configuration file", str(e.exception))

    def setup_run(
        self,
        config_fname: str,
        models: Iterable[str] = ("reef", "pyro"),
    ) -> List[str]:
        """Set up to run with a valid updater script and AP.

        Args:
            config_fname: Model configuration file to use.
            models: List of models to filter to.

        Returns:
            List of arguments to pass to pack_firmware.main()
        """

        args = [
            ".",
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

        for model in models:
            args.extend(["--model", model])

        os.environ["SYSROOT"] = "test"
        os.environ["FILESDIR"] = "test"
        return args

    def test_mocked_run(self):
        """start up with a valid updater script and AP."""
        args = self.setup_run("config.yaml")
        with cros_test_lib.RunCommandMock() as rc:
            self._add_mocks(rc)
            pack_firmware.main(args)
            pack_firmware.packer._versions.getvalue().splitlines()

    def test_mocked_run_with_mixed_uris(self):
        """start up with config where a subset of devices have firmware uris."""
        args = self.setup_run("config_mixed_uri.yaml")
        with cros_test_lib.RunCommandMock() as rc:
            self._add_mocks(rc)
            pack_firmware.main(args)
            # Only reef is in the output, configs without firmware uris are
            # skipped.
            self._assert_versions_equal(
                pack_firmware.packer._versions.getvalue(),
                [
                    "Model: +reef",
                    "AP image: +99a6fc64e45596aa2c1a9911cddce952"
                    r" \*.*/reef/image.bin",
                    f"AP version: +{RO_FRID}",
                    rf"AP \(RW\) version: +{RW_FWID}",
                    "EC image: +60c08e5aefa3a660687c7027d1358df0"
                    r" \*.*/reef/ec.bin",
                    f"EC version: +{EC_RO_FRID}",
                    rf"EC \(RW\) version: +{EC_RW_FWID}",
                ],
            )

    def test_mocked_run_with_merge(self):
        """start up with a valid updater script and both RO and RW AP."""

        # Due to a bug in the DT impl, the RW firmware image was never actually
        # present.
        # TODO(sjg): Change this to config_rw.json once the RW merge case if
        # correctly supported.
        args = self.setup_run("config.yaml")
        with cros_test_lib.RunCommandMock() as rc:
            self._add_mocks(rc)
            self._add_merge_mocks(rc, FMAP_OUTPUT)
            pack_firmware.main(args)
            pack_firmware.packer._versions.getvalue().splitlines()

    def test_no_ec_firmware(self):
        """Simple test of creating firmware without an EC image."""
        args = self.setup_run("config_no_ec.yaml", models=[])

        with cros_test_lib.RunCommandMock() as rc:
            self._add_mocks(rc)
            pack_firmware.main(args)

        # There should be only AP version in the VERSION file.
        expected_versions = [
            "Model: +fake",
            r"AP image: +\w+ \*/.*/fake/image.bin",
            f"AP version: +{RO_FRID}",
            rf"AP \(RW\) version: +{RW_FWID}",
        ]
        self._assert_versions_equal(
            pack_firmware.packer._versions.getvalue(), expected_versions
        )


if __name__ == "__main__":
    unittest.main(module=__name__)
