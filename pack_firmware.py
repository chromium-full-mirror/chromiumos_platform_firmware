#!/usr/bin/env python3
# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Packages firmware images into an executable "shell-ball".

It requires:
- at least one firmware image (*.bin, should be AP or EC or ...)
- pack/sfx?.sh as the self extraction (and installation) script
"""

import collections
import dataclasses
import glob
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import struct
import sys
import tarfile
import tempfile
from typing import Dict

# pylint: disable=import-error
from cros_config_host import libcros_config_host


# Find chromite!  Assume this code only runs inside the SDK.
sys.path.insert(0, "/mnt/host/source")


# pylint: disable=wrong-import-position
from chromite.lib import commandline
from chromite.lib import cros_build_lib
from chromite.lib import osutils


# pylint: enable=import-error


# Script paths
SWAP_EC_RW = "/usr/share/vboot/bin/swap_ec_rw"

MAIN = "BIOS"
EC = "EC"
EC_RW_FMAP_SECTION_NAME = "EC_MAIN_A"
EC_RW_CBFS_NAME = "ecrw"
MODELS_DIR = "models"
IMG_DIR = "images"
Section = collections.namedtuple("Section", ["offset", "size"])
FirmwareIds = collections.namedtuple("FirmwareIds", ["ro_id", "rw_id"])
FirmwareVersion = collections.namedtuple("Version", ["major", "minor", "patch"])

# File names in firmware tarball from BCS
EC_IMAGE = "ec.bin"
EC_COMPONENT_MANIFEST = "component_manifest.json"


@dataclasses.dataclass
class ImageSource:
    """File path and info of a firmware image."""

    file: str
    uri_version: tuple


# ImageSource for all firmware images.
FirmwareSource = collections.namedtuple(
    "FirmwareSource", ["bios", "bios_rw", "ec", "ec_rw", "ec_component"]
)

# File path and info of a final (merged) firmware image.
ImageFile = collections.namedtuple(
    "ImageFile", ["filename", "build_target", "firmware_ids"]
)

# image_files:  Dict containing information on the output image files for this
#     model:
#   key: Image name (e.g. 'BIOS (RW)' or 'EC').
#   value: ImageFile, containing filename and version.
# key_id: key ID to use to sign the image for this model
ModelDetails = collections.namedtuple(
    "ModelDetails", ["image_files", "key_id", "brand_code"]
)

# File execution permissions. We could use state.S_... but that's confusing.
CHMOD_ALL_READ = 0o444
CHMOD_ALL_EXEC = 0o555

# For testing
packer = None

# Variables that we replace in pack/setvars_template.
REPLACE_VARS = [
    "TARGET_RO_FWID",
    "TARGET_FWID",
    "TARGET_ECID",
    "TARGET_PLATFORM",
]


class PackError(Exception):
    """Exception returned by FirmwarePacker when something goes wrong."""


class FirmwarePacker:
    """Handles building a shell-ball firmware update.

    Most member functions raise an exception on error. This can be
    RunCommandError if an executed tool fails, or PackError on some other error.

    Private members:
        _args: Parsed arguments.
        _script_base: Base directory with useful files (src/platform/firmware).
        _sfx_file: Path to the SFX program (pack/sfx?.sh).
        _basedir: Base temporary directory.
        _tmpdir: Temporary directory for use for running tools.
        _tmp_dirs: List of temporary directories created.
        _versions: Collected version information (StringIO).
        _force_dash: Replace the /bin/sh shebang at the top of all scripts with
            /bin/dash. This is only used for testing.
    """

    def __init__(self, progname):
        # This may or may not provide the full path to the script, but in any
        # case we can access the script files using the same path as the script.
        self._script_base = os.path.dirname(progname)
        self._args = None
        self._sfx_file = os.path.join(self._script_base, "pack", "sfx2.sh")
        self._setvars_template_file = os.path.join(
            self._script_base, "pack", "setvars_template"
        )
        self._basedir = None
        self._tmpdir = None
        self._tmp_dirs = []
        self._versions = io.StringIO()
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
        parser.add_argument(
            "-L",
            "--legacy",
            action="store_true",
            help="Legacy mode for use with boards not using " "unified builds",
        )
        parser.add_argument(
            "-m",
            "--model",
            type=str,
            dest="models",
            action="append",
            help="Model name to include in firmware update",
        )
        parser.add_argument(
            "-c",
            "--config",
            type="str_path",
            help="Filename of model configuration .json file",
        )
        parser.add_argument(
            "-l",
            "--local",
            action="store_true",
            help="Build a local firmware image. With this option you must "
            "provide -b, -e and -p flags to indicate where to find the images "
            "for each model. You can use MODEL in the filenames as a "
            "placeholder for the model name. For example "
            '-b "${root}/firmware/image-BUILD_TARGET.bin"',
        )
        parser.add_argument(
            "-i",
            "--imagedir",
            type="str_path",
            default=".",
            help="Default locations for source images",
        )
        parser.add_argument(
            "-b",
            "--bios_image",
            type="str_path",
            help="Path of input AP (BIOS) firmware image",
        )
        parser.add_argument(
            "-w",
            "--bios_rw_image",
            type="str_path",
            help="Path of input BIOS RW firmware image",
        )
        parser.add_argument(
            "-e",
            "--ec_image",
            type="str_path",
            help="Path of input Embedded Controller firmware image",
        )
        parser.add_argument(
            "--ec_rw_image",
            type="str_path",
            help="Path of input Embedded Controller RW firmware image",
        )
        parser.add_argument(
            "-o", "--output", type="str_path", help="Path of output filename"
        )
        parser.add_argument(
            "--ec_component_manifest_output",
            dest="ec_comp_output",
            default="cme",
            help="Output directory for EC component manifests",
        )
        parser.add_argument(
            "-t",
            "--testing",
            action="store_true",
            help="Testing mode for mocked unit tests.",
        )
        parser.add_argument(
            "-q",
            "--quiet",
            action="store_true",
            help="Avoid output except for warnings/errors",
        )

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
        fname = tempfile.mkdtemp(".pack_firmware-%d" % os.getpid())
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
            digest = hashlib.md5()
            digest.update(osutils.ReadFile(fname, mode="rb"))

            # Modify the filename to replace any use of our base directory with
            # a constant string, so we produce the same output on each run. Also
            # drop any build and temp directies since they are not useful to the
            # user.
            short_fname = fname
            if self._basedir:
                short_fname = short_fname.replace(
                    self._basedir,
                    os.path.join(os.path.dirname(self._basedir), "tmp"),
                )
            if self._tmpdir and short_fname.startswith(self._tmpdir):
                short_fname = short_fname[len(self._tmpdir) :]
            print(
                "%s image:%s%s *%s"
                % (
                    name,
                    " " * max(3, 7 - len(name)),
                    digest.hexdigest(),
                    short_fname,
                ),
                file=self._versions,
            )
        if version:
            print(
                "%s version:%s%s"
                % (name, " " * max(1, 5 - len(name)), version),
                file=self._versions,
            )

    def _ExtractFMAPSection(self, image_file, section_name, output=None):
        """Extract FMAP section `section_name` from `image_file`.

        Args:
            image_file: File to process.
            section_name: Name of the section to be extracted.
            output: The file name for the section to be saved to. If not
                specified, a default name will be used.

        Returns:
            The output file name.
        """
        # `futility dump_fmap -x` produces a file named by the section name
        default_output = os.path.join(self._tmpdir, section_name)

        # Remove any file that might be in the way (if not testing).
        if not self._args.testing and os.path.exists(default_output):
            os.remove(default_output)
        cros_build_lib.dbg_run(
            ["futility", "dump_fmap", "-x", image_file, section_name],
            capture_output=True,
            cwd=self._tmpdir,
            check=False,
        )

        if output:
            os.rename(default_output, output)
        else:
            output = default_output
        return output

    def _ExtractFrid(self, image_file, section_name="RO_FRID"):
        """Extracts the firmware ID from an image file.

        Args:
            image_file: File to process.
            section_name: Name of the section of image_file which contains the
                firmware ID.

        Returns:
            Firmware ID as a string, if found, else ''
        """
        fname = self._ExtractFMAPSection(image_file, section_name)
        if not os.path.exists(fname):
            return ""
        raw_id = osutils.ReadFile(fname, mode="rb").split(b"\x00")[0]
        try:
            return raw_id.decode("utf-8")
        except UnicodeDecodeError:
            raise PackError(
                f"Invalid firmware ID in {image_file} section {section_name}. "
                "Please only use a NULL terminated UTF-8 compatible string. "
                "Hex dump of the firmware ID: " + raw_id.hex()
            )

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
        rw_id = (
            self._ExtractFrid(rw_image_file)
            if rw_image_file
            else self._ExtractFrid(image_file, "RW_FWID")
        )
        return FirmwareIds(ro_id, rw_id or ro_id)

    def _BaseDirPath(self, basename):
        """Build a filename in the temporary base directory.

        Args:
            basename: Leafname (with no directory) of file to build.

        Returns:
            New filename within the self._basedir directory.
        """
        return os.path.join(self._basedir, basename)

    def _TmpDirPath(self, basename):
        """Build a filename in the temporary directory.

        Args:
            basename: Leafname (with no directory) of file to build.

        Returns:
            New filename within the self._tmpdir directory.
        """
        return os.path.join(self._tmpdir, basename)

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
        result = cros_build_lib.dbg_run(
            ["futility", "dump_fmap", "-p", fname],
            capture_output=True,
            cwd=self._tmpdir,
            encoding="utf-8",
        )
        sections = {}
        for line in result.stdout.splitlines():
            name, offset, size = line.split()
            sections[name] = Section(int(offset), int(size))
        return sections

    @staticmethod
    def _MergeFile(
        large_path, small_path, large_offset, small_offset=0, size=None
    ):
        """Merges file in small_path to large_path in given offset.

        Args:
            large_path: A string for path of the file to merged to.
            small_path: A string for path of the file to merge from.
            large_offset: The offset to write in large_path.
            small_offset: The offset to read in small_path.
            size: Count of bytes to read/write. None to read whole small_path.
        """
        with open(large_path, "rb+") as output:
            output.seek(large_offset)
            with open(small_path, "rb") as source:
                source.seek(small_offset)
                output.write(
                    source.read() if size is None else source.read(size)
                )

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
                print(
                    "Ignored nonexistent optional section '%s' in RW firmware "
                    "image" % section
                )
            return
        if src_section is None or dst_section is None:
            raise PackError("Firmware section '%s' does not exist" % section)
        if not src_section.size:
            raise PackError("Firmware section '%s' is invalid" % section)
        if src_section.size != dst_section.size:
            raise PackError(
                "Firmware section '%s' size is different, cannot clone"
                % section
            )
        if src_section.offset != dst_section.offset:
            raise PackError(
                "Firmware section '%s' is not in same location, cannot "
                "clone" % section
            )
        self._MergeFile(
            dst, src, dst_section.offset, src_section.offset, src_section.size
        )

    def _MergeRwMainFirmware(self, ro_fname, rw_fname):
        """Merge RW sections from main RW firmware to main RO firmware.

        The RO image is cloned to a temporary directory before the RW image
        is merged.

        Args:
            ro_fname: RO firmware image file (relative or absolute path).
            rw_fname: RW firmware image file (relative or absolute path).
        """
        self._CloneFirmwareSection(ro_fname, rw_fname, "RW_SECTION_A")
        self._CloneFirmwareSection(ro_fname, rw_fname, "RW_SECTION_B")
        self._CloneFirmwareSection(
            ro_fname, rw_fname, "RW_LEGACY", optional=True
        )
        self._CloneFirmwareSection(ro_fname, rw_fname, "RW_MISC", optional=True)

    def _ExtractEcRwUsingFMAP(self, fname, section_name, ecrw_fname):
        """Use FMAP to extract section_name section containing an EC binary.

        Args:
            fname: Filename of firmware image (relative or absolute path).
            section_name: Name of the FMAP section which contains the EC binary.
            ecrw_fname: Filename to put EC binary into (relative or absolute
                path).
        """
        cros_build_lib.dbg_run(
            ["futility", "dump_fmap", "-x", fname, section_name],
            capture_output=True,
            cwd=self._tmpdir,
        )
        ec_main_a = os.path.join(self._tmpdir, section_name)
        with open(ec_main_a, mode="rb") as fd:
            count, offset, size = struct.unpack("<III", fd.read(12))
        if count != 1 or offset != 12:
            raise PackError(
                "Unexpected %s (%d, %d). Cannot merge EC RW"
                % (section_name, count, offset)
            )
        # To make sure files to be merged are both prepared, _MergeFile will
        # only accept existing files, so we have to create ecrw now.
        osutils.Touch(ecrw_fname)
        self._MergeFile(ecrw_fname, ec_main_a, 0, offset, size)

    def _ExtractEcRwUsingCBFS(self, fname, cbfs_name, ecrw_fname):
        """Extract an EC binary from a CBFS image.

        Args:
            fname: Filename of firmware image (relative or absolute path).
            cbfs_name: Name of file in CBFS which contains the EC binary.
            ecrw_fname: Filename to put EC binary into (relative or absolute
                path).
        """
        cros_build_lib.dbg_run(
            [
                "cbfstool",
                fname,
                "extract",
                "-n",
                cbfs_name,
                "-f",
                ecrw_fname,
                "-r",
                "FW_MAIN_A",
            ],
            capture_output=True,
            cwd=self._tmpdir,
        )

    def _ExtractEcRwFromBIOS(self, fname, ecrw_fname):
        """Extract the EC RW binary from BIOS image.

        If the BIOS image contains an FMAP section "EC_MAIN_A", then the EC
        RW will be extracted from it. Otherwise, the EC RW is assumed to be
        stored as a CBFS file "ecrw".

        Args:
            fname: Filename of BIOS image (relative or absolute path).
            ecrw_fname: Filename to put EC binary into (relative or absolute
                path).

        Raises:
            PackError or RunCommandError if an error occurs.
        """
        if EC_RW_FMAP_SECTION_NAME in self._GetFMAP(fname):
            self._ExtractEcRwUsingFMAP(
                fname, EC_RW_FMAP_SECTION_NAME, ecrw_fname
            )
        else:
            self._ExtractEcRwUsingCBFS(fname, EC_RW_CBFS_NAME, ecrw_fname)

    def _SwapEcRwInBIOS(self, bios_fname, ec_fname):
        """Swap out EC RW payload stored in BIOS.

        Args:
            bios_fname: Filename of BIOS image to be modified.
            ec_fname: Filename of EC image, whose RW will be added to BIOS RW to
                replace the existing EC RW in BIOS.
        """
        cros_build_lib.dbg_run(
            [SWAP_EC_RW, "-i", bios_fname, "-e", ec_fname],
            capture_output=True,
            cwd=self._tmpdir,
        )

    def _MergeRwEcFirmware(
        self, ec_fname, ec_rw_section_fname, check_size=False
    ):
        """Merge EC RW section from `ec_rw_section_fname` to `ec_fname`.

        Args:
            ec_fname: Filename of EC image to merge EC RW into.
            ec_rw_section_fname: Filename of EC RW FMAP section (either EC_RW or
                RW_FW).
            check_size: Whether to require the size of `ec_rw_section_fname` to
                be the same as the EC_RW section of `ec_fname`.
        """
        section = self._GetFMAP(ec_fname)["EC_RW"]
        ec_rw_size = os.stat(ec_rw_section_fname).st_size
        if check_size:
            if ec_rw_size != section.size:
                raise PackError(
                    f"New EC RW size {ec_rw_size:#x} is different from FMAP"
                    f" EC_RW section size {section.size:#x}"
                )
        elif ec_rw_size > section.size:
            raise PackError(
                f"New EC RW size {ec_rw_size:#x} is larger than FMAP"
                f" EC_RW section size {section.size:#x}"
            )
        # TODO(yupingso): Add 0xff paddings if ec_rw_size < section.size.
        self._MergeFile(ec_fname, ec_rw_section_fname, section.offset)

    def _VerifyBIOSRwCBFS(self, bios_fname, rw_cbfs_hash):
        """Verify RW CBFS (FW_MAIN_A and FW_MAIN_B) of `bios_fname`."""
        for section in ("FW_MAIN_A", "FW_MAIN_B"):
            fname = self._ExtractFMAPSection(bios_fname, section)
            if rw_cbfs_hash.algorithm != "md5sum":
                raise PackError(
                    f"Unsupported hash algorithm {rw_cbfs_hash.algorithm}"
                )
            md5 = hashlib.md5()
            md5.update(osutils.ReadFile(fname, mode="rb"))
            digest = md5.hexdigest()
            if digest != rw_cbfs_hash.digest:
                raise PackError(
                    f"Wrong md5 digest {digest}, "
                    f"expected {rw_cbfs_hash.digest}"
                )

    def _MergeRwFirmware(self, fw_source, merge_dir, fw_main_a_hash):
        """Merge all RW firmware into corresponding RO images.

        RO images are cloned to a given directory before being modified.

        Args:
            fw_source: Locations of original images.
            merge_dir: Directory in which to perform the merge.
            fw_main_a_hash: Expected hash of FW_MAIN_A in hex.

        Returns:
            FirmwareSource of merged firmware.
        """
        for image in (
            fw_source.bios,
            fw_source.bios_rw,
            fw_source.ec,
            fw_source.ec_rw,
        ):
            if image:
                image.file = self._CopyFile(
                    image.file, merge_dir, preserve_path=True
                )

        bios = fw_source.bios
        bios_rw = fw_source.bios_rw
        ec = fw_source.ec
        ec_rw = fw_source.ec_rw

        # Decide the source of truth for EC RW.
        # - If ec_rw is specified, always use the EC RW from ec_rw.
        # - If ec_rw is not specified, use the EC RW from ec if BIOS and EC came
        #   from different branches. Otherwise use the EC RW stored in BIOS RW.
        # The branch is determined by the major version.
        use_ec_rw_from_ec_source = False
        if ec_rw:
            use_ec_rw_from_ec_source = True
        elif bios and bios.uri_version and ec and ec.uri_version:
            if bios.uri_version.major != ec.uri_version.major:
                use_ec_rw_from_ec_source = True
            elif bios.uri_version.minor != ec.uri_version.minor:
                # If the major version is the same but not the minor version,
                # that probably indicates an incorrect version. The patch
                # versions, however, are allowed to be different, because
                # people may increment the patch version in order to fix BCS
                # tarball issues (such missing component_manifest.json).
                raise PackError(
                    "AP and EC have same major version, "
                    "but different minor versions.\n"
                    f"AP {bios.uri_version}, EC {ec.uri_version}"
                )

        # Merge BIOS firmware
        if bios:
            if bios_rw:
                self._MergeRwMainFirmware(bios.file, bios_rw.file)
            if use_ec_rw_from_ec_source:
                effective_ec_rw = ec_rw or ec
                if effective_ec_rw:
                    self._SwapEcRwInBIOS(bios.file, effective_ec_rw.file)
            self._CopyTimestamp(bios_rw.file, bios.file)
        elif bios_rw:
            raise PackError("Need main (RO) firmware image to merge BIOS RW")

        # Merge EC firmware
        if ec:
            ec_rw_section = None
            ec_rw_source = None
            if use_ec_rw_from_ec_source:
                if ec_rw:
                    ec_rw_section = os.path.join(self._tmpdir, "ecrw_from_ec")
                    self._ExtractFMAPSection(ec_rw.file, "EC_RW", ec_rw_section)
                    ec_rw_source = ec_rw.file
            elif bios_rw:
                ec_rw_section = os.path.join(self._tmpdir, "ecrw_from_bios")
                # The EC RW stored in BIOS might be either EC's EC_RW (CrOS EC)
                # or RW_FW (Zephyr EC) FMAP section.
                self._ExtractEcRwFromBIOS(bios_rw.file, ec_rw_section)
                ec_rw_source = bios_rw.file
            if ec_rw_section:
                self._MergeRwEcFirmware(ec.file, ec_rw_section)
                self._CopyTimestamp(ec_rw_source, ec.file)
        elif ec_rw:
            raise PackError("Need EC (RO) firmware image to merge EC RW")

        # Verify BIOS RW CBFS where "ecrw" is stored.
        if use_ec_rw_from_ec_source and fw_main_a_hash:
            self._VerifyBIOSRwCBFS(bios.file, fw_main_a_hash)

        return fw_source

    @staticmethod
    def _ExtractEcVersion(fw_id):
        """Extracts EC version from a firmware ID, swapping dots for dashes.

        Returns empty string if fw_id is empty or malformed.

        Letting V denote [0-9], the expected version regex is "V+.V+.V+"

        Expected format is "<board>_vV.V.VVVV-<hash>" or "<board>-VVVV.VV.V".
        For example, "reef_v1.1.5857-77f6ed7" becomes "1-1-5857" and
        "rex-13579.24.0" becomes "13579-24-0".

        Args:
            fw_id: The firmware ID providing version information.

        Returns:
            The version number.
        """
        matched_version = re.search(r"((\d+\.){2}\d+)", fw_id)
        if not matched_version:
            raise PackError("Malformed EC firmware ID: %s" % fw_id)
        return matched_version.group(1).replace(".", "-")

    @staticmethod
    def _ExtractMainVersion(fw_id):
        """Extracts main version from a firmware ID.

        Note this function also swaps periods for dashes, and removes
        timestamps from local builds. Returns empty string if fw_id is empty
        or malformed.

        Letting V denote [0-9], the expected version regex is...
            "V+.V+" for local builds
            "V+.V+.V+" for official builds

        Expected FW ID format is:
            "Google_<board>.<version>(_\\w+)*" in the general case, but most
            commonly:
            "Google_<board>.<version>_dyyyy_mm_dd_tttttt" for local builds
            "Google_<board>.<version>" for official builds

        For example...
            "Google_Reef.9264.0.1_d2017_02_09_124025" becomes "9264-0-1"
            "Google_Reef.9264.0.1" becomes "9264-0-1"

        Args:
            fw_id: The firmware ID providing version information.

        Returns:
            The version number.
        """
        matched_version = re.search(r"(\d+\.\d+\.\d+)", fw_id)
        if not matched_version:
            raise PackError("Malformed coreboot firmware ID: %s" % fw_id)
        version = matched_version.group(1)
        return version.replace(".", "-")

    def _FirmwareImageOutput(
        self,
        tag,
        build_target=None,
        fw_ids=None,
        extract_version=None,
        target_dir=None,
    ):
        """Creates output file name for some firmware image.

        Filename will have the form...
            "<target_dir>/<tag>.<ro_version>.<rw_version>.bin",

        If versions or model are not provided, returns empty string.
        If legacy or testing is set, returns a generic name of the form
        <target_dir>/<tag>.bin.

        Args:
            tag: Type of image (e.g, "bios").
            build_target: The build target for this image.
            fw_ids: The firmware IDs for this image.
            extract_version: Function extracting image version from a firmware
                ID.
            target_dir: Target directory to be prepended to filename.

        Returns:
            Filename of the output image.
        """
        use_generic = self._args.legacy or self._args.testing
        has_required_fields = fw_ids and all(
            [build_target, fw_ids.ro_id, fw_ids.rw_id, extract_version]
        )
        if not use_generic and not has_required_fields:
            return ""
        fname = (
            "%s.bin" % tag
            if use_generic
            else "%s-%s.ro-%s.rw-%s.bin"
            % (
                tag,
                build_target,
                extract_version(fw_ids.ro_id),
                extract_version(fw_ids.rw_id),
            )
        )
        return os.path.join(target_dir, fname) if target_dir else fname

    def _EcImageOutput(self, image_file=None, target_dir=None):
        """Creates output file name for given EC image.

        Args:
            image_file: The input firmware image (ImageFile object).
            target_dir: Target directory to be prepended to filename.

        Returns:
            Filename of the output image.
        """
        return self._FirmwareImageOutput(
            tag="ec",
            build_target=image_file.build_target,
            fw_ids=image_file.firmware_ids,
            extract_version=self._ExtractEcVersion,
            target_dir=target_dir,
        )

    def _MainImageOutput(self, image_file=None, target_dir=None):
        """Creates output file name for given main image.

        Args:
            image_file: The input firmware image (ImageFile object).
            target_dir: Target directory to be prepended to filename.

        Returns:
            Filename of the output image.
        """
        return self._FirmwareImageOutput(
            tag="bios",
            build_target=image_file.build_target,
            fw_ids=image_file.firmware_ids,
            extract_version=self._ExtractMainVersion,
            target_dir=target_dir,
        )

    def _CopyFirmwareFiles(self, image_files, target_dir):
        """Process firmware files and copy them into the working directory.

        Args:
            image_files: Dict:
                key: Type of image (e.g. 'BIOS')
                value: Corresponding ImageFile object.
            target_dir: Target directory for output images.

        Returns:
            Dict containing information on the output image files:
                key: Name, one of BIOS, BIOS (RW), EC, EC (RW).
                value: ImageFile, containing filename and version.
        """
        bios, ec = [image_files.get(label, None) for label in [MAIN, EC]]
        if bios:
            shutil.copy2(bios.filename, self._MainImageOutput(bios, target_dir))
        if ec:
            shutil.copy2(ec.filename, self._EcImageOutput(ec, target_dir))

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
            print("Model:        %s" % model, file=self._versions)
        for name, image in sorted(image_files.items()):
            filename = image.filename
            ro_id, rw_id = image.firmware_ids
            self._AddVersionInfo(name, filename, ro_id)
            # Write the RW version only if it differs from RO version.
            if rw_id != ro_id:
                self._AddVersionInfo(name + " (RW)", None, rw_id)

    def _UntarFile(
        self, tar_path, dst_dirname, suffix="", fname=None, required=True
    ):
        """Unpack a file in a tar file.

        Read a file from a tar file.

        Args:
            tar_path: Path of the tar file to unpack.
            dst_dirname: Destination directory to place the unpacked file.
            suffix: String to append to output filename.
            fname: The file to be extracted from the tar file. When the tar file
                contains multiple members, this must be specified.
            required: Whether `fname` must exist in `tar_path`. If True and the
                file is absent in `tar_path`, an exception will be raised.

        Returns:
            Path of unpacked file, or `None` if `fname` is absent.
        """
        with tarfile.open(tar_path) as tar:
            members = tar.getmembers()
            if not members:
                raise PackError(f"No member found in {tar_path!r} ({fname=})")

            matched_members = []
            for member in members:
                name = member.name
                if name.startswith("./"):
                    name = name[2:]
                if "/" in name:
                    raise PackError(
                        f"Tar file {tar_path!r} member {member.name!r} "
                        "should be a simple name"
                    )
                if not fname or name == fname:
                    matched_members.append((member, name))

            if not matched_members:
                if not required:
                    return None
                raise PackError(
                    f"No matched member {fname!r} found in {tar_path!r}"
                )

            if len(matched_members) > 1:
                if fname:
                    raise PackError(
                        f"Multiple matched members found in {tar_path!r}: "
                        f"{[x[0] for x in matched_members]}"
                    )
                else:
                    raise PackError(
                        f"Expected 1 member in {tar_path!r} "
                        f"but found {len(matched_members)}"
                    )

            matched_member, matched_name = matched_members[0]
            tar.extract(matched_member, self._tmpdir)

        out_file = os.path.join(dst_dirname, matched_name + suffix)
        if os.path.exists(out_file):
            raise PackError(f"Output file {out_file!r} already exists")
        os.rename(os.path.join(self._tmpdir, matched_member.name), out_file)
        return out_file

    @staticmethod
    def _CopyFile(src, dst, mode=CHMOD_ALL_READ, preserve_path=False):
        """Copy a file (to another file or into a directory) and set its mode.

        Fails if src/dst do not exist.

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
                dst, src.strip("/") if preserve_path else os.path.basename(src)
            )
        osutils.SafeMakedirs(
            dst if os.path.isdir(dst) else os.path.dirname(dst)
        )
        shutil.copy2(src, dst)
        os.chmod(dst, os.stat(dst).st_mode | mode)
        return dst

    def _GetReplaceDict(self, image_files):
        """Build a dict of string replacements for use with the update script.

        Args:
            image_files: Dict with:
                key: Image type (e.g. 'BIOS').
                value: ImageFile object containing filename and version.

        Returns:
            Dict with:
                key: String to replace.
                value: Value to replace with.
        """
        empty = ImageFile("", "", FirmwareIds("", ""))
        bios, ec = [image_files.get(label, empty) for label in [MAIN, EC]]
        return {
            "REPLACE_TARGET_IMAGE_RO_MAIN": self._MainImageOutput(
                bios, IMG_DIR
            ),
            "REPLACE_TARGET_IMAGE_EC": self._EcImageOutput(ec, IMG_DIR),
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
        # The template file is going to be ASCII everywhere, so we don't need to
        # jump through binary mode hoops here.
        data = osutils.ReadFile(infile)
        rep = dict((re.escape(k), v) for k, v in replace_dict.items())
        pattern = re.compile("|".join(rep.keys()))
        data = pattern.sub(lambda m: rep[re.escape(m.group(0))], data)

        osutils.WriteFile(outfile, data)
        os.chmod(outfile, os.stat(outfile).st_mode | 0o555)

    def _WriteVersionFile(self):
        """Write out the VERSION file with our collected version information."""
        print(file=self._versions)
        osutils.WriteFile(
            self._BaseDirPath("VERSION"), self._versions.getvalue()
        )

    def _BuildShellball(self):
        """Build a shell-ball containing the firmware update.

        Create a new shell-ball by copying from SFX file, add our files to the
        shell-ball, and display all version information.
        """
        self._CopyFile(self._sfx_file, self._args.output, mode=CHMOD_ALL_EXEC)
        cros_build_lib.run(
            ["sh", self._args.output, "--repack", self._basedir],
            print_cmd=self._args.quiet,
            capture_output=self._args.quiet,
        )
        if not self._args.quiet:
            for fname in glob.glob(self._BaseDirPath("VERSION*")):
                print(osutils.ReadFile(fname))

    def _AddImageFile(
        self, image_files, key, build_target, ro_source, rw_source=None
    ):
        """Adds an ImageFile to the given dict if possible.

        Does nothing in ro_image not provided.

        Args:
            image_files: Dictionary in which to insert ImageFile.
            key: Dictionary key for the new ImageFile.
            build_target: The firmware build target.
            ro_source: ImageSource for the RO image.
            rw_source: ImageSource for the RW image, if any.
        """
        if not ro_source:
            return
        image_files[key] = ImageFile(
            ro_source.file,
            build_target,
            self._ExtractFirmwareIds(
                ro_source.file, rw_source.file if rw_source else None
            ),
        )

    def _ProcessFirmware(
        self,
        fw_source,
        target_dir,
        main_target=None,
        ec_target=None,
        fw_name=None,
        fw_main_a_hash=None,
    ):
        """Prepares firmware to be copied into shellball, then copies it.

        In particular, merges the RW firmware if it is provided.

        Args:
            fw_source: Locations of original images.
            target_dir: Output location for firmware images.
            main_target: The build target for main firmware, if available.
            ec_target: The build target for ec firmware, if available.
            fw_name: Name for this firmware.
            fw_main_a_hash: Expected hash of FW_MAIN_A in hex.

        Returns:
            Dict:
                key: Type of firmware (e.g. 'BIOS')
                value: Corresponding ImageFile object.
        """
        if not fw_source.bios and not fw_source.ec:
            raise PackError(
                "Target '%s': Must assign at least one of BIOS or EC " % fw_name
            )
        if fw_source.bios_rw:
            merge_dir = self._TmpDirPath("%s-merged" % (fw_name or "images"))
            # We use os.mkdir instead of osutils.SafeMakedirs because merge_dir
            # should not exist. If it does, something is broken and we want to
            # know. This prevents FirmwarePacker from silently reusing dirty
            # images.
            os.mkdir(merge_dir)
            fw_source = self._MergeRwFirmware(
                fw_source, merge_dir, fw_main_a_hash
            )
        image_files = {}
        self._AddImageFile(
            image_files, MAIN, main_target, fw_source.bios, fw_source.bios_rw
        )
        # TODO(yupingso): Add fw_source.ec_rw.
        self._AddImageFile(image_files, EC, ec_target, fw_source.ec)
        self._CopyFirmwareFiles(image_files, target_dir)
        return image_files

    def _CreateLocalImageSource(self, image_path):
        """Create an ImageSource for local image."""
        if not image_path:
            return None
        return ImageSource(image_path, None)

    def _ExtractUriVersion(self, uri):
        """Extract the firmware version from `uri`.

        `uri` is expected to be one of the form:
        1. bcs://Name.xxxxx.yy.tbz2
        2. bcs://Name.xxxxx.yy.z.tbz2
        """
        stem = pathlib.PosixPath(uri).stem
        version = [int(x) for x in stem.split(".")[1:]]
        if len(version) == 2:
            # Append the optional "patch"
            version.append(0)
        if len(version) != 3:
            raise PackError(f"Failed to extract version from URI {uri}")
        return FirmwareVersion(*version)

    def _ExtractFile(
        self,
        build_target,
        fname_template,
        uri,
        dirname,
        suffix="",
        fname=None,
        required=True,
    ):
        """Obtain a file based on provided information.

        This obtains the file in one of two ways:
        - if we are building local firmware, it creates a filename using the
            provided template, with MODEL replaced with the current model.
            This is used to build a firmware update from the firmware ebuilds.
            The file is only used if there is a valid property value, since
            that is what indicates that the file is needed in the update.
        - otherwise it finds a tar file, unpacks it and returns the filename
            of the resulting unpacked file. This is used to build the firmware
            updater from the configured firmware versions, downloaded from BCS.

        Args:
            build_target: The name of the build target uses, driving the file
                output.
            fname_template: Filename template to use in local model.
            uri: URI containing the file.
            dirname: Output directory where the unpacked file should be placed.
            suffix: String to append to output filename.
            fname: The filename contained in `uri` to be extracted. This is
                required when there are multiple files in `uri`.
            required: Whether the `fname` must exist in `uri`.

        Returns:
            Filename of the resulting image file or None if the file doesn't
                exist.
        """
        if self._args.local:
            if not fname_template:
                return None
            # TODO(shapiroc): Remove MODEL after cros-firmware.eclass is updated
            real_path = fname_template.replace("MODEL", build_target).replace(
                "BUILD_TARGET", build_target
            )
            if not os.path.exists(real_path):
                return None
            return self._CreateLocalImageSource(real_path)

        if not uri:
            return None

        tar_fname = uri.replace("bcs://", "")
        path = self._UntarFile(
            os.path.join(self._args.imagedir, tar_fname),
            dirname,
            suffix=suffix,
            fname=fname,
            required=required,
        )
        version = self._ExtractUriVersion(uri)
        return ImageSource(path, version) if path else None

    def _VerifyEcComponentManifest(self, manifest_file, ec_version):
        """Verify the EC compoenent manifest file."""
        with open(manifest_file, encoding="utf-8") as f:
            manifests = json.load(f)
        required_keys = ("manifest_version", "ec_version")
        for key in required_keys:
            if key not in manifests:
                raise PackError(f"Missing {key!r} in {manifest_file!r}")
        manifest_ec_version = manifests["ec_version"]
        if ec_version != manifest_ec_version:
            raise PackError(
                f"EC version '{ec_version}' doesn't match "
                f"ec_version '{manifest_ec_version}' in {manifest_file!r}"
            )

    def _RequireEcComponentManifest(self, ec_comp_uri):
        """Whether the EC component manifest is required in `ec_comp_uri`.

        The component_manifest.json file is required if the firmware build
        contains CL:5046021, which landed in the following versions:
        - 13606.620.0 (dedede)
        - 14505.678.0 (brya)
        - 15194.145.0 (corsola)
        - 15217.397.0 (nissa)
        - 15696.0.0
        """
        version = self._ExtractUriVersion(ec_comp_uri)

        # There are existing BCS EC tarballs (>= the landed versions) missing
        # the component manifest. Therefore, rule them out by increasing the
        # min version to 1 larger than existing problematic tarball version.
        min_versions = (
            (13606, 620),
            (14505, 678),
            (15194, 145),
            (15217, 532),  # nissa: Glassway_EC.15217.531.0.tbz2
            (15709, 101),  # rex: Karis_EC.15709.100.0.tbz2
        )
        for v in min_versions:
            if version.major == v[0] and version.minor >= 1:
                return version.minor >= v[1]
        return version.major >= 15696

    def _ExtractFirmware(self, firmware, dirname):
        """Extract all firmware images to a temporary directory.

        Args:
            firmware: FirmwareInfo object, describing what firmware to extract.
            dirname: Destination for extracted firmware.

        Returns:
            FirmwareSource object containing filenames of extracted files.
        """
        bios = self._ExtractFile(
            firmware.bios_build_target,
            self._args.bios_image,
            firmware.main_image_uri,
            dirname,
        )
        bios_rw = self._ExtractFile(
            firmware.bios_build_target,
            None,
            firmware.main_rw_image_uri,
            dirname,
            suffix="rw",
        )
        ec = self._ExtractFile(
            firmware.ec_build_target,
            self._args.ec_image,
            firmware.ec_image_uri,
            dirname,
            fname=EC_IMAGE,
        )
        ec_rw = self._ExtractFile(
            firmware.ec_build_target,
            self._args.ec_rw_image,
            firmware.ec_rw_image_uri,
            dirname,
            suffix="rw",
            fname=EC_IMAGE,
        )

        # EC component manifest, which must be in sync with EC RW
        if ec_rw:
            ec_comp_uri = firmware.ec_rw_image_uri
            effective_ec_rw = ec_rw
        else:
            ec_comp_uri = firmware.ec_image_uri
            effective_ec_rw = ec
        ec_comp = self._ExtractFile(
            firmware.ec_build_target,
            None,  # Not supported for local updater (self._args.local)
            ec_comp_uri,
            dirname,
            fname=EC_COMPONENT_MANIFEST,
            required=False,
        )
        if ec_comp:
            ec_rw_id = self._ExtractFirmwareIds(effective_ec_rw.file).rw_id
            self._VerifyEcComponentManifest(ec_comp.file, ec_rw_id)
        elif (
            ec_comp_uri
            and firmware.is_zephyr_ec
            and self._RequireEcComponentManifest(ec_comp_uri)
        ):
            raise PackError(
                f"Missing {EC_COMPONENT_MANIFEST!r} in {ec_comp_uri}\n"
            )

        return FirmwareSource(bios, bios_rw, ec, ec_rw, ec_comp)

    def _WriteFirmwareImages(self, firmware_info, devices_fw_target):
        """Extract and build all firmware images, then copy to the shellball.

        Args:
            firmware_info: Dict:
                key: Model name.
                value: FirmwareInfo object for that model.
            devices_fw_target: Dict:
                key: Device name.
                value: Firmware target name.

        Returns:
            A tuple of `images` and `ec_component_manifests`:
                images (dict):
                    key: Target name.
                    value: Firmware files for that model (ImageFiles object).
                ec_component_manifests (dict):
                    key: Target name.
                    value: Path of EC component manifest.
        """
        images = {}
        ec_component_manifests = {}
        for device, fw_target in devices_fw_target.items():
            firmware = firmware_info[device]
            if fw_target in images:
                continue

            unpack_dir = self._TmpDirPath(fw_target)
            osutils.SafeMakedirs(unpack_dir)
            fw_source = self._ExtractFirmware(firmware, unpack_dir)

            if not fw_source.bios and not fw_source.ec:
                continue

            images[fw_target] = self._ProcessFirmware(
                fw_source,
                self._BaseDirPath(IMG_DIR),
                firmware.bios_build_target,
                firmware.ec_build_target,
                fw_target,
                firmware.main_rw_a_hash,
            )

            if fw_source.ec and fw_source.ec_component:
                ec_component_manifests[fw_target] = fw_source.ec_component.file

        return images, ec_component_manifests

    def _GenerateSetVars(self, firmware, image_files):
        """Generate the setvars.sh script for a single model.

        Args:
            firmware: Firmware information to process (FirmwareInfo object)
            image_files: Dict:
                key: Image type (e.g. BIOS).
                value: ImageFile object for that image.
        """
        setvars_dirname = self._BaseDirPath(
            os.path.join(MODELS_DIR, firmware.model)
        )
        osutils.SafeMakedirs(setvars_dirname)
        setvars_dict = self._GetReplaceDict(image_files)
        setvars_dict["REPLACE_MODEL"] = firmware.model

        # For zero-touch customlabel devices tell the firmware updater to get
        # its signature ID from the VPD.
        # TODO(sjg): Update this to say that it comes from mosys / cros_config.
        setvars_dict["REPLACE_SIGNATURE_ID"] = firmware.sig_id
        setvars_fname = os.path.join(setvars_dirname, "setvars.sh")
        self._CreateFileFromTemplate(
            self._setvars_template_file, setvars_fname, setvars_dict
        )

    def _WriteSignerInstructions(self, model_details):
        """Write the signer instructions file.

        This file tells the signer the mapping between models and their images
        and key IDs. The signer uses this to work out which images to sign, the
        key to use to sign each image and the model name to use in the vblock
        filename.

        Note that a change with the format can break the code which has already
        been released. Update all the consumers first so that they can handle
        old and new formats, then push the change here.

        Args:
            model_details: collections.OrderedDict:
                key: Model name (in the order that information is wanted in the
                    instruction file).
                value: ModelDetails object for that model.
        """
        with open(
            self._BaseDirPath("signer_config.csv"), "w", encoding="utf-8"
        ) as fd:
            print(
                "model_name,firmware_image,key_id,ec_image,brand_code", file=fd
            )
            for model, details in model_details.items():
                if not details.key_id:
                    # Some models will not have a key. At present this is
                    # normally just the zero-touch customlabel devices.
                    continue
                image_fname = self._MainImageOutput(
                    details.image_files[MAIN], IMG_DIR
                )
                if EC in details.image_files:
                    ec_fname = self._EcImageOutput(
                        details.image_files[EC], IMG_DIR
                    )
                else:
                    ec_fname = ""
                print(
                    ",".join(
                        (
                            model,
                            image_fname,
                            details.key_id,
                            ec_fname,
                            details.brand_code,
                        )
                    ),
                    file=fd,
                )

    def _SaveEcComponentManifests(self, manifests: Dict[str, str]):
        """Save EC component manifests.

        Args:
            manifests:
                key: Device name.
                value: EC component manifest file path.
        """
        # The output directory should not exist
        os.makedirs(self._args.ec_comp_output)
        for fw_name, manifest_file in manifests.items():
            if not manifest_file:
                continue
            out_dir = os.path.join(self._args.ec_comp_output, fw_name)
            os.mkdir(out_dir)
            out_file = os.path.join(out_dir, "component_manifest.json")
            shutil.copy2(manifest_file, out_file)

    def Start(self, argv, remove_tmpdirs=True):
        """Handle the creation of a firmware shell-ball.

        argv: List of arguments (excluding the program name/argv[0]).

        Raises:
            PackError if any error occurs.
        """
        args = self._args = self.ParseArgs(argv)

        self._EnsureCommand("zip", "zip")
        if not os.path.exists(self._sfx_file):
            raise PackError("Cannot find required file '%s'" % self._sfx_file)
        try:
            if not args.output:
                raise PackError("Missing output file")
            self._basedir = self._CreateTmpDir()
            self._tmpdir = self._CreateTmpDir()
            model_details = collections.OrderedDict()
            if not args.legacy:
                # Most of the arguments are meaningless with unified builds
                # since we get the information from the model configuration.
                # Add checks here to avoid confusion. We could possibly do some
                # of this using mutual exclusivity in the argparser, but I'm not
                # sure it is better.
                image_args = [args.bios_image, args.ec_image]
                if any(image_args) and not args.local:
                    raise PackError("Cannot use -b/-p/-e with -m")
                if args.local and not any(image_args):
                    raise PackError("Must provide one of -b, -e, -p with -l")
                if not args.config:
                    raise PackError("Missing model configuration file (use -c)")

                conf = libcros_config_host.CrosConfig(args.config)
                osutils.SafeMakedirs(self._BaseDirPath(MODELS_DIR))
                osutils.SafeMakedirs(self._BaseDirPath(IMG_DIR))

                devices_fw_target = conf.GetFirmwareConfigsByDevice()

                # TODO(yshaul): delete this once the api migration is complete.
                firmware_info = conf.GetFirmwareInfo()
                # TODO: Ignore the passed-in model list and use the list from
                # libcros_config_host always. For now the passed-in list is used
                # for testing.
                if args.models:
                    # tests depend on exact ordering
                    devices_fw_target = collections.OrderedDict(
                        (model, devices_fw_target[model])
                        for model in args.models
                    )

                images, ec_component_manifests = self._WriteFirmwareImages(
                    firmware_info, devices_fw_target
                )

                if not images:
                    if not args.quiet:
                        print("No image created\n")
                    return

                for device, fw_target in devices_fw_target.items():
                    fw = firmware_info[device]
                    if fw_target not in images:
                        # It is possible that some devices have images and
                        # others do not. Do validity checks and continue past
                        # this device.
                        assert not args.local, (
                            "Target %s must have an image if args.local is set."
                            % fw_target
                        )
                        assert not any(
                            (
                                fw.main_image_uri,
                                fw.main_rw_image_uri,
                                fw.ec_image_uri,
                                fw.ec_rw_image_uri,
                            )
                        ), (
                            "Expected an image for %s if any image_uri is set."
                            % device
                        )

                        continue

                    image_files = images[fw_target]
                    model_details[device] = ModelDetails(
                        image_files, fw.key_id, fw.brand_code
                    )
                    if not fw.have_image:
                        continue
                    self._WriteVersions(device, image_files)
                    self._GenerateSetVars(fw, image_files)
                self._SaveEcComponentManifests(ec_component_manifests)
            else:
                bios = self._CreateLocalImageSource(args.bios_image)
                bios_rw = self._CreateLocalImageSource(args.bios_rw_image)
                ec = self._CreateLocalImageSource(args.ec_image)
                ec_rw = self._CreateLocalImageSource(args.ec_rw_image)
                image_files = self._ProcessFirmware(
                    FirmwareSource(bios, bios_rw, ec, ec_rw, None),
                    self._basedir,
                )
                self._WriteVersions("", image_files)
            self._WriteVersionFile()
            if model_details:
                self._WriteSignerInstructions(model_details)
            self._BuildShellball()
            if not args.quiet:
                print("Packed output image is: %s" % args.output)
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
