# Copyright 2015 The Chromium OS Authors. All rights reserved.
# Distributed under the terms of the GNU General Public License v2

EAPI=4
CROS_WORKON_COMMIT="df16601bd66a249b05ebce8c2946412a911d2629"
CROS_WORKON_TREE="a9c86ccc3acf3bbc10c781bb3933947b4502dd7f"
CROS_WORKON_LOCALNAME="firmware"
CROS_WORKON_PROJECT="chromiumos/platform/firmware"

inherit cros-workon cros-firmware

DESCRIPTION="Chrome OS Firmware for Reef"
HOMEPAGE="http://src.chromium.org"
LICENSE="BSD-Google"
SLOT="0"
KEYWORDS="-* amd64 x86"

# Remove other virtual packages
RDEPEND="!chromeos-base/chromeos-firmware-null"

# ---------------------------------------------------------------------------
# CUSTOMIZATION SECTION

# Default firmware image files.
# To use the Binary Component Server (BCS), copy a tbz2 archive to CPFE:
#   http://www.google.com/chromeos/partner/fe/
# This archive should contain only the image file at its root.
# Examples
#  CROS_FIRMWARE_MAIN_IMAGE="bcs://filename.tbz2" - Fetch from BCS.
#  CROS_FIRMWARE_MAIN_IMAGE="${ROOT}/firmware/filename.bin" - Local file path.

# When you modify any files below, please also update manifest file in chroot:
#  ebuild-$board chromeos-firmware-$board-9999.ebuild manifest

CROS_FIRMWARE_BCS_OVERLAY="overlay-reef-private"
CROS_FIRMWARE_MAIN_IMAGE="bcs://Reef.9042.87.0.tbz2"
CROS_FIRMWARE_MAIN_RW_IMAGE="bcs://Reef.9042.85.0.tbz2"
CROS_FIRMWARE_EC_IMAGE="bcs://Reef_EC.9042.87.0.tbz2"
CROS_FIRMWARE_PD_IMAGE="bcs://Reef_PD.9042.80.0.tbz2"


# Stable firmware settings. Devices with firmware version smaller than stable
# version will get RO+RW update if write protection is not enabled.
CROS_FIRMWARE_STABLE_MAIN_VERSION="Google_Reef.9042.87.0"
CROS_FIRMWARE_STABLE_EC_VERSION="reef_v1.1.5899-b349d2b"
CROS_FIRMWARE_STABLE_PD_VERSION="reef_v1.1.5800-49d2bb3"

# Updater configurations
CROS_FIRMWARE_PLATFORM="Reef"

# Updater script to use
# For device using ChromeOS-EC, use updater4; otherwise, updater3.
CROS_FIRMWARE_SCRIPT="updater4.sh"

CROS_FIRMWARE_EXTRA_LIST="$FILESDIR/a_directory;$FILESDIR/a_file"
CROS_FIRMWARE_EXTRA_LIST+=";MoreStuff"
CROS_FIRMWARE_BUILD_MAIN_RW_IMAGE=TRUE

CROS_FIRMWARE_EXTRA_LIST+=";YetMoreStuff"\
";will_it_ever_end?"

CROS_FW_MAIN_REV0="bcs://gru_fw_rev0_8676.0.2016_08_05.tbz2"
CROS_FW_EC_REV0="bcs://gru_ec_rev0_8676.0.2016_08_05.tbz2"
CROS_FIRMWARE_EXTRA_LIST+=";${CROS_FW_MAIN_REV0}"
CROS_FIRMWARE_EXTRA_LIST+=";${CROS_FW_EC_REV0}"
CROS_FIRMWARE_EXTRA_LIST+=";${ROOT}/root;${SYSROOT}/sysroot"

cros-firmware_setup_source
