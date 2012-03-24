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

# Updater for firmware v3 (two-stop)
# This is designed for x86/arm platform, with following assumption:
# 1. Perform updates even if active firmware is "developer mode"
# 2. Sleep/suspend (S3) after RO update may fail system (due to ACPI/ASL code
#    dependency), but that only happens on internal developer's dogfood devices

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

DEFINE_boolean update_ec $FLAGS_TRUE "Enable updating Embedded Firmware." ""
DEFINE_boolean update_main $FLAGS_TRUE "Enable updating Main Firmware." ""

DEFINE_boolean check_keys $FLAGS_TRUE "Check firmware keys before updating." ""
DEFINE_boolean check_wp $FLAGS_TRUE \
  "Check if write protection is enabled before updating RO sections" ""
DEFINE_boolean check_rw_compatible $FLAGS_TRUE \
  "Check if RW firmware is compatible with current RO" ""
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

preserve_gbb() {
  if [ -z "$HWID" ]; then
    debug_msg "preserve_gbb: Running on non-ChromeOS firmware system. Skip."
    return
  fi
  debug_msg "Preseving main firmware GBB data..."
  [ -s "$IMAGE_MAIN" ] || die "preserve_gbb: no main firmware."
  silent_invoke "flashrom $TARGET_OPT_MAIN -i GBB:_gbb.bin -r _temp.rom"
  # Preseves flags (--flags output format: "flags: 0x0000001")
  local flags="$(gbb_utility -g --flags _gbb.bin 2>/dev/null |
                 sed -nr 's/^flags: ([x0-9]+)/\1/p')"
  debug_msg "Current firmware flags: $flags"
  if [ -n "$flags" ]; then
    silent_invoke "gbb_utility -s --flags=$((flags)) $IMAGE_MAIN"
  fi
}

# Compares two slots from current and target folder.
is_equal_slot() {
  check_param "is_equal_slot(type, slot, ...)" "$@"
  local type_name="$1" slot_name="$2" slot2_name="$3"
  [ "$#" -lt 4 ] || die "is_equal_slot: internal error"
  [ -n "$slot2_name" ] || slot2_name="$slot_name"
  local current="$DIR_CURRENT/$type_name/$slot_name"
  local target="$DIR_TARGET/$type_name/$slot2_name"
  cros_compare_file "$current" "$target"
}

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

need_update_main_vblock() {
  # Check if VBLOCK (key version and firmware signature) is different.
  prepare_main_image
  prepare_main_current_image

  # Compare VBLOCK from current A slot and target B slot (normal firmware).
  ! is_equal_slot "$TYPE_MAIN" "VBLOCK_A" "VBLOCK_B"
}

need_update_ec() {
  prepare_ec_image
  prepare_ec_current_image
  if ! is_ecfw_write_protected && ! is_equal_slot "$TYPE_EC" "$SLOT_EC_RO"; then
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
    false
  elif ! cros_is_hardware_write_protected; then
    false
  else
    flashrom $TARGET_OPT_MAIN --wp-status 2>/dev/null |
      grep -q "write protect is enabled"
  fi
}

is_ecfw_write_protected() {
  if [ "$FLAGS_check_wp" = $FLAGS_FALSE ]; then
    verbose_msg "Warning: write protection checking is bypassed."
    false
  elif ! cros_is_hardware_write_protected; then
    false
  else
    flashrom $TARGET_OPT_EC --wp-status 2>/dev/null |
      grep -q "write protect is enabled"
  fi
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
  # installation, when the firmwaere is still non-Chrome).
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
      is_ecfw_write_protected || update_ecfw "$SLOT_EC_RO"
      update_ecfw "$SLOT_EC_RW"
    fi
    cros_set_startup_update_tries 0
    cros_reboot
  else
    cros_set_startup_update_tries 0
  fi
}

