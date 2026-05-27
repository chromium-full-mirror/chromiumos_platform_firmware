# Copyright 2024 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Parsers for different configuration formats."""

import collections
import dataclasses
import os


# pylint: disable=import-error
try:
    from cros_config_host import libcros_config_host
except ModuleNotFoundError:
    libcros_config_host = None

# TODO(b/368993770): This module is missing in the environment of one builder.
# Remove the try clause after resolving it.
try:
    from google.protobuf import text_format
except ModuleNotFoundError:
    text_format = None

from chromite.api.gen.chromiumos import firmware_config_pb2


# pylint: enable=import-error


class ConfigParserError(Exception):
    """Exception returned by config_parser when something goes wrong."""


@dataclasses.dataclass(frozen=True)
class ImageUriSource:
    """URI with the sha256 hash.

    Attributes:
        uri: The URI at which the firmware binary can be found.
        sha256: The hex digest of SHA256 hash of the target file.
    """

    uri: str
    sha256: str = ""


@dataclasses.dataclass(frozen=True)
class FirmwareUpdateInfo:
    """This contains the neccessary info for the firmware updater.

    Attributes:
        device: Device name.
        base_device: The base device for custom label devices.
        ap_build_target: Build target to use to build the AP.
        ec_build_target: Build target to use to build the EC.
        ap_image_source: URI used to obtain the AP firmware image.
        ap_rw_image_source: URI used to obtain AP firmware RW image.
        ec_image_source: URI used to obtain the EC firmware image.
        ec_rw_image_source: URI used to obtain the EC RW firmware image.
        key_id: Key ID used to sign firmware for this device (e.g. "BRYAA").
        brand_code: Uniquely identifies a given brand (see go/chromeos-rlz).
        have_image: True if this base_device have dedicate image.
            If this is False it indicates that this key share the image with
            another key.  Signing instructions should still be generated for
            this device.
        main_rw_a_hash: Expected hash of FW_MAIN_A in hex.
    """

    device: str
    base_device: str

    ap_build_target: str
    ec_build_target: str
    ap_image_source: ImageUriSource
    ap_rw_image_source: ImageUriSource
    ec_image_source: ImageUriSource
    ec_rw_image_source: ImageUriSource
    ap_image_for_ec_rw_source: ImageUriSource

    key_id: str
    brand_code: str

    have_image: bool

    main_rw_a_hash: str = ""


class CrOSConfig:
    """Configs from a CrOS config file."""

    def __init__(self, config):
        self.conf = libcros_config_host.CrosConfig(config)

    @classmethod
    def _to_firmware_update_info(cls, cros_firmware_info):
        # Convert from CrOS config FirmwareInfo to FirmwareUpdateInfo
        return FirmwareUpdateInfo(
            device=cros_firmware_info.model,
            base_device=cros_firmware_info.shared_model,
            ap_build_target=cros_firmware_info.bios_build_target,
            ec_build_target=cros_firmware_info.ec_build_target,
            ap_image_source=ImageUriSource(cros_firmware_info.main_image_uri),
            ap_rw_image_source=ImageUriSource(
                cros_firmware_info.main_rw_image_uri
            ),
            ec_image_source=ImageUriSource(cros_firmware_info.ec_image_uri),
            ec_rw_image_source=ImageUriSource(
                cros_firmware_info.ec_rw_image_uri
            ),
            ap_image_for_ec_rw_source=ImageUriSource(""),
            key_id=cros_firmware_info.key_id,
            brand_code=cros_firmware_info.brand_code,
            have_image=cros_firmware_info.have_image,
            main_rw_a_hash=cros_firmware_info.main_rw_a_hash,
        )

    def get_firmware_info(self):
        firmware_update_info = collections.OrderedDict()
        for key, value in self.conf.GetFirmwareInfo().items():
            firmware_update_info[key] = self._to_firmware_update_info(value)
        return firmware_update_info

    def get_firmware_configs_by_device(self):
        return self.conf.GetFirmwareConfigsByDevice()


