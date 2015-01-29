#!/bin/sh
#
# Copyright (c) 2011 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

# THIS FILE DEPENDS ON common.sh

# ----------------------------------------------------------------------------
# ChromeOS Firmware Unpacking Utilities

# Folders for preparing and unpacking firmware
# Layout: DIR_[TYPE]/[TYPE]/[SECTION], DIR_[TYPE]/[IMAGE]
DIR_CURRENT="_current"
DIR_TARGET="_target"

# Parameters for flashrom command
TARGET_OPT_MAIN="-p host"
TARGET_OPT_EC="-p ec"
TARGET_OPT_PD="-p ec:dev=1"
WRITE_OPT="--fast-verify"

# Image and key files.
IMAGE_MAIN="bios.bin"
IMAGE_MAIN_RW="bios_rw.bin"  # optional image.
IMAGE_EC="ec.bin"
IMAGE_PD="pd.bin"
KEYSET_DIR="keyset"

# Overrides this with any function name to perform special tasks when EC is
# updated (Ex, notify EC to check battery firmware updates)
CUSTOMIZATION_EC_POST_UPDATE=""
CUSTOMIZATION_PD_POST_UPDATE=""

# Unpacks target image, in full image and unpacked form.
crosfw_unpack_image() {
  check_param "crosfw_unpack_image(type, image, target_opt)" "$@"
  local type_name="$1" image_name="$2" target_opt="$3"
  debug_msg "preparing $type_name firmware images..."
  mkdir -p "$DIR_TARGET/$type_name"
  cp -f "$image_name" "$DIR_TARGET/$image_name"
  ( cd "$DIR_TARGET/$type_name";
    dump_fmap -x "../$image_name" >/dev/null 2>&1) ||
    die "Invalid firmware image (missing FMAP) in $image_name."
}

# Unpacks image from current system EEPROM, in full and/or unpacked form.
crosfw_unpack_current_image() {
  check_param "crosfw_unpack_current_image(type,image,target_opt,...)" "$@"
  local type_name="$1" image_name="$2" target_opt="$3"
  shift; shift; shift
  debug_msg "trying to read $type_name firmware from system EEPROM..."
  mkdir -p "$DIR_CURRENT/$type_name"
  local list="" i=""
  for i in $@ ; do
    list="$list -i $i"
  done
  invoke "flashrom $target_opt $list -r $DIR_CURRENT/$image_name"
  # current may not have FMAP... (ex, from factory setup)
  ( cd "$DIR_CURRENT/$type_name";
    dump_fmap -x "../$image_name" >/dev/null 2>&1) || true
}

# Copies VPD data by merging sections from current system into target firmware
# image file.
crosfw_dupe_vpd() {
  check_param "crosfw_dupe_vpd(vpd_list, output, opt_input)" "$@"
  # Preserve VPD when updating an existing system (maya be legacy firmware or
  # ChromeOS firmware). The system may not have FMAP, so we need to use
  # emulation mode otherwise reading sections may fail.
  local vpd_list="$1"
  local output="$2"
  local input="$3"
  debug_msg "crosfw_dupe_vpd: Duplicating VPD... ($vpd_list)"

  # TODO(hungte) Speed up by reading only partitions specified in vpd_list
  # unless active firmware is nonchrome.
  if [ -z "$input" ]; then
    input="_vpd_temp.bin"
    debug_msg "Reading active firmware..."
    silent_invoke "flashrom $TARGET_OPT_MAIN -r $input" ||
      die "Failed to read current main firmware."
  fi
  local size_input="$(cros_get_file_size "$input")"
  local size_output="$(cros_get_file_size "$output")"

  if [ -z "$size_input" ] || [ "$size_input" = "0" ]; then
    die "Invalid current main firmware. Abort."
  fi
  if [ "$size_input" != "$size_output" ]; then
    die "Incompatible firmware image size ($size_input != $size_output)."
  fi

  # Build command for VPD list
  local vpd_list_cmd="" vpd_name=""
  local is_trusted_vpd=1
  local param="dummy:emulate=VARIABLE_SIZE,image=$output,size=$size_input"

  if dump_fmap "$input" >/dev/null 2>&1; then
    # Trust the FMAP in input
    debug_msg "Using VPD location from input FMAP"
    is_trusted_vpd=
    local tmpdir="_dupe_vpd.tmp"
    local input_path="$(readlink -f "$input")"
    (mkdir "$tmpdir"; cd "$tmpdir"; dump_fmap -x "$input_path") >/dev/null 2>&1
    for vpd_name in $vpd_list; do
      vpd_list_cmd="$vpd_list_cmd -i $vpd_name:$tmpdir/$vpd_name"
    done
  else
    debug_msg "Using VPD location from target FMAP"
    for vpd_name in $vpd_list; do
      vpd_list_cmd="$vpd_list_cmd -i $vpd_name"
    done
  fi

  debug_msg "Preserving VPD: -p $param $vpd_list_cmd"
  if ! silent_invoke "flashrom -p $param $vpd_list_cmd -w $input"; then
    if [ -n "$is_trusted_vpd" ]; then
      die "Failed to dupe VPD. Please check target firmware image."
    else
      alert "Warning: corrupted VPD in current firmware - reset."
    fi
  fi

  # Check if VPD is valid. Reset if required.
  debug_msg "Checking VPD partitions..."
  for vpd_name in $vpd_list; do
    if ! silent_invoke "vpd -i $vpd_name -f $output"; then
      alert "Warning: corrupted VPD ($vpd_name) - Reset."
      silent_invoke "vpd -O -i $vpd_name -f $output"
    fi
  done

  debug_msg "crosfw_dupe_vpd: $output updated."
}