# Update Engine - Current Boot Successful (chromeos_setgoodkernel)
mode_bootok() {
  local mainfw_act="$(cros_get_prop mainfw_act)"
  # Copy main firmware to the spare slot.
  if [ "$mainfw_act" = "A" ]; then
    dup2_mainfw "$SLOT_A" "$SLOT_B"
  elif [ "$mainfw_act" = "B" ]; then
    dup2_mainfw "$SLOT_B" "$SLOT_A"
  else
    # Recovery mode, or non-chrome.
    die "bootok: abnormal active firmware ($mainfw_act)..."
  fi
  cros_set_fwb_tries 0

  if [ "${FLAGS_update_ec}" = ${FLAGS_TRUE} ] &&
     [ "$(cros_get_prop ecfw_act)" = "RO" ]; then
    verbose_msg "EC was boot by RO and may need an update/recovery."
    cros_set_startup_update_tries 6
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
      if [ "$TARGET_FWID" != "$FWID" ] || need_update_main_vblock; then
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
      die_need_reboot "Done (retry update next boot)"
    elif [ "$mainfw_act" != "A" ]; then
      die "autoupdate: unexpected active firmware ($mainfw_act)..."
    fi

    prepare_main_image
    prepare_main_current_image
    check_compatible_keys
    update_mainfw "$SLOT_B" "$FWSRC_NORMAL"
    cros_set_fwb_tries 6
  fi

  # Don't call need_update_ec because it will freeze the keyboard.
  if [ "${FLAGS_update_ec}" = "${FLAGS_TRUE}" ]; then
    cros_set_startup_update_tries 6
    verbose_msg "Done (EC update will occur at Startup)"
  fi
}

# Transition to Developer Mode
mode_todev() {
  enable_dev_boot
  echo "
  Booting any self-signed kernel from SSD/USB/SDCard slot is enabled.
  Insert bootable media into USB / SDCard slot and press Ctrl-U in developer
  screen to boot your own image.
  "
  clear_update_cookies
}

# Transition to Normal Mode
mode_tonormal() {
  # This is optional because whenever you turn off developer switch, the
  # dev_boot_usb is also cleared by firmware.
  disable_dev_boot
  echo "Booting from USB device is disabled."
  clear_update_cookies
}

# Recovery Installer
mode_recovery() {
  # TODO(hungte) Add flags to control RO updating, not is_*_write_protected.

  local prefix="mode_recovery"
  [ "${FLAGS_mode}" = "recovery" ] || prefix="${FLAGS_mode}(recovery)"
  if [ "${FLAGS_update_main}" = ${FLAGS_TRUE} ]; then
    if ! is_mainfw_write_protected; then
      verbose_msg "$prefix: update RO+RW"
      preserve_vpd
      preserve_gbb
      update_mainfw
    else
      # TODO(hungte) check if FMAP is not changed
      verbose_msg "$prefix: update main/RW:A,B,SHARED"
      prepare_main_image
      prepare_main_current_image
      check_compatible_keys
      update_mainfw "$SLOT_A" "$FWSRC_NORMAL"
      update_mainfw "$SLOT_B" "$FWSRC_NORMAL"
      update_mainfw "$SLOT_RW_SHARED"
    fi
  fi

  if [ "${FLAGS_update_ec}" = ${FLAGS_TRUE} ]; then
    prepare_ec_image
    if ! is_ecfw_write_protected; then
      verbose_msg "$prefix: update ec/RO+RW"
      update_ecfw
    else
      verbose_msg "$prefix: update ec/RW"
      update_ecfw "$SLOT_EC_RW"
    fi
  fi

  clear_update_cookies
}

# Factory Installer
mode_factory_install() {
  # Everything executed here must assume the system may be not using ChromeOS
  # firmware.
  is_write_protection_disabled ||
    die "You need to first disable hardware write protection switch."

  if [ "${FLAGS_update_main}" = ${FLAGS_TRUE} ]; then
    # We may preserve bitmap here, just like recovery mode. However if there's
    # some issue (or incompatible stuff) found in bitmap, we will need a method
    # to update the bitmaps.
    preserve_vpd || verbose_msg "Warning: cannot preserve VPD."
    update_mainfw
  fi
  if [ "${FLAGS_update_ec}" = ${FLAGS_TRUE} ]; then
    update_ecfw
  fi
  cros_clear_nvdata
  silent_sh enable_dev_boot
  clear_update_cookies
}

