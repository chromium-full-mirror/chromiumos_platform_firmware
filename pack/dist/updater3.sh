#!/bin/sh
#
# Copyright (c) 2012 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
#
# For factory and auto update, after shell-ball self-extracts, this script is
# called to update BIOS and EC firmware as per how many files are extracted.
# To simply design, THIS SCRIPT MUST BE EXECUTED IN A R/W EXCLUSIVE TEMP FOLDER.
# AND ALL FILENAMES FOR INPUT AND OUTPUT MUST NOT CONTAIN SPACE.
#
# Allowed commands are:
# - standard shell commands
# - flashrom
# - futility
# - crossystem
# All other special commands should be defined by a function in crosutil.sh.
#
# Temporary files should be named as "_*" to prevent confliction

# Updater for firmware v3 (two-stop)
# This is designed for x86/arm platform, with following assumption:
# 1. Perform updates even if active firmware is "developer mode"
# 2. Sleep/suspend (S3) after RO update may fail system (due to ACPI/ASL code
#    dependency), but that only happens on internal developer's dogfood devices
# Assume SLOT_A and SLOT_B has exactly same contents (and same keyblock).

SCRIPT_BASE="$(dirname "$0")"
. "$SCRIPT_BASE/common.sh"

# Use bundled tools with highest priority, to prevent dependency when updating
cros_setup_path

# ----------------------------------------------------------------------------
# Customization Section

# Customization script file name - do not change this.
# You have to create a file with this name to put your customization.
CUSTOMIZATION_SCRIPT="updater_custom.sh"

# Customization script main entry - do not change this.
# You have to define a function with this name to run your customization.
CUSTOMIZATION_MAIN="updater_custom_main"

# ----------------------------------------------------------------------------
# Constants

# Slot names defined by ChromeOS Firmware Specification
SLOT_A="RW_SECTION_A"
SLOT_B="RW_SECTION_B"
SLOT_RO="RO_SECTION"
SLOT_RW_SHARED="RW_SHARED"
SLOT_LEGACY="RW_LEGACY"
SLOT_EC_RO="EC_RO"
SLOT_EC_RW="EC_RW"

# Main = Application (AP) or SoC firmware, sometimes considered as "BIOS"
TYPE_MAIN="main"
# EC = Embedded Controller
TYPE_EC="ec"

# ----------------------------------------------------------------------------
# Global Variables

# Current system identifiers (may be empty if running on non-ChromeOS systems)
HWID="$(crossystem hwid 2>/dev/null)" || HWID=""

# Compare following values with TARGET_FWID, TARGET_ECID, TARGET_PLATFORM
# (should be passed by wrapper as environment variables)
FWID="$(crossystem fwid 2>/dev/null)" || FWID=""
RO_FWID="$(crossystem ro_fwid 2>/dev/null)" || RO_FWID=""
PLATFORM="${FWID%%.*}"

# Determine EC version
SYSFS_CROS_EC="/sys/class/chromeos/cros_ec/version"
if [ -e "${SYSFS_CROS_EC}" ]; then
  ECID="$(sed -n 's/^RW version: *//p' ${SYSFS_CROS_EC})"
else
  ECINFO="$(mosys -k ec info 2>/dev/null)" || ECINFO=""
  ECID="$(eval "$ECINFO"; echo "$fw_version")"
fi

# This should be overriden via a custom_updater.sh script.
LEGACY_UPGRADE_WHITELIST=""

# ----------------------------------------------------------------------------
# Helper functions

