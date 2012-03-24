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

# Updater for firmware v1 (simplified version)
# - No partial A/B update for main firmware.
# - Only supports recoverying and EC AU.
# - No preserving VPD

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

# Override FMAP with board-specific layout files
LAYOUT_MAIN="bios.layout"
LAYOUT_EC="ec.layout"
if [ -f "$LAYOUT_MAIN" ]; then
  TARGET_OPT_MAIN="$TARGET_OPT_MAIN -l $LAYOUT_MAIN"
fi
if [ -f "$LAYOUT_EC" ]; then
  # TODO(hungte) --ignore-fmap may be removed issue chrome-os-partner:7260 has
  # been fixed in flashrom.
  TARGET_OPT_EC="$TARGET_OPT_EC -l $LAYOUT_EC --ignore-fmap"
fi

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
  # Preserves bitmap volume
  silent_invoke "flashrom $TARGET_OPT_MAIN -i GBB:_gbb.bin -r _temp.rom"
  silent_invoke "gbb_utility -g --bmpfv=_bmpfv.bin _gbb.bin"
  silent_invoke "gbb_utility -s --bmpfv=_bmpfv.bin $IMAGE_MAIN"
  [ -s "_bmpfv.bin" ] || die "preserve_gbb: invalid bmpfv"
  # Preseves flags (--flags output format: "flags: 0x0000001")
  local flags="$(gbb_utility -g --flags _gbb.bin 2>/dev/null |
                 sed -nr 's/^flags: ([x0-9]+)/\1/p')"
  debug_msg "Current firmware flags: $flags"
  if [ -n "$flags" ]; then
    silent_invoke "gbb_utility -s --flags=$((flags)) $IMAGE_MAIN"
  fi
}

# Verifies if current system is installed with compatible rootkeys
check_compatible_keys() {
  if [ "${FLAGS_check_keys}" = ${FLAGS_FALSE} ]; then
    debug_msg "check_compatible_keys: ignored."
    return $FLAGS_TRUE
  fi
  silent_invoke "flashrom $TARGET_OPT_MAIN -i GBB:_gbb.bin -r _temp.rom"
  if ! cros_check_same_root_keys "_gbb.bin" "$IMAGE_MAIN"; then
    alert_incompatible_rootkey
    die "Incompatible Rootkey."
  fi
}

need_update_ec() {
  [ "$TARGET_ECID" != "$ECID" ]
}

is_mainfw_write_protected() {
  if [ "$FLAGS_check_wp" = $FLAGS_FALSE ]; then
    verbose_msg "Warning: write protection checking is bypassed."
    false
  elif ! cros_is_hardware_write_protected; then
    false
  else
    cros_is_software_write_protected "$TARGET_OPT_MAIN"
  fi
}

is_ecfw_write_protected() {
  if [ "$FLAGS_check_wp" = $FLAGS_FALSE ]; then
    verbose_msg "Warning: write protection checking is bypassed."
    false
  elif ! cros_is_hardware_write_protected; then
    false
  else
    cros_is_software_write_protected "$TARGET_OPT_EC"
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
    cros_set_startup_update_tries 0 ) >/dev/null 2>&1 ||
      debug_msg "clear_update_cookies: there were some errors, ignored."
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
    else
      alert "No need to update EC firmware ($ECID)."
    fi
    cros_set_startup_update_tries 0
    cros_reboot
  else
    cros_set_startup_update_tries 0
  fi
}

# Update Engine - Current Boot Successful (chromeos_setgoodkernel)
mode_bootok() {
  # AU via bootok is not designed for v1 platforms.
  clear_update_cookies
}

# Update Engine - Received Update
mode_autoupdate() {
  # NOTE: AU for main firmware is disabled.

  # Quick check if we need to update
  local need_update=0
  # Check EC firmware
  if [ "${FLAGS_update_ec}" = ${FLAGS_TRUE} ]; then
    if [ "$TARGET_ECID" != "$ECID" ]; then
      need_update=1
    else
      FLAGS_update_ec=$FLAGS_FALSE
    fi
  fi

  if [ "$need_update" -eq 0 ]; then
    verbose_msg "Latest EC firmware already installed. No need to update."
    return
  fi

  # Don't call need_update_ec because it will freeze the keyboard.
  if [ "${FLAGS_update_ec}" = "${FLAGS_TRUE}" ]; then
    cros_set_startup_update_tries 1
    verbose_msg "Done (EC update will occur at Startup)"
  fi
}

# Transition to Developer Mode
mode_todev() {
  echo "--mode=${FLAGS_mode} is not supported on $PLATFORM."
}

# Transition to Normal Mode
mode_tonormal() {
  echo "--mode=${FLAGS_mode} is not supported on $PLATFORM."
}

# Recovery Installer
mode_recovery() {
  local prefix="mode_recovery"
  [ "${FLAGS_mode}" = "recovery" ] || prefix="${FLAGS_mode}(recovery)"
  if [ "${FLAGS_update_main}" = ${FLAGS_TRUE} ]; then
    if ! is_mainfw_write_protected; then
      verbose_msg "$prefix: update RO+RW"
      preserve_gbb
      update_mainfw
    else
      # TODO(hungte) check if FMAP is not changed
      verbose_msg "$prefix: update main/RW:A,B,SHARED"
      check_compatible_keys
      update_mainfw "$SLOT_A"
      update_mainfw "$SLOT_B"
      # RW_SHARED is temporary not available for v1 platforms.
      # update_mainfw "$SLOT_RW_SHARED"
    fi
  fi

  if [ "${FLAGS_update_ec}" = ${FLAGS_TRUE} ]; then
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
    update_mainfw
  fi
  if [ "${FLAGS_update_ec}" = ${FLAGS_TRUE} ]; then
    update_ecfw
  fi
  clear_update_cookies
}

# Factory Wipe
mode_factory_final() {
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

  verbose_msg "Starting $TARGET_PLATFORM firmware updater v1s ($FLAGS_mode)..."
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
