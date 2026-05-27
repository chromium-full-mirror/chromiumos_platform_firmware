#!/usr/bin/env python3
# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Packages firmware images into an executable "shell-ball".

It requires:
- at least one firmware image (*.bin, should be AP or EC or ...)
- pack/sfx?.sh as the self extraction (and installation) script
- cbfstool and futility. If you are running out side of SDK, you can install it
  by the following cipd command. Add the tmp/bin path to your PATH.
  `cipd ensure -ensure-file cipd_manifest.txt -root tmp`
"""

import collections
import dataclasses
import enum
import filecmp
import glob
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import shutil
import site
import struct
import sys
import tarfile
import tempfile
from typing import Callable, Dict, Optional


# pylint: disable=import-error


# Find chromite!  This code may run outside the SDK from the source tree,
# where chromite is expected to be found in one of the parent directories.
for search_path in [
    Path("/mnt/host/source"),
    *Path(__file__).resolve().parents,
]:
    if (search_path / "chromite" / "__init__.py").is_file():
        site.addsitedir(search_path)
        break
else:
    raise RuntimeError("Unable to find chromite.")

# pylint: disable=wrong-import-position
import config_parser

from chromite.lib import commandline
from chromite.lib import cros_build_lib
from chromite.lib import gs
from chromite.lib import osutils


# pylint: enable=import-error


AP = "AP"
EC = "EC"
EC_RW_FMAP_SECTION_NAME = "EC_MAIN_A"
EC_ZEPHYR_RW_FMAP_SECTION_NAME = "ZEPHYR_RW"
EC_RW_CBFS_NAME = "ecrw"
IMG_DIR = "images"


@dataclasses.dataclass(frozen=True)
class Section:
    """Information about a flashmap section.

    Attributes:
        offset: The offset to the section.
        size: The size of the section.
    """

    offset: int
    size: int


@dataclasses.dataclass(frozen=True)
class FirmwareIds:
    """A FRID and FWID extracted from a firmware image.

    Attributes:
        ro_id: The FRID extracted from the image.
        rw_id: The FWID extracted from the image.
    """

    ro_id: str
    rw_id: str


@dataclasses.dataclass(frozen=True)
class FirmwareVersion:
    """A firmware version extracted from a URI.

    Attributes:
        major: The most significant component of the ChromeOS version number.
        minor: The middle component of the ChromeOS version number.
        patch: The least significant component of the ChromeOS version number.
    """

    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}-{self.minor}-{self.patch}"


@dataclasses.dataclass(frozen=True)
class FirmwareVersions:
    """Extracted versions from URIs for a firmware type.

    Attributes:
        ro: The extracted RO firmware version.
        rw: The extracted RW firmware version.
    """

    ro: FirmwareVersion
    rw: FirmwareVersion


# File names in firmware tarball
EC_IMAGE = "ec.bin"
EC_CONFIG = "ec.config"
EC_COMPONENT_MANIFEST = "component_manifest.json"


@dataclasses.dataclass(frozen=True)
class ImageSource:
    """Information about a firmware image source.

    Attributes:
        file: File path to the firmware image.
        uri_version: Version information extracted from the image URIs.
        firmware_ids: FRID and FWID from the firmware image.
        remote_image_uri_source: ImageUriSource if not local.
        ec_config_file: The location of the ec.config file, if present.
    """

    # TODO: file should be changed to Path type.
    file: str
    uri_version: FirmwareVersion
    firmware_ids: FirmwareIds
    remote_image_uri_source: config_parser.ImageUriSource
    ec_config_file: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class FirmwareSource:
    """Sources for each of the firmware images.

    Attributes:
        ap: ImageSource for the RO AP firmware.
        ap_rw: ImageSource for the RW AP firmware.
        ec: ImageSource for the RO EC firmware.
        ec_rw: ImageSource for the RW EC firmware.
        ec_component: ImageSource for the EC component manifest.
    """

    ap: ImageSource
    ap_rw: ImageSource
    ec: ImageSource
    ec_rw: Optional[ImageSource] = None
    ap_for_ec_rw: Optional[ImageSource] = None
    ec_component: Optional[ImageSource] = None


@dataclasses.dataclass(frozen=True)
class ImageFile:
    """File path and info of a final (merged) firmware image.

    Attributes:
        filename: Path to the merged firmware image.
        build_target: The build target for the firmware.
        firmware_ids: The FirmwareIds for the firmware.
        uri_versions: The FirmwareVersions for the original firmware sources.
        ap_ecrw_version: For an AP image, this may be set to the string
            representing the version of the EC firmware in CBFS.  Not applicable
            for EC images.
    """

    # TODO: filename should be changed to Path type.
    filename: str
    build_target: str
    firmware_ids: FirmwareIds
    uri_versions: FirmwareVersions
    ap_ecrw_version: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class ModelDetails:
    """Information about the output images for a specific model for signing.

    Attributes:
        image_files: A dictionary mapping the type of image (e.g., "AP (RW)")
            to an ImageFile.
        key_id: Key used for signing.
        brand_code: RLZ brand code used for signing.
    """

    image_files: Dict[str, ImageFile]
    key_id: str
    brand_code: str


class EcRwSource(enum.Enum):
    """The source of EC RW."""

    # Use EC RW in EC_RO image and AP_RO image. No merge required.
    TWO_SOURCES = enum.auto()

    # Use EC RW in EC_RO image as single source.
    EC_RO = enum.auto()

    # Use EC RW in EC_RW image as single source.
    EC_RW = enum.auto()

    # Use EC RW in AP_RW image as single source.
    AP_RW = enum.auto()

    # Use EC RW in AP_FOR_EC image as single source.
    AP_FOR_EC = enum.auto()


def uri_filename(uri: str) -> str:
    """Given an URI, extract the filename component."""
    return uri.rsplit("/", 1)[1]


def file_sha256(file_path: Path) -> str:
    """Reture the digest of the given file."""
    # Does this save memory? didn't explict reuse memory.
    sha256 = hashlib.sha256()
    with file_path.open("rb") as f:
        while True:
            data = f.read(2**18)
            if not data:
                break
            sha256.update(data)
    return sha256.hexdigest()


def extract_uri_version(uri: str) -> FirmwareVersion:
    """Extract the firmware version from `uri`.

    `uri` is expected to be one of the form:
    1. bcs://Name.xxxxx.yy.tbz2
    2. bcs://Name.xxxxx.yy.z.tbz2
    3. gs://path/name.xxxxx.yy.zz.tar.bz2
    4. gs://path/name.EC.xxxxx.yy.zz.tar.bz2
    5. gs://path/xxxxx.yy.zz/name.EC.tar.bz2
    """
    filename = uri_filename(uri)
    match = re.fullmatch(
        r"[-_.\w]+\.(\d+)\.(\d+)\.(\d+)\.[\w.]+",
        filename,
        flags=re.ASCII,
    )
    if match:
        version = [int(x) for x in match.group(1, 2, 3)]
        return FirmwareVersion(*version)

    match = re.fullmatch(
        r".*/(\d+)\.(\d+)\.(\d+)/[-_.\w]+\.[\w.]+",
        uri,
        flags=re.ASCII,
    )
    if match:
        version = [int(x) for x in match.group(1, 2, 3)]
        return FirmwareVersion(*version)

    if uri.startswith("bcs://"):
        match = re.fullmatch(
            r"[-_\w]+\.(\d+)\.(\d+)\.[\w.]+",
            filename,
            flags=re.ASCII,
        )
        if match:
            version = [int(x) for x in match.group(1, 2)]
            # Append the optional "patch"
            version.append(0)
            return FirmwareVersion(*version)

    raise PackError(f"Failed to extract version from URI {uri}")


# File execution permissions. We could use state.S_... but that's confusing.
CHMOD_ALL_READ = 0o444
CHMOD_ALL_EXEC = 0o555

# For testing
packer = None


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
        self._basedir = None
        self._tmpdir = None
        self._tmp_dirs = []
        self._versions = io.StringIO()
        self._force_dash = False
        self._gs_context = None
        self._bill_of_materials = {}

    @staticmethod
    def parse_args(argv):
        """Parse the available arguments.

        Invalid arguments or -h cause this function to print a message and exit.

        Args:
            argv: List of string arguments (excluding program name / argv[0])

        Returns:
            argparse.Namespace object containing the attributes.
        """
        parser = commandline.ArgumentParser(description=__doc__)
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
            help="Filename of model configuration .json file. If using "
            "textproto config, this is the path to the directory of all "
            "textproto configs.",
        )
        parser.add_bool_argument(
            "--download",
            default=False,
            enabled_desc="Download the tarball of firmware blobs if missing.",
            disabled_desc="Do not download the tarballs of firmware blobs.",
        )
        parser.add_argument(
            "--textproto",
            action="store_true",
            help="Use textproto config.",
        )
        parser.add_argument(
            "-l",
            "--local",
            action="store_true",
            help="Build a local firmware image. With this option you must "
            "provide -b or -e flags to indicate where to find the images "
            "for each model. You can use BUILD_TARGET in the filenames as a "
            "placeholder for the build target. For example "
            '-b "${root}/firmware/image-BUILD_TARGET.bin"',
        )
        parser.add_argument(
            "--lzma_zip",
            action="store_true",
            help="Use LZMA compresssion with zip format.",
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
            dest="ap_image",
            type="str_path",
            help="Path of input AP (BIOS) firmware image",
        )
        parser.add_argument(
            "-w",
            "--bios_rw_image",
            dest="ap_rw_image",
            type="str_path",
            help="Path of input AP RW firmware image",
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
            "--image_output",
            default="images",
            help="Output directory for firmware images",
        )
        parser.add_argument(
            "--ec_component_manifest_output",
            dest="ec_comp_output",
            default="cme",
            help="Output directory for EC component manifests",
        )
        parser.add_argument(
            "--bill_of_materials",
            dest="bill_of_materials_output",
            type="str_path",
            help="Output directory for bill of materials JSON summary",
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

        opts.freeze()
        return opts

    @staticmethod
    def _ensure_command(cmd, portage_package, cipd_package=None):
        """Ensure that a command is available, raising an exception if not.

        Args:
            cmd: Command to check (just the name, not the full path).
            portage_package: Name of portage package to install to obtain this
                tool.
            cipd_package: Name of cipd package to install to obtain this tool.
        """
        if not osutils.Which(cmd):
            package_source = ["portage package '%s'" % (portage_package)]
            if cipd_package:
                package_source.append("cipd package '%s'" % (cipd_package))
            raise PackError(
                "You need '%s' (%s)" % (cmd, " or ".join(package_source))
            )

    def _create_tmp_dir(self):
        """Create a temporary directory, and remember it for later removal.

        Returns:
            Path name of temporary directory.
        """
        fname = tempfile.mkdtemp(".pack_firmware-%d" % os.getpid())
        self._tmp_dirs.append(fname)
        return fname

    def _remove_tmpdirs(self):
        """Remove all the temporary directories."""
        for fname in self._tmp_dirs:
            shutil.rmtree(fname)
        self._tmp_dirs = []

    def _add_version_info(self, name, fname, version):
        """Add version info for a single file.

        Calculates the MD5 hash of the file and adds this and other file details
        into the collection of version information.

        Args:
            name: User-readable name of the file (e.g. 'AP').
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

    def _extract_fmap_section(self, image_file, section_name, output=None):
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

    def _extract_frid(self, image_file, section_name="RO_FRID"):
        """Extracts the firmware ID from an image file.

        Args:
            image_file: File to process.
            section_name: Name of the section of image_file which contains the
                firmware ID.

        Returns:
            Firmware ID as a string, if found, else ''
        """
        fname = self._extract_fmap_section(image_file, section_name)
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

    def _extract_firmware_ids(self, image_file):
        """Extracts the RO/RW firmware IDs from image files.

        Args:
            image_file: File to process.

        Returns:
            FirmwareIds object containing RO and RW firmware IDs.
        """
        ro_id = self._extract_frid(image_file)
        if not ro_id:
            raise PackError(f"Failed to extract RO_FRID from {image_file}")
        rw_id = self._extract_frid(image_file, "RW_FWID_A")
        if not rw_id:
            rw_id = self._extract_frid(image_file, "RW_FWID")
        if not rw_id:
            raise PackError(
                f"Failed to extract RW_FWID/RW_FWID_A from {image_file}"
            )
        return FirmwareIds(ro_id, rw_id)

    def _base_dir_path(self, basename):
        """Build a filename in the temporary base directory.

        Args:
            basename: Leafname (with no directory) of file to build.

        Returns:
            New filename within the self._basedir directory.
        """
        return os.path.join(self._basedir, basename)

    def _tmp_dir_path(self, basename):
        """Build a filename in the temporary directory.

        Args:
            basename: Leafname (with no directory) of file to build.

        Returns:
            New filename within the self._tmpdir directory.
        """
        return os.path.join(self._tmpdir, basename)

    @staticmethod
    def _copy_timestamp(reference_fname, fname):
        """Copy the timestamp from a reference file to another file.

        reference_fname: Reference file for timestamp.
        fname: File to copy timestamp to.
        """
        mtime = os.stat(reference_fname).st_mtime
        os.utime(fname, (mtime, mtime))

    def _get_fmap(self, fname):
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
    def _merge_file(
        large_path,
        small_path,
        large_offset,
        small_offset=0,
        size=None,
        padding=None,
    ):
        """Merges file in small_path to large_path in given offset.

        Args:
            large_path: A string for path of the file to merged to.
            small_path: A string for path of the file to merge from.
            large_offset: The offset to write in large_path.
            small_offset: The offset to read in small_path.
            size: Count of bytes to read/write. None to read whole small_path.
            padding: Byte value for padding.
        """
        with open(small_path, "rb") as source:
            source.seek(small_offset)
            if size is None:
                data = source.read()
            else:
                data = source.read(size)

        padding_data = b""
        if size is not None and len(data) < size and padding is not None:
            padding_data = bytes([padding] * (size - len(data)))

        with open(large_path, "rb+") as output:
            output.seek(large_offset)
            output.write(data)
            if padding_data:
                output.write(padding_data)

    def _clone_firmware_section(self, dst, src, section, optional=False):
        """Clone a section in one file from another.

        Args:
            dst: Destination file (relative or absolute path).
            src: Source file (relative or absolute path).
            section: Section to clone.
            optional: Ignore if the section does not exist.
        """
        src_section = self._get_fmap(src).get(section, None)
        dst_section = self._get_fmap(dst).get(section, None)

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
        self._merge_file(
            dst, src, dst_section.offset, src_section.offset, src_section.size
        )

    def _merge_ap_file(self, ro_fname, rw_fname):
        """Merge RW sections from AP RW firmware to AP RO firmware.

        The RO image is cloned to a temporary directory before the RW image
        is merged.

        Args:
            ro_fname: RO firmware image file (relative or absolute path).
            rw_fname: RW firmware image file (relative or absolute path).
        """
        self._clone_firmware_section(ro_fname, rw_fname, "RW_SECTION_A")
        self._clone_firmware_section(ro_fname, rw_fname, "RW_SECTION_B")
        self._clone_firmware_section(
            ro_fname, rw_fname, "RW_LEGACY", optional=True
        )
        self._clone_firmware_section(
            ro_fname, rw_fname, "RW_MISC", optional=True
        )

    def _extract_ec_rw_using_fmap(self, fname, section_name, ecrw_fname):
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
        # To make sure files to be merged are both prepared, _merge_file will
        # only accept existing files, so we have to create ecrw now.
        osutils.Touch(ecrw_fname)
        self._merge_file(ecrw_fname, ec_main_a, 0, offset, size)

    def _extract_ec_rw_using_cbfs(self, fname, cbfs_name, ecrw_fname):
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

    def _extract_ec_rw_from_ap(self, fname, ecrw_fname):
        """Extract the EC RW binary from AP image.

        If the AP image contains an FMAP section "EC_MAIN_A", then the EC
        RW will be extracted from it. Otherwise, the EC RW is assumed to be
        stored as a CBFS file "ecrw".

        Args:
            fname: Filename of AP image (relative or absolute path).
            ecrw_fname: Filename to put EC binary into (relative or absolute
                path).

        Raises:
            PackError or RunCommandError if an error occurs.
        """
        if EC_RW_FMAP_SECTION_NAME in self._get_fmap(fname):
            self._extract_ec_rw_using_fmap(
                fname, EC_RW_FMAP_SECTION_NAME, ecrw_fname
            )
        else:
            self._extract_ec_rw_using_cbfs(fname, EC_RW_CBFS_NAME, ecrw_fname)

    def _swap_ec_rw_in_ap(
        self, ap_fname, ec_fname, source_ap_fname, ec_config_fname
    ):
        """Swap out EC RW payload stored in AP.

        Args:
            ap_fname: Filename of AP image to be modified.
            ec_fname: Filename of EC image, whose RW will be added to AP RW to
                replace the existing EC RW in AP.
            source_ap_fname: Filename of source AP image, whose EC_RW will
                be added to AP RW to replace the existing EC RW in AP.
            ec_config_fname: Filename of EC RW config, to be added to AP RW.
        """
        # Use the copy installed in the SDK if we are in the SDK.  Otherwise use
        # the swap_ec_rw shell script in the source tree.
        if cros_build_lib.IsInsideChroot():
            script_path = Path("/usr/share/vboot/bin/swap_ec_rw")
        else:
            script_path = (
                Path(self._script_base).resolve().parent
                / "vboot_reference"
                / "scripts"
                / "image_signing"
                / "swap_ec_rw"
            )
        cmd = [script_path, "-i", ap_fname]
        if ec_fname:
            if not ec_config_fname:
                raise PackError("ec.config is required for swap_ec_rw!")
            cmd += ["-e", ec_fname]
            cmd += ["--ec_config", ec_config_fname]
        else:
            cmd += ["-a", source_ap_fname]
        cros_build_lib.dbg_run(cmd, capture_output=True, cwd=self._tmpdir)

    def _merge_ec_file(self, ec_fname, ec_rw_section_fname, check_size=False):
        """Merge EC RW section from `ec_rw_section_fname` to `ec_fname`.

        Args:
            ec_fname: Filename of EC image to merge EC RW into.
            ec_rw_section_fname: Filename of EC RW FMAP section (either EC_RW or
                RW_FW).
            check_size: Whether to require the size of `ec_rw_section_fname` to
                be the same as the EC_RW section of `ec_fname`.
        """
        section = self._get_fmap(ec_fname)["EC_RW"]
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

        self._merge_file(
            ec_fname,
            ec_rw_section_fname,
            section.offset,
            size=section.size,
            padding=0xFF,
        )

    def _verify_ap_rw_cbfs(self, ap_fname, rw_cbfs_hash):
        """Verify RW CBFS (FW_MAIN_A and FW_MAIN_B) of `ap_fname`."""
        for section in ("FW_MAIN_A", "FW_MAIN_B"):
            fname = self._extract_fmap_section(ap_fname, section)
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

    def _merge_ap_firmware(
        self,
        fw_source: FirmwareSource,
        ec_rw_source: EcRwSource,
        build_target: str,
        merge_dir: str,
    ) -> Optional[ImageFile]:
        """Merge AP RO and RW images into one.

        Args:
            fw_source: Firmware source images.
            ec_rw_source: The source of EC_RW. If the source is AP_RW, there is
                no need to swap the "ecrw" file.
            build_target: AP build target.
            merge_dir: Directory to put the merged image files.

        Returns:
            Merged AP image file.
        """
        ap = fw_source.ap
        ap_rw = fw_source.ap_rw

        if not ap:
            if ap_rw:
                raise PackError("Need AP (RO) firmware image to merge AP RW")
            return None

        merged_file = self._copy_file(ap.file, merge_dir, preserve_path=True)

        ro_fwid = ap.firmware_ids.ro_id
        rw_fwid = ap.firmware_ids.rw_id
        ro_uri_version = ap.uri_version
        rw_uri_version = ap.uri_version
        ap_ecrw_version = None
        if ap_rw:
            self._merge_ap_file(merged_file, ap_rw.file)
            rw_fwid = ap_rw.firmware_ids.rw_id
            rw_uri_version = ap_rw.uri_version
        if ec_rw_source in (EcRwSource.EC_RO, EcRwSource.EC_RW):
            effective_ec_rw = (
                fw_source.ec_rw
                if ec_rw_source == EcRwSource.EC_RW
                else fw_source.ec
            )
            ec_fname = effective_ec_rw.file
            ap_ecrw_id = effective_ec_rw.firmware_ids.rw_id
            ap_ecrw_version = self._extract_ec_version(ap_ecrw_id)
            if ap_ecrw_version:
                ap_ecrw_version = str(ap_ecrw_version)
            else:
                logging.warning(
                    "ap_ecrw_id is in legacy format: %s", ap_ecrw_id
                )
                ap_ecrw_version = ap_ecrw_id
            self._swap_ec_rw_in_ap(
                merged_file, ec_fname, None, effective_ec_rw.ec_config_file
            )
        elif ec_rw_source == EcRwSource.AP_FOR_EC:
            source_ap_fname = fw_source.ap_for_ec_rw.file
            ap_ecrw_id = fw_source.ap_for_ec_rw.firmware_ids.rw_id
            ap_ecrw_version = str(self._extract_ap_version(ap_ecrw_id))
            self._swap_ec_rw_in_ap(merged_file, None, source_ap_fname, None)
        if ap_rw:
            self._copy_timestamp(ap_rw.file, merged_file)

        firmware_ids = FirmwareIds(ro_fwid, rw_fwid)
        uri_versions = FirmwareVersions(ro_uri_version, rw_uri_version)
        return ImageFile(
            merged_file,
            build_target,
            firmware_ids,
            uri_versions,
            ap_ecrw_version,
        )

    def _merge_ec_firmware(
        self,
        fw_source: FirmwareSource,
        ec_rw_source: EcRwSource,
        build_target: str,
        merge_dir: str,
    ) -> Optional[ImageFile]:
        """Merge EC RO and RW images into one.

        Args:
            fw_source: Firmware source images.
            ec_rw_source: The source of EC_RW. If the source is AP_RW, the
                "ecrw" CBFS file stored in the source AP RW image will be used
                for EC RW.
            build_target: EC build target.
            merge_dir: Directory to put the merged image files.

        Returns:
            Merged EC image file.
        """
        ec = fw_source.ec
        ec_rw = fw_source.ec_rw

        if not ec:
            if ec_rw:
                raise PackError("Need EC (RO) firmware image to merge EC RW")
            return None

        merged_file = self._copy_file(ec.file, merge_dir, preserve_path=True)

        ro_fwid = ec.firmware_ids.ro_id
        rw_fwid = ec.firmware_ids.rw_id
        ro_uri_version = ec.uri_version
        rw_uri_version = ec.uri_version
        ec_rw_section = None
        ec_rw_fwid = None
        effective_ec_rw_image_source = None
        if ec_rw_source == EcRwSource.EC_RW:
            ec_rw_section = os.path.join(self._tmpdir, "ecrw_from_ec")
            self._extract_fmap_section(ec_rw.file, "EC_RW", ec_rw_section)
            ec_rw_fwid = ec_rw.firmware_ids.rw_id
            effective_ec_rw_image_source = ec_rw
        elif ec_rw_source in (EcRwSource.AP_RW, EcRwSource.AP_FOR_EC):
            effective_ap_for_ec_image_source = (
                fw_source.ap_rw
                if ec_rw_source == EcRwSource.AP_RW
                else fw_source.ap_for_ec_rw
            )
            ec_rw_section = os.path.join(self._tmpdir, "ecrw_from_ap")
            # The EC RW stored in AP might be either EC's EC_RW (CrOS EC)
            # or RW_FW (Zephyr EC) FMAP section.
            self._extract_ec_rw_from_ap(
                effective_ap_for_ec_image_source.file, ec_rw_section
            )
            effective_ec_rw_image_source = effective_ap_for_ec_image_source
        if ec_rw_section:
            self._merge_ec_file(merged_file, ec_rw_section)
            if ec_rw_fwid:
                rw_fwid = ec_rw_fwid
                rw_uri_version = ec_rw.uri_version
            else:
                # When ec_rw_section comes from ap_rw, we need to extract
                # FWIDs again from the merged EC image, because
                # ec_rw_section doesn't contain the FMAP layout.
                rw_fwid = self._extract_firmware_ids(merged_file).rw_id
                rw_uri_version = effective_ec_rw_image_source.uri_version
            self._copy_timestamp(effective_ec_rw_image_source.file, merged_file)

        firmware_ids = FirmwareIds(ro_fwid, rw_fwid)
        uri_versions = FirmwareVersions(ro_uri_version, rw_uri_version)
        return ImageFile(
            merged_file, build_target, firmware_ids, uri_versions, None
        )

    def _same_branch(self, ap: ImageSource, ec: ImageSource):
        """Whether AP and EC are from the same branch.

        Args:
            ap: The ImageSource of AP image.
            ec: The ImageSource of EC image.

        Returns:
            Whether AP and EC are from the same branch.
        """
        # The branch is determined by the major version.
        if ap.uri_version.major != ec.uri_version.major:
            return False
        # If the major version is the same but not the minor version,
        # that probably indicates an incorrect version. The patch
        # versions, however, are allowed to be different, because
        # people may increment the patch version in order to fix
        # tarball issues (such missing component_manifest.json).
        elif ap.uri_version.minor != ec.uri_version.minor:
            raise PackError(
                "AP and EC have same major version, "
                "but different minor versions.\n"
                f"AP {ap.uri_version}, EC {ec.uri_version}"
            )
        return True

    def _select_ec_rw_source(self, fw_source):
        """Select the EC RW source.

        Args:
            fw_source: Locations of original images.

        Returns:
            The source of EC RW.
        """
        ap = fw_source.ap
        ap_rw = fw_source.ap_rw
        ec = fw_source.ec
        ec_rw = fw_source.ec_rw
        ap_for_ec_rw = fw_source.ap_for_ec_rw

        # No need to swap EC RW for local images.
        if self._args.local:
            return EcRwSource.TWO_SOURCES

        # No need to swap EC RW if one of AP and EC images is missing.
        if not ap or not ec:
            return EcRwSource.TWO_SOURCES

        if ec_rw:
            return EcRwSource.EC_RW
        elif ap_for_ec_rw:
            return EcRwSource.AP_FOR_EC
        elif not self._same_branch(ap, ec):
            return EcRwSource.EC_RO
        elif ap_rw:
            return EcRwSource.AP_RW

        return EcRwSource.TWO_SOURCES

    def _merge_rw_firmware(
        self, fw_source, merge_dir, ap_target, ec_target, fw_main_a_hash
    ):
        """Merge all RW firmware into corresponding RO images.

        RO images are cloned to a given directory before being modified.

        Args:
            fw_source: Locations of original images.
            merge_dir: Directory to put the merged image files.
            ap_target: AP build target.
            ec_target: EC build target.
            fw_main_a_hash: Expected hash of FW_MAIN_A in hex.

        Returns:
            tuple: (ap_image, ec_image)
                ap_image: ImageFile of AP image.
                ec_image: ImageFile of EC image.
        """
        ec_rw_source = self._select_ec_rw_source(fw_source)

        ap_image = self._merge_ap_firmware(
            fw_source, ec_rw_source, ap_target, merge_dir
        )
        ec_image = self._merge_ec_firmware(
            fw_source, ec_rw_source, ec_target, merge_dir
        )

        # Verify AP RW CBFS where "ecrw" is stored.
        if (
            ec_rw_source
            in (EcRwSource.EC_RO, EcRwSource.EC_RW, EcRwSource.AP_FOR_EC)
            and fw_main_a_hash
        ):
            self._verify_ap_rw_cbfs(ap_image.filename, fw_main_a_hash)

        return ap_image, ec_image

    @staticmethod
    def _extract_ec_version(fw_id: str) -> Optional[FirmwareVersion]:
        """Extracts ChromeOS version from EC `fw_id`.

        Letting V denote [0-9], the expected version regex is "V+.V+.V+"

        The expected format is "<board>-VVVVV.VV.V", where "VVVVV.VV.V" comes
        from ChromeOS build version. Older EC images' FWID (of format
        "<board>_vV.V.VVVV-<hash>") doesn't contain the ChromeOS version, so
        None will be returned.

        For example...
            "rex-13579.24.0" becomes (13579, 24, 0).

        Args:
            fw_id: The firmware ID providing version information.

        Returns:
            The ChromeOS version, if present in `fw_id`. Otherwise None.
        """
        flags = re.ASCII
        match = re.fullmatch(r"[\w -]+-(\d+)\.(\d+)\.(\d+)", fw_id, flags=flags)
        if match:
            return FirmwareVersion(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
            )

        # If CrOS version cannot be extracted, FWID must be of the old format.
        match = re.fullmatch(r"[\w -]+_v\d+\.\d+\.\d+-\S+", fw_id, flags=flags)
        if not match:
            raise PackError(f"Malformed EC firmware ID: {fw_id}")
        return None

    @staticmethod
    def _extract_ap_version(fw_id: str) -> FirmwareVersion:
        """Extracts ChromeOS version from AP `fw_id`.

        Letting V denote [0-9], the expected version regex is "V+.V+.V+".

        Expected FW ID format is:
            "Google_<board>.<version>(_\\w+)*" in the general case, but most
            commonly:
            "Google_<board>.<version>_dyyyy_mm_dd_tttttt" for local builds
            "Google_<board>.<version>" for official builds

        For example...
            "Google_Reef.9264.0.1_d2017_02_09_124025" becomes (9264, 0, 1).
            "Google_Reef.9264.0.1" becomes (9264, 0, 1).

        Args:
            fw_id: The firmware ID providing version information.

        Returns:
            The ChromeOS version.
        """
        match = re.fullmatch(
            r"[\w -]+\.(\d+)\.(\d+)\.(\d+)(?:_\w+)?", fw_id, flags=re.ASCII
        )
        if not match:
            raise PackError("Malformed AP firmware ID: %s" % fw_id)
        return FirmwareVersion(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
        )

    def _firmware_image_output(
        self,
        tag: str,
        build_target: str = None,
        fw_ids: FirmwareIds = None,
        uri_versions: FirmwareVersions = None,
        extract_cros_version: Callable[[str], FirmwareVersion] = None,
        ap_ecrw_version: str = None,
        target_dir: str = None,
    ) -> str:
        """Creates output file name for some firmware image.

        Filename will have the form...
            "<target_dir>/<tag>-<build_target>.<ro_version>.<rw_version>.bin",

        If versions or model are not provided, returns empty string.

        Args:
            tag: Type of image (e.g, "ap").
            build_target: The build target for this image.
            fw_ids: The RO and RW FWIDs for this image.
            uri_versions: The RO and RW URI versions of this image.
            extract_cros_version: Function to extract the ChromeOS version from
                a FWID.
            ap_ecrw_version: The firmware version of "ecrw" CBFS file in AP,
                if it has been swapped with the RW_FW section of an EC image.
            target_dir: Target directory to be prepended to filename.

        Returns:
            Filename of the output image.
        """

        # Older EC images' FWID (such as "blipper_v2.0.11644-1892ef4217")
        # doesn't contain the CrOS version (see CL:5027710). Instead, the FWID
        # is determined by the commit of the EC repo(s). As a result, EC images
        # from different CrOS versions may have identical FWID.
        #
        # For example, the EC build versions of dedede's blipper and beetley
        # are 13606.421.0 and 13606.422.0, respectively, but their version
        # strings are both "blipper_v2.0.11644-1892ef4217", and their output
        # file names are both "ec-blipper.ro-2-0-11644.rw-2-0-13496.bin".
        #
        # Here, the generated version string needs to be unique for each
        # firmware target, in order to have unique file names in the updater.
        # Therefore, when CrOS version cannot be extracted from the FWID, the
        # URI version (which is CrOS version) will be used.
        def get_version_string(fwid: str, uri_version: FirmwareVersion) -> str:
            v = extract_cros_version(fwid) or uri_version
            if not v:
                raise PackError(f"Cannot extract CrOS version from FWID {fwid}")
            return str(v)

        if not uri_versions:
            uri_versions = FirmwareVersions(None, None)
        has_required_fields = fw_ids and all(
            [build_target, fw_ids.ro_id, fw_ids.rw_id, extract_cros_version]
        )
        if not has_required_fields:
            return ""
        parts = [f"{tag}-{build_target}"]
        parts.append(
            "ro-%s" % get_version_string(fw_ids.ro_id, uri_versions.ro)
        )
        parts.append(
            "rw-%s" % get_version_string(fw_ids.rw_id, uri_versions.rw)
        )
        if ap_ecrw_version:
            parts.append("ecrw-%s" % ap_ecrw_version)
        fname = ".".join(parts) + ".bin"
        return os.path.join(target_dir, fname) if target_dir else fname

    def _ec_image_output(self, image_file=None, target_dir=None):
        """Creates output file name for given EC image.

        Args:
            image_file: The input firmware image (ImageFile object).
            target_dir: Target directory to be prepended to filename.

        Returns:
            Filename of the output image.
        """
        return self._firmware_image_output(
            "ec",
            build_target=image_file.build_target,
            fw_ids=image_file.firmware_ids,
            uri_versions=image_file.uri_versions,
            extract_cros_version=self._extract_ec_version,
            target_dir=target_dir,
        )

    def _ap_image_output(self, image_file=None, target_dir=None):
        """Creates output file name for given AP image.

        Args:
            image_file: The input firmware image (ImageFile object).
            target_dir: Target directory to be prepended to filename.

        Returns:
            Filename of the output image.
        """
        return self._firmware_image_output(
            "ap",
            build_target=image_file.build_target,
            fw_ids=image_file.firmware_ids,
            uri_versions=image_file.uri_versions,
            extract_cros_version=self._extract_ap_version,
            ap_ecrw_version=image_file.ap_ecrw_version,
            target_dir=target_dir,
        )

    def _copy_firmware_file(self, src, dst):
        """Copy `src` to `dst` if `dst` does not exist.

        If `dst` already exists and its content differs from `src`, PackError
        will be raised.
        """
        if os.path.exists(dst):
            if not filecmp.cmp(src, dst, shallow=False):
                raise PackError(
                    f"Attempting to copy {src} to {dst} (which exists), "
                    "but their content is different"
                )
        else:
            shutil.copy2(src, dst)

    def _copy_firmware_files(self, image_files, target_dir):
        """Process firmware files and copy them into the working directory.

        Args:
            image_files: Dict:
                key: Type of image (e.g. 'AP')
                value: Corresponding ImageFile object.
            target_dir: Target directory for output images.

        Returns:
            Tuple of (ap_output, ec_output).
        """
        ap, ec = [image_files.get(label, None) for label in [AP, EC]]
        ap_output, ec_output = None, None
        if ap:
            ap_output = self._ap_image_output(ap, target_dir)
            self._copy_firmware_file(ap.filename, ap_output)
        if ec:
            ec_output = self._ec_image_output(ec, target_dir)
            self._copy_firmware_file(ec.filename, ec_output)
        return ap_output, ec_output

    def _write_versions(self, model, image_files):
        """Write version information for all image files into the version file.

        Args:
            model: Name of model these image files are for (or '' if none).
            image_files: Dict with:
                key: Image type (e.g. 'AP').
                value: ImageFile object containing filename and version.
        """
        print(file=self._versions)  # Blank line.
        print("Model:        %s" % model, file=self._versions)
        for name, image in sorted(image_files.items()):
            filename = image.filename
            self._add_version_info(name, filename, image.firmware_ids.ro_id)
            # Write the RW version only if it differs from RO version.
            if image.firmware_ids.rw_id != image.firmware_ids.ro_id:
                self._add_version_info(
                    name + " (RW)", None, image.firmware_ids.rw_id
                )

    def _untar_file(
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
            matched_name = None
            for member in tar:
                name = member.name
                if name.startswith("./"):
                    name = name[2:]
                if "/" in name:
                    raise PackError(
                        f"Tar file {tar_path!r} member {member.name!r} "
                        "should be a simple name"
                    )
                if fname and name != fname:
                    continue
                if matched_name:
                    if fname:
                        raise PackError(
                            f"Multiple matched members found in {tar_path!r}."
                        )
                    else:
                        raise PackError(
                            f"Expected 1 member in {tar_path!r} "
                            f"but found at least 2."
                        )
                logging.debug(
                    "Extracting %s from tar file %s to %s",
                    member.name,
                    tar_path,
                    self._tmpdir,
                )
                tar.extract(member, self._tmpdir)
                matched_name = name

            if not matched_name:
                if not required:
                    return None
                raise PackError(
                    f"No matched member {fname!r} found in {tar_path!r}"
                )
        out_file = os.path.join(dst_dirname, matched_name + suffix)
        if os.path.exists(out_file):
            raise PackError(f"Output file {out_file!r} already exists")
        logging.debug(
            "Renaming %s to %s",
            os.path.join(self._tmpdir, matched_name),
            out_file,
        )
        os.rename(os.path.join(self._tmpdir, matched_name), out_file)
        return out_file

    @staticmethod
    def _copy_file(src, dst, mode=CHMOD_ALL_READ, preserve_path=False):
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

    def _write_version_file(self):
        """Write out the VERSION file with our collected version information."""
        print(file=self._versions)
        osutils.WriteFile(
            self._base_dir_path("VERSION"), self._versions.getvalue()
        )

    def _write_bill_of_materials_file(self, path: str):
        """Encode bill_of_materials to JSON and write to provided path."""
        with open(path, "w", encoding="utf-8") as file:
            json.dump(
                [
                    {"uri": m.uri, "sha256": m.sha256}
                    for m in self._bill_of_materials.values()
                ],
                file,
                indent=2,
            )

    def _write_archive_format_file(self, use_lzma_zip: bool):
        """Write the indication file for archive format."""
        lzma_zip_file = self._base_dir_path("LZMA_ZIP")
        if use_lzma_zip:
            osutils.WriteFile(lzma_zip_file, "1")
        else:
            osutils.SafeUnlink(lzma_zip_file)

    def _build_shellball(self):
        """Build a shell-ball containing the firmware update.

        Create a new shell-ball by copying from SFX file, add our files to the
        shell-ball, and display all version information.
        """
        self._copy_file(self._sfx_file, self._args.output, mode=CHMOD_ALL_EXEC)
        cros_build_lib.run(
            ["sh", self._args.output, "--repack", self._basedir],
            print_cmd=self._args.quiet,
            capture_output=self._args.quiet,
        )
        if not self._args.quiet:
            for fname in glob.glob(self._base_dir_path("VERSION*")):
                print(osutils.ReadFile(fname))

    def _process_firmware(
        self,
        fw_source,
        target_dir,
        ap_target,
        ec_target,
        fw_name,
        fw_main_a_hash,
    ):
        """Prepares firmware to be copied into shellball, then copies it.

        In particular, merges the RW firmware if it is provided.

        Args:
            fw_source: Locations of original images.
            target_dir: Output location for firmware images.
            ap_target: The build target for AP firmware.
            ec_target: The build target for EC firmware.
            fw_name: Name for this firmware.
            fw_main_a_hash: Expected hash of FW_MAIN_A in hex.

        Returns:
            Dict:
                key: Type of firmware (e.g. 'AP')
                value: Corresponding ImageFile object.
        """
        if not fw_source.ap and not fw_source.ec:
            raise PackError(
                "Target '%s': Must assign at least one of AP or EC " % fw_name
            )

        merge_dir = self._tmp_dir_path("%s-merged" % (fw_name or "images"))
        # We use os.mkdir instead of osutils.SafeMakedirs because merge_dir
        # should not exist. If it does, something is broken and we want to know.
        # This prevents FirmwarePacker from silently reusing dirty images.
        os.mkdir(merge_dir)
        ap_image, ec_image = self._merge_rw_firmware(
            fw_source,
            merge_dir,
            ap_target,
            ec_target,
            fw_main_a_hash,
        )

        image_files = {}
        if ap_image:
            image_files[AP] = ap_image
        if ec_image:
            image_files[EC] = ec_image
        self._copy_firmware_files(image_files, target_dir)
        return image_files

    def _extract_file(
        self,
        build_target: str,
        fname_template: str,
        source: config_parser.ImageUriSource,
        dirname: str,
        suffix: str = "",
        fname: str = None,
        required: bool = True,
        extract_fwid: bool = True,
        extract_ec_config: bool = False,
    ) -> Optional[ImageSource]:
        """Obtain a file based on provided information.

        This obtains the file in one of the two ways:
        - If we are building local firmware (--local), it creates an image
            source using `fname_template`, with BUILD_TARGET replaced with the
            current build target name.
        - Otherwise, it finds a tar file specified in `source`, unpacks it, and
            returns an image source of the resulting unpacked file. This is used
            to build the official firmware updater from the firmware images.

        Args:
            build_target: The name of the build target uses, driving the file
                output.
            fname_template: Filename template to use in local model.
            source: Source of the tar file containing the image file.
            dirname: Output directory where the unpacked file should be placed.
            suffix: String to append to output filename.
            fname: The filename contained in `source` to be extracted. This is
                required when there are multiple files in `source`.
            required: Whether the `fname` must exist in `source`.
            extract_fwid: Whether to extract FWIDs.
            extract_ec_config: Whether to extract ec.config.

        Returns:
            Image source object of the extracted image file, or None if the file
                doesn't exist.
        """
        if self._args.local:
            if not fname_template:
                return None
            real_path = fname_template.replace("BUILD_TARGET", build_target)
            if not os.path.exists(real_path):
                return None
            firmware_ids = self._extract_firmware_ids(real_path)
            ec_config_path = None
            if extract_ec_config:
                ec_config_path = Path(
                    real_path.removesuffix(EC_IMAGE) + EC_CONFIG
                )
            return ImageSource(
                real_path,
                None,
                firmware_ids,
                None,
                ec_config_path,
            )

        if not source.uri:
            return None

        output_path = os.path.join(
            self._args.imagedir, uri_filename(source.uri)
        )

        if self._args.download:
            self._download_uri(source, Path(output_path))

        path = self._untar_file(
            output_path,
            dirname,
            suffix=suffix,
            fname=fname,
            required=required,
        )
        if not path:
            return None

        version = extract_uri_version(source.uri)
        firmware_ids = (
            self._extract_firmware_ids(path) if extract_fwid else None
        )
        ec_config_path = None
        if extract_ec_config:
            ec_config_path = self._untar_file(
                output_path,
                dirname,
                suffix=suffix,
                fname=EC_CONFIG,
                required=False,
            )
        return ImageSource(path, version, firmware_ids, source, ec_config_path)

    def _verify_ec_component_manifest(self, manifest_file, ec_version):
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

    def _require_ec_component_manifest(self, ec_comp_uri):
        """Whether the EC component manifest is required in `ec_comp_uri`.

        The component_manifest.json file is required if the firmware build
        contains CL:5046021, which landed in the following versions:
        - 13606.620.0 (dedede)
        - 14505.678.0 (brya)
        - 15194.145.0 (corsola)
        - 15217.397.0 (nissa)
        - 15696.0.0
        """
        version = extract_uri_version(ec_comp_uri)

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

    def _extract_firmware(self, firmware, dirname):
        """Extract all firmware images to a temporary directory.

        Args:
            firmware: FirmwareUpdateInfo object, describing what firmware to
                extract.
            dirname: Destination for extracted firmware.

        Returns:
            FirmwareSource object containing filenames of extracted files.
        """
        ap = self._extract_file(
            firmware.ap_build_target,
            self._args.ap_image,
            firmware.ap_image_source,
            dirname,
        )
        ap_rw = self._extract_file(
            firmware.ap_build_target,
            None,
            firmware.ap_rw_image_source,
            dirname,
            suffix="rw",
        )
        ec = self._extract_file(
            firmware.ec_build_target,
            self._args.ec_image,
            firmware.ec_image_source,
            dirname,
            fname=EC_IMAGE,
            extract_ec_config=True,
        )
        ec_rw = self._extract_file(
            firmware.ec_build_target,
            self._args.ec_rw_image,
            firmware.ec_rw_image_source,
            dirname,
            suffix="rw",
            fname=EC_IMAGE,
            extract_ec_config=True,
        )
        ap_for_ec_rw = self._extract_file(
            None,
            None,  # Not supported for local.
            firmware.ap_image_for_ec_rw_source,
            dirname,
            suffix="for_ec",
        )

        # EC component manifest, which must be in sync with EC RW
        if ec_rw:
            ec_comp_source = firmware.ec_rw_image_source
            effective_ec_rw = ec_rw
        else:
            ec_comp_source = firmware.ec_image_source
            effective_ec_rw = ec
        ec_comp = self._extract_file(
            firmware.ec_build_target,
            None,  # Not supported for local updater (self._args.local)
            ec_comp_source,
            dirname,
            fname=EC_COMPONENT_MANIFEST,
            required=False,
            extract_fwid=False,
        )
        if ec_comp:
            ec_rw_id = effective_ec_rw.firmware_ids.rw_id
            self._verify_ec_component_manifest(ec_comp.file, ec_rw_id)
        elif (
            not self._args.local
            and ec_comp_source.uri
            and self._require_ec_component_manifest(ec_comp_source.uri)
        ):
            # Component manifest is required only for Zephyr EC
            if EC_ZEPHYR_RW_FMAP_SECTION_NAME in self._get_fmap(ec.file):
                raise PackError(
                    f"Missing {EC_COMPONENT_MANIFEST!r} in"
                    f" {ec_comp_source.uri}\n"
                )

        return FirmwareSource(ap, ap_rw, ec, ec_rw, ap_for_ec_rw, ec_comp)

    def _check_firmware_ids(
        self,
        image: ImageSource,
        extract_cros_version: Callable[[str], FirmwareVersion],
    ) -> None:
        """Check the FWIDs in `image`.

        Args:
            image: The source image to be checked.
            extract_cros_version: Function to extract image CrOS version from
                the firmware ID.
        """
        # RO_FRID and RW_FWID must be the same for source images.
        ro_id = image.firmware_ids.ro_id
        rw_id = image.firmware_ids.rw_id
        if ro_id != rw_id:
            raise PackError(
                f"Mismatched RO_FRID and RW_FWID in {image.file}:"
                f" {ro_id}, {rw_id}"
            )

        # Check FWID against URI version.
        uri_version = image.uri_version
        fwid_version = extract_cros_version(ro_id)
        # FWID in old EC image doesn't contain the ChromeOS version.
        if not uri_version or not fwid_version:
            return
        # The "patch" version is usually 0 on firmware and release branches.
        # When a wrong tarball (such as missing component_manifest.json) is
        # uploaded, people may re-upload it by incrementing the URI patch
        # version. In that case, the "patch" version won't match. However, on
        # some special branches where the "patch" version is not 0, we can check
        # it against the URI patch version.
        if (
            fwid_version.major != uri_version.major
            or fwid_version.minor != uri_version.minor
            or (
                fwid_version.patch > 0
                and fwid_version.patch != uri_version.patch
            )
        ):
            raise PackError(
                f"FWID {ro_id} doesn't match URI version {uri_version}"
            )

    def _check_firmware_source(self, fw_source: FirmwareSource) -> None:
        """Check firmware source images.

        Args:
            fw_source: Firmware source images.
        """
        # Due to the design of mocking reading firmware IDs in unit tests, the
        # check for 'RO_FRID == RW_FWID_A' will fail. Therefore skip this
        # function for unit tests.
        if self._args.testing:
            return

        for image in (fw_source.ap, fw_source.ap_rw):
            if image:
                self._check_firmware_ids(image, self._extract_ap_version)

        for image in (fw_source.ec, fw_source.ec_rw):
            if image:
                self._check_firmware_ids(image, self._extract_ec_version)

    def _write_firmware_images(self, firmware_info, devices_fw_target):
        """Extract and build all firmware images, then copy to the shellball.

        Args:
            firmware_info: Dict:
                key: Model name.
                value: FirmwareUpdateInfo object for that model.
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
        all_errors = []
        for device, fw_target in devices_fw_target.items():
            try:
                firmware = firmware_info[device]
                if fw_target in images:
                    continue

                unpack_dir = self._tmp_dir_path(fw_target)
                osutils.SafeMakedirs(unpack_dir)
                fw_source = self._extract_firmware(firmware, unpack_dir)

                if not fw_source.ap and not fw_source.ec:
                    continue

                self._check_firmware_source(fw_source)
                images[fw_target] = self._process_firmware(
                    fw_source,
                    self._base_dir_path(IMG_DIR),
                    firmware.ap_build_target,
                    firmware.ec_build_target,
                    fw_target,
                    firmware.main_rw_a_hash,
                )

                if fw_source.ec and fw_source.ec_component:
                    ec_component_manifests[fw_target] = (
                        fw_source.ec_component.file
                    )

                for field in dataclasses.fields(fw_source):
                    m = getattr(fw_source, field.name)
                    if isinstance(m, ImageSource):
                        if m.remote_image_uri_source:
                            self._bill_of_materials[
                                m.remote_image_uri_source.uri
                            ] = m.remote_image_uri_source
                        elif m.file:
                            self._bill_of_materials[m.file] = (
                                config_parser.ImageUriSource(uri=m.file)
                            )
            except PackError as e:
                logging.error(
                    "Failed to pack firmware for model %s fw target %s: %s",
                    device,
                    fw_target,
                    e,
                )
                all_errors.append(
                    PackError(
                        f"Failed to pack firmware for model {device} "
                        f"fw target {fw_target}: {e}"
                    )
                )
        if len(all_errors) == 1:
            raise all_errors[0]
        if all_errors:
            raise ExceptionGroup("Multiple models failed", all_errors)
        return images, ec_component_manifests

    def _write_signer_instructions(self, model_details):
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
            self._base_dir_path("signer_config.csv"), "w", encoding="utf-8"
        ) as fd:
            print(
                "model_name,firmware_image,key_id,ec_image,brand_code", file=fd
            )
            for model, details in model_details.items():
                if not details.key_id:
                    # Some models will not have a key. At present this is
                    # normally just the zero-touch customlabel devices.
                    continue
                image_fname = self._ap_image_output(
                    details.image_files[AP], IMG_DIR
                )
                if EC in details.image_files:
                    ec_fname = self._ec_image_output(
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

    def _save_raw_image_files(
        self,
        devices_fw_target: Dict[str, str],
        images: Dict[str, Dict[str, ImageFile]],
        output_dir: str,
    ):
        """Save image files to a directory separate from the shellball.

        The directory structure is the same as ${SYSROOT}/firmware:
            image-<DEVICE0>.bin
            image-<DEVICE1>.bin
            <DEVICE0>/ec.bin
            <DEVICE1>/ec.bin

        Args:
            devices_fw_target:
                key: Device name.
                value: Firmware target name.
            images:
                key: Firmware target name.
                value: Dict of image files for that model:
                    key: Image type.
                    value: ImageFile.
            output_dir: Directory to save image files to.
        """
        # The output directory should not exist.
        os.makedirs(output_dir)

        # Copy files
        for device, fw_target in devices_fw_target.items():
            image_files = images.get(fw_target)
            if not image_files:
                continue
            ap_file = image_files.get(AP)
            ec_file = image_files.get(EC)
            if ap_file:
                ap_output = os.path.join(output_dir, f"image-{device}.bin")
                self._copy_firmware_file(ap_file.filename, ap_output)
            if ec_file:
                ec_dir = os.path.join(output_dir, device)
                os.mkdir(ec_dir)
                ec_output = os.path.join(ec_dir, EC_IMAGE)
                self._copy_firmware_file(ec_file.filename, ec_output)

    def _save_ec_component_manifests(self, manifests: Dict[str, str]):
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

    def _download_uri(
        self,
        source: config_parser.ImageUriSource,
        output_path: Path,
    ) -> Path:
        """Download a file from the URI and verify the sha256.

        Args:
            source: The URI spec to download.
            output_path: The destination path.

        Returns:
            The path to the downloaded file.
        """

        if not source.sha256:
            raise PackError("Cannot download files without sha256.")

        if output_path.is_file():
            if file_sha256(output_path) == source.sha256:
                logging.debug(
                    "%s already downloaded and SHA256 OK!", source.uri
                )
                return output_path
            else:
                logging.warning(
                    "%s already downloaded, but SHA256 is bad. Re-downloading.",
                    source.uri,
                )

        if not self._gs_context:
            self._gs_context = gs.GSContext()
        self._gs_context.Copy(source.uri, output_path)

        sha256 = file_sha256(output_path)
        if sha256 != source.sha256:
            raise PackError(
                f"{source.uri} has sha256 mismatch "
                f"(expected={source.sha256!r}, actual={sha256!r})!"
            )
        return output_path

    def start(self, argv, remove_tmpdirs=True):
        """Handle the creation of a firmware shell-ball.

        argv: List of arguments (excluding the program name/argv[0]).

        Raises:
            PackError if any error occurs.
        """
        args = self._args = self.parse_args(argv)

        self._ensure_command("zip", "zip")
        self._ensure_command(
            "cbfstool", "coreboot-utils", "chromiumos/infra/tools/cbfstool"
        )
        self._ensure_command(
            "futility", "vboot_reference", "chromiumos/infra/tools/futility"
        )
        if not os.path.exists(self._sfx_file):
            raise PackError("Cannot find required file '%s'" % self._sfx_file)
        try:
            if not args.output:
                raise PackError("Missing output file")
            self._basedir = self._create_tmp_dir()
            self._tmpdir = self._create_tmp_dir()
            model_details = collections.OrderedDict()

            image_args = [args.ap_image, args.ec_image]
            if any(image_args) and not args.local:
                raise PackError("Cannot use -b/-p/-e with -m")
            if args.local and not any(image_args):
                raise PackError("Must provide one of -b, -e, -p with -l")
            if not args.config:
                raise PackError("Missing model configuration file (use -c)")

            osutils.SafeMakedirs(self._base_dir_path(IMG_DIR))

            if args.textproto:
                # args.config is dir for txtpb files.
                conf = config_parser.parse_textproto_config(args.config)
            else:
                conf = config_parser.parse_cros_config(args.config)

            devices_fw_target = conf.get_firmware_configs_by_device()

            # TODO(yshaul): delete this once the api migration is complete.
            firmware_info = conf.get_firmware_info()
            # TODO: Ignore the passed-in model list and use the list from
            # libcros_config_host always. For now the passed-in list is used
            # for testing.
            if args.models:
                # tests depend on exact ordering
                devices_fw_target = collections.OrderedDict(
                    (model, devices_fw_target[model]) for model in args.models
                )

            images, ec_component_manifests = self._write_firmware_images(
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
                            fw.ap_image_source.uri,
                            fw.ap_rw_image_source.uri,
                            fw.ec_image_source.uri,
                            fw.ec_rw_image_source.uri,
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
                self._write_versions(device, image_files)

            if not args.local:
                self._save_raw_image_files(
                    devices_fw_target,
                    images,
                    args.image_output,
                )

            self._save_ec_component_manifests(ec_component_manifests)
            self._write_version_file()

            if model_details:
                self._write_signer_instructions(model_details)
            self._write_archive_format_file(use_lzma_zip=args.lzma_zip)
            self._build_shellball()
            if args.bill_of_materials_output and self._bill_of_materials:
                self._write_bill_of_materials_file(
                    args.bill_of_materials_output
                )
                logging.info(
                    "wrote bill of materials to: %s",
                    args.bill_of_materials_output,
                )
            if not args.quiet:
                print("Packed output image is: %s" % args.output)
        finally:
            if remove_tmpdirs:
                self._remove_tmpdirs()


# The style guide says that we cannot pass in sys.argv[0]. That makes testing
# a pain, so this is a full argv.
def main(argv):
    # pylint: disable=W0603
    global packer

    packer = FirmwarePacker(argv[0])
    packer.start(argv[1:])


if __name__ == "__main__":
    main(sys.argv)
