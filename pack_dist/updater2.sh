#!/bin/sh
#
# Copyright (c) 2011 The Chromium OS Authors. All rights reserved.
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
IMAGE_MAIN="bios.bin"
IMAGE_EC="ec.bin"

# ----------------------------------------------------------------------------
# Global Variables

# Current system identifiers (may be empty if running on non-ChromeOS systems)
HWID="$(crossystem hwid 2>/dev/null)" || HWID=""
ECINFO="$(mosys -k ec info 2>/dev/null)" || ECINFO=""

# Compare following values with TARGET_FWID, TARGET_ECID, TARGET_PLATFORM
# (should be passed by wrapper as environment variables)
FWID="$(crossystem fwid 2>/dev/null)" || FWID=""
ECID="$(eval "$ECINFO"; echo "$fw_version")"
PLATFORM="$(mosys platform name 2>/dev/null)" || PLATFORM=""

# RO update flags are usually enabled only in customization.
FLAGS_update_ro_main="$FLAGS_FALSE"
FLAGS_update_ro_ec="$FLAGS_FALSE"

# TARGET_UNSTABLE is non-zero if the firmware is not stable.
: ${TARGET_UNSTABLE:=}

# ----------------------------------------------------------------------------
# Parameters

DEFINE_string mode "" \
 "Updater mode ( startup | bootok | autoupdate | todev | tonormal |"\
" recovery | factory_install | factory_final | incompatible_update )" "m"
DEFINE_boolean debug $FLAGS_FALSE "Enable debug messages." "d"
DEFINE_boolean verbose $FLAGS_TRUE "Enable verbose messages." "v"
DEFINE_boolean dry_run $FLAGS_FALSE "Enable dry-run mode." ""
DEFINE_boolean force $FLAGS_FALSE "Try to force update." ""
DEFINE_boolean allow_reboot $FLAGS_TRUE \
  "Allow rebooting system immediately if required."

DEFINE_boolean update_ec $FLAGS_TRUE "Enable updating for Embedded Firmware." ""
DEFINE_boolean update_main $FLAGS_TRUE "Enable updating for Main Firmware." ""

DEFINE_boolean check_keys $FLAGS_TRUE "Check firmware keys before updating." ""
DEFINE_boolean check_wp $FLAGS_TRUE \
  "Check if write protection is enabled before updating RO sections" ""
DEFINE_boolean check_rw_compatible $FLAGS_TRUE \
  "Check if RW firmware is compatible with current RO" ""
DEFINE_boolean check_devfw $FLAGS_TRUE \
  "Bypass firmware updates if active firmware type is developer" ""
DEFINE_boolean check_platform $FLAGS_TRUE \
  "Bypass firmware updates if the system platform name is different" ""
# Required for factory compatibility
DEFINE_boolean factory $FLAGS_FALSE "Equivalent to --mode=factory_install"

# ----------------------------------------------------------------------------
# General Updater

# Update Main Firmware (BIOS, SPI)
update_mainfw() {
  # Syntax: update_mainfw SLOT FIRMWARE_SOURCE_TYPE
  #    Write assigned type (normal/developer) of firmware source into
  #    assigned slot (returns directly if the target is already filled
  #    with correct data)
  # Syntax: update_mainfw SLOT
  #    Write assigned slot from MAIN_TARGET_IMAGE.
  # Syntax: update_mainfw
  #    Write complete MAIN_TARGET_IMAGE

  local slot="$1"
  local source_type="$2"
  debug_msg "invoking: update_mainfw($@)"
  # TODO(hungte) verify if slot is valid.
  [ -s "$IMAGE_MAIN" ] || die "missing firmware image: $IMAGE_MAIN"
  if [ "$slot" = "" ]; then
    invoke "flashrom $TARGET_OPT_MAIN $WRITE_OPT -w $IMAGE_MAIN"
  elif [ "$source_type" = "" ]; then
    invoke "flashrom $TARGET_OPT_MAIN $WRITE_OPT -w $IMAGE_MAIN -i $slot"
  else
    local section_file="$DIR_TARGET/$TYPE_MAIN/$source_type"
    [ -s "$section_file" ] || die "update_mainfw: missing $section_file"
    slot="$slot:$section_file"
    invoke "flashrom $TARGET_OPT_MAIN $WRITE_OPT -w $IMAGE_MAIN -i $slot"
  fi
}

