#!/bin/sh
#
# Copyright (c) 2011 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

# THIS FILE DEPENDS ON common.sh.

# ----------------------------------------------------------------------------
# ChromeOS Specific Utilities

# Retrieves MD5 hash of a given file.
cros_get_file_hash() {
  md5sum -b "$1" 2>/dev/null | sed 's/ .*//'
}

# Compares two files on Chrome OS (there may be no cmp/diff)
cros_compare_file() {
  local hash1="$(cros_get_file_hash "$1")"
  local hash2="$(cros_get_file_hash "$2")"
  debug_msg "cros_compare_file($1, $2): $hash1, $hash2"
  [ -n "$hash1" ] && [ "$hash1" = "$hash2" ]
}

# Gets file size.
cros_get_file_size() {
  [ -e "$1" ] || die "cros_get_file_size: invalid file: $1"
  stat -c "%s" "$1" 2>/dev/null
}

# Gets a Chrome OS system property (must exist).
cros_get_prop() {
  crossystem "$@" || die "cannot get crossystem property: $@"
}

# Sets a Chrome OS system property.
cros_set_prop() {
  if [ "${FLAGS_dry_run}" = "${FLAGS_TRUE}" ]; then
    alert "dry_run: cros_set_prop $@"
    return ${FLAGS_TRUE}
  fi
  crossystem "$@" || die "cannot SET crossystem property: $@"
}

# Queries a Chrome OS system property, return error if not available.
cros_query_prop() {
  crossystem "$@" 2>/dev/null || debug_msg "FAILURE: crossystem $@"
}

# Sets the "startup update tries" counter.
cros_set_startup_update_tries() {
  local startup_update_tries="$1"
  local fwupdate_tries=$(crossystem fwupdate_tries)
  if [ -n "$fwupdate_tries" ]; then
    cros_set_prop fwupdate_tries=$startup_update_tries
  fi
}

# Gets the "startup update tries" counter.
cros_get_startup_update_tries() {
  local fwupdate_tries=$(crossystem fwupdate_tries || echo 0)
  echo $fwupdate_tries
}

# Sets the "firmare B tries" counter
cros_set_fwb_tries() {
  cros_set_prop fwb_tries="$1"
}

cros_get_fwb_tries() {
  cros_query_prop fwb_tries
}

# Reboots the system immediately
cros_reboot() {
  verbose_msg "Rebooting system..."
  if [ "${FLAGS_dry_run}" = "${FLAGS_TRUE}" ]; then
    alert "dry_run: reboot"
    return ${FLAGS_TRUE}
  elif [ "${FLAGS_allow_reboot}" = "${FLAGS_FALSE}" ]; then
    alert "Rebooting from updater is disabled."
    return ${FLAGS_TRUE}
  fi
  sync; sync; sync
  reboot
}

# Returns if the hardware write-protection switch is enabled.
cros_is_hardware_write_protected() {
  local ret=${FLAGS_FALSE}
  # In current design, hardware write protection is one single switch for all
  # targets. NOTE: if wpsw_cur gives error, we should treat like "protected"
  # so the test uses "!= 0" instead of "= 1".
  if [ "$(cros_query_prop wpsw_cur)" != "0" ]; then
    debug_msg "Hardware write protection is enabled!"
    ret=${FLAGS_TRUE}
  fi
  return $ret
}

# Returns if the software write-protection register is enabled.
cros_is_software_write_protected() {
  local opt="$1"
  # Do not pipe flashrom stdout to grep to prevent SIGPIPE
  FLASHROM_OUT=$(flashrom $opt --wp-status 2>/dev/null)
  echo $FLASHROM_OUT | grep -q "write protect is enabled"
}

# Reports write protection status
cros_report_wp_status() {
  local test_main="$1" test_ec="$2"
  local wp_hw="off" wp_sw_main="off" wp_sw_ec="off"
  cros_is_hardware_write_protected && wp_hw="ON"
  local message="Hardware: $wp_hw, Software:"
  if [ "$test_main" = $FLAGS_TRUE ]; then
    cros_is_software_write_protected "$TARGET_OPT_MAIN" && wp_sw_main="ON"
    message="$message Main=$wp_sw_main"
  fi
  if [ "$test_ec" = $FLAGS_TRUE ]; then
    cros_is_software_write_protected "$TARGET_OPT_EC" && wp_sw_ec="ON"
    message="$message EC=$wp_sw_ec"
  fi
  echo "$message"
}

# Reports the information from given key file.
cros_report_key() {
  local key_file="$1"
  local key_hash="$(vbutil_key --unpack "$key_file" |
                    sed -nr 's/^Key sha1sum: *([^ ]*)$/\1/p')"
  local label=""
  case "$key_hash" in
    b11d74edd286c144e1135b49e7f0bc20cf041f10 )
      label="DEV-signed rootkey"
      ;;
    "" )
      label="Unknown (failed to unpack key)"
      ;;
    * )
      ;;
  esac
  echo "$key_hash $label"
}

