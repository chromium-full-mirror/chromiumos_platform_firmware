# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Utility functions for tests.

This includes a few useful functions shared among unit and functional tests.
"""

import dataclasses
import json
import os
import subprocess


@dataclasses.dataclass(frozen=True)
class ModelVersion:
    """BIOS and EC versions of a model."""

    bios_ro_id: str
    bios_rw_id: str
    ec_ro_id: str
    ec_rw_id: str
    bios_ecrw_id: str = ""


def read_manifest(folder):
    """Read the manifest information from given folder.

    Args:
        folder: Path to a folder with unpacked firmware updater package

    Returns:
        Dict with:
            key: Keys of versions
            value: Value of given key
    """
    with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as f:
        data = json.load(f)
    result = {}
    for k, v in data.items():
        model = {}

        host_versions = v["host"]["versions"]
        model["MAIN_RO_FWID"] = host_versions["ro"]
        model["MAIN_RW_FWID"] = host_versions["rw"]

        ec_versions = v.get("ec", {}).get("versions", {})
        model["EC_RO_FWID"] = ec_versions.get("ro", "")
        model["EC_RW_FWID"] = ec_versions.get("rw", "")

        model["IMAGE_MAIN"] = v["host"]["image"]
        model["IMAGE_EC"] = v.get("ec", {}).get("image", "")
        result[k] = model
    return result


def read_versions(fname):
    """Read the version of images in an updater archive.

    Args:
        fname: Filename of script file.

    Returns:
        Dict with:
            key: Model name.
            value: ModelVersion object.
    """
    raw_data = subprocess.check_output(["sh", fname, "--manifest"])
    data = json.loads(raw_data)
    versions = {}
    for k, v in data.items():
        host_versions = v["host"]["versions"]
        main_ro_fwid = host_versions["ro"]
        main_rw_fwid = host_versions["rw"]
        main_ecrw_fwid = host_versions.get("ecrw", "")

        ec_versions = v.get("ec", {}).get("versions", {})
        ec_ro_fwid = ec_versions.get("ro", "")
        ec_rw_fwid = ec_versions.get("rw", "")

        versions[k] = ModelVersion(
            bios_ro_id=main_ro_fwid,
            bios_rw_id=main_rw_fwid,
            ec_ro_id=ec_ro_fwid,
            ec_rw_id=ec_rw_fwid,
            bios_ecrw_id=main_ecrw_fwid,
        )
    return versions


def make_test_files():
    """Create test files that we need."""
    subprocess.check_call(["./make_test_files.sh"], shell=True)