dup2_mainfw() {
  # Syntax: dup2_mainfw SLOT_FROM SLOT_TO
  #    Duplicates a slot to another place on system live firmware
  local slot_from="$1" slot_to="$2"
  # Writing in flashrom, even if there's nothing to write, is still much slower
  # than reading because it needs to read + verify whole image. So we should
  # only write firmware on if there's something changed.
  local temp_from="_dup2_temp_from"
  local temp_to="_dup2_temp_to"
  local temp_image="_dup2_temp_image"
  debug_msg "invoking: dup2_mainfw($@)"
  local opt_read_slots="-i $slot_from:$temp_from -i $slot_to:$temp_to"
  invoke "flashrom $TARGET_OPT_MAIN $opt_read_slots -r $temp_image"
  if [ -s "$temp_from" ] && cros_compare_file "$temp_from" "$temp_to"; then
    debug_msg "dup2_mainfw: slot content is the same, no need to copy."
  else
    slot="$slot_to:$temp_from"
    invoke "flashrom $TARGET_OPT_MAIN $WRITE_OPT -w $temp_image -i $slot"
  fi
}

# Update EC Firmware (LPC)
update_ecfw() {
  local slot="$1"
  debug_msg "invoking: update_ecfw($@)"
  # Syntax: update_mainfw SLOT
  #    Update assigned slot with proper firmware.
  # Syntax: update_mainfw
  #    Write complete MAIN_TARGET_IMAGE
  # TODO(hungte) verify if slot is valid.
  [ -s "$IMAGE_EC" ] || die "missing firmware image: $IMAGE_EC"
  if [ -n "$slot" ]; then
    invoke "flashrom $TARGET_OPT_EC $WRITE_OPT -w $IMAGE_EC -i $slot"
  else
    invoke "flashrom $TARGET_OPT_EC $WRITE_OPT -w $IMAGE_EC"
  fi
}

# ----------------------------------------------------------------------------
# Helper functions

# Note this will change $IMAGE_MAIN so any processing to the file (ex,
# prepare_main_image) must be invoked AFTER this call.
preserve_vpd() {
  crosfw_dupe_vpd "RO_VPD RW_VPD" "$IMAGE_MAIN" ""
}

preserve_hwid() {
  [ -s "$IMAGE_MAIN" ] || die "preserve_hwid: no main firmware."
  silent_invoke "gbb_utility -s --hwid='$HWID' $IMAGE_MAIN"
}

preserve_bmpfv() {
  if [ -z "$HWID" ]; then
    debug_msg "preserve_bmpfv: Running on non-ChromeOS firmware system. Skip."
    return
  fi
  debug_msg "Preseving main firmware images..."
  [ -s "$IMAGE_MAIN" ] || die "preserve_bmpfv: no main firmware."
  silent_invoke "flashrom $TARGET_OPT_MAIN -i GBB:_gbb.bin -r _temp.rom"
  silent_invoke "gbb_utility -g --bmpfv=_bmpfv.bin _gbb.bin"
  [ -s "_bmpfv.bin" ] || die "preserve_bmpfv: invalid bmpfv"
  silent_invoke "gbb_utility -s --bmpfv=_bmpfv.bin $IMAGE_MAIN"
}

# Compares two slots from current and target folder.
is_equal_slot() {
  check_param "is_equal_slot(type, slot)" "$@"
  local type_name="$1" slot_name="$2"
  local current="$DIR_CURRENT/$type_name/$slot_name"
  local target="$DIR_TARGET/$type_name/$slot_name"
  cros_compare_file "$current" "$target"
}

