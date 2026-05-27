#!/usr/bin/env python3
# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Functional test for pack_firmware.py.

This runs a basic scenario and checks the output by running the update script
with a few fake tools.
"""

import collections
import glob
import gzip
import json
import os
import pprint
import re
import shutil
import sys
import tarfile
import typing
import unittest

import pack_firmware
import pack_firmware_utils


# Find chromite!  Assume this code only runs inside the SDK.
sys.path.insert(0, "/mnt/host/source")


# pylint: disable=wrong-import-position
from chromite.lib import cros_build_lib
from chromite.lib import cros_test_lib
from chromite.lib import osutils
from chromite.lib import path_util


# We need to poke around in internal members of PackFirmware.
# pylint: disable=W0212

REEF_HWID = "Reef A12-B3C-D5E-F6G-H7I"
REEF_MODEL = "reef"
REEF_RO_AP_VERSION = "Google_Reef.9042.43.0"

AP_IMAGES = {
    "reef": "ap-reef.ro-9042-50-0.rw-9042-50-0.bin",
    "pyro": "ap-pyro.ro-9042-41-0.rw-9042-41-0.bin",
    "sand": "ap-reef.ro-9042-50-0.rw-9042-50-0.bin",
}
AP_IMAGES["electro"] = AP_IMAGES["reef"]
AP_IMAGES["basking"] = AP_IMAGES["sand"]
AP_IMAGES["clref"] = AP_IMAGES["reef"]
EC_IMAGES = {
    "reef": "ec-reef.ro-9042-50-0.rw-9042-50-0.bin",
    "pyro": "ec-pyro.ro-9042-41-0.rw-9042-41-0.bin",
}
EC_IMAGES["sand"] = EC_IMAGES["reef"]
EC_IMAGES["electro"] = EC_IMAGES["reef"]
EC_IMAGES["basking"] = EC_IMAGES["reef"]
IMG_DIR = "images"

# Firmware update runs on the device using the dash shell. Try to use this if
# available.
HAVE_DASH = os.path.exists("/bin/dash")
SHELL = "/bin/dash" if HAVE_DASH else "/bin/sh"


# Result of firmware packer.
# outfile: Output file of firmware updater.
# files: List of files in the updater.
# image_output: Output directory of image files.
# ec_comp_files: List of EC component manifest files.
# versions: Dict of versions from the updater manifest.
PackerResult = collections.namedtuple(
    "PackerResult",
    ["outfile", "files", "image_output", "ec_comp_files", "versions"],
)


class TestFunctional(cros_test_lib.TempDirTestCase):
    """Functional test for firmware packer script.

    If a test needs additional writable paths, they can use self.tempdir.  Just
    make sure to not pick a path already used by one of the other members (see
    the code below for actual paths).

    Attributes:
        indir: Directory which contains the input firmware files (e.g.
            image.bin).
        basedir: Directory containing this script.
        outdir: Directory to place output shellball.
        tempdir: Temporary directory to use as our base for other temp dirs.
        unpackdir: Directory used to unpack shellball into.
    """

    IMAGE_DIR = "functest/images"

    @classmethod
    def setUpClass(cls):
        # This will generate files in cls.IMAGE_DIR.
        pack_firmware_utils.make_test_files()

    def setUp(self):
        # These are the named paths we use under self.tempdir.
        self.indir = os.path.join(self.tempdir, "indir")
        self.outdir = os.path.join(self.tempdir, "outdir")
        self.unpackdir = os.path.join(self.tempdir, "unpackdir")

        osutils.SafeMakedirs(self.indir)
        osutils.SafeMakedirs(self.outdir)
        osutils.SafeMakedirs(self.unpackdir)

        self.packer = pack_firmware.FirmwarePacker("test")
        with tarfile.open(f"{self.IMAGE_DIR}/Reef.9042.50.0.tbz2") as tar:
            tar.extractall(self.indir)
        with tarfile.open(f"{self.IMAGE_DIR}/Reef_EC.9042.50.0.tbz2") as tar:
            tar.extractall(self.indir)
        self.basedir = os.path.realpath(os.path.dirname(__file__))
        self.chroot = path_util.ToChrootPath("/")
        self.packer._force_dash = HAVE_DASH

    @staticmethod
    def _get_files(dir_path):
        """Get the relative path of all files in `dir_path`."""
        files = []
        for root, _, fnames in os.walk(dir_path):
            for fname in fnames:
                file_path = os.path.join(root, fname)
                files.append(os.path.relpath(file_path, dir_path))
        return sorted(files)

    @staticmethod
    def _expected_files(extra_files, models=()):
        """Get a sorted list of files that we expect to see in the shellball.

        Args:
            extra_files: A list of extra files to include.
            models: A list of models whose files need to be included.

        Returns:
            A sorted list of files to expect.
        """
        expected_files = set(["manifest.json", "VERSION"] + extra_files)
        for model in models:
            expected_files.add(os.path.join(IMG_DIR, AP_IMAGES[model]))
            expected_files.add(os.path.join(IMG_DIR, EC_IMAGES[model]))
        return sorted(expected_files)

    @staticmethod
    def _get_full_model(model, customlabel_tag):
        """Get the model descriptor combining the customlabel_tag.

        Args:
            model: A string of the model name.
            customlabel_tag: A string as the tag for the custom label model.

        Returns:
            A string representing the custom label model name.
        """
        if customlabel_tag:
            return f"{model}-{customlabel_tag}"
        return model

    def _run_script(self, outfile, model, mode="output", customlabel_tag=""):
        """Run an autoupdate with the shellball and check that it works.

        This relies on fake tools, principally crossystem which is controlled by
        environment variables set here.

        Args:
            outfile: Shellball output file to test.
            model: Model name to provide when the script asks for it.
            mode: Execution mode (can be 'autoupdate' or 'output')
            customlabel_tag: Value to return for from the fake vpd

        Returns:
            List of lines output from the script
        """
        # These are used by our fake vpd/mosys programs (see functest/
        # directory).
        os.environ["FAKE_CUSTOMLABEL_TAG"] = customlabel_tag
        os.environ["FAKE_MODEL"] = model
        input_path = os.path.join(self.indir, "image.bin")
        new_path = ":".join(
            (os.path.join(self.basedir, "functest/bin"), os.environ.get("PATH"))
        )

        cmd = [SHELL, outfile]
        cmd += ["--mode", mode]
        if mode == "output":
            cmd += [
                "--model",
                self._get_full_model(model, customlabel_tag),
                "--output_dir",
                self.outdir,
            ]
        cmd += ["--emulate", input_path]
        cmd += ["--verbose", "--debug"]
        result = cros_build_lib.run(
            cmd,
            capture_output=True,
            extra_env={"PATH": new_path},
            print_cmd=False,
            encoding="utf-8",
        )

        # The stderr may contain debug messages, info, and status (>>).
        # Anything else (for instance, 'ERROR:') should be error.
        errors = [
            line
            for line in result.stderr.splitlines()
            if line.split()
            and line.split()[0] not in ["(DEBUG)", "DEBUG:", ">>", "INFO:"]
        ]
        self.assertEqual(errors, [])
        return result.stdout.splitlines()

    def _run_pack_firmware(self, extra_args):
        """Run the FirmwarePacker process and read the resulting shellball.

        Args:
            extra_args: Extra arguments to pass to FirmwarePacker.

        Returns:
            PackerResult.
        """
        outfile = os.path.join(self.outdir, "output.sh")
        image_outdir = os.path.join(self.outdir, "images")
        ec_comp_outdir = os.path.join(self.outdir, "ec_comp_output")
        argv = extra_args + [
            "-o",
            outfile,
            "--image_output",
            image_outdir,
            "--ec_component_manifest_output",
            ec_comp_outdir,
            "-q",
        ]

        # Create the shellball, extract it, and get a list of files it contains.
        os.environ["SYSROOT"] = "test"
        os.environ["FILESDIR"] = "test"
        self.packer.start(argv)
        cros_build_lib.dbg_run(
            [outfile, "--unpack", self.unpackdir], capture_output=True
        )
        files = self._get_files(self.unpackdir)

        # EC component manifests.
        ec_comp_files = self._get_files(ec_comp_outdir)

        versions = pack_firmware_utils.read_versions(outfile)
        return PackerResult(
            outfile=outfile,
            files=files,
            image_output=image_outdir,
            ec_comp_files=ec_comp_files,
            versions=versions,
        )

    def _assert_version_lines(
        self,
        expected_lines: typing.Union[dict, list],
        ignore_extra_model: bool = False,
    ):
        """Verify the content of VERSION file (ignoring blank lines).

        Args:
            expected_lines: As a dict, key is the model name and value is the
                list of expected lines. As a list, it's the list of expected
                lines.
            ignore_extra_model: Whether to ignore extra models found in VERSION.
        """
        content = osutils.ReadFile(os.path.join(self.unpackdir, "VERSION"))
        raw_lines = [line for line in content.splitlines() if line]
        if not isinstance(expected_lines, dict):
            # Make a placeholder model name: None
            expected_lines = {None: expected_lines}

        # Parse the lines, grouped by model.
        model = None
        model_lines = collections.defaultdict(list)
        for line in raw_lines:
            match = re.fullmatch(r"Model: +(\w+)", line)
            if match:
                model = match.group(1)
            else:
                model_lines[model].append(line)

        # Check model_lines.
        for model, lines in model_lines.items():
            if model not in expected_lines:
                if ignore_extra_model:
                    continue
                raise AssertionError(
                    f"Unexpected model {model} found in VERSION:\n"
                    f"{pprint.pformat(raw_lines)}"
                )
            exp_lines = expected_lines[model]
            n = len(exp_lines)
            if len(lines) != n:
                raise AssertionError(
                    f"Wrong #lines {len(lines)}, expected {n}\n"
                    f"Versions: \n{pprint.pformat(lines)}\n"
                    f"Expected versions: \n{pprint.pformat(exp_lines)}"
                )
            for i, line in enumerate(lines):
                self.assertRegex(line, exp_lines[i])

        # Check extra model in expected_lines.
        for model in expected_lines:
            if model not in model_lines:
                raise AssertionError(
                    f"Model {model} not found in VERSION:\n"
                    f"{pprint.pformat(raw_lines)}"
                )

    def _check_versions_reef(self, versions):
        """Check the versions match expectations for reef.

        Args:
            versions: Dict of version information:
                key: Shell variable name..
                value: Value of that variable.
        """
        self.assertEqual("Google_Reef.9042.50.0", versions["AP_RO_FWID"])
        self.assertEqual("Google_Reef.9042.50.0", versions["AP_RW_FWID"])
        self.assertEqual("reef_v1.1.5857-77f6ed7", versions["EC_RO_FWID"])
        self.assertEqual("reef_v1.1.5857-77f6ed7", versions["EC_RW_FWID"])

    def _check_vars(self, model, versions):
        """Check the versions match expectations.

        Args:
            model: The model considered.
            versions: Dict of version information:
                key: Shell variable name..
                value: Value of that variable.
        """
        base = "images/%s"
        self.assertEqual(base % AP_IMAGES[model], versions["IMAGE_AP"])
        self.assertEqual(base % EC_IMAGES[model], versions["IMAGE_EC"])

    def _setup_args(self, extra_models=None):
        """Set up the arguments to execute a functional test.

        This is a convenience function to hold common code.

        Args:
            extra_models: List of extra models to generate firmware for, or None

        Returns:
            Tuple:
                List of extra arguments to pass to the firmware updater
                List of files we expect to see in the firmware update
        """
        models = ["reef", "pyro", "sand"]
        all_models = models + ["electro", "basking"]  # Share others firmware
        if extra_models:
            all_models += extra_models
        extra_args = []
        for model in all_models:
            extra_args += ["-m", model]
        extra_args += ["-c", "test/config.yaml", "-i", self.IMAGE_DIR]
        expected_files = self._expected_files(["signer_config.csv"], models)
        return extra_args, expected_files

    def _create_signer_line(
        self, model, fw_target=None, key_id=None, brand_code=None
    ):
        """Creates an expected CSV line for signer_instructions.csv.

        Args:
            model: Expected model name.
            fw_target: Expected firmware target.
            key_id: Expected key ID.
            brand_code: Expected brand code.

        Returns:
            The expected signer instructions CSV line.
        """
        fw_target = fw_target or model
        key_id = key_id or model.upper()
        brand_code = "REEF" if brand_code is None else brand_code
        return "%s,images/%s,%s,images/%s,%s" % (
            model,
            AP_IMAGES[fw_target],
            key_id,
            EC_IMAGES[fw_target],
            brand_code,
        )

    def test_firmware_update(self):
        """Run the firmware packer, unpack the result and check it."""
        extra_args, expected_files = self._setup_args()
        result = self._run_pack_firmware(extra_args)

        self.assertEqual(expected_files, result.files)
        self.assertEqual([], result.ec_comp_files)

        self._assert_version_lines(
            {
                "reef": [
                    r"reef/image\.bin",
                    r"AP version: +Google_Reef\.9042\.50\.0",
                    "/reef/ec.bin",
                    r"EC version: +reef_v1\.1\.5857-77f6ed7",
                ],
                "pyro": [
                    r"pyro/image\.bin",
                    r"AP version: +Google_Pyro\.9042\.41\.0",
                    "/pyro/ec.bin",
                    r"EC version: +pyro_v1\.1\.5840-f0d7761",
                ],
                "sand": [
                    r"sand/image\.bin",
                    r"AP version: +Google_Reef\.9042\.50\.0",
                    "/sand/ec.bin",
                    r"EC version: +reef_v1\.1\.5857-77f6ed7",
                ],
                "electro": [
                    r"reef/image\.bin",
                    r"AP version: +Google_Reef\.9042\.50\.0",
                    "/reef/ec.bin",
                    r"EC version: +reef_v1\.1\.5857-77f6ed7",
                ],
                "basking": [
                    r"sand/image\.bin",
                    r"AP version: +Google_Reef\.9042\.50\.0",
                    "/sand/ec.bin",
                    r"EC version: +reef_v1\.1\.5857-77f6ed7",
                ],
            }
        )

        manifest = pack_firmware_utils.read_manifest(self.unpackdir)
        versions = manifest["reef"]
        self._check_versions_reef(versions)
        self._check_vars("reef", versions)

        versions = manifest["electro"]
        self._check_versions_reef(versions)
        self._check_vars("electro", versions)

        versions = manifest["pyro"]
        self.assertEqual("Google_Pyro.9042.41.0", versions["AP_RW_FWID"])
        self._check_vars("pyro", versions)

        lines = osutils.ReadFile(
            os.path.join(self.unpackdir, "signer_config.csv")
        ).splitlines()
        self.assertEqual(
            "model_name,firmware_image,key_id,ec_image,brand_code", lines[0]
        )
        self.assertEqual(self._create_signer_line("reef"), lines[1])
        self.assertEqual(self._create_signer_line("pyro"), lines[2])
        self.assertEqual(self._create_signer_line("sand"), lines[3])
        self.assertEqual(self._create_signer_line("electro", "reef"), lines[4])
        self.assertEqual(self._create_signer_line("basking", "sand"), lines[5])

        # Check the output image files.
        expected_ap_files = (
            "image-basking.bin",
            "image-electro.bin",
            "image-pyro.bin",
            "image-reef.bin",
            "image-sand.bin",
        )
        expected_ec_files = (
            "basking/ec.bin",
            "electro/ec.bin",
            "pyro/ec.bin",
            "reef/ec.bin",
            "sand/ec.bin",
        )
        for opt, expected_files in zip(
            ("-i", "-e"), (expected_ap_files, expected_ec_files)
        ):
            for file in expected_files:
                file_path = os.path.join(result.image_output, file)
                # Validity check of AP and EC image.
                cros_build_lib.run(
                    ["futility", "update", "--manifest", opt, file_path],
                    print_cmd=False,
                    capture_output=True,
                )

    def test_firmware_update_merge(self):
        """Test the firmware packer with merging RW firmware."""
        extra_args = ["-c", "test/config_geralt.yaml", "-i", self.IMAGE_DIR]
        result = self._run_pack_firmware(extra_args)

        expected_versions = {
            # main-ro-image and ec-ro-image have the same version.
            # No ec-rw-image specified, so EC RW comes from main-rw-image.
            "braenn": pack_firmware_utils.ModelVersion(
                "Google_Ciri.15705.0.0",
                "Google_Ciri.15706.0.0",
                "ciri-15705.0.0",
                "ciri-15706.0.0",
            ),
            # main-ro-image and ec-ro-image have the same version.
            # EC RW comes from ec-rw-image.
            "ciri": pack_firmware_utils.ModelVersion(
                "Google_Ciri.15705.0.0",
                "Google_Ciri.15706.0.0",
                "ciri-15705.0.0",
                "ciri-15821.0.0",
                "ciri-15821.0.0",
            ),
            # main-ro-image version differs from ec-ro-image.
            # No ec-rw-image specified, so EC RW comes from ec-ro-image.
            # No main-rw-image specified.
            "duny": pack_firmware_utils.ModelVersion(
                "Google_Ciri.15705.0.0",
                "Google_Ciri.15705.0.0",
                "ciri-15820.0.0",
                "ciri-15820.0.0",
                "ciri-15820.0.0",
            ),
            # main-ro-image version differs from ec-ro-image.
            # No ec-rw-image specified, so EC RW comes from ec-ro-image.
            "emhyr": pack_firmware_utils.ModelVersion(
                "Google_Ciri.15705.0.0",
                "Google_Ciri.15706.0.0",
                "ciri-15820.0.0",
                "ciri-15820.0.0",
                "ciri-15820.0.0",
            ),
            # main-ro-image version differs from ec-ro-image.
            # No main-rw-image specified.
            # EC RW comes from ec-rw-image.
            "falka": pack_firmware_utils.ModelVersion(
                "Google_Ciri.15705.0.0",
                "Google_Ciri.15705.0.0",
                "ciri-15820.0.0",
                "ciri-15821.0.0",
                "ciri-15821.0.0",
            ),
            # main-ro-image version differs from ec-ro-image.
            # EC RW comes from ec-rw-image.
            "fenn": pack_firmware_utils.ModelVersion(
                "Google_Ciri.15705.0.0",
                "Google_Ciri.15706.0.0",
                "ciri-15820.0.0",
                "ciri-15821.0.0",
                "ciri-15821.0.0",
            ),
            # main-ro-image and ec-ro-image have the same version.
            # EC RW comes from main-rw-image.
            "geralt": pack_firmware_utils.ModelVersion(
                "Google_Geralt.15663.0.0",
                "Google_Geralt.15663.0.0",
                "geralt_v3.5.119161-ec:dc7985,os",
                "geralt_v3.5.119161-ec:dc7985,os",
            ),
        }
        self.assertEqual(expected_versions, result.versions)

    def test_firmware_update_ap_for_ec(self):
        """Test the firmware packer with AP_FOR_EC."""
        extra_args = [
            "-c",
            "test/config_geralt/",
            "--textproto",
            "-i",
            self.IMAGE_DIR,
        ]
        result = self._run_pack_firmware(extra_args)

        expected_versions = {
            # main-ro-image and ec-ro-image have the same version.
            # EC RW comes from ap-for-ec-image.
            "ciri": pack_firmware_utils.ModelVersion(
                "Google_Ciri.15705.0.0",
                "Google_Ciri.15705.0.0",
                "ciri-15705.0.0",
                "geralt_v3.5.119161-ec:dc7985,os",
            ),
        }
        self.assertEqual(expected_versions, result.versions)

    def test_firmware_update_wrong_firmware_version(self):
        """Test with firmware packer with mismatched FWID and URI versions."""
        extra_args = [
            "-c",
            "test/config_wrong_firmware_version.yaml",
            "-i",
            self.IMAGE_DIR,
        ]
        with self.assertRaisesRegex(
            pack_firmware.PackError,
            "FWID .* doesn't match URI version .*",
        ):
            self._run_pack_firmware(extra_args)

    def test_firmware_update_wrong_hash(self):
        """Test the firmware packer with merging RW firmware."""
        extra_args = ["-c", "test/config_wrong_hash.yaml", "-i", self.IMAGE_DIR]
        with self.assertRaisesRegex(
            pack_firmware.PackError,
            "Wrong md5 digest",
        ):
            self._run_pack_firmware(extra_args)

    def test_firmware_update_ec_component_manifests(self):
        """Check the extracted EC component manifests."""
        extra_args = ["-c", "test/config_geralt.yaml", "-i", self.IMAGE_DIR]
        ec_comp_files = self._run_pack_firmware(extra_args).ec_comp_files
        expected_files = [
            "braenn/component_manifest.json",
            "ciri/component_manifest.json",
            "duny/component_manifest.json",
            "emhyr/component_manifest.json",
            "falka/component_manifest.json",
            "fenn/component_manifest.json",
            # geralt EC (15663.0.0) doesn't have component manifest
        ]
        self.assertEqual(set(expected_files), set(ec_comp_files))

    def test_firmware_update_ec_component_manifests_version_not_match(self):
        """Check the extracted EC component manifests version."""
        extra_args = [
            "-c",
            "test/config_wrong_ec_manifest_version.yaml",
            "-i",
            self.IMAGE_DIR,
        ]
        with self.assertRaisesRegex(
            pack_firmware.PackError,
            "EC version '.*' doesn't match "
            r"ec_version '.*' in .*/component_manifest\.json",
        ):
            self._run_pack_firmware(extra_args)

    def test_firmware_update_ec_component_manifests_not_exist(self):
        """Check the extracted EC component manifests exist."""
        extra_args = [
            "-c",
            "test/config_no_ec_manifest.yaml",
            "-i",
            self.IMAGE_DIR,
        ]
        with self.assertRaisesRegex(
            pack_firmware.PackError,
            "Missing 'component_manifest.json'",
        ):
            self._run_pack_firmware(extra_args)

    def test_version_output(self):
        """Check that the -V option shows version information as expected."""
        extra_args, expected_files = self._setup_args()
        packer_result = self._run_pack_firmware(extra_args)

        cmd = [SHELL, packer_result.outfile, "-V"]
        result = cros_build_lib.run(
            cmd, capture_output=True, print_cmd=False, encoding="utf-8"
        )
        check_version_lines = result.stdout.splitlines()

        version_lines = osutils.ReadFile(
            os.path.join(self.unpackdir, "VERSION")
        ).splitlines()
        self.assertEqual(
            version_lines, check_version_lines[: len(version_lines)]
        )
        self.assertEqual(set(expected_files), set(packer_result.files))

    def test_files_sorted(self):
        """Files in the shellball should be sorted by filename."""
        extra_args, _ = self._setup_args()
        result = self._run_pack_firmware(extra_args)
        files = result.files

        # The shellball shows files in a comment with this format:
        # 16777216 -rw-r--r-- ap.bin
        re_files = re.compile(rb"^# +[0-9]+ [-rwx]+ \(.*\)$")
        for line in osutils.ReadFile(result.outfile, mode="rb").splitlines():
            m = re_files.match(line)
            if m:
                files.append(m.group(1).decode("utf-8"))
        self.assertEqual(files, sorted(files))

    def _assert_file_equal(self, expect_fname, fname):
        """Check that two files have the same contents.

        This causes a test failure if the file contents do not match.

        Args:
            expect_fname: File containing expected contents
            fname: File containing contents to check
        """
        expect = osutils.ReadFile(expect_fname, mode="rb")
        data = osutils.ReadFile(fname, mode="rb")
        if expect != data:
            self.fail(
                "Contents of '%s' does not match '%s'" % (fname, expect_fname)
            )

    def _get_unequal_regions(
        self, expect_fname, fname, sig_id=None, expect_root_sum=None
    ):
        """Get a list of firmware regions which are not the same.

        This is used to compare two firmware images. It checks the files regions
        by region. Any regions which do not match are added to the returned
        list. For vblock regions the contents are checked against the
        corresponding vblock file in functest rather than the contents of
        expect_fname. For GBB regions the root key is checked against
        expect_root_sum.

        The goal of this function is to check that the firmware the updater
        would write has the correct keys inside it for the model being written.
        Note that 'SECTION' regions (include 'WP_RO' are ignored since they
        cover other regions (all of which we check).

        Args:
            expect_fname: File containing expected contents
            fname: File containing contents to check
            sig_id: Signature ID for vblock, or None
            expect_root_sum: Expected SHA1 sum of the root key (as a string),
                or None

        Returns:
            List of file regions that differ  (e.g. ['GBB']) excluding any
                'section' regions
        """
        try:
            expect_dir = os.path.join(self.tempdir, "expect")
            osutils.SafeMakedirs(expect_dir)
            cros_build_lib.dbg_run(
                ["dump_fmap", "-x", expect_fname],
                capture_output=True,
                cwd=expect_dir,
            )
            actual_dir = os.path.join(self.tempdir, "actual")
            osutils.SafeMakedirs(actual_dir)
            cros_build_lib.dbg_run(
                ["dump_fmap", "-x", fname], capture_output=True, cwd=actual_dir
            )
            differ = []
            for expect_file in glob.glob("%s/*" % expect_dir):
                expect = osutils.ReadFile(expect_file, mode="rb")
                basename = os.path.basename(expect_file)
                data = osutils.ReadFile(
                    os.path.join(actual_dir, basename), mode="rb"
                )
                if sig_id and basename in ["VBLOCK_A", "VBLOCK_B"]:
                    expect = self._get_vblock(sig_id, basename[-1])
                elif expect_root_sum and basename == "GBB":
                    result = cros_build_lib.dbg_run(
                        ["futility", "show", fname],
                        capture_output=True,
                        check=False,
                        encoding="utf-8",
                    )
                    # pylint: disable=C0301
                    # Relevant output is:
                    #   Root Key:
                    #     Vboot API:           1.0
                    #     Algorithm:           11 RSA8192 SHA512
                    #     Key Version:         1
                    #     Key sha1sum:         ac7c01b1bea84da486f30a52bba5eb67ff45f50f
                    # pylint: enable=C0301
                    lines = result.stdout.splitlines()
                    filtered_lines = [
                        line for line in lines if "sha1sum" in line
                    ]
                    data = re.match(
                        " *Key sha1sum: *(.*)$", filtered_lines[0]
                    ).group(1)
                    expect = expect_root_sum

                if (
                    "SECTION" not in basename
                    and basename != "WP_RO"
                    and expect != data
                ):
                    differ.append(basename)

        finally:
            osutils.RmDir(expect_dir)
            osutils.RmDir(actual_dir)
        return differ

    def test_firmware_output(self):
        """Check the --output feature."""
        extra_args, _ = self._setup_args()
        outfile = self._run_pack_firmware(extra_args).outfile
        lines = self._run_script(outfile, REEF_MODEL)
        # TODO(evanhernandez): Consider changing futility so the image
        # names below align with those in the shellball. For now, too
        # much other code assumes the '{ap,ec}.bin' naming convention.
        self.assertIn(
            "Firmware image saved in: %s/image.bin" % self.outdir, lines
        )

        # Check that the files were written correctly
        files = glob.glob("%s/*" % self.outdir)
        ec_file = "%s/ec.bin" % self.outdir
        ap_file = os.path.join(self.outdir, "image.bin")
        self.assertIn(ec_file, files)
        self.assertIn(ap_file, files)

        # Now check that the file contents match.
        self._assert_file_equal(ap_file, "%s/image.bin" % self.indir)
        self._assert_file_equal(ec_file, "%s/ec.bin" % self.indir)

        # Electro should be the same as reef
        self._run_script(outfile, "electro")
        self._assert_file_equal(ap_file, "%s/image.bin" % self.indir)

    def _get_vblock(self, sig_id, a_or_b):
        """Get the contents of an A or B vblock for a given signature ID.

        Args:
            sig_id: Signature ID for vblock (this is key_id in the update
                script)
            a_or_b: Either 'A' or 'B' to select which vblock to use

        Returns:
            Contents of the vblock file as a string
        """
        self.assertIn(a_or_b, ["A", "B"])
        with gzip.GzipFile("functest/vblock_%s.%s.gz" % (a_or_b, sig_id)) as fd:
            return fd.read()

    def _copy_vblock(self, keydir, sig_id, a_or_b):
        """Copy a vblock file into the firmware-update key directory.

        Args:
            keydir: Destination directory to add files into
            sig_id: Signature ID for vblock
            a_or_b: Either 'A' or 'B' to select which vblock to use
        """
        path = os.path.join(keydir, "vblock_%s.%s" % (a_or_b, sig_id))
        osutils.WriteFile(path, self._get_vblock(sig_id, a_or_b), mode="wb")

    def _add_keys(self, keydir, sig_id):
        """Add root key and vblock information for testing.

        Args:
            keydir: Destination directory to add files into
            sig_id: Signature ID used to identify files in functest/

        Returns:
            SHA1 hash of the root key (as a string)
        """
        rootkey = "functest/rootkey.%s" % sig_id
        shutil.copy(rootkey, keydir)
        self._copy_vblock(keydir, sig_id, "A")
        self._copy_vblock(keydir, sig_id, "B")
        result = cros_build_lib.dbg_run(
            ["futility", "show", rootkey], capture_output=True, encoding="utf-8"
        )

        # The last output line has the following format:
        #   Key sha1sum:         ac7c01b1bea84da486f30a52bba5eb67ff45f50f
        return result.stdout.splitlines()[-1].split()[2]

    def _check_output(self, outfile, model, rootkey_sum, customlabel_tag=""):
        """Check that the firmware updater can generate the correct output.

        This runs the firmware in 'output' mode with the given model and checks
        that the resulting firmware image is correctly signed for that model.

        Args:
            outfile: Firmware update shellball filename
            model: Name of model to generate firmware for
            rootkey_sum: SHA1 sum of the root key (as a string)
            customlabel_tag: Value to return from the fake vpd
        """
        self._run_script(outfile, model, customlabel_tag=customlabel_tag)
        ap_file = os.path.join(self.outdir, "image.bin")
        regions = self._get_unequal_regions(
            "%s/image.bin" % self.indir,
            ap_file,
            self._get_full_model(model, customlabel_tag),
            rootkey_sum,
        )
        self.assertEqual([], regions)

    def add_fake_keys(self, outfile, key_ids):
        """Add fake keys to an existing firmware update to allow testing.

        Args:
            outfile: Firmware update file to modify
            key_ids: List of key IDs to add

        Returns:
            List of sums for each key in key_ids
        """
        cros_build_lib.dbg_run(
            [outfile, "--unpack", self.unpackdir], capture_output=True
        )
        keydir = os.path.join(self.unpackdir, "keyset")
        osutils.SafeMakedirs(keydir)
        key_sums = [self._add_keys(keydir, x) for x in key_ids]
        cros_build_lib.dbg_run(
            [outfile, "--repack", self.unpackdir], capture_output=True
        )
        return key_sums

    def test_signed_firmware_output(self):
        """Check the --output feature with signed firmware."""
        extra_args, _ = self._setup_args()
        outfile = self._run_pack_firmware(extra_args).outfile

        # Add some fake root keys and vblocks for some of the models, to
        # simulate the action of the signer.
        reef_sum, electro_sum = self.add_fake_keys(outfile, ["reef", "electro"])

        self._check_output(outfile, REEF_MODEL, reef_sum)
        self._check_output(outfile, "electro", electro_sum)

    def test_custom_label(self):
        """Test generation of firmware for a customlabel model."""
        extra_args, _ = self._setup_args(["customlabel_test"])
        self._run_pack_firmware(extra_args)

        # Check that the customlabel version output matches sand.
        self._assert_version_lines(
            {
                "customlabel_test": [
                    r"sand/image\.bin",
                    r"AP version: +Google_Reef\.9042\.50\.0",
                    r"/sand/ec\.bin",
                    r"EC version: +reef_v1\.1\.5857-77f6ed7",
                ],
            },
            ignore_extra_model=True,
        )

        # Check that the manifest is correct (points to sand).
        versions = pack_firmware_utils.read_manifest(self.unpackdir)[
            "customlabel_test"
        ]
        self.assertEqual("Google_Reef.9042.50.0", versions["AP_RW_FWID"])
        self._check_vars("sand", versions)

        # Check the signer instructions - the last line should be for
        # customlabel.
        lines = osutils.ReadFile(
            os.path.join(self.unpackdir, "signer_config.csv")
        ).splitlines()
        self.assertEqual(
            self._create_signer_line("customlabel_test", "sand", "CUSTOMLABEL"),
            lines[-1],
        )

    def _check_zero_touch_custom_label(self, model, cl_models, cl_tags):
        """Test generation of firmware for a 'zero-touch' customlabel model.

        This is one where the device cannot tell its model name by hardware
        detection, but must use the model name stored in the VPD
        customization_id.

        Args:
            model: AP customlabel model to generate firmware for
            cl_models: List of customlabel models to generate firmware for
            cl_tags: List of customlabel tags to generate firmware for
        """
        if cl_tags:
            assert (
                not cl_models
            ), "CL tags cannot be tested with multiple models"
            cl_models = ["%s-%s" % (model, cl_tag) for cl_tag in cl_tags]
        extra_args, _ = self._setup_args([model] + cl_models)

        result = self._run_pack_firmware(extra_args)

        # Check that the customlabel version output matches reef.
        self._assert_version_lines(
            {
                model: [
                    r"reef/image\.bin",
                    r"AP version: +Google_Reef\.9042\.50\.0",
                    r"/reef/ec\.bin",
                    r"EC version: +reef_v1\.1\.5857-77f6ed7",
                ],
            },
            ignore_extra_model=True,
        )

        # Check that the manifest is correct (points to sand).
        versions = pack_firmware_utils.read_manifest(self.unpackdir)[model]
        self.assertEqual("Google_Reef.9042.50.0", versions["AP_RW_FWID"])
        self._check_vars("reef", versions)

        # Check the signer instructions - we should get keys for the two
        # zero-touch customlabels.
        lines = osutils.ReadFile(
            os.path.join(self.unpackdir, "signer_config.csv")
        ).splitlines()
        # The signer instructions should be end by basking, clref, and
        # cl_models.
        self.assertEqual(
            self._create_signer_line("basking", "sand"),
            lines[-2 - len(cl_models)],
        )
        self.assertEqual(
            self._create_signer_line("clref", "reef", "REEF", ""),
            lines[-1 - len(cl_models)],
        )
        for i, cl_model in enumerate(cl_models):
            self.assertEqual(
                self._create_signer_line(cl_model, "reef"),
                lines[i - len(cl_models)],
            )

        outfile = result.outfile
        sums = self.add_fake_keys(outfile, cl_models)

        for i, cl_tag in enumerate(cl_tags):
            self._check_output(outfile, model, sums[i], cl_tag)

    def test_zero_touch_custom_label(self):
        """Test generation of firmware for a 'zero-touch' customlabel model.

        Customlabels use a single model with multiple 'customlabel' tags
        associated with it.
        """
        self._check_zero_touch_custom_label("clref", [], ["cltag1", "cltag2"])

    def test_empty_firmware_output(self):
        """Ensure no output is generated if not --local and no URIs."""
        args = ["-c", "test/config_no_uri.yaml", "-i", self.IMAGE_DIR]

        outfile = os.path.join(self.outdir, "output.sh")
        args += ["-o", outfile]

        os.environ["SYSROOT"] = "test"
        os.environ["FILESDIR"] = "test"
        self.packer.start(args)

        # Check that no files were written
        files = glob.glob("%s/*" % self.outdir)
        self.assertListEqual(files, [], "Expected outdir to be empty")

    def test_lzma_zip_format(self):
        """Test packing and unpacking of LZMA zip format firmware updater."""
        extra_args, expected_files = self._setup_args()
        extra_args.append("--lzma_zip")
        expected_files.append("LZMA_ZIP")
        expected_files = sorted(expected_files)
        result = self._run_pack_firmware(extra_args)
        self.assertEqual(expected_files, result.files)

    def test_bill_of_materials(self):
        """Check that the bill of materials is generated correctly."""
        bom_path = os.path.join(self.outdir, "bom.json")
        extra_args, _ = self._setup_args()
        extra_args += ["--bill_of_materials", bom_path]

        self._run_pack_firmware(extra_args)

        self.assertExists(bom_path)
        with open(bom_path, "r", encoding="utf-8") as f:
            bom = json.load(f)

        self.assertIsInstance(bom, list)
        uris = set(entry["uri"] for entry in bom)
        expected_uris = {
            "bcs://Reef.9042.50.0.tbz2",
            "bcs://Reef_EC.9042.50.0.tbz2",
            "bcs://Pyro.9042.41.0.tbz2",
            "bcs://Pyro_EC.9042.41.0.tbz2",
        }
        self.assertTrue(expected_uris.issubset(uris))


if __name__ == "__main__":
    unittest.main(module=__name__)