# Factory Wipe
mode_factory_final() {
  silent_sh disable_dev_boot
  clear_update_cookies
}

# Updates for incompatible RW firmware (need to update RO)
mode_incompatible_update() {
  # TODO(hungte) check if we really need to stop user by comparing RO firmware
  # image, bit-by-bit.
  is_write_protection_disabled ||
    die "You need to first disable hardware write protection switch."
  mode_recovery
}

# ----------------------------------------------------------------------------
# Main Entry

main_check_rw_compatible() {
  local is_compatible="${FLAGS_TRUE}"
  if [ "${FLAGS_check_rw_compatible}" = ${FLAGS_FALSE} ]; then
    verbose_msg "Bypassed RW compatbility check. You're on your own."
    return $is_compatible
  fi

  if [ -n "${TARGET_UNSTABLE}" ]; then
    debug_msg "Current image is tagged as UNSTABLE."
    if [ "${FLAGS_update_main}" = ${FLAGS_TRUE} ] &&
       [ "$FWID" != "$TARGET_FWID" ]; then
      verbose_msg "Found unstable main firmware $TARGET_FWID. Current: $FWID."
      is_compatible="${FLAGS_FALSE}"
    fi
    if [ "${FLAGS_update_ec}" = ${FLAGS_TRUE} ] &&
       [ "$ECID" != "$TARGET_ECID" ]; then
      verbose_msg "Found unstable EC firmware $TARGET_ECID. Current: $ECID."
      is_compatible="${FLAGS_FALSE}"
    fi
  fi

  # Try explicit match
  if [ -n "$CUSTOMIZATION_RW_COMPATIBLE_CHECK" ]; then
    debug_msg "Checking customized RW compatibility..."
    "$CUSTOMIZATION_RW_COMPATIBLE_CHECK" || is_compatible="${FLAGS_ERROR}"
  fi

  case "$is_compatible" in
    "${FLAGS_FALSE}" )
      verbose_msg "Try to update with recovery mode..."
      mode_recovery
      ;;
    "${FLAGS_ERROR}" )
      verbose_msg "
        RW firmware update is not compatible with current RO firmware.
        Starting full update...
        "
      mode_incompatible_update
      ;;
    * )
      true
  esac
  return $is_compatible
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
  if [ "${FLAGS_factory}" = ${FLAGS_TRUE} ] ||
     [ "${FLAGS_mode}" = "factory" ]; then
    FLAGS_mode=factory_install
  fi

  verbose_msg "Starting $TARGET_PLATFORM firmware updater v3 (${FLAGS_mode})..."
  verbose_msg " - Updater package: [$TARGET_FWID / $TARGET_ECID]"
  verbose_msg " - Current system:  [$FWID / $ECID]"

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

  local wpmsg="$(cros_report_wp_status $FLAGS_update_main $FLAGS_update_ec)"
  verbose_msg " - Write protection: $wpmsg"

  # Check platform except in factory_install mode.
  if [ "${FLAGS_check_platform}" = ${FLAGS_TRUE} ] &&
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

  case "${FLAGS_mode}" in
    # Modes which update RW firmware only and only in normal mode; these need to
    # verify if existing RO firmware is compatible.  If not, try to perform
    # RO+RW update.
    autoupdate )
      debug_msg "mode with dev and compatibility check: ${FLAGS_mode}"
        main_check_rw_compatible &&
        mode_"${FLAGS_mode}"
      ;;

    # Modes which update RW firmware only; these need to verify if existing RO
    # firmware is compatible.  If not, try to perform RO+RW update.
    recovery )
      debug_msg "mode with compatibility check: ${FLAGS_mode}"
      main_check_rw_compatible &&
        mode_"${FLAGS_mode}"
      ;;

    # Modes which don't mix existing RO firmware with new RW firmware from the
    # updater.  They either copy RW firmware between EEPROM slots, or copy both
    # RO+RW from the shellball.  Either way, RO+RW compatibility is assured.
    startup | bootok | todev | tonormal | factory_install | factory_final | \
      incompatible_update )
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
