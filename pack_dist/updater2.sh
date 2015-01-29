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
# - gbb_utility
# - crossystem
# All other special commands should be defined by a function in crosutil.sh.
#
# Temporary files should be named as "_*" to prevent confliction

# Updater for firmware v2 (split developer/normal)
# This is designed for platform Alex and ZGB.
# 1. May include both BIOS and EC firmware
# 2. RW firmware is either developer (slot A) or normal (slot B) mode.
# 3. Never perform updates in developer mode

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

# Customization script "RW compatible check" function.
# Overrides this with any function names to test if RW firmware in current
# system is compatible with this updater. The function must returns $FLAGS_FALSE
# if RW is not compatible (i.e., need incompatible_update mode)
CUSTOMIZATION_RW_COMPATIBLE_CHECK=""

# ----------------------------------------------------------------------------
# Constants

# Slot names defined by ChromeOS Firmware Specification
SLOT_A="RW_SECTION_A"
SLOT_B="RW_SECTION_B"
SLOT_RO="RO_SECTION"
SLOT_RW_SHARED="RW_SHARED"
SLOT_EC_RO="EC_RO"
SLOT_EC_RW="EC_RW"

FWSRC_NORMAL="$SLOT_B"
FWSRC_DEVELOPER="$SLOT_A"

# Folders for preparing and unpacking firmware
# Layout: DIR_[TYPE]/[TYPE]/[SECTION], DIR_[TYPE]/[IMAGE]
TYPE_MAIN="main"
TYPE_EC="ec"

# ----------------------------------------------------------------------------
# Global Variables

# Current system identifiers (may be empty if running on non-ChromeOS systems)
HWID="$(crossystem hwid 2>/dev/null)" || HWID=""
ECINFO="$(mosys -k ec info 2>/dev/null)" || ECINFO=""

# Compare following values with TARGET_FWID, TARGET_ECID, TARGET_PLATFORM
# (should be passed by wrapper as environment variables)
FWID="$(crossystem fwid 2>/dev/null)" || FWID=""
RO_FWID="$(crossystem ro_fwid 2>/dev/null)" || RO_FWID=""
ECID="$(eval "$ECINFO"; echo "$fw_version")"
PLATFORM="${FWID%%.*}"

# RO update flags are usually enabled only in customization.
FLAGS_update_ro_main="$FLAGS_FALSE"
FLAGS_update_ro_ec="$FLAGS_FALSE"

# ----------------------------------------------------------------------------
# Special Parameters

DEFINE_boolean check_devfw $FLAGS_TRUE \
  "Bypass firmware updates if active firmware type is developer" ""

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
}

need_update_ro() {
  # Maybe this needs an override on future systems, so make it an
  # extra function.
  # For now, just always assume true, and let flashrom figure out
  # whether an update is actually needed
  true
}

need_update_ec_ro() {
  # Maybe this needs an override on future systems, so make it an
  # extra function.
  # For now, just always assume true, and let flashrom figure out
  # whether an update is actually needed
  true
}