class TextprotoConfig:
    """Configs from textproto files."""

    def __init__(self, config_dir):
        self.devices_fw_target = collections.OrderedDict()
        self.firmware_info = collections.OrderedDict()

        txtpb_list = os.listdir(config_dir)

        configs = []
        for txtpb in txtpb_list:
            with open(
                os.path.join(config_dir, txtpb), encoding="utf-8"
            ) as infile:
                configs.append(
                    text_format.Parse(
                        infile.read(),
                        firmware_config_pb2.FirmwareConfigForModel(),
                    )
                )
        # The config of custom label should appear later than the base model.
        # The name of custom label device contains "-" which is considered
        # greater than the end of string.
        configs = sorted(configs, key=lambda message: message.model)

        for message in configs:
            device = message.model
            base_device = device.split("-")[0]

            signing = message.signing
            ap_firmware = message.ap_firmware
            ec_firmware = message.ec_firmware
            ap_firmware_for_ec_rw = message.ap_firmware_for_ec_rw

            ap_build_target = ap_firmware.ro_firmware.uri
            ap_build_target = (
                ap_build_target.split("/")[-1].split(".")[0].lower()
            )

            ec_build_target = ec_firmware.ro_firmware.uri
            ec_build_target = (
                ec_build_target.split("/")[-1].split(".")[0].lower()
            )

            info = FirmwareUpdateInfo(
                device=device,
                base_device=base_device,
                key_id=signing.key_id,
                have_image=(device == base_device),
                ap_build_target=ap_build_target,
                ec_build_target=ec_build_target,
                ap_image_source=ImageUriSource(
                    ap_firmware.ro_firmware.uri, ap_firmware.ro_firmware.sha256
                ),
                ap_rw_image_source=ImageUriSource(
                    ap_firmware.rw_firmware.uri, ap_firmware.rw_firmware.sha256
                ),
                ec_image_source=ImageUriSource(
                    ec_firmware.ro_firmware.uri, ec_firmware.ro_firmware.sha256
                ),
                ec_rw_image_source=ImageUriSource(
                    ec_firmware.rw_firmware.uri, ec_firmware.rw_firmware.sha256
                ),
                ap_image_for_ec_rw_source=ImageUriSource(
                    ap_firmware_for_ec_rw.uri, ap_firmware_for_ec_rw.sha256
                ),
                brand_code=signing.brand_code,
            )
            self.firmware_info[device] = info
            self.devices_fw_target[device] = base_device

        source_fields = [
            "ap_image_source",
            "ap_rw_image_source",
            "ec_image_source",
            "ec_rw_image_source",
            "ap_image_for_ec_rw_source",
        ]

        for device, info in self.firmware_info.items():
            if (
                info.ec_rw_image_source.uri
                and info.ap_image_for_ec_rw_source.uri
            ):
                raise ConfigParserError(
                    "ec_rw and ap_image_for_ec_rw should not be set at the "
                    "same time."
                )
            if device != info.base_device:
                base_info = self.firmware_info[info.base_device]
                for field in source_fields:
                    if getattr(info, field) != getattr(base_info, field):
                        raise ConfigParserError(
                            f"Custom label device should use the same images "
                            f"as base device. On {field} field:\n"
                            f"Custom label device ({device}):\n"
                            f"{getattr(info, field)}\n"
                            f"Base device ({base_info.device}):\n"
                            f"{getattr(base_info, field)}\n"
                        )

    def get_firmware_info(self):
        return self.firmware_info

    def get_firmware_configs_by_device(self):
        return self.devices_fw_target


def parse_cros_config(config):
    """Obtain FirmwareUpdateInfo from CrOS config.

    Args:
        config: The filename of model configuration .json file.

    Returns:
        CrOSConfig object contains FirmwareUpdateInfo.
    """
    if not libcros_config_host:
        raise ConfigParserError("Can't import cros_config_host.")
    return CrOSConfig(config)


def parse_textproto_config(config_dir):
    """Obtain FirmwareUpdateInfo from textproto configs.

    Args:
        config_dir: The path to the directory of all textproto configs.

    Returns:
        TextprotoConfig object contains FirmwareUpdateInfo.
    """
    if not text_format:
        raise ConfigParserError("Can't import text_format.")
    return TextprotoConfig(config_dir)
