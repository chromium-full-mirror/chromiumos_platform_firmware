#!/bin/sh
#
# Copyright (c) 2011 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

: ${SCRIPT_BASE:="$(dirname "$0")"}
if [ -s "$SCRIPT_BASE/shflags" ]; then
  . "$SCRIPT_BASE/shflags"
else
  . /usr/share/misc/shflags
fi
. "$SCRIPT_BASE/crosutil.sh"
. "$SCRIPT_BASE/crosfw.sh"

# ----------------------------------------------------------------------------
# Common Command Line Arguments

DEFINE_string mode "" \
 "Updater mode ( startup | bootok | autoupdate | todev | tonormal |"\
" recovery | factory_install | factory_final | incompatible_update |"\
" fast_version_check )" "m"
DEFINE_string wp "" "Override write protection state (0/1)." ""

DEFINE_boolean debug $FLAGS_FALSE "Enable debug messages." "d"
DEFINE_boolean verbose $FLAGS_TRUE "Enable verbose messages." "v"
DEFINE_boolean dry_run $FLAGS_FALSE "Enable dry-run mode." ""
DEFINE_boolean force $FLAGS_FALSE "Try to force update." ""
DEFINE_boolean allow_reboot $FLAGS_TRUE \
  "Allow rebooting system immediately if required."
DEFINE_string customization_id "" \
  "Customization ID for keysets." ""

DEFINE_boolean update_ec $FLAGS_TRUE "Enable updating Embedded Firmware." ""
DEFINE_boolean update_pd $FLAGS_TRUE "Enable updating PD Firmware." ""
DEFINE_boolean update_main $FLAGS_TRUE "Enable updating Main Firmware." ""

DEFINE_boolean check_keys $FLAGS_TRUE "Check firmware keys before updating." ""
DEFINE_boolean check_rw_compatible $FLAGS_TRUE \
  "Check if RW firmware is compatible with current RO" ""
DEFINE_boolean check_platform $FLAGS_TRUE \
  "Bypass firmware updates if the system platform name is different" ""
# Required for factory compatibility
DEFINE_boolean factory $FLAGS_FALSE "Equivalent to --mode=factory_install"

# ----------------------------------------------------------------------------
# Common Utilities

# Reports some critical message in stderr
alert() {
  echo "$*" 1>&2
}

# Reports error message and exit(1)
# NOTE: top level script does not exit if call to die is inside a
# sub-shell (ex: (func-call), $(func-call)) or if-blocks.
die() {
  alert "ERROR: $*"
  exit 1
}

# Alias for die, backward compatibility for custom scripts in board overlay
err_die() {
  die "$@"
}

# Reports error message and exit(3)
# NOTE: top level script does not exit if call to die is inside a
# sub-shell (ex: (func-call), $(func-call)) or if-blocks.
die_need_reboot() {
  alert "ERROR: $*"
  exit 3
}

# Reports error message and exit(4)
# NOTE: top level script does not exit if call to die is inside a
# sub-shell (ex: (func-call), $(func-call)) or if-blocks.
die_need_ro_update() {
  alert "ERROR: $*"
  exit 4
}

# Prints a message if in verbose mode
verbose_msg() {
  if [ "$FLAGS_verbose" = "$FLAGS_TRUE" ] ||
     [ "$FLAGS_debug" = "$FLAGS_TRUE" ]; then
    echo "$*"
  fi
}

# Prints a message if in debug mode
debug_msg() {
  if [ "$FLAGS_debug" = "$FLAGS_TRUE" ]; then
    alert " (DEBUG) $*"
  fi
}