need_update_ec() {
  prepare_ec_image
  prepare_ec_current_image
  if [ "${FLAGS_update_ro_ec}" = "${FLAGS_TRUE}" ] &&
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

is_developer_firmware() {
  if [ "$FLAGS_check_devfw" = $FLAGS_FALSE ]; then
    verbose_msg "Warning: developer firmware checking is bypassed."
    return $FLAGS_FALSE
  fi
  [ "$(cros_get_prop mainfw_type)" = "developer" ]
}

clear_update_cookies() {
  # Always success because the system may not have crossystem ready yet if we're
  # trying to recover a broken firmware or after transition from legacy firmware
  ( cros_set_fwb_tries 0
    cros_set_startup_update_tries 0
    cros_set_prop recovery_request=0 ) >/dev/null 2>&1 ||
      debug_msg "clear_update_cookies: there were some errors, ignored."
}

# ----------------------------------------------------------------------------
# Core logic in different modes

# Startup
mode_startup() {
  if is_developer_firmware; then
    debug_msg "Developer firmware detected - bypass firmware updates."
    cros_set_startup_update_tries 0
    return
  fi
  # decreasing of the "startup_update_tries" should be done inside
  # chromeos_startup.
  if [ "${FLAGS_update_main}" = ${FLAGS_TRUE} ] &&
     [ "${FLAGS_update_ro_main}" = "${FLAGS_TRUE}" ]; then
    alert_write_protection
    prepare_main_image
    # We can allow updating multiple sections at once, but that may lead system
    # into unknown state if crashed during the large update, so let's update
    # step-by-step.
    crosfw_update_main "$SLOT_RO"
    if [ "$(cros_get_prop mainfw_type)" = "developer" ]; then
      crosfw_update_main "$SLOT_A" "$FWSRC_DEVELOPER"
    else
      crosfw_update_main "$SLOT_A" "$FWSRC_NORMAL"
    fi
    crosfw_update_main "$SLOT_B"
    crosfw_update_main "$SLOT_RW_SHARED"
  fi

  if [ "${FLAGS_update_ec}" = ${FLAGS_TRUE} ]; then
    prepare_ec_image
    if [ "${FLAGS_update_ro_ec}" = "${FLAGS_TRUE}" ]; then
      alert_write_protection
      crosfw_update_ec "$SLOT_EC_RO"
    fi
    crosfw_update_ec "$SLOT_EC_RW"
  fi

  cros_set_startup_update_tries 0
  cros_reboot
}

# Update Engine - Current Boot Successful (chromeos_setgoodkernel)
mode_bootok() {
  if is_developer_firmware; then
    debug_msg "Developer firmware detected - bypass firmware updates."
    cros_set_fwb_tries 0
    return
  fi
  # TODO(hungte) check if WP disabled and RO_FWID does not match embedded
  # firmware

  if [ "$(cros_get_prop ecfw_act)" = "RO" ]; then
    verbose_msg "EC was boot by RO and may need an update/recovery."
    if [ "${FLAGS_update_ec}" = "${FLAGS_TRUE}" ]; then
      cros_set_startup_update_tries 6
    else
      debug_msg "Although EC was boot from RO, updating for EC is disabled."
    fi
  fi

  if [ "${FLAGS_update_main}" = "${FLAGS_TRUE}" ]; then
    local mainfw_act="$(cros_get_prop mainfw_act)"
    # Copy firmware to the spare slot.
    # flashrom will check if we really need to update the bits
    if [ "$mainfw_act" = "A" ]; then
      crosfw_dup2_mainfw "$SLOT_A" "$SLOT_B"
    elif [ "$mainfw_act" = "B" ]; then
      crosfw_dup2_mainfw "$SLOT_B" "$SLOT_A"
    else
      die "bootok: unexpected active firmware ($mainfw_act)..."
    fi
  fi
  cros_set_fwb_tries 0
  # TODO(hungte) signal updater to request a reboot from user?
}

# Update Engine - Received Update
mode_autoupdate() {
  # Only RW updates in main firmware is allowed.
  # RO updates and EC updates requires a reboot and fires in startup.
  if is_developer_firmware; then
    verbose_msg "Developer firmware detected - bypass firmware updates."
    return
  fi

  # Quick check if we need to update
  local need_update=0
  if [ "${FLAGS_force}" != "${FLAGS_TRUE}" ]; then
    if [ "${FLAGS_update_main}" = "${FLAGS_TRUE}" ] &&
       [ "$TARGET_FWID" != "$FWID" ]; then
        verbose_msg "System firmware update available: [$TARGET_FWID]"
        verbose_msg "Currently installed system firmware: [$FWID]"
        need_update=1
    else
      FLAGS_update_main=$FLAGS_FALSE
    fi
    if [ "${FLAGS_update_ec}" = "${FLAGS_TRUE}" ] &&
       [ "$TARGET_ECID" != "$ECID" ]; then
        verbose_msg "EC firmware update available: [$TARGET_ECID]"
        verbose_msg "Currently installed EC firmware: [$ECID]"
        need_update=1
    else
      FLAGS_update_ec=$FLAGS_FALSE
    fi
    if [ "$need_update" -eq 0 ]; then
      verbose_msg "No firmware auto update is available. Returning gracefully."
      return
    fi
  fi

  if [ "${FLAGS_update_main}" = "${FLAGS_TRUE}" ]; then
    if [ "${FLAGS_update_ro_main}" = "${FLAGS_TRUE}" ] && need_update_ro ; then
      # TODO(hungte) complete need_update_ro to verify if RO section really has
      # some changes, byt bit-wise compare. However, since RO updating is not a
      # normal case, this is not a high priority task.
      cros_set_startup_update_tries 6
      verbose_msg "Done (update will occur at Startup)"
      return
    fi
    local mainfw_act="$(cros_get_prop mainfw_act)"
    if [ "$mainfw_act" = "B" ]; then
      die_need_reboot "Done (retry update next boot)"
    elif [ "$mainfw_act" != "A" ]; then
      die "autoupdate: unexpected active firmware ($mainfw_act)..."
    fi
    local fwsrc="$FWSRC_NORMAL"
    if [ "$(cros_get_prop mainfw_type)" = "developer" ]; then
      fwsrc="$FWSRC_DEV"
    fi
    prepare_main_image
    prepare_main_current_image
    check_compatible_keys
    crosfw_update_main "$SLOT_B" "$fwsrc"
    cros_set_fwb_tries 6
  fi

  if [ "${FLAGS_update_ec}" = "${FLAGS_TRUE}" ] && need_update_ec; then
    cros_set_startup_update_tries 6
    verbose_msg "Done (EC update will occur at Startup)"
    return
  fi
}

# Transition to Developer Mode
mode_todev() {
  crossystem dev_boot_usb=1 || true
  if [ "${FLAGS_update_main}" != "${FLAGS_TRUE}" ]; then
    die "Cannot switch to developer mode due to missing main firmware"
  fi
  if [ "${FLAGS_force}" != "${FLAGS_TRUE}" ] &&
     [ "$(cros_get_fwb_tries)" != "0" ]; then
    die "
    It seems a firmware autoupdate is in progress.
    Re-run with --force to proceed with developer firmware transition.
    Or you can reboot and retry, in which case you should get updated
    developer firmware."
  fi

  # Make sure no auto updates come in our way. Sometimes the update-engine is
  # already stopped so we must ignore the return value.
  initctl stop update-engine || true

  prepare_main_image
  prepare_main_current_image
  check_compatible_keys
  crosfw_update_main "$SLOT_A" "$FWSRC_DEVELOPER"

  # Make sure we run developer firmware on next reboot.
  clear_update_cookies
  cros_reboot
}

# Transition to Normal Mode
mode_tonormal() {
  crossystem dev_boot_usb=0 || true
  if [ "${FLAGS_update_main}" != "${FLAGS_TRUE}" ]; then
    die "Cannot switch to normal mode due to missing main firmware"
  fi
  prepare_main_image
  prepare_main_current_image
  check_compatible_keys
  crosfw_update_main "$SLOT_A" "$FWSRC_NORMAL"
  clear_update_cookies
  cros_reboot
}

# Recovery Installer
mode_recovery() {
  if [ "${FLAGS_update_main}" = "${FLAGS_TRUE}" ]; then
    if ! is_mainfw_write_protected; then
      verbose_msg "mode_recovery: update RO+RW"
      crosfw_preserve_vpd
      prepare_main_image
      crosfw_preserve_bmpfv
      # HWID should be already preserved
      crosfw_update_main
      if ! is_developer_firmware; then
        crosfw_update_main "$SLOT_A" "$FWSRC_NORMAL"
      fi
    else
      verbose_msg "mode_recovery: update main/RW:A,B,SHARED"
      prepare_main_image
      prepare_main_current_image
      check_compatible_keys
      if is_developer_firmware; then
        crosfw_update_main "$SLOT_A" "$FWSRC_DEVELOPER"
      else
        crosfw_update_main "$SLOT_A" "$FWSRC_NORMAL"
      fi
      crosfw_update_main "$SLOT_B" "$FWSRC_NORMAL"
      crosfw_update_main "$SLOT_RW_SHARED"
    fi
  fi

  if [ "${FLAGS_update_ec}" = "${FLAGS_TRUE}" ]; then
    prepare_ec_image
    if ! is_ecfw_write_protected; then
      verbose_msg "mode_recovery: update ec/RO+RW"
      crosfw_update_ec
    else
      verbose_msg "mode_recovery: update ec/RW"
      crosfw_update_ec "$SLOT_EC_RW"
    fi
  fi

  clear_update_cookies
}

# Factory Installer
mode_factory_install() {
  if is_mainfw_write_protected || is_ecfw_write_protected; then
    # TODO(hungte) check if we really need to stop user by comparing firmware
    # image, bit-by-bit.
    die_need_ro_update "You need to first disable hardware write protection."
  fi
  if [ "${FLAGS_update_main}" = "${FLAGS_TRUE}" ]; then
    crosfw_preserve_vpd
    crosfw_update_main
  fi
  if [ "${FLAGS_update_ec}" = "${FLAGS_TRUE}" ]; then
    crosfw_update_ec
  fi
  cros_clear_nvdata
  clear_update_cookies
}

# Factory Wipe
mode_factory_final() {
  # To prevent theat factory has installed a more recent version of firmware,
  # don't use the firmware from bundled image. Use the one from current system.
  crosfw_dup2_mainfw "$SLOT_B" "$SLOT_A"
  crossystem dev_boot_usb=0 || true
  clear_update_cookies
}

# Updates for incompatible RW firmware (need to update RO)
mode_incompatible_update() {
  if is_mainfw_write_protected || is_ecfw_write_protected; then
    # TODO(hungte) check if we really need to stop user by comparing
    # firmware image, bit-by-bit.
    die_need_ro_update "You need to first disable hardware write protection."
  fi
  FLAGS_update_ro_main=$FLAGS_TRUE
  FLAGS_update_ro_ec=$FLAGS_TRUE
  mode_recovery

  # incompatible_update may be redirected from a "startup" update, which expects
  # a reboot after update complete.
  if [ "${FLAGS_mode}" = "startup" ]; then
    cros_reboot
  fi
}

mode_fast_version_check() {
  alert "Not implemented."
  true
}

# ----------------------------------------------------------------------------
# Main Entry

main_check_rw_compatible() {
  local try_autoupdate="$1"
  if [ "${FLAGS_check_rw_compatible}" = "${FLAGS_FALSE}" ]; then
    verbose_msg "Bypassed RW compatbility check. You're on your own."
    return $FLAGS_TRUE
  fi
  local is_compatible="${FLAGS_TRUE}"

  # Try explicit match
  if [ -n "$CUSTOMIZATION_RW_COMPATIBLE_CHECK" ]; then
    debug_msg "Checking customized RW compatibility..."
    "$CUSTOMIZATION_RW_COMPATIBLE_CHECK" || is_compatible="${FLAGS_FALSE}"
  elif [ "$is_compatible" = "${FLAGS_TRUE}" ]; then
    cros_check_stable_firmware || is_compatible="${FLAGS_FALSE}"
  fi

  if [ "$is_compatible" = "${FLAGS_TRUE}" ]; then
    return $FLAGS_TRUE
  fi

  verbose_msg "RW firmware update is not compatible with current RO firmware."
  verbose_msg "Need to update RO (RW incompatible mode update)."

  if [ "$try_autoupdate" = "$FLAGS_FALSE" ]; then
    # No need to print anything in this case.
    return $FLAGS_FALSE
  fi

  if is_developer_firmware; then
    try_autoupdate=$FLAGS_FALSE
    verbose_msg "Developer firmware detected - not scheduling auto updates."
  else
    # Try to schedule an autoupdate.
    (cros_set_startup_update_tries 6) || try_autoupdate="${FLAGS_FALSE}"
  fi

  alert_incompatible_firmware "$try_autoupdate"
  return $FLAGS_FALSE
}

main() {
  cros_acquire_lock
  set_flags

  # factory compatibility
  if [ "${FLAGS_factory}" = "${FLAGS_TRUE}" ] ||
     [ "${FLAGS_mode}" = "factory" ]; then
    FLAGS_mode=factory_install
  fi

  verbose_msg "Starting $TARGET_PLATFORM firmware updater v2 (${FLAGS_mode})..."

  if [ "${FLAGS_update_main}" = ${FLAGS_TRUE} -a -n "${HWID}" ]; then
    # always preserve HWID for current system, if available.
    crosfw_preserve_hwid
    debug_msg "preserved HWID as: $HWID."
  fi

  # Check platform except in factory_install mode.
  if [ "${FLAGS_check_platform}" = "${FLAGS_TRUE}" ] &&
     [ "${FLAGS_mode}" != "factory_install" ] &&
     [ -n "$TARGET_PLATFORM" ] &&
     [ "$PLATFORM" != "$TARGET_PLATFORM" ]; then
    alert_unknown_platform "$PLATFORM" "$TARGET_PLATFORM"
    exit 1
  fi

  # load customization
  if [ -r "$CUSTOMIZATION_SCRIPT" ]; then
    debug_msg "loading customization..."
    . ./$CUSTOMIZATION_SCRIPT
    # invoke customization
    debug_msg "starting customized updater main..."
    $CUSTOMIZATION_MAIN
  fi

  verbose_msg " - Updater package: [$TARGET_FWID / $TARGET_ECID]"
  verbose_msg " - Current system:  [RO:$RO_FWID, ACT:$FWID / $ECID]"

  local wpmsg="$(cros_report_wp_status $FLAGS_update_main $FLAGS_update_ec)"
  verbose_msg " - Write protection: $wpmsg"

  case "${FLAGS_mode}" in
    # Modes which can attempt to update RO if RO+RW are not compatible.
    startup | recovery )
      debug_msg "mode allowing compatibility update: ${FLAGS_mode}"
      if main_check_rw_compatible $FLAGS_FALSE; then
        mode_"${FLAGS_mode}"
      else
        verbose_msg "Starting a RW incompatible mode update..."
        mode_incompatible_update
      fi
      ;;

    # Modes which update RW firmware only; these need to verify if existing RO
    # firmware is compatible.  If not, schedule a RO+RW update at next startup.
    autoupdate | todev | tonormal )
      debug_msg "mode with compatibility check: ${FLAGS_mode}"
      if main_check_rw_compatible $FLAGS_TRUE; then
        mode_"${FLAGS_mode}"
      fi
      ;;

    # Modes which don't mix existing RO firmware with new RW firmware from the
    # updater.  They either copy RW firmware between EEPROM slots, or copy both
    # RO+RW from the shellball.  Either way, RO+RW compatibility is assured.
    bootok | factory_install | factory_final | incompatible_update )
      debug_msg "mode without incompatible checks: ${FLAGS_mode}"
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