# Utility to return if main firmware write protection is enabled.
is_mainfw_write_protected() {
  if [ -n "$FLAGS_wp" ]; then
    verbose_msg "Warning: MAIN WP state overridden as ${FLAGS_wp}"
    "${FLAGS_wp}"
  else
    cros_is_hardware_write_protected &&
      cros_is_software_write_protected "$TARGET_OPT_MAIN"
  fi
}

# Utility to return if EC firmware write protection is enabled.
is_ecfw_write_protected() {
  if [ -n "$FLAGS_wp" ]; then
    verbose_msg "Warning: EC WP state overridden as ${FLAGS_wp}"
    "${FLAGS_wp}"
  else
    cros_is_hardware_write_protected &&
      cros_is_software_write_protected "$TARGET_OPT_EC"
  fi
}

# Utility to return if PD firmware write protection is enabled.
is_pdfw_write_protected() {
  if [ -n "$FLAGS_wp" ]; then
    verbose_msg "Warning: PD WP state overridden as ${FLAGS_wp}"
    "${FLAGS_wp}"
  else
    cros_is_hardware_write_protected &&
      cros_is_software_write_protected "$TARGET_OPT_PD"
  fi
}

crosfw_dup2_mainfw() {
  # Syntax: crosfw_dup2_mainfw SLOT_FROM SLOT_TO
  #    Duplicates a slot to another place on system live firmware
  local slot_from="$1" slot_to="$2"
  # Writing in flashrom, even if there's nothing to write, is still much slower
  # than reading because it needs to read + verify whole image. So we should
  # only write firmware on if there's something changed.
  local temp_from="_dup2_temp_from"
  local temp_to="_dup2_temp_to"
  local temp_image="_dup2_temp_image"
  debug_msg "invoking: crosfw_dup2_mainfw($@)"
  local opt_read_slots="-i $slot_from:$temp_from -i $slot_to:$temp_to"
  invoke "flashrom $TARGET_OPT_MAIN $opt_read_slots -r $temp_image"
  if [ -s "$temp_from" ] && cros_compare_file "$temp_from" "$temp_to"; then
    debug_msg "crosfw_dup2_mainfw: slot content is the same, no need to copy."
  else
    slot="$slot_to:$temp_from"
    invoke "flashrom $TARGET_OPT_MAIN $WRITE_OPT -w $temp_image -i $slot"
  fi
}

# Compares two slots from current and target folder.
crosfw_is_equal_slot() {
  check_param "crosfw_is_equal_slot(type, slot, ...)" "$@"
  local type_name="$1" slot_name="$2" slot2_name="$3"
  [ "$#" -lt 4 ] || die "crosfw_is_equal_slot: internal error"
  [ -n "$slot2_name" ] || slot2_name="$slot_name"
  local current="$DIR_CURRENT/$type_name/$slot_name"
  local target="$DIR_TARGET/$type_name/$slot2_name"
  cros_compare_file "$current" "$target"
}

# ----------------------------------------------------------------------------
# General Updater

# Update Main Firmware (BIOS, AP)
crosfw_update_main() {
  # Syntax: crosfw_update_main SLOT FIRMWARE_SOURCE_TYPE
  #    Write assigned type (normal/developer) of firmware source into
  #    assigned slot (returns directly if the target is already filled
  #    with correct data)
  # Syntax: crosfw_update_main SLOT
  #    Write assigned slot from MAIN_TARGET_IMAGE.
  # Syntax: crosfw_update_main
  #    Write complete MAIN_TARGET_IMAGE

  local slot="$1"
  local source_type="$2"
  debug_msg "invoking: crosfw_update_main($@)"
  # TODO(hungte) verify if slot is valid.
  [ -s "$IMAGE_MAIN" ] || die "missing firmware image: $IMAGE_MAIN"
  if [ "$slot" = "" ]; then
    invoke "flashrom $TARGET_OPT_MAIN $WRITE_OPT -w $IMAGE_MAIN"
  elif [ "$source_type" = "" ]; then
    invoke "flashrom $TARGET_OPT_MAIN $WRITE_OPT -w $IMAGE_MAIN -i $slot"
  else
    local section_file="$DIR_TARGET/$TYPE_MAIN/$source_type"
    [ -s "$section_file" ] || die "crosfw_update_main: missing $section_file"
    slot="$slot:$section_file"
    invoke "flashrom $TARGET_OPT_MAIN $WRITE_OPT -w $IMAGE_MAIN -i $slot"
  fi
}