# Checks if the root keys (from Google Binary Block) are the same.
cros_check_same_root_keys() {
  check_param "cros_check_same_root_keys(current, target)" "$@"
  local keyfile1="_gk1"
  local keyfile2="_gk2"
  local keyfile1_strip="${keyfile1}_strip"
  local keyfile2_strip="${keyfile2}_strip"
  local ret=${FLAGS_TRUE}

  # current(1) may not contain root key, but target(2) MUST have a root key
  if silent_invoke "gbb_utility -g --rootkey=$keyfile1 $1" 2>/dev/null; then
    silent_invoke "gbb_utility -g --rootkey=$keyfile2 $2" ||
      die "Cannot find ChromeOS GBB RootKey in $2."
    # to workaround key paddings...
    cat $keyfile1 | sed 's/\xff*$//g; s/\x00*$//g;' >$keyfile1_strip
    cat $keyfile2 | sed 's/\xff*$//g; s/\x00*$//g;' >$keyfile2_strip
    if ! cros_compare_file "$keyfile1_strip" "$keyfile2_strip"; then
      ret=$FLAGS_FALSE
      alert "Current key: $(cros_report_key "$keyfile1")"
      alert "Target  key: $(cros_report_key "$keyfile2")"
    fi
  else
    debug_msg "warning: cannot get rootkey from $1"
    ret=$FLAGS_ERROR
  fi
  return $ret
}

# Gets the vbutil_firmware information of a RW firmware image.
cros_get_rw_firmware_info() {
  check_param "cros_get_rw_firmware_info(vblock, fw_main, image)" "$@"
  local vblock="$1"
  local fw_main="$2"
  local image="$3"

  local rootkey="_rootkey"
  silent_invoke "gbb_utility -g --rootkey=$rootkey $image" 2>/dev/null ||
    return

  local fw_info
  fw_info="$(vbutil_firmware --verify "$vblock" \
                             --signpubkey "$rootkey" \
                             --fv "$fw_main" 2>/dev/null)" || fw_info=""
  echo "$fw_info"
}

# Checks if the firmare key and version are allowed by TPM.
cros_check_tpm_key_version() {
  check_param "cros_check_tpm_key_version(fw_info)" "$@"
  local fw_info="$1"

  local tpm_fwver="$(cros_query_prop tpm_fwver)"
  if [ -z "$tpm_fwver" ]; then
    alert "Warning: failed to retrieve TPM information."
    # TODO(hungte) what now?
    return "$FLAGS_ERROR"
  fi
  tpm_fwver="$((tpm_fwver))"
  debug_msg "tpm_fwver: $tpm_fwver"

  local data_key_version="$(
    echo "$fw_info" | sed -n '/^ *Data key version:/s/.*:[ \t]*//p')"
  debug_msg "data_key_version: $data_key_version"
  local firmware_version="$(
    echo "$fw_info" | sed -n '/^ *Firmware version:/s/.*:[ \t]*//p')"
  debug_msg "firmware_version: $firmware_version"
  if [ -z "$data_key_version" ] || [ -z "$firmware_version" ]; then
    die "Cannot verify firmware key version from target image."
  fi

  local fw_key_version="$((
    (data_key_version << 16) | (firmware_version & 0xFFFF) ))"
  debug_msg "fw_key_version: $fw_key_version"

  if [ "$tpm_fwver" -gt "$fw_key_version" ]; then
    fw_key_version="$(printf "0x%x" $fw_key_version)"
    tpm_fwver="$(printf "0x%x" $tpm_fwver)"
    alert "Firmware key ($fw_key_version) will be rejected by TPM ($tpm_fwver)."
    return $FLAGS_FALSE
  fi
  return $FLAGS_TRUE
}

# Gets the flag of firmware preamble data.
cros_get_firmware_preamble_flags() {
  check_param "cros_get_firmware_preamble_flags(fw_info)" "$@"
  local fw_info="$1"

  local preamble_flags="$(
    echo "$fw_info" | sed -n '/^ *Preamble flags:/s/.*:[ \t]*//p')"
  debug_msg "preamble_flags: $preamble_flags"
  echo "$preamble_flags"
}

# Returns if firmware was boot with VBSD_LF_USE_RO_NORMAL flag.
cros_is_ro_normal_boot() {
  local VBSD_LF_USE_RO_NORMAL=0x08
  local vdat_flags="$(cros_get_prop vdat_flags 2>/dev/null)"
  [ "$((vdat_flags & VBSD_LF_USE_RO_NORMAL))" -gt "0" ]
}

# Clears ChromeOS related NVData (usually stored on NVRAM/CMOS, storing firmware
# related settings and cookies).
cros_clear_nvdata() {
  mosys nvram clear >/dev/null 2>&1 ||
    debug_msg " - (NVData not cleared)."
}

# Adds path to PATH if given tool is available (and supports empty param).
cros_add_tool_path() {
  local path="$1"
  local tool="$2"
  if [ -x "$path/$tool" ] && "$path/$tool" >/dev/null 2>&1; then
    debug_msg "$path/$tool works fine."
    PATH="$path:$PATH"; export PATH
    return $FLAGS_TRUE
  fi
  return $FLAGS_FALSE
}

# Configures PATH by detecting if current architecture is compatible with
# bundled executable binaries, then "32b" (if exist) or system default ones.
cros_setup_path() {
  local base="$(readlink -f "$SCRIPT_BASE")"
  if cros_add_tool_path "$base" "crossystem"; then
    debug_msg "Using programs in $base."
    return
  fi

  if cros_add_tool_path "$base/32b" "crossystem"; then
    debug_msg "Using programs in $base/32b."
    return
  fi

  debug_msg "Using programs in system."
}