# Verifies if current system is installed with compatible rootkeys
check_compatible_keys() {
  local current_image="$DIR_CURRENT/$IMAGE_MAIN"
  local target_image="$DIR_TARGET/$IMAGE_MAIN"
  if [ "${FLAGS_check_keys}" = ${FLAGS_FALSE} ]; then
    debug_msg "check_compatible_keys: ignored."
    return $FLAGS_TRUE
  fi
  if ! cros_check_same_root_keys "$current_image" "$target_image"; then
    alert_incompatible_rootkey
    die "Incompatible Rootkey."
  fi

  # Get RW firmware information
  local fw_info
  fw_info="$(cros_get_rw_firmware_info "$DIR_TARGET/$TYPE_MAIN/VBLOCK_A" \
                                       "$DIR_TARGET/$TYPE_MAIN/FW_MAIN_A" \
                                       "$target_image")" || fw_info=""
  [ -n "$fw_info" ] || die "Failed to get RW firmware information"

  # Check TPM
  if ! cros_check_tpm_key_version "$fw_info"; then
    alert_incompatible_tpmkey
    die "Incompatible TPM Key."
  fi

  # Warn for RO-normal updates
  local flag_ro_normal_boot=1
  local current_flags="$(cros_get_firmware_preamble_flags "$fw_info")"
  if [ "$((current_flags & flag_ro_normal_boot))" = "$flag_ro_normal_boot" ]
  then
    alert "
    WARNING: FIRMWARE IMAGE TO BE UPDATED IS SIGNED WITH 'RO-NORMAL' FLAG.
    THIS IS A KEY-BLOCK-ONLY UPDATE WITHOUT FIRMWARE CODE CHANGE.
    YOUR FWID (ACTIVE FIRMWARE ID) WON'T CHANGE AFTER APPLYING THIS UPDATE.
    "
  fi
}

need_update_legacy() {
  local legacy_hash="$(crosfw_slot_hash "$TYPE_MAIN" "$SLOT_LEGACY")"
  debug_msg "RW_LEGACY hash $legacy_hash, white list: $LEGACY_UPGRADE_WHITELIST"

  for hash in $LEGACY_UPGRADE_WHITELIST; do
    if [ "$legacy_hash" = "$hash" ]; then
      debug_msg "RW_LEGACY needs update."
      return $FLAGS_TRUE
    fi
  done

  debug_msg "No updates for RW_LEGACY"
  return $FLAGS_FALSE
}

need_update_main() {
  if [ "$TARGET_FWID" != "$FWID" ]; then
    return $FLAGS_TRUE
  fi

  prepare_main_image
  prepare_main_current_image

  # Compare VBLOCK from current A slot and target B slot (normal firmware).
  if ! crosfw_is_equal_slot "$TYPE_MAIN" "VBLOCK_A" "VBLOCK_B"; then
    debug_msg "VBLOCK in A and B differ"
    return $FLAGS_TRUE
  fi

  # Compare legacy hash with whitelisted set of legacy hashes to update.
  need_update_legacy
}

need_update_ec() {
  prepare_ec_image
  prepare_ec_current_image
  if ! is_ecfw_write_protected &&
     ! crosfw_is_equal_slot "$TYPE_EC" "$SLOT_EC_RO"; then
      debug_msg "EC RO needs update."
      return $FLAGS_TRUE
  fi
  if ! crosfw_is_equal_slot "$TYPE_EC" "$SLOT_EC_RW"; then
      debug_msg "EC RW needs update."
      return $FLAGS_TRUE
  fi
  return $FLAGS_FALSE
}

prepare_main_image() {
  crosfw_unpack_image "$TYPE_MAIN" "$IMAGE_MAIN" "$TARGET_OPT_MAIN"
}

prepare_ec_image() {
  crosfw_unpack_image "$TYPE_EC" "$IMAGE_EC" "$TARGET_OPT_EC"
}

prepare_main_current_image() {
  crosfw_unpack_current_image "$TYPE_MAIN" "$IMAGE_MAIN" "$TARGET_OPT_MAIN" "$@"
}

prepare_ec_current_image() {
  crosfw_unpack_current_image "$TYPE_EC" "$IMAGE_EC" "$TARGET_OPT_EC" "$@"
}

is_write_protection_disabled() {
  if [ "${FLAGS_update_main}" = ${FLAGS_TRUE} ]; then
    is_mainfw_write_protected && return $FLAGS_FALSE || true
  fi

  if [ "${FLAGS_update_ec}" = ${FLAGS_TRUE} ]; then
    is_ecfw_write_protected && return $FLAGS_FALSE || true
  fi

  return $FLAGS_TRUE
}

clear_update_cookies() {
  # Always success because the system may not have crossystem ready yet if we're
  # trying to recover a broken firmware or after transition from legacy firmware
  ( cros_set_fwb_tries 0
    cros_set_startup_update_tries 0
    cros_set_prop recovery_request=0 ) >/dev/null 2>&1 ||
      debug_msg "clear_update_cookies: there were some errors, ignored."
}

