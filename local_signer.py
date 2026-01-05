#!/usr/bin/env python3
# Copyright 2022 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Generates a signer_config.csv file for local build artifacts.

Scans /build/$BOARD/firmware and look up chromeos-config to create the signer
config file for the firmware updater to use.
"""

import os
import sys

# pylint: disable=import-error
from cros_config_host import libcros_config_host


# Find chromite!  Assume this code only runs inside the SDK.
sys.path.insert(0, "/mnt/host/source")

# pylint: disable=wrong-import-position
from chromite.lib import commandline


# pylint: enable=import-error


class LocalSignerConfig:
    """Scans and generates the signer config from local."""

    def __init__(self):
        self._args = None
        self._conf = None

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
            "-c",
            "--config",
            type="str_path",
            help="Filename of model configuration .json file",
        )
        parser.add_argument(
            "-r",
            "--sysroot",
            type="str_path",
            help="Sysroot of firmware images",
        )

        opts = parser.parse_args(argv)

        opts.freeze()
        return opts

    def generate_signer_config(self, fd, stderr):
        """Scans build files and generate the signer config."""
        print("model_name,firmware_image,key_id,ec_image,brand_code", file=fd)

        fw_dir = os.path.join(self._args.sysroot, "firmware")
        fw_info = self._conf.GetFirmwareInfo()

        for device in self._conf.GetFirmwareConfigsByDevice():
            dev_info = fw_info[device]
            ap_target = dev_info.bios_build_target
            ec_target = dev_info.ec_build_target
            key_id = dev_info.key_id
            ap_image = f"image-{ap_target}.bin"
            ec_image = f"{ec_target}/ec.bin"
            brand_code = dev_info.brand_code

            if not os.path.exists(os.path.join(fw_dir, ap_image)):
                print(
                    f"Missing AP image file {ap_image} for {device}",
                    file=stderr,
                )
                continue
            if not os.path.exists(os.path.join(fw_dir, ec_image)):
                ec_image = ""
            print(
                f"{device},{ap_image},{key_id},{ec_image},{brand_code}", file=fd
            )

    def start(self, argv):
        self._args = self.parse_args(argv)
        self._conf = libcros_config_host.CrosConfig(self._args.config)
        with open("signer_config.csv", "w", encoding="utf-8") as fd:
            self.generate_signer_config(fd, sys.stderr)


def main(argv):
    generator = LocalSignerConfig()
    generator.start(argv[1:])


if __name__ == "__main__":
    main(sys.argv)