# Update Embedded Controller Firmware
crosfw_update_ec() {
  local slot="$1"
  debug_msg "invoking: crosfw_update_ec($@)"
  # Syntax: crosfw_update_ec SLOT
  #    Update assigned slot with proper firmware.
  # Syntax: crosfw_update_ec
  #    Write complete MAIN_TARGET_IMAGE
  # TODO(hungte) verify if slot is valid.
  [ -s "$IMAGE_EC" ] || die "missing firmware image: $IMAGE_EC"
  [ -z "$CUSTOMIZATION_EC_PRE_UPDATE" ] || "$CUSTOMIZATION_EC_PRE_UPDATE"
  if [ -n "$slot" ]; then
    invoke "flashrom $TARGET_OPT_EC $WRITE_OPT -w $IMAGE_EC -i $slot"
  else
    invoke "flashrom $TARGET_OPT_EC $WRITE_OPT -w $IMAGE_EC"
  fi
  [ -z "$CUSTOMIZATION_EC_POST_UPDATE" ] || "$CUSTOMIZATION_EC_POST_UPDATE"
}

# Update Embedded Controller PD Firmware
crosfw_update_pd() {
  local slot="$1"
  debug_msg "invoking: crosfw_update_pd($@)"
  # Syntax: crosfw_update_pd SLOT
  #    Update assigned slot with proper firmware.
  # Syntax: crosfw_update_pd
  #    Write complete MAIN_TARGET_IMAGE
  [ -s "$IMAGE_PD" ] || die "missing firmware image: $IMAGE_PD"
  [ -z "$CUSTOMIZATION_PD_PRE_UPDATE" ] || "$CUSTOMIZATION_PD_PRE_UPDATE"
  if [ -n "$slot" ]; then
    invoke "flashrom $TARGET_OPT_PD $WRITE_OPT -w $IMAGE_PD -i $slot"
  else
    invoke "flashrom $TARGET_OPT_PD $WRITE_OPT -w $IMAGE_PD"
  fi
  [ -z "$CUSTOMIZATION_PD_POST_UPDATE" ] || "$CUSTOMIZATION_PD_POST_UPDATE"
}

# Note following "preserve" utilities will change $IMAGE_MAIN so any processing
# to the file (ex, prepare_main_image) must be invoked AFTER this call.
crosfw_preserve_hwid() {
  [ -s "$IMAGE_MAIN" ] || die "preserve_hwid: no main firmware."
  silent_invoke "gbb_utility -s --hwid='$HWID' $IMAGE_MAIN"
}

crosfw_preserve_vpd() {
  crosfw_dupe_vpd "RO_VPD RW_VPD" "$IMAGE_MAIN" ""
}

crosfw_preserve_bmpfv() {
  if [ -z "$HWID" ]; then
    debug_msg "crosfw_preserve_bmpfv: Running non-ChromeOS firmware. Skip."
    return
  fi
  debug_msg "Preseving main firmware bitmap volume data..."
  [ -s "$IMAGE_MAIN" ] || die "crosfw_preserve_bmpfv: no main firmware."
  # Preserves V1, V2 bitmap volumes.
  silent_invoke "flashrom $TARGET_OPT_MAIN -i GBB:_gbb.bin -r _temp.rom"
  silent_invoke "gbb_utility -g --bmpfv=_bmpfv.bin _gbb.bin"
  [ -s "_bmpfv.bin" ] || die "crosfw_preserve_bmpfv: invalid bmpfv"
  silent_invoke "gbb_utility -s --bmpfv=_bmpfv.bin $IMAGE_MAIN"
}

crosfw_preserve_gbb() {
  if [ -z "$HWID" ]; then
    debug_msg "crosfw_preserve_gbb: Running non-ChromeOS firmware. Skip."
    return
  fi
  debug_msg "Preseving main firmware GBB data..."
  [ -s "$IMAGE_MAIN" ] || die "crosfw_preserve_gbb: no main firmware."
  silent_invoke "flashrom $TARGET_OPT_MAIN -i GBB:_gbb.bin -r _temp.rom"

  # Preseves flags (--flags output format: "flags: 0x0000001")
  local flags="$(gbb_utility -g --flags _gbb.bin 2>/dev/null |
                 sed -nr 's/^flags: ([x0-9]+)/\1/p')"
  debug_msg "Current firmware flags: $flags"
  if [ -n "$flags" ]; then
    silent_invoke "gbb_utility -s --flags=$((flags)) $IMAGE_MAIN"
  fi
}