silent_sh() {
  # Calls given commands and ignores any error (mostly for to factory
  # installation, when the firmware is still non-Chrome).
  ( "$@" ) >/dev/null 2>&1 ||
    debug_msg "Failed calling: $@"
}

enable_dev_boot() {
  cros_set_prop dev_boot_usb=1 dev_boot_signed_only=0
}

disable_dev_boot() {
  # The firmware will decide and reset default values of dev_boot_usb and
  # dev_boot_signed_only on reoot when user turned off developer switch (i.e.,
  # normal mode). It's safe to set dev_boot_usb to zero here, but
  # dev_boot_signed_only may expect different default values, so we leave it
  # untouched and let firmware decide.
  cros_set_prop dev_boot_usb=0
}

# ----------------------------------------------------------------------------
# Core logic in different modes

# Startup
mode_startup() {
  if [ "${FLAGS_update_ec}" = ${FLAGS_TRUE} ]; then
    if need_update_ec; then
      # EC image already prepared in need_update_ec
      if is_ecfw_write_protected; then
        crosfw_update_ec "$SLOT_EC_RW"
      else
        crosfw_update_ec
      fi
    fi
    cros_set_startup_update_tries 0
    cros_reboot
  else
    cros_set_startup_update_tries 0
  fi
}

# Update Engine - Received Update
mode_autoupdate() {
  # Quick check if we need to update
  local need_update=0
  if [ "${FLAGS_force}" = ${FLAGS_TRUE} ]; then
    need_update=1
  else
    # Check main firmware
    if [ "${FLAGS_update_main}" = ${FLAGS_TRUE} ]; then
      if need_update_main; then
        need_update=1
      else
        FLAGS_update_main=$FLAGS_FALSE
      fi
    fi
    # Check EC firmware
    if [ "${FLAGS_update_ec}" = ${FLAGS_TRUE} ]; then
      if [ "$TARGET_ECID" != "$ECID" ]; then
        need_update=1
      else
        FLAGS_update_ec=$FLAGS_FALSE
      fi
    fi
  fi

  if [ "$need_update" -eq 0 ]; then
    verbose_msg "Latest firmware already installed. No need to update."
    return
  fi

  if [ "${FLAGS_update_main}" = "${FLAGS_TRUE}" ]; then
    local mainfw_act="$(cros_get_prop mainfw_act)"
    if [ "$mainfw_act" = "B" ]; then
      # mainfw_act is only updated at system reboot; if two updates (both with
      # firmware updates) are pushed in a row, next update will be executed
      # while mainfw_act is still B. Since we don't use RW BIOS directly when
      # updater is running, it should be safe to update in this case.
      debug_msg "mainfw_act=B, checking if we can still update FW B..."
      prepare_main_current_image
      cros_compare_file "$DIR_CURRENT/$TYPE_MAIN/$SLOT_A" \
                        "$DIR_CURRENT/$TYPE_MAIN/$SLOT_B" &&
        alert "Installing updates while mainfw_act is B (should be safe)." ||
        die_need_reboot "Done (retry update next boot)"
    elif [ "$mainfw_act" != "A" ]; then
      die "autoupdate: unexpected active firmware ($mainfw_act)..."
    fi

    prepare_main_image
    prepare_main_current_image
    check_compatible_keys
    crosfw_update_main "$SLOT_B"
    cros_set_fwb_tries 6

    #Update legacy if needed
    if need_update_legacy; then
      crosfw_update_main "$SLOT_LEGACY"
    fi
  fi

  if [ "${FLAGS_update_ec}" = "${FLAGS_TRUE}" ]; then
    cros_set_startup_update_tries 6
    verbose_msg "Done (EC update will occur at Startup)"
  fi
}

# Recovery Installer
mode_recovery() {
  local prefix="mode_recovery"
  [ "${FLAGS_mode}" = "recovery" ] || prefix="${FLAGS_mode}(recovery)"
  if [ "${FLAGS_update_main}" = ${FLAGS_TRUE} ]; then
    if ! is_mainfw_write_protected; then
      verbose_msg "$prefix: update RO+RW"
      crosfw_preserve_vpd
      crosfw_preserve_gbb
      crosfw_update_main
    else
      # TODO(hungte) check if FMAP is not changed
      verbose_msg "$prefix: update main/RW:A,B,SHARED"
      prepare_main_image
      prepare_main_current_image
      check_compatible_keys
      crosfw_update_main "$SLOT_A"
      crosfw_update_main "$SLOT_B"
      crosfw_update_main "$SLOT_RW_SHARED"
    fi
  fi

  if [ "${FLAGS_update_ec}" = ${FLAGS_TRUE} ]; then
    if ! is_ecfw_write_protected; then
      verbose_msg "$prefix: update ec/RO+RW"
      crosfw_update_ec
    else
      prepare_ec_image
      verbose_msg "$prefix: update ec/RW"
      crosfw_update_ec "$SLOT_EC_RW"
    fi
  fi

  clear_update_cookies
}