# Verifies if current system is installed with compatible rootkeys
check_compatible_keys() {
  local current_image="$DIR_CURRENT/$IMAGE_MAIN"
  local target_image="$DIR_TARGET/$IMAGE_MAIN"
  if [ "${FLAGS_check_keys}" = "${FLAGS_FALSE}" ]; then
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
     ! is_equal_slot "$TYPE_EC" "$SLOT_EC_RO"; then
      debug_msg "EC RO needs update."
      return $FLAGS_TRUE
  fi
  if ! is_equal_slot "$TYPE_EC" "$SLOT_EC_RW"; then
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

is_mainfw_write_protected() {
  if [ "$FLAGS_check_wp" = $FLAGS_FALSE ]; then
    verbose_msg "Warning: write protection checking is bypassed."
    return $FLAGS_FALSE
  fi
  if ! cros_is_hardware_write_protected; then
    false
  else
    flashrom $TARGET_OPT_MAIN --wp-status 2>/dev/null |
      grep -q "write protect is enabled"
  fi
}

is_ecfw_write_protected() {
  if [ "$FLAGS_check_wp" = $FLAGS_FALSE ]; then
    verbose_msg "Warning: write protection checking is bypassed."
    return $FLAGS_FALSE
  fi
  if ! cros_is_hardware_write_protected; then
    false
  else
    flashrom $TARGET_OPT_EC --wp-status 2>/dev/null |
      grep -q "write protect is enabled"
  fi
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
    update_mainfw "$SLOT_RO"
    if [ "$(cros_get_prop mainfw_type)" = "developer" ]; then
      update_mainfw "$SLOT_A" "$FWSRC_DEVELOPER"
    else
      update_mainfw "$SLOT_A" "$FWSRC_NORMAL"
    fi
    update_mainfw "$SLOT_B"
    update_mainfw "$SLOT_RW_SHARED"
  fi

  if [ "${FLAGS_update_ec}" = ${FLAGS_TRUE} ]; then
    prepare_ec_image
    if [ "${FLAGS_update_ro_ec}" = "${FLAGS_TRUE}" ]; then
      alert_write_protection
      update_ecfw "$SLOT_EC_RO"
    fi
    update_ecfw "$SLOT_EC_RW"
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
  # TODO(hungte) check if WP disabled and FRID does not match embedded firmware

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
      dup2_mainfw "$SLOT_A" "$SLOT_B"
    elif [ "$mainfw_act" = "B" ]; then
      dup2_mainfw "$SLOT_B" "$SLOT_A"
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
    update_mainfw "$SLOT_B" "$fwsrc"
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
  update_mainfw "$SLOT_A" "$FWSRC_DEVELOPER"

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
  update_mainfw "$SLOT_A" "$FWSRC_NORMAL"
  clear_update_cookies
  cros_reboot
}

# Recovery Installer
mode_recovery() {
  if [ "${FLAGS_update_main}" = "${FLAGS_TRUE}" ]; then
    if ! is_mainfw_write_protected; then
      verbose_msg "mode_recovery: update RO+RW"
      preserve_vpd
      prepare_main_image
      preserve_bmpfv
      # HWID should be already preserved
      update_mainfw
      if ! is_developer_firmware; then
        update_mainfw "$SLOT_A" "$FWSRC_NORMAL"
      fi
    else
      verbose_msg "mode_recovery: update main/RW:A,B,SHARED"
      prepare_main_image
      prepare_main_current_image
      check_compatible_keys
      if is_developer_firmware; then
        update_mainfw "$SLOT_A" "$FWSRC_DEVELOPER"
      else
        update_mainfw "$SLOT_A" "$FWSRC_NORMAL"
      fi
      update_mainfw "$SLOT_B" "$FWSRC_NORMAL"
      update_mainfw "$SLOT_RW_SHARED"
    fi
  fi

  if [ "${FLAGS_update_ec}" = "${FLAGS_TRUE}" ]; then
    prepare_ec_image
    if ! is_ecfw_write_protected; then
      verbose_msg "mode_recovery: update ec/RO+RW"
      update_ecfw
    else
      verbose_msg "mode_recovery: update ec/RW"
      update_ecfw "$SLOT_EC_RW"
    fi
  fi

  clear_update_cookies
}

# Factory Installer
mode_factory_install() {
  if is_mainfw_write_protected || is_ecfw_write_protected; then
    # TODO(hungte) check if we really need to stop user by comparing firmware
    # image, bit-by-bit.
    die "You need to first disable hardware write protection switch."
  fi
  if [ "${FLAGS_update_main}" = "${FLAGS_TRUE}" ]; then
    preserve_vpd
    update_mainfw
  fi
  if [ "${FLAGS_update_ec}" = "${FLAGS_TRUE}" ]; then
    update_ecfw
  fi
  cros_clear_nvdata
  clear_update_cookies
}

# Factory Wipe
mode_factory_final() {
  # To prevent theat factory has installed a more recent version of firmware,
  # don't use the firmware from bundled image. Use the one from current system.
  dup2_mainfw "$SLOT_B" "$SLOT_A"
  crossystem dev_boot_usb=0 || true
  clear_update_cookies
}

# Updates for incompatible RW firmware (need to update RO)
mode_incompatible_update() {
  if is_mainfw_write_protected || is_ecfw_write_protected; then
    # TODO(hungte) check if we really need to stop user by comparing
    # firmware image, bit-by-bit.
    die "You need to first disable hardware write protection switch."
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

# ----------------------------------------------------------------------------
# Main Entry

main_check_rw_compatible() {
  local try_autoupdate="$1"
  if [ "${FLAGS_check_rw_compatible}" = "${FLAGS_FALSE}" ]; then
    verbose_msg "Bypassed RW compatbility check. You're on your own."
    return $FLAGS_TRUE
  fi
  local is_compatible="${FLAGS_TRUE}"

  if [ -n "${TARGET_UNSTABLE}" ]; then
    debug_msg "Current image is tagged as UNSTABLE."
    if [ "${FLAGS_update_main}" = ${FLAGS_TRUE} ] &&
       [ "$FWID" != "$TARGET_FWID" ]; then
      debug_msg "Incompatible: $FWID != $TARGET_FWID".
      is_compatible="${FLAGS_FALSE}"
    fi
    if [ "${FLAGS_update_ec}" = ${FLAGS_TRUE} ] &&
       [ "$ECID" != "$TARGET_ECID" ]; then
      debug_msg "Incompatible: $ECID != $TARGET_ECID".
      is_compatible="${FLAGS_FALSE}"
    fi
  fi

  if [ "$is_compatible" = "${FLAGS_TRUE}" ]; then
    if [ -z "$CUSTOMIZATION_RW_COMPATIBLE_CHECK" ]; then
      debug_msg "No compatibility check rules defined in customization."
      return $FLAGS_TRUE
    fi
    debug_msg "Checking customized RW compatibility..."
    "$CUSTOMIZATION_RW_COMPATIBLE_CHECK" || is_compatible="${FLAGS_FALSE}"
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

LOCK_FILE="/tmp/chromeos-firmwareupdate-running"

drop_lock() {
  rm -f "$LOCK_FILE"
}

acquire_lock() {
  if [ -r "$LOCK_FILE" ]; then
    die "Firmware Updater already running ($LOCK_FILE). Please retry later."
  fi
  touch "$LOCK_FILE"
  # Clean up on regular or error exits.
  trap drop_lock EXIT
}

main() {
  acquire_lock

  # factory compatibility
  if [ "${FLAGS_factory}" = "${FLAGS_TRUE}" ] ||
     [ "${FLAGS_mode}" = "factory" ]; then
    FLAGS_mode=factory_install
  fi

  verbose_msg "Starting $TARGET_PLATFORM firmware updater v2 (${FLAGS_mode})..."

  # quick check and setup for basic envoronments
  if [ ! -s "$IMAGE_MAIN" ]; then
    FLAGS_update_main=${FLAGS_FALSE}
    verbose_msg "No main firmware bundled in updater, ignored."
  elif [ -n "$HWID" ]; then
    # always preserve HWID for current system, if available.
    preserve_hwid
    debug_msg "preserved HWID as: $HWID."
  fi
  if [ ! -s "$IMAGE_EC" ]; then
    FLAGS_update_ec=${FLAGS_FALSE}
    debug_msg "No EC firmware bundled in updater, ignored."
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
  verbose_msg " - Current system:  [$FWID / $ECID]"

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
