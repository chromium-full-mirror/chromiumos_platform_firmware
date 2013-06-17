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
WRITE_OPT="--fast-verify"

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
