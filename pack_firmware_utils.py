# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Utility functions for tests.

This includes a few useful functions shared among unit and functional tests.
"""

from __future__ import print_function

import json
import os
import subprocess


def ReadManifest(folder):
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
        model["TARGET_RO_FWID"] = v["host"]["versions"]["ro"]
        model["TARGET_FWID"] = v["host"]["versions"]["rw"]
        model["TARGET_ECID"] = v.get("ec", {}).get("versions", {}).get("ro", "")
        model["TARGET_PLATFORM"] = v["host"]["versions"]["ro"].partition(".")[0]
        model["IMAGE_MAIN"] = v["host"]["image"]
        model["IMAGE_EC"] = v.get("ec", {}).get("image", "")
        model["SIGNATURE_ID"] = v.get("signature_id", k or "")
        result[k] = model
    return result


def ReadVersions(fname):
    """Read the version of images in an updater archive.

    Args:
        fname: Filename of script file.

    Returns:
        Dict with:
            key: Version name.
            value: Version value.
    """
    raw_data = subprocess.check_output(["sh", fname, "--manifest"])
    data = json.loads(raw_data)
    if len(data) == 1:
        data = next(iter(data.values()))
        ro_fwid = data["host"]["versions"]["ro"]
        rw_fwid = data["host"]["versions"]["rw"]
        platform = ro_fwid.partition(".")[0]
        ec_id = data["ec"]["versions"]["ro"] if "ec" in data else ""
    else:
        ro_fwid = rw_fwid = platform = ec_id = ""

    versions = {
        "TARGET_RO_FWID": ro_fwid,
        "TARGET_FWID": rw_fwid,
        "TARGET_ECID": ec_id,
        "TARGET_PLATFORM": platform,
    }
    return versions


def MakeTestFiles():
    """Create test files that we need."""
    subprocess.check_call(["./make_test_files.sh"], shell=True)