# Helps functions to check if there were given parameters in correct form.
# Syntax: check_param "prototype" "$@"
#         prototype is a string in "funcname(arg1,arg2,arg3)" form.
check_param() {
  local original_format="$1"
  local format="$(echo "$1" | sed 's/[(,) ]/ /g; s/  */ /g;')"
  local has_wild_arg=0
  local i=0 param=""

  # truncate the function name
  shift
  format="$(echo "$format" | sed 's/^[^ ]* //')"

  # check parameter numbers
  i=0
  for param in $format ; do
    if [ "$param" = "..." ]; then
      # ignore everything after '...'
      has_wild_arg=1
      break
    fi
    i=$(($i + 1))
  done
  if [ $has_wild_arg = 1 ]; then
    [ $# -ge $i ] || die "$original_format: need $i+ params (got $#): $@"
  else
    [ $# -eq $i ] || die "$original_format: need $i params (got $#): $@"
  fi

  # check if we have any empty parameters
  i=1
  for param in "$@"; do
    local param_name=$(echo "$format" | cut -d" " -f $i)
    # forr params started with 'opt_', allow it to be empty.
    if [ -z "$param" -a "${param_name##opt_*}" != "" ]; then
      shift # remove the invoke command name
      die "check_param: $original_format: " \
              "param '$param_name' is empty: '$@'"
    fi
    i=$(($i + 1))
  done
}

# Quick check and setup for basic envoronments.
set_flags() {
  # Adjust WP flags.
  case "$FLAGS_wp" in
    on | ON | true | TRUE | 1)
      FLAGS_wp=true
      ;;
    off | OFF | false | FALSE | 0)
      FLAGS_wp=false
      ;;
    "")
      FLAGS_wp=
      ;;
    *)
      die "Invalid option for --wp: ${FLAGS_wp}"
      ;;
  esac

  # Adjust update_* flags according to known images.
  if [ ! -s "$IMAGE_MAIN" ]; then
    FLAGS_update_main=${FLAGS_FALSE}
    verbose_msg "No main firmware bundled in updater, ignored."
  fi
  if [ ! -s "$IMAGE_EC" ]; then
    FLAGS_update_ec=${FLAGS_FALSE}
    debug_msg "No EC firmware bundled in updater, ignored."
  fi
  if [ ! -s "$IMAGE_PD" ]; then
    FLAGS_update_pd=${FLAGS_FALSE}
    debug_msg "No PD firmware bundled in updater, ignored."
  fi
}

# Executes a command, and provide messages if it failed.
silent_invoke() {
  local ret=$FLAGS_TRUE
  if [ "${FLAGS_dry_run}" = "${FLAGS_TRUE}" ]; then
    return $ret
  fi
  ( eval "$@" ) >_exec.stdout 2>_exec.stderr || ret=$?
  if [ "$ret" != "0" ]; then
    alert " Execution failed ($ret): $*"
    alert " Messages:"
    cat _exec.stdout >&2
    cat _exec.stderr >&2
  fi
  return $ret
}

# Prints a verbose message before calling silent_invoke.
invoke() {
  verbose_msg " * invoke: $@"
  silent_invoke "$@" || die "Execution FAILED."
}

alert_write_protection() {
  if ! cros_is_hardware_write_protected;  then
    echo "
    WARNING: Hardware write protection may be still enabled, and a RO update is
             scheduled. The update may fail.
    "
  fi
}

# Prints a standard "unofficial HWID" warning message
alert_unofficial_hwid() {
  # TODO(hungte) maybe we don't need this anymore - simply check if keys are
  # compatible is enough.
  echo "
  Your system is using an unofficial version of $1 firmware,
  and is forbidden to invoke this update.

  Please restore your original firmware with correct HWID,
  or invoke this update in factory mode.
  "
}

# Prints a standard "unkonwn platform" error message
alert_unknown_platform() {
  # Sytnax: alert_unknown_platform CURRENT EXPECTED
  echo "
  Sorry, this firmware update is only for $2 platform.
  Your system ($1) is either incompatible or using an
  unknown version of firmware.
  "
}

# Prints a "incompatible" warning message
alert_incompatible_firmware() {
  # Syntax: alert_incompatible_firmware reboot_will_autoupdate
  echo "
  Your current RO firmware is NOT compatible with the new RW firmware, and needs
  a RO update.  Please make sure you've DISABLED write protection for such
  special update."

  if [ "$1" = "${FLAGS_TRUE}" ]; then
    echo "
  A RO autoupdate is also scheduled in next boot.
  "
  fi

  echo "
  If you want to start a manual update immediately, please use command:

  sudo chromeos-firmwareupdate --mode=incompatible_update
  "
}

alert_incompatible_rootkey() {
  alert "
  Incompatible firmware image (Root key is different).

  You may need to disable hardware write protection and perform a factory
  install by '--mode=factory_install' or recovery by '--mode=recovery'.
  "
}

alert_incompatible_tpmkey() {
  alert "
  Incompatible firmware image (Rollback - older than keys stored in TPM).

  Please update with latest recovery image and firmware, or restart a
  factory setup process to reset TPM key version.
  "
}