# Factory Installer
mode_factory_install() {
  # Everything executed here must assume the system may be not using ChromeOS
  # firmware.
  is_write_protection_disabled ||
    die_need_ro_update "You need to first disable hardware write protection."

  if [ "${FLAGS_update_main}" = ${FLAGS_TRUE} ]; then
    # We may preserve bitmap here, just like recovery mode. However if there's
    # some issue (or incompatible stuff) found in bitmap, we will need a method
    # to update the bitmaps.
    crosfw_preserve_vpd || verbose_msg "Warning: cannot preserve VPD."
    crosfw_update_main
  fi
  if [ "${FLAGS_update_ec}" = ${FLAGS_TRUE} ]; then
    crosfw_update_ec
  fi
  cros_clear_nvdata
  silent_sh enable_dev_boot
  clear_update_cookies
}

# ----------------------------------------------------------------------------
# Main Entry

main() {
  cros_acquire_lock
  set_flags

  # factory compatibility
  if [ "${FLAGS_factory}" = ${FLAGS_TRUE} ] ||
     [ "${FLAGS_mode}" = "factory" ]; then
    FLAGS_mode=factory_install
  fi
  local ro_type=""
  cros_is_ro_normal_boot && ro_type="$ro_type[RO_NORMAL]"

  verbose_msg "Starting $TARGET_PLATFORM firmware updater v3 (${FLAGS_mode})..."
  verbose_msg " - Updater package: [$TARGET_FWID / $TARGET_ECID]"
  verbose_msg " - Current system:  [RO:$RO_FWID $ro_type, ACT:$FWID / $ECID]"

  if [ "${FLAGS_update_main}" = ${FLAGS_TRUE} -a -n "${HWID}" ]; then
    # always preserve HWID for current system, if available.
    crosfw_preserve_hwid
    debug_msg "preserved HWID as: $HWID."
  fi

  local wpmsg="$(cros_report_wp_status $FLAGS_update_main $FLAGS_update_ec)"
  verbose_msg " - Write protection: $wpmsg"

  # Check platform except in factory_install mode.
  if [ "${FLAGS_check_platform}" = ${FLAGS_TRUE} ] &&
     [ "${FLAGS_mode}" != "factory_install" ] &&
     ! cros_check_compatible_platform "${TARGET_PLATFORM}" "${PLATFORM}"; then
    alert_unknown_platform "$PLATFORM" "$TARGET_PLATFORM"
    exit 1
  fi

  # Since CL:798773, all additional files are in bin/.
  # TODO(hungte) Remove this hack when we have separated extra programs and data.
  if [ ! -r "${CUSTOMIZATION_SCRIPT}" ] && [ -r "bin/${CUSTOMIZATION_SCRIPT}" ]
  then
    CUSTOMIZATION_SCRIPT="bin/${CUSTOMIZATION_SCRIPT}"
  fi

  # load customization
  if [ -r "$CUSTOMIZATION_SCRIPT" ]; then
    debug_msg "loading customization..."
    . ./$CUSTOMIZATION_SCRIPT
    # invoke customization
    debug_msg "starting customized updater main..."
    $CUSTOMIZATION_MAIN
  fi

  case "${FLAGS_mode}" in
    autoupdate | recovery | startup | factory_install )
      mode_"${FLAGS_mode}"
      ;;

    "" )
      die "Please assign updater mode by --mode option."
      ;;

    * )
      die "Unknown mode: ${FLAGS_mode}"
      ;;
  esac
  verbose_msg "Firmware update (${FLAGS_mode}) completed."
}

# Parse command line
FLAGS "$@" || exit 1
eval set -- "$FLAGS_ARGV"

# Exit on error
set -e

# Main Entry
main
