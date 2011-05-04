#!/bin/sh
#
# Copyright (c) 2010 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

# THIS FILE DEPENDS ON common.sh.

# ----------------------------------------------------------------------------
# ChromeOS Specific Utilities

# Compares two files on Chrome OS (there may be no cmp/diff)
cros_compare_file() {
  local hash1="$(md5sum -b "$1" 2>/dev/null | sed 's/ .*//')"
  local hash2="$(md5sum -b "$2" 2>/dev/null | sed 's/ .*//')"
  debug_msg "cros_compare_file($1, $2): $hash1, $hash2)"
  [ -n "$hash1" ] && [ "$hash1" = "$hash2" ]
}

# Gets file size.
cros_get_file_size() {
  [ -e "$1" ] || err_die "cros_get_file_size: invalid file: $1"
  stat -c "%s" "$1" 2>/dev/null
}

# Gets a Chrome OS system property (must exist).
cros_get_prop() {
  crossystem "$@" || err_die "cannot get crossystem property: $@"
}

# Sets a Chrome OS system property.
cros_set_prop() {
  if [ "${FLAGS_dry_run}" = "${FLAGS_TRUE}" ]; then
    alert "dry_run: cros_set_prop $@"
    return ${FLAGS_TRUE}
  fi
  crossystem "$@" || err_die "cannot SET crossystem property: $@"
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
  fi
  sync; sync; sync
  /sbin/reboot
}

# Returns if the hardware write-protection switch is enabled.
cros_is_hardware_write_protected() {
  local ret=${FLAGS_FALSE}
  # In current design, hardware write protection is one single switch for all
  # targets. NOTE: if wpsw_cur gives error, we should treat like "protected"
  # so the test uses "!= 0" instead of "= 1".
  if [ "$(cros_query_prop wpsw_cur)" != "0" ]; then
    verbose_msg "Hardware write protection is enabled!"
    ret=${FLAGS_TRUE}
  fi
  return $ret
}

# Checks if the root keys (from Google Binary Block) are the same.
cros_check_same_root_keys() {
  check_param "chromeos_check_same_root_keys(current, target)" "$@"
  local keyfile1="_gk1"
  local keyfile2="_gk2"
  local keyfile1_strip="${keyfile1}_strip"
  local keyfile2_strip="${keyfile2}_strip"
  local ret=${FLAGS_TRUE}

  # current(1) may not contain root key, but target(2) MUST have a root key
  if silent_invoke "gbb_utility -g --rootkey=$keyfile1 $1" 2>/dev/null; then
    silent_invoke "gbb_utility -g --rootkey=$keyfile2 $2" ||
      err_die "Cannot find ChromeOS GBB RootKey in $2."
    # to workaround key paddings...
    cat $keyfile1 | sed 's/\xff*$//g; s/\x00*$//g;' >$keyfile1_strip
    cat $keyfile2 | sed 's/\xff*$//g; s/\x00*$//g;' >$keyfile2_strip
    cros_compare_file "$keyfile1_strip" "$keyfile2_strip" || ret=$FLAGS_FALSE
  else
    debug_msg "warning: cannot get rootkey from $1"
    ret=$FLAGS_ERROR
  fi
  return $ret
}

