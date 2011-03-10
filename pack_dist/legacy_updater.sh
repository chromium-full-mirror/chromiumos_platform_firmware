#!/bin/sh
#
# Copyright (c) 2010 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
#
# For factory and auto update, after shell-ball self-extracts, this script is
# called to update BIOS and EC firmware as per how many files are extracted.
# To simply design, THIS SCRIPT MUST BE EXECUTED IN A R/W EXCLUSIVE TEMP FOLDER.
# AND ALL FILENAMES FOR INPUT AND OUTPUT MUST NOT CONTAIN SPACE.

SCRIPT_BASE="$(dirname "$0")"
. "$SCRIPT_BASE/common.sh"

# customizable parameters
# ----------------------------------------------------------------------------
# If you want to customize any of these parameters, make a script and name it
# as the constant string in CUSTOM_SCRIPT_FILENAME.

# The script to be loaded for customization;
# please put all your own settings in this file.
CUSTOM_SCRIPT_FILENAME='legacy_updater_custom.sh'

# Default file names of carried (target) image files
BIOS_IMAGE_FILENAME='bios.bin'
EC_IMAGE_FILENAME='ec.bin'

# flashrom(8) tool path, can be changed to use system default flashrom
FLASHROM_TOOLPATH='flashrom'

# Default command to reboot immediately
CHROMEOS_REBOOT_CMD="reboot"

# Default command to decode fmap
CHROMEOS_FMAP_DECODE_CMD='mosys -k eeprom map'
# note: an alternative is "fmap_decode, or dump_fmap"

# Default Chrome OS control tag file to request firmware update after reboot
CHROMEOS_NEED_REBOOT_TAG='/mnt/stateful_partition/.need_firmware_update'

# Workaround for chrome-os-partner:1563. Backup for CMOS flag.
NEED_FIRMWARE_TRYB='/mnt/stateful_partition/.need_firmware_tryb'

# Default command to change boot index.
CHROMEOS_CHANGE_BOOT_INDEX_CMD='crossystem fwb_tries=1 || true'

# Default command to query next (planned) boot index
CHROMEOS_QUERY_TRIAL_BOOT_CMD='crossystem fwb_tries || true'

# Default command to select BIOS as flashrom target
CHROMEOS_SELECT_BIOS_OPT='-p internal:bus=spi'

# Default command to select EC (Embedded Controller) as flashrom target
CHROMEOS_SELECT_EC_OPT='-p internal:bus=lpc'

# Default Chrome OS BIOS firmware memory layout,
# overwrite this if you plan to support different layout
# TODO(hungte) we may remove this and always rely on fmap in the future.
# NOTE: RW_VPD actually exists inside NV_COMMON_STORE now (may be moved to
# somewhere else anyway), since we rely on fmap now so putting it there
# should be OK.
CHROMEOS_BIOS_LAYOUT_DESC="
  FV_LOG          = 0x020000,
  NV_COMMON_STORE = 0x010000,
  VBOOTA          = 0x010000,
  FVMAIN          = 0x0D0000,
  VBOOTB          = 0x010000,
  FVMAINB         = 0x0D0000,
  NVSTORAGE       = 0x010000,
  RW_VPD          = 0x001000,
  FV_RW_RESERVED  = *,
  |
  FV_RO_RESERVED  = *,
  RO_VPD          = 0x020000,
  FVDEV           = 0x100000,
  FV_RO_DATA      = 0x020000,
  FV_GBB          = 0x040000,
  FV_BSTUB        = 0x040000,
"

# Default Chrome OS BIOS firmware write-protection memory layout.
CHROMEOS_BIOS_WP_LAYOUT_DESC="
  RW,
  |
  RO,
"

# Default Chrome OS BIOS Firmware RO/RW list
CHROMEOS_BIOS_RO_LIST="FV_BSTUB FVDEV"
CHROMEOS_BIOS_RW_A_LIST="VBOOTA FVMAIN"
CHROMEOS_BIOS_RW_B_LIST="VBOOTB FVMAINB"
# To support changing GBB/HWID after firmware update (in factory),
# we only check/update GBB in factory mode.
CHROMEOS_BIOS_FACTORY_RO_LIST="FV_BSTUB FV_GBB FVDEV"

# Default VBOOT section content (for checking layout correctness)
CHROMEOS_VBOOT_LIST="VBOOTA VBOOTB"
CHROMEOS_VBOOT_SIGNATURE="CHROMEOS"
CHROMEOS_VBOOT_SIGNATURE_LENGTH=8

# Default (GBB) section which contains rootkey
CHROMEOS_ROOTKEY_SECTION_NAME="FV_GBB"

# Default Chrome OS Embedded Controller firmware memory layout,
# overwrite this if you plan to support different layout
CHROMEOS_EC_LAYOUT_DESC="
  EC_RO
  |
  EC_RW
"

# Default Chrome OS Embedded Controller firmware write-protection memory layout.
CHROMEOS_EC_WP_LAYOUT_DESC="
  RO,
  |
  RW,
"

# Default Chrome OS EC Firmware RO/RW list
CHROMEOS_EC_RO_LIST="EC_RO"
CHROMEOS_EC_RW_LIST="EC_RW"

# Default Chrome OS Firmware verification skip list
# Syntax: list of string tuples (separated by ':'): PARTNAME:OFFSET:SIZE
# Use this when flashrom chip will be automatically changed data during rewrite
# (eg, time stamp, checksum)
CHROMEOS_BIOS_SKIP_VERIFY_LIST=""
CHROMEOS_EC_SKIP_VERIFY_LIST="EC_RO:0x48:4"

# A list (separated by space) of sections that you want to always preserve, even
# in factory whole image rewrite. Example: "RO_VPD FV_GBB"
CHROMEOS_BIOS_PRESERVE_LIST=""
CHROMEOS_EC_PRESERVE_LIST=""

# Default list of those targets requiring foreground execution for updates
# (eg, Embedded Controller, because it may freeze keyboard)
CHROMEOS_NEED_FOREGROUND_UPDATE_TARGETS=""

# Default list of those targets requiring to reboot immediately after updates
# (eg, Embedded Controller, because it may freeze keyboard)
# Note: does not apply to factory, as factory install is noninteractive.
CHROMEOS_NEED_DIRECT_REBOOT_UPDATE_TARGETS=""

# Default list of targets to be updated. Can be changed by --targets='xxx'.
CHROMEOS_UPDATE_TARGETS="bios ec"

# ----------------------------------------------------------------------------
# Global variables

# Set this to 1 for more messages
is_verbose=0

# Set this to 1 to allow rebooting system directly if required
is_allow_reboot=0

# Set this to 1 to enable background update process
is_background=0

# Set this to 1 to enable factory test / installer mode
is_factory=0

# Set this to 1 to check/update only RW parts (skip RO),
#   0 for RW (A/B at the same time) plus RO code (boot stub / recovery image)
#   and keep VPD/GBB.
is_rw_only=1

# Set this to 1 to enable checking flash memory layout compatibility
is_check_layout=1

# Set this to 1 to enable checking ChromeOS rootkey compatibility
is_check_rootkey=1

# Set this to 1 to enable checking ChromeOS firmware signature (VBOOT)
is_check_vboot=1

# Set this to 0 to disable Chrome OS A/B style 2 stage update
# (can be changed by --rw-ab-together)
allow_2stage_update=1

# Set this to 1 to enable write protection after update complete
is_enable_write_protect=0

# Set this to 1 to enable double confirm of each write to flashrom
is_always_verify=0

# Set this to 1 to provide debug messages
is_debug=0

# Set to 0 if you don't want to use fmap_decode for layout
allow_fmap_decode_layout=1

# (internal) File name of current flashrom data
CURRENT_IMAGE='_current.bin'
# (internal) File name of current flashrom layout
CURRENT_LAYOUT='_layout.txt'
# (internal) Skip verify list of current target
CURRENT_SKIP_VERIFY=""
# (internal) Target option for flashom
CURRENT_TARGET_OPT=""
# (internal) List to for keeping what targets has been modified.
CURRENT_MODIFIED_TARGETS=""

# (internal) layout data structure
# Many POSIX shells do not support association/array, let's use a simple
# string list to simulate array (since the numer of list is not so much).
LAYOUT_LIST=""
LAYOUT_SIZE=""
LAYOUT_OFFSET=""

# Script should exit on any unexpected error
set -e

# Tool program (flashrom, mosys, ...) search path:
# 1. bundled (.) 2. $INSTALL_ROOT/* 3. defaults (/usr/sbin,/sbin) and $PATH
PATH="/usr/sbin:/sbin:$PATH"
if [ -n "${INSTALL_ROOT}" -a "${INSTALL_ROOT}" != "/" ]; then
  PATH="${INSTALL_ROOT}/usr/bin:${INSTALL_ROOT}/bin:$PATH"
  PATH="${INSTALL_ROOT}/usr/sbin:${INSTALL_ROOT}/sbin:$PATH"
fi
PATH=".:$PATH"; export PATH

# ----------------------------------------------------------------------------
# Firmware Update Procedure
#
# TODO(hungte) We can check firmware version number with the version in TPM
#              later. If it's older than TPM then we can skip update process
#              because we know it will eventually fail.
# ----------------------------------------------------------------------------
# We support both factory setup and auto-update here.
# 1. Read current flashrom.
# 2. If RO part is different (factory first-time setup), rewrite whole image:
#    2a. If rewrite failed, abort.
#    2b. If rewrite succeeded, exit as normal.
# 3. If we are running in 'factory test' or 'factory installer' mode,
#    update all different RW parts at the same time.
#    3a. If rewrite failed, abort.
#    3b. If rewrite succeeded, exit as normal.
# 4. (partial-update) Compare RW parts, and rewrite only those different.
#    NOTE: for Chrome OS main firmware (A/B design), see "Chrome OS Main
#    Firmware Update Procedure" below, otherwise:
#    4a. If rewrite failed, abort.
#    4b. If rewrite succeeded, exit as normal or reboot if required.
# ----------------------------------------------------------------------------
# Chrome OS Main Firmware Update Procedure:
#   The following procedure use these terms to refer firmware images:
#   A = firmware A in current flash ROM (VBOOTA+FVMAIN)
#   B = firmware B in current flash ROM (VBOOTB+FVMAINB)
#   T = "target", image carried in update pack (shell-ball, part A == part B)
# 1. If T == B == A, that means no need to update.
#    Do nothing and exit.
# 2. Get last boot firmware index.
# 3. If last boot = B...
#    Do copy B to A.
#    3a. If T == B, that means a successful update.
#        Do nothing and exit.
#    3b. If T != B, that means A went bad.
#        Skip to 5.
# 4. Now last boot = A.
#    If T == B, that means trial boot from B failed;
#        Do copy A to B and exit.
# 5. Now T != B...
#    Do copy T to B.
#    5a. If T != A, this is a fresh update we need to try.
#        Set firmware_try_b and reboot.
#    5b. If T == A, this means B went bad.
#        Simply do nothing (no need to reboot).
# ----------------------------------------------------------------------------

# Utilities

is_positive() {
  # Returns $FLAGS_TRUE or $FLAGS_FALSE for compare result of $1 > 0
  # NOTE: this function CANNOT invoke check_param.
  [ $1 -gt 0 ]
}

list_car() {
  # (lisp-style) Prints the first parameter (to ease parsing)
  # NOTE: this function CANNOT invoke check_param.
  echo $1
}

list_cdr() {
  # (lisp-style) Discards the first parameter and prints the remainings
  # NOTE: this function CANNOT invoke check_param.
  shift
  echo $@
}

list_length() {
  # (lisp-style) Prints the length of param.
  # NOTE: this function CANNOT invoke check_param.
  echo $#
}

nth() {
  # (lisp-style) Prints the (zero-based) nth parameter
  # NOTE: this function CANNOT invoke check_param.
  # Syntax: nth(n, ...)
  shift $(($1 + 1))
  echo $1
}

assert_str() {
  # Checks if $1 is non-empty string, and print message provided in $2
  # NOTE: this function CANNOT invoke check_param.
  if [ -z "$1" ]; then
    if [ -z "$*" ]; then
      err_die "!!! assert_str failed. abort."
    else
      err_die "!!! assert_str failed: $*. abort."
    fi
  fi
}

in_list() {
  # Determines if a given name is in shell-style list.
  check_param "in_list(name, list)" "$@"

  local name
  for name in $2; do
    if [ "$name" = "$1" ]; then
      return $FLAGS_TRUE
    fi
  done
  return $FLAGS_FALSE
}

echo_file_size() {
  # Prints the size of file $1
  check_param "echo_file_size(filename)" "$@"

  list_car $(wc -c $1)
}

execute_command() {
  # Executes a shell command (and provide message if required)
  check_param "execute_command(cmd)" "$@"
  local ret=$FLAGS_TRUE

  # to interpret quotes correctly, use $* instead of $@
  is_positive $is_verbose && alert "  * execute_command: $*" || true
  ( eval "$@" ) >_exec.stdout 2>_exec.stderr || ret=$?
  if [ "$ret" != "0" ]; then
    alert " !! Execution failed ($ret): $*"
    alert " Messages:"
    cat _exec.stdout >&2
    cat _exec.stderr >&2
  fi
  return $ret
}

has_command() {
  # Test if some command is available in this system.
  check_param "has_command(cmd)" "$@"

  # XXX 'type' in dash does not have '-P' option
  type "$1" >/dev/null 2>&1 || return $?
}

compare_file() {
  # Compare two files (platform independent)
  check_param "compare_file(file1, file2)" "$@"

  # try to find a file compare tool...
  if has_command cmp; then
    # cmp and diff are usually in same package.
    debug_msg "compare with: cmp -s $1 $2"
    cmp -s $1 $2 || return $?
  elif has_command md5sum; then
    debug_msg "compare with: md5sum $1 $2"
    local md5_1 md5_2
    md5_1=$(md5sum $1) && md5_1=$(list_car $md5_1) || err_die "cannot md5sum $1"
    md5_2=$(md5sum $2) && md5_2=$(list_car $md5_2) || err_die "cannot md5sum $2"
    debug_msg "md5: $1=$md5_1, $2=$md5_2"
    [ "$md5_1" = "$md5_2" ] || return $?
    [ -n "$md5_1" ] || return $?
  elif has_command od; then
    debug_msg "compare with: od $1 $2"
    local od1 od2
    od1=$(od -t x1 $1) || err_die "cannot od $1"
    od2=$(od -t x1 $2) || err_die "cannot od $2"
    [ "$od1" = "$od2" ] || return $?
  else
    # TODO(hungte) we may even use hexdump, od, hd, uuencode...
    err_die "sorry, cannot find any file compare tools."
  fi
  return $FLAGS_TRUE
}

# ----------------------------------------------------------------------------
# Flashrom Specific Utilities

flashrom_read_whole() {
  # Reads entire flashrom into $CURRENT_IMAGE file.
  check_param "flashrom_read_whole()" "$@"

  execute_command "$FLASHROM_TOOLPATH $CURRENT_TARGET_OPT -r $CURRENT_IMAGE" ||
    err_die "cannot read flashrom"
}

flashrom_write_whole() {
  # Writes entire flashrom image from given file.
  check_param "flashrom_read_whole(image_file)" "$@"

  execute_command "$FLASHROM_TOOLPATH $CURRENT_TARGET_OPT -w $1" ||
    err_die "cannot write whole flashrom"
}

flashrom_lookup_section_info() {
  # looks up section and return related nth data
  check_param "flashrom_lookup_section_info(name, info_list)" "$@"

  local name i=0
  for name in $LAYOUT_LIST; do
    if [ "$name" = "$1" ]; then
      echo $(nth $i $2)
      return
    fi
    i=$(($i + 1))
  done
  err_die "invalid section name: $1 [$2]"
}

flashrom_echo_section_offset() {
  # Prints section offset from current layout
  check_param "flashrom_echo_section_offset(section_name)" "$@"

  flashrom_lookup_section_info $1 "$LAYOUT_OFFSET"
}

flashrom_echo_section_size() {
  # Prints section size from current layout
  check_param "flashrom_echo_section_size(section_name)" "$@"

  flashrom_lookup_section_info $1 "$LAYOUT_SIZE"
}

flashrom_check_valid_section_name() {
  # Checks if $1 is a valid section name.
  check_param "flashrom_check_valid_section_name(section_name)" "$@"

  # if anything goes wrong, flashrom_lookup_section_info should die
  flashrom_echo_section_offset $1 >/dev/null
  flashrom_echo_section_size $1 >/dev/null
}

flashrom_partial_write() {
  # Writes (partially) given sections to flashrom
  check_param "flashrom_partial_write(list, image_file)" "$@"

  local list i
  for i in $1 ; do
    # check if every given section is in layout because flashrom does not check.
    flashrom_check_valid_section_name $i
    list="$list -i $i"
  done
  local opt="$CURRENT_TARGET_OPT -l $CURRENT_LAYOUT $list -w $2"
  execute_command "$FLASHROM_TOOLPATH $opt" ||
    err_die "flashrom_partial_write failed"
}

flashrom_select_target() {
  # Selects flashrom mapping target
  check_param "flashrom_select_target(target)" "$@"

  case $1 in
    bios )
      CURRENT_TARGET_OPT="$CHROMEOS_SELECT_BIOS_OPT"
      ;;
    ec )
      CURRENT_TARGET_OPT="$CHROMEOS_SELECT_EC_OPT"
      ;;
    reset )
      CURRENT_TARGET_OPT=""
      ;;
    * )
      err_die "unknown target for flashrom selection: $1"
  esac
}

flashrom_enable_write_protect() {
  # Enables the "write protection" for specified section on flashrom.
  check_param "flashrom_enable_write_protect(section)" "$@"

  flashrom_check_valid_section_name $1 ||
    err_die "flashrom_enable_write_protect: invalid target section: $1"
  local offset=$(flashrom_echo_section_offset $1)
  local size=$(flashrom_echo_section_size $1)

  debug_msg "flashrom_enable_write_protect(section=$1, off=$offset, sz=$size)"
  local opt="$CURRENT_TARGET_OPT"
  execute_command "$FLASHROM_TOOLPATH $opt --wp-range $offset $size" ||
    err_die "flashrom_enable_write_protect failed (wp-range $offset $size)"
  execute_command "$FLASHROM_TOOLPATH $opt --wp-enable" ||
    err_die "flashrom_enable_write_protect failed (wp-enable $offset $size)"
}

flashrom_build_layout_file() {
  # Creates $CURRENT_LAYOUT file according to current layout information
  check_param "flashrom_build_layout_file()" "$@"

  # write layout file
  rm -f $CURRENT_LAYOUT
  local i=0 name size offset off_beg off_end off_size
  for name in $LAYOUT_LIST ; do
    size=$(nth $i $LAYOUT_SIZE)
    offset=$(nth $i $LAYOUT_OFFSET)
    off_beg=$(printf '0x%06x' $offset)
    off_end=$(printf '0x%06x' $(($offset + $size - 1)))
    off_size=$(printf '0x%06x' $size)
    echo "$off_beg:$off_end $name" >>$CURRENT_LAYOUT
    debug_msg "LAYOUT: off=$off_beg size=$off_size $name -> $offset $size"
    i=$(($i + 1))
  done

  [ $i != 0 ] || err_die "Empty memory layout information."
}

flashrom_detect_layout_by_fmap_decode() {
  # Detects flashrom layout by given image file and creates $CURRENT_LAYOUT
  check_param "flashrom_detect_layout_by_fmap_decode(image_file)" "$@"

  debug_msg "trying flashrom_detect_layout_by_fmap_decode"
  local voff vsize vnames pre_parse
  pre_parse="$($CHROMEOS_FMAP_DECODE_CMD $1 2>/dev/null)" ||
    return $FLAGS_FALSE
  if [ -z "$pre_parse" ]; then
    return $FLAGS_FALSE
  fi
  debug_msg "detect_layout_by_fmap_decode"
  voff=$(echo "$pre_parse" |
         grep 'area_offset="' |
         sed 's/.*area_offset="\([^"]*\)".*/\1/' );
  vsize=$(echo "$pre_parse" |
          grep 'area_size="' |
          sed 's/.*area_size="\([^"]*\)".*/\1/' );
  # debug_msg "offsets: $voff"
  # debug_msg "sizes: $vsize"

  # convert and build name array. Note shell script does not allow spaces in
  # list, so we convert spaces to '%20' temporary.
  vnames=$(echo "$pre_parse" | sed 's/" area_/"\tarea_/g' | awk -F '\t' '
  BEGIN {
    n="Log Volume"; map[n]="FV_LOG";
    n="Firmware A Key"; map[n]="VBOOTA";
    n="Firmware A Data"; map[n]="FVMAIN";
    n="Firmware B Key"; map[n]="VBOOTB";
    n="Firmware B Data"; map[n]="FVMAINB";
    n="Recovery Firmware"; map[n]="FVDEV";
    n="GBB Area"; map[n]="FV_GBB";
    n="Boot Stub"; map[n]="FV_BSTUB";
    n="RO VPD"; map[n]="RO_VPD";
    n="RW VPD"; map[n]="RW_VPD";

    n="VBLOCK_A"; map[n]="VBOOTA";
    n="FW_MAIN_A"; map[n]="FVMAIN";
    n="VBLOCK_B"; map[n]="VBOOTB";
    n="FW_MAIN_B"; map[n]="FVMAINB";
    n="GBB"; map[n]="FV_GBB";
    n="BOOT_STUB"; map[n]="FV_BSTUB";
    n="RECOVERY"; map[n]="FVDEV";
  }
  /area_/ {
    gsub( /.*area_name="/, "" );
    gsub( /".*/, "" );
    if ($1 in map)
      print map[$1];
    else {
      gsub( / /, "%20" );
      print $1;
    }
  }
  ')

  # verify collected information
  [ $(list_length $voff) -eq $(list_length $vsize) ] ||
    err_die "number of offsets ($(list_length $voff))" \
          " must match sizes ($(list_length $vsize))"
  [ $(list_length $voff) -eq $(list_length $vnames) ] ||
    err_die "number of offsets ($(list_length $voff))" \
          " must match names ($(list_length $vnames))"

  # rebuild layout information
  LAYOUT_LIST="$vnames"
  local i=0
  local name
  for name in $LAYOUT_LIST; do
    LAYOUT_SIZE="$LAYOUT_SIZE $(($(nth $i $vsize)))"
    LAYOUT_OFFSET="$LAYOUT_OFFSET $(($(nth $i $voff)))"
    i=$(($i + 1))
  done

  flashrom_build_layout_file
  return $FLAGS_TRUE
}

flashrom_detect_layout_by_default_map() {
  # Detects flashrom layout by $CURRENT_IMAGE and creates $CURRENT_LAYOUT
  check_param "flashrom_detect_layout_by_default_map(target)" "$@"

  debug_msg "flashrom_detect_layout: by_default_map"
  if [ ! -s $CURRENT_IMAGE ]; then
    err_die "invalid flashrom image in $CURRENT_IMAGE"
  fi
  local rom_size=$(echo_file_size $CURRENT_IMAGE)
  local layout_desc

  case $1 in
    bios )
      layout_desc="$CHROMEOS_BIOS_LAYOUT_DESC"
      ;;
    ec )
      layout_desc="$CHROMEOS_EC_LAYOUT_DESC"
      ;;
    bios_wp )
      layout_desc="$CHROMEOS_BIOS_WP_LAYOUT_DESC"
      ;;
    ec_wp )
      layout_desc="$CHROMEOS_EC_WP_LAYOUT_DESC"
      ;;
    * )
      err_die "unknown target for layout detection: $1"
  esac

  # calculate block size
  local blocks=$(($(echo $layout_desc | sed 's/[^|]//g' | wc -c)))
  local bs=$((rom_size / blocks))

  # canonicalize layout description
  local parsed=$(echo $layout_desc | sed 's/[ \t]*//g; s/[|]/,|,/g')

  # use awk to generate the form NAME=SIZE.
  # variable length fields will get duplicated (with determined final size) at
  # end of each block, with prefix '|' -- this is called "fix-up field".
  # NOTE: awk compatibility issue here.
  #       awk on modern BSD takes 0x as hex numbers no strtonum.
  #       gawk (awk on Linux) has strtonum bug accepts 0x only in --posix.
  local awk_cmd='gawk --posix'
  has_command gawk || awk_cmd='awk'
  debug_msg "selected awk command: $awk_cmd"
  parsed="$(echo -n "$parsed" | $awk_cmd -v bs=$bs '
    BEGIN    { FS = "="; RS="," }
    /^[^\|]/ { v=$2+0; print $1"="v; sum+=v; if(v==0) zero=$1 }
    /^\|/    { print "|"zero"="bs-sum; sum=0 }
    END      { print "|"zero"="bs-sum; sum=0 }
  ')" || err_die "flashrom_detect_layout: failed to process with awk."

  # build list and size of each section
  local entry ename esize
  for entry in $parsed ; do
    ename=${entry%%=*}
    assert_str "$ename" "parsing layout failed: $entry, missing section name"
    esize=${entry##*=}
    assert_str "$ename" "parsing layout failed: $entry, missing section size"
    esize=$(($esize))
    if [ "${ename%%|*}" = "" ]; then
      # found a "fix-up" field, modify the field.
      debug_msg "fix layout size [$ename] to $esize"
      LAYOUT_SIZE=$(echo $LAYOUT_SIZE | sed "s/FIXME/$esize/")
    else
      [ $esize -eq 0 ] && esize=FIXME || true
      LAYOUT_SIZE="$LAYOUT_SIZE $esize"
      LAYOUT_LIST="$LAYOUT_LIST $ename"
    fi
  done

  # build offsets
  local offset=0
  for esize in $LAYOUT_SIZE; do
    LAYOUT_OFFSET="$LAYOUT_OFFSET $offset"
    offset=$(($offset + $esize))
  done

  flashrom_build_layout_file
}

flashrom_reset_layout() {
  # Resets all previous detected layout information

  LAYOUT_LIST=""
  LAYOUT_SIZE=""
  LAYOUT_OFFSET=""
}

flashrom_detect_layout() {
  # Detects flashrom layout by target code or image file name.
  check_param "flashrom_detect_layout(target, image_filename)" "$@"

  flashrom_reset_layout
  assert_str $allow_fmap_decode_layout
  if is_positive $allow_fmap_decode_layout &&
      flashrom_detect_layout_by_fmap_decode $2; then
    debug_msg "using decoded fmap and ignore default map"
  else
    flashrom_detect_layout_by_default_map $1
  fi
}

flashrom_section_filename() {
  # Prints temporary file name for given section name ($1)
  check_param "flashrom_section_filename(section)" "$@"

  echo _$1.blob
}

flashrom_get_section() {
  # Extract a blob data from given section of a image file, and
  # prints the (generated) filename containing section data
  check_param "flashrom_get_section(section, image, ...)" "$@"

  local offset=$(flashrom_echo_section_offset $1)
  local size=$(flashrom_echo_section_size $1)
  local fname=$3$(flashrom_section_filename $1)

  execute_command "dd if=$2 of=$fname bs=1 skip=$offset count=$size" ||
    err_die "flashrom_get_section($1, $2, $3)"
  echo $fname
}

flashrom_put_section() {
  # Overwrite a section in image file from given data blob file
  check_param "flashrom_put_section(section, image, data_file)" "$@"

  local offset=$(flashrom_echo_section_offset $1)
  local size=$(flashrom_echo_section_size $1)
  local data_size=$(echo_file_size $3)

  [ $data_size -eq $size ] ||
    err_die "flashrom_put_section: incompatible data($3) for section '$1'"

  execute_command "dd if=$3 of=$2 bs=1 seek=$offset count=$size conv=notrunc" ||
    err_die "flashrom_put_section($1, $2, $3)"
}

flashrom_cat_section() {
  # Read a blob data (via od) from given section of a image file, and
  # print to stdout.
  check_param "flashrom_cat_section(section, image)" "$@"

  local offset=$(flashrom_echo_section_offset $1)
  local size=$(flashrom_echo_section_size $1)

  # cannot use execute_command here because we need output
  debug_msg "(flashrom_cat_section) od -A n -t x1 -j $offset -N $size $2"
  od -A n -t x1 -j $offset -N $size $2 ||
    err_die "flashrom_cat_section($1, $2)"
}

flashrom_set_skip_verify_list() {
  # Enables and checks a 'skip verify' list
  check_param "flashrom_set_skip_verify_list(opt_list)" "$@"

  local ventry vsec voff vsize
  local section_size
  for ventry in $1 ; do
    # decompose name:offset:size
    vsec=$(echo $ventry | cut -d: -f 1) || err_die "invalid entry: $ventry"
    voff=$(($(echo $ventry | cut -d: -f 2))) || err_die "invalid entry: $ventry"
    vsize=$(($(echo $ventry | cut -d: -f 3))) || err_die "invalid entry:$ventry"
    flashrom_check_valid_section_name $vsec
    section_size=$(flashrom_echo_section_size $vsec)
    if [ $(($voff + $vsize)) -gt $section_size ]; then
      err_die "skip verify entry exceed section boundary: $ventry"
    fi
  done
  CURRENT_SKIP_VERIFY="$1"
}

flashrom_preserve_sections() {
  # Preserves assigned sections from current flashrom to target image
  check_param "flashrom_preserve_sections(opt_list,current,image)" "$@"
  local new_image_name=$3

  if [ -n "$1" ]; then
    debug_msg "preserve sections: $opt_list"
    new_image_name=$(env TMPDIR=. mktemp _psvXXXXXXXX) ||
      err_die "cannot create temporary file for preserving sections"
    cp -f $3 $new_image_name
    flashrom_copy_image "$1" "$1" $2 $new_image_name
  fi
  echo $new_image_name
  # TODO we need some way to clear the preserve images.
}

flashrom_apply_skip_verify() {
  # Applies 'skip verify' (fill with zero) to section blob
  check_param "flashrom_apply_skip_verify(section, blob_filename)" "$@"

  local ventry vsec voff vsize
  local blobsz
  local section=$1

  for ventry in $CURRENT_SKIP_VERIFY ; do
    # decompose name:offset:size
    vsec=$(echo $ventry | cut -d: -f 1)
    if [ "$vsec" != "$section" ]; then
      continue
    fi
    debug_msg "found skip on $vsec"
    # voff and vsize should be already checked. use them directly.
    voff=$(($(echo $ventry | cut -d: -f 2)))
    vsize=$(($(echo $ventry | cut -d: -f 3)))

    # if udev/devfs is not ready, we may need to use some other big files.
    # $CURRENT seems like a good choice.
    local zero_fn=/dev/zero
    if [ ! -r $zero_fn ]; then
      alert "warning: $zero_fn is not available. use current image."
      zero_fn=$CURRENT_IMAGE
    fi
    debug_msg "apply skip verify: (sec=$1, blob=$2)[$ventry]"

    local dd_opt="if=$zero_fn of=$2 bs=1 seek=$voff count=$vsize conv=notrunc"
    execute_command "dd $dd_opt" ||
      err_die "flashrom_apply_skip_verify($1, $2)[$ventry]"
  done
}

flashrom_verify_sections() {
  # Verifies a list of sections in 2 images
  check_param "flashrom_verify_sections(list1, list2, image1, image2)" "$@"

  # extract images
  local list1="$1" list2="$2" image1="$3" image2="$4"
  local result=0 len1="" len2=""

  # process if we need whole image verification (list=*)
  if [ "$list1" = "*" ]; then
    if [ "$list2" != "*" ]; then
      err_die "(internal error) comparing whole images need both list to '*'".
    fi
    if [ -z "$CURRENT_SKIP_VERIFY" ]; then
      # quick compare
      compare_file $image1 $image2 || result=$?
      debug_msg "flashrom_verify_image('$image1','$image2'): $result"
      return $result
    else
      err_die "sorry, not implemented now."
    fi
  else
    # set $list2 to sequential parameters. $[1-4] becomes different now.
    set $list2
    local blob1 blob2 section1 section2
    for section1 in $list1; do
      section2=$1
      assert_str "$section2" "unmatched verify list: $list1 <-> $list2"
      shift
      # TODO(hungte) even if skip_verify list is not empty, we can still use od
      # to compare those fields not in skip_verify list.
      if has_command od && [ -z "$CURRENT_SKIP_VERIFY" ]; then
        # quick compare via od+shell
        blob1=$(flashrom_cat_section $section1 $image1) &&
          blob2=$(flashrom_cat_section $section2 $image2) ||
          err_die "invalid section in $section1 $section2"
        len1=${#blob1}
        len2=${#blob2}
        # XXX we can't check "$len1=$len2" because cat results may be very
        # different.
        debug_msg "(cat) section1[$section1]:$len1, section2[$section2]:$len2"
        [ $len1 -gt 0 ] || err_die "empty section!? check $section1"
        [ $len2 -gt 0 ] || err_die "empty section!? check $section2"
        [ "$blob1" = "$blob2" ] || result=$?
        unset blob1 blob2  # hope this helps free memory
      else
        # extract blobs and compare
        blob1=$(flashrom_get_section $section1 $image1 _fvs1) &&
          blob2=$(flashrom_get_section $section2 $image2 _fvs2) ||
          err_die "invalid section in $section1 $section2"
        flashrom_apply_skip_verify $section1 $blob1
        flashrom_apply_skip_verify $section2 $blob2
        len1="$len1,$(echo_file_size $blob1)" &&
          len2="$len2,$(echo_file_size $blob2)" ||
          err_die "cannot get file size: $blob1 $blob2"
        [ "$len1" = "$len2" ] ||
          err_die "flashrom_verify_sections: unmatched length: " \
                  "$section1:$len1 $section2:$len2"
        compare_file $blob1 $blob2 || result=$?
        # clean up if not debug mode
        is_positive $is_debug || rm -f $blob1 $blob2
      fi
      # quick abort
      [ "$result" = "0" ] || break
    done
  fi

  debug_msg "flashrom_verify_sections($list1,$list2,$image1,$image2): $result"
  return $result
}

flashrom_copy_image() {
  # Copies section data between two image files.
  check_param "flashrom_copy_image(list_src, list_dst, img_src, img_dst)" "$@"

  # extract images
  local section_src section_dst
  local list_src="$1" list_dst="$2" image_src="$3" image_dst="$4"
  set $2
  for section_src in $list_src; do
    section_dst=$1
    shift
    debug_msg "flashrom_copy_image: $section_src->$section_dst"
    assert_str "$section_dst" "copy_image: unmatched: '$list_src':'$list_dst'"
    local offset_src=$(flashrom_echo_section_offset $section_src)
    local size_src=$(flashrom_echo_section_size $section_src)
    local offset_dst=$(flashrom_echo_section_offset $section_dst)
    local size_dst=$(flashrom_echo_section_size $section_dst)
    if [ "$size_src" != "$size_dst" ]; then
      err_die "copy_image: incompatible section: $section_src,$section_dst"
    fi
    local dd_cmd="dd if=$image_src of=$image_dst bs=1 conv=notrunc"
    dd_cmd="$dd_cmd skip=$offset_src seek=$offset_dst count=$size_src"
    execute_command "$dd_cmd" || err_die "flashrom_copy_image() faild"
  done
}

# ----------------------------------------------------------------------------
# Chrome OS Specific Utilities

chromeos_need_reboot() {
  # Utility to schedule a system reboot.
  check_param "chromeos_need_reboot()" "$@"

  # In current Chrome OS Auto Update design, the actual behavior is to
  # request firmware update script being executed again after reboot.
  touch "$CHROMEOS_NEED_REBOOT_TAG" || err_die "cannot tag for reboot"
  sync  # to make sure the tag is flushed to disk
}

chromeos_get_last_boot_index() {
  # Utility to get last boot BIOS firmware index
  check_param "chromeos_get_last_boot_index()" "$@"

  # See "Google Chrome OS Firmware - High Level Specification" section
  # "BINF (Chrome OS boot information)" for more detail.
  local binf_val="$(crossystem mainfw_act || true)"
  debug_msg "original binf_val=$binf_val"

  # Possible values: 'A', 'B', 'recovery'
  # get_index return values requires 0=A, 1=B
  case $binf_val in
    A | recovery )
      return 0
      ;;
    B )
      return 1
      ;;
    * )
      err_die "unknown last boot index in BINF.1: [$binf_val]."
  esac
}

chromeos_check_scheduled_trial_reboot() {
  # Utility to check if a reboot with try_firmware_b is scheduled.
  # Returns $FLAGS_TRUE or $FLAGS_FALSE
  check_param "chromeos_check_scheduled_trial_reboot()" "$@"

  local bootinfo=$(eval $CHROMEOS_QUERY_TRIAL_BOOT_CMD 2>/dev/null)
  if [ "$bootinfo" = "1" ]; then
    debug_msg "chromeos_check_scheduled_trial_reboot: TRUE"
    return $FLAGS_TRUE
  fi
  debug_msg "chromeos_check_scheduled_trial_reboot: FALSE"
  return $FLAGS_FALSE
}

chromeos_change_boot_index() {
  # Utility to change firmware boot index.
  check_param "chromeos_change_boot_index()" "$@"

  # In Chrome OS, we use the "try_firmware_b".
  execute_command "$CHROMEOS_CHANGE_BOOT_INDEX_CMD" ||
    err_die "chromeos_change_boot_index(): failed to set try_firmware_b"

  # Workaround for chrome-os-partner:1563. Touch the try-firmware-B flag file
  # in case CMOS loses the flag.
  touch "$NEED_FIRMWARE_TRYB"
}

chromeos_need_foreground() {
  # Determines if given target needs foreground update.
  # Returns $FLAGS_TRUE or $FLAGS_FALSE
  check_param "chromeos_need_foreground(target)" "$@"

  case $1 in
    bios | ec )
      true
      ;;
    * )
      err_die "chromeos_need_foreground: invalid target ($1)"
  esac

  local target
  for target in $CHROMEOS_NEED_FOREGROUND_UPDATE_TARGETS; do
    if [ "$target" = $1 ]; then
      debug_msg "chromeos_need_foreground: NEED ($target)"
      return $FLAGS_TRUE
    fi
  done
  return $FLAGS_FALSE
}

chromeos_post_update() {
  # Put all procedures / checks here to perform after an update (which really
  # changed some data on flashrom).
  check_param "chromeos_post_update(target)" "$@"

  # Log modified targets
  CURRENT_MODIFIED_TARGETS="$CURRENT_MODIFIED_TARGETS $1"
}

chromeos_check_background_update() {
  # Checks if given target is safe for background update.
  # Returns $FLAGS_TRUE for safe to continue, $FLAGS_FALSE to abort.
  check_param "chromeos_check_background_update(target)" "$@"

  assert_str $is_background
  local target="$1"
  if is_positive $is_background && chromeos_need_foreground "$target"; then
    echo "  - running in background update mode, postpone to next boot."
    chromeos_need_reboot || err_die "cannot set reboot tag"
    return $FLAGS_FALSE
  fi
  return $FLAGS_TRUE
}

chromeos_check_same_root_keys() {
  # Checks if the root keys (from Google Binary Block) are the same
  # Returns $FLAGS_TRUE for same, $FLAGS_FALSE for different,
  # $FLAGS_ERROR for missing in current image
  check_param "chromeos_check_same_root_keys(current, target)" "$@"

  # if we can't find gbb_utility, ignore it.
  if ! has_command "gbb_utility"; then
    debug_msg "cannot find gbb_utility."
    return $FLAGS_TRUE
  fi

  # try to retrieve the keys
  local ret=$FLAGS_TRUE
  local keyfile1 keyfile2 keyfile1_strip keyfile2_strip keyfiles
  keyfile1=$(env TMPDIR=. mktemp _gk1XXXXXXXX) ||
    err_die "canot create temporary file for root key retrieval"
  keyfile2=$(env TMPDIR=. mktemp _gk2XXXXXXXX) ||
    err_die "canot create temporary file for root key retrieval"
  keyfile1_strip=${keyfile1}_strip
  keyfile2_strip=${keyfile2}_strip
  keyfiles="$keyfile1 $keyfile2 $keyfile1_strip $keyfile2_strip"
  # current may not contain root key, but target MUST have a root key
  if execute_command "gbb_utility -g --rootkey=$keyfile1 $1" 2>/dev/null; then
    execute_command "gbb_utility -g --rootkey=$keyfile2 $2" ||
      err_die "cannot get rootkey from $2"
    # to workaround key paddings...
    cat $keyfile1 | sed 's/\xff*$//g; s/\x00*$//g;' >$keyfile1_strip
    cat $keyfile2 | sed 's/\xff*$//g; s/\x00*$//g;' >$keyfile2_strip
    compare_file "$keyfile1_strip" "$keyfile2_strip" || ret=$FLAGS_FALSE
  else
    debug_msg "warning: cannot get rootkey from $1"
    ret=$FLAGS_ERROR
  fi

  # clean up if not debug mode
  is_positive $is_debug || rm -f $keyfiles
  debug_msg "chromeos_check_same_root_keys: result(shell-style)=$ret"
  return $ret
}


# ----------------------------------------------------------------------------
# Updater

start_firmware_section_update() {
  # Updates some sections into system flashrom.
  check_param "start_firmware_section_update(src_list, dst_list, image) " "$@"
  debug_msg "start_firmware_section_update(src=$1,dest=$2,image=$3)"

  # create image for flashrom
  debug_msg "creating temporary image file"
  local tmp_image
  tmp_image=$(env TMPDIR=. mktemp _imgXXXXXXXX) ||
    err_die "cannot create temporary file for building image"
  cp -f $CURRENT_IMAGE $tmp_image
  flashrom_copy_image "$1" "$2" $3 $tmp_image ||
    err_die "failed to create temporary image for update"

  # write to flashrom
  debug_msg "writing partial ($2) image to flashrom"
  flashrom_partial_write "$2" $tmp_image ||
    err_die "failed to partial write to flashrom"

  # verify image
  if is_positive $is_always_verify ; then
    debug_msg "verifying image from flashrom: read current image"
    flashrom_read_whole ||
      err_die "cannot read flashrom for verification"
    # TODO(hungte) current implementation verifies only copy destination.
    # we may consider comparing whole image in the future.
    debug_msg "verifying image from flashrom: compare with target image"
    flashrom_verify_sections "$1" "$2" $CURRENT_IMAGE $tmp_image ||
      err_die "update result: flashrom image data verification failed."
  fi

  # clean up if not debug mode
  is_positive $is_debug || rm -f $tmp_image
}

chromeos_firmware_AB_update() {
  # Performs Chrome OS "AB Main Firmware" Update Rules
  check_param "chromeos_firmware_AB_update(listA, listB, new_image)" "$@"

  local listA="$1"
  local listB="$2"
  local target=$3
  local base=$CURRENT_IMAGE
  local index_A=0
  local index_B=1
  local T_equals_A=1
  local T_equals_B=1

  flashrom_verify_sections "$listA" "$listA" $base $target || T_equals_A=0
  flashrom_verify_sections "$listB" "$listB" $base $target || T_equals_B=0
  debug_msg "AB_update: T_equals_A=$T_equals_A T_equals_B=$T_equals_B"

  # T==A==B should be already handled...
  if is_positive $T_equals_A && is_positive $T_equals_B ; then
    err_die "T==A==B, must be handled somewhere else."
  fi

  # A, B should be the same for target image.
  flashrom_verify_sections "$listA" "$listB" $target $target ||
    err_die "invalid image file (different A/B section): $target"

  # determine last boot index
  local last_boot_index=0
  chromeos_get_last_boot_index || last_boot_index=$?
  debug_msg "AB_update: last_boot_index=$last_boot_index"
  if [ $last_boot_index != $index_A -a $last_boot_index != $index_B ]; then
    err_die "unknown boot index: $last_boot_index"
  fi

  if [ $last_boot_index = $index_B ]; then
    # do copy B to A
    if is_positive $T_equals_B ; then
      echo "  * action: copy B to A (after successful update)"
      start_firmware_section_update "$listB" "$listA" $base
      return
    else
      echo "  * action: copy B to A (A went bad)"
      start_firmware_section_update "$listB" "$listA" $base
      last_boot_index=$index_A
      T_equals_A=$T_equals_B
    fi
  fi

  if is_positive $T_equals_B &&
    chromeos_check_scheduled_trial_reboot; then
    # A very special case: if boot with B was already scheduled, that means
    # an update has not been verified yet while update program was invoked
    # again. Treat T_equals_B as FALSE to prevent a rollback.
    echo "  * action: skip (same update already scheduled and need a reboot)"
    return
  fi

  if is_positive $T_equals_B ; then
    # trial boot from B failed (need recover)
    echo "  * action: copy A to B (after try-boot failure)"
    start_firmware_section_update "$listA" "$listB" $base
    return
  fi

  # either a flash update, or B went bad
  if ! is_positive $T_equals_A ; then
    echo "  * action: copy T to B (flash update, try_b then reboot)"
    start_firmware_section_update "$listB" "$listB" $target
    echo "  - selecting firmware B for next boot"
    chromeos_change_boot_index || err_die "cannot set try_firmware_b"
    echo "  - need to reboot..."
    chromeos_need_reboot || err_die "cannot set update tag for next reboot."
  else
    echo "  * action: copy T to B (B went bad)"
    start_firmware_section_update "$listB" "$listB" $target
  fi
}

check_chromeos_vboot_section() {
  # Checks if the layout has proper VBOOT record
  check_param "check_chromeos_vboot_section(vboot_list, image_file)" "$@"

  local vboot_section_name offset length value
  for vboot_section_name in $1; do
    offset="$(flashrom_echo_section_offset $vboot_section_name)"
    length="$CHROMEOS_VBOOT_SIGNATURE_LENGTH"
    value="$(dd if=$2 bs=1 skip=$offset count=$length 2>/dev/null)"
    debug_msg "check_chromeos_vboot_section: $vboot_section_name@$offset:$value"
    if [ "$value" != "$CHROMEOS_VBOOT_SIGNATURE" ]; then
      return $FLAGS_FALSE
    fi
  done
  return $FLAGS_TRUE
}

build_layout_signature() {
  # Builds a signature of given layout sections.
  check_param "build_layout_signature(section_list)" "$@"

  local signature="LAYOUT_SIG"
  local name="" offset="" size=""
  for name in $1; do
    offset=$(flashrom_echo_section_offset $name)
    size=$(flashrom_echo_section_size $name)
    signature="$signature,($name,$offset,$size)"
  done
  echo $signature
}

update_firmware() {
  # Firmware update procedure.
  check_param "update_firmware(
    code, name, image, ro, rw_a,
    opt_rw_b, opt_skip, opt_preserve, opt_vboot, opt_rootkey)" "$@"

  local code=$1
  local image_fname=$3 current_fname=$CURRENT_IMAGE
  local orig_image_fname=$image_fname
  local ro_list="$4" rw_list_a="$5" rw_list_b="$6"
  local rw_list="$rw_list_a $rw_list_b"
  echo " * Updating $2..."
  flashrom_select_target $code
  echo "  - reading current flashrom image"
  flashrom_read_whole

  # Check if target image size is same as current flashrom chip.
  # TODO(hungte) In the future we may allow expanding image to real flashrom
  # chips, but let's assume they must be the same now.
  [ $(echo_file_size $current_fname) -eq $(echo_file_size $image_fname) ] ||
    err_die "Target image size=$(echo_file_size $image_fname) is" \
            "different from real flashrom=$(echo_file_size $current_fname)"

  # process layout
  flashrom_detect_layout $code $current_fname
  local current_layout_signature="$(build_layout_signature "$ro_list $rw_list")"
  debug_msg "current layout signature: $current_layout_signature"
  flashrom_detect_layout $code $image_fname
  local target_layout_signature="$(build_layout_signature "$ro_list $rw_list")"
  debug_msg "target layout signature: $target_layout_signature"
  flashrom_set_skip_verify_list "$7"

  # build a preserved image if required
  if [ -n "$8" ]; then
    image_fname=$(flashrom_preserve_sections "$8" $current_fname $image_fname) \
      || err_die "cannot create temporary file for preserving sections"
  fi

  # if the layout is different, we only allow factory setup
  assert_str $is_check_layout
  if is_positive $is_check_layout &&
    [ "$current_layout_signature" != "$target_layout_signature" ]; then
    if ! is_positive $is_factory; then
      err_die "Image memory layout is incompatible to current system." \
              "You may need to perform a factory install by --factory."
    fi
  fi

  # verify if this seems like a valid layout
  assert_str $is_check_vboot
  if is_positive $is_check_vboot && [ -n "$9" ] &&
    ! check_chromeos_vboot_section "$9" $image_fname; then
    err_die "Invalid memory layout or corrupted ChromeOS firmware image."
  fi

  # check rootkey to see if they are compatible
  local compare_rootkey_result=0
  assert_str $is_check_rootkey
  if is_positive $is_check_rootkey && [ -n "${10}" ]; then
    chromeos_check_same_root_keys $current_fname $image_fname ||
      compare_rootkey_result=$?
  fi

  # if the rootkey seems different, we only allow factory setup
  debug_msg "result: $compare_rootkey_result"
  if [ $compare_rootkey_result -ne 0 ] && ! is_positive $is_factory; then
    if [ $compare_rootkey_result -eq 1 ]; then
      err_die "Incompatible firmware image (Root key is different). " \
              "You may need to perform a factory install by --factory."
    else
      err_die "Cannot find root key in current system. " \
              "You may need to perform a factory install by --factory."
    fi
  fi

  # It is possible to check write protection status via $ACPI_ROOT/BINF.3
  # (Boot GPIO States) or use factory mode tags to decide if we can update
  # RO. However a different RO (with write protected) usually means we're
  # running on a different (wrong) configuration.  So the logic here is, if
  # RO is different, rewrite entire image or fail.

  # An update should be always called after chromeos_check_background but before
  # chromeos_post_update.
  # Even if is_factory=true, is_background still may to be true because when the
  # updater is trying to upgrade an incompatible firmware, it may force entering
  # factory mode.

  if ! is_positive $is_rw_only; then
    echo "  - checking RO data: $ro_list"
    local verify_list="*"
    if ! flashrom_verify_sections \
      "$ro_list" "$ro_list" $current_fname $image_fname ; then
      chromeos_check_background_update "$code" || return
      # for factory, rewrite everything. otherwise (with-ro-code),
      # update only the sections we want.
      if is_positive $is_factory; then
        echo "  - start factory (first-time) setup"
        echo "  * action: rewrite whole image"
        flashrom_write_whole $image_fname
      else
        echo "  - start RW (AB together) + RO code (bootstub/recovery) update"
        local update_list="$ro_list"
        flashrom_verify_sections \
          "$rw_list" "$rw_list" $current_fname $image_fname ||
          update_list="$update_list $rw_list"
        echo "  * action: update $update_list"
        start_firmware_section_update \
          "$update_list" "$update_list" $image_fname
        verify_list="$update_list"
      fi

      if is_positive $is_always_verify ; then
        echo "  - verify written image"
        flashrom_read_whole
        flashrom_verify_sections \
          "$verify_list" "$verify_list" $current_fname $image_fname ||
          err_die " ! image verification failed..."
      fi
      chromeos_post_update "$code"
      echo "  - success and complete."
      return
    fi
  fi

  # factory mode, with RO OK; try to update all RW at the same time.
  if is_positive $is_factory; then
    echo "  - start factory (RW-only) update"
    if flashrom_verify_sections \
         "$rw_list" "$rw_list" $current_fname $image_fname ; then
      echo "  - no need to update."
    else
      chromeos_check_background_update "$code" || return
      echo "  * action: update $rw_list"
      start_firmware_section_update "$rw_list" "$rw_list" $image_fname
      chromeos_post_update "$code"
      echo "  - success and complete."
    fi
    return
  fi

  # auto-partial-update here.
  echo "  - start auto-partial-update"
  echo "  - checking RW data"
  if flashrom_verify_sections "$rw_list" "$rw_list" $current_fname $image_fname
  then
    # T==A==B, everything is updated
    echo "  - already updated to latest version."
    return
  fi

  # some part is different, determine what task to do here.
  chromeos_check_background_update "$code" || return

  if is_positive $allow_2stage_update && [ -n "$rw_list_b" ]; then
    # A/B
    debug_msg "invoke firmware_AB_update"
    chromeos_firmware_AB_update "$rw_list_a" "$rw_list_b" $image_fname ||
      err_die "AB firmware update failed"
  else
    # simple update
    debug_msg "invoke firmware_section_update"
    echo "  * action: update $rw_list"
    start_firmware_section_update "$rw_list" "$rw_list" $image_fname ||
      err_die "RW firmware update failed"
  fi
  chromeos_post_update "$code"

  echo " - success and complete."
}

end_update_firmware() {
  # Put all procedures / checks here to perform after an update trial (no matter
  # if any data is really changed. See also chromeos_post_update).
  check_param "end_update_firmware(code)" "$@"

  # enable write-protection if required
  assert_str $is_enable_write_protect
  if is_positive $is_enable_write_protect; then
    echo "  - enable write protection..."
    flashrom_reset_layout
    flashrom_detect_layout_by_default_map "$1_wp" || err_die "invalid map info."
    flashrom_enable_write_protect "RO" || err_die "write protection failed."
  fi

  # reset flashrom target
  flashrom_select_target "reset" || alert "Cannot reset flashrom target."
}

updater_shutdown() {
  # Put all procedures / checks here to perform after all updates are complete.
  # Ex, check if we need to reboot directly for the keyboard frozen issue
  local target
  assert_str is_factory
  assert_str is_background
  assert_str is_allow_reboot
  debug_msg "updater_shutdown: modified targets are: $CURRENT_MODIFIED_TARGETS"

  for target in $CURRENT_MODIFIED_TARGETS; do
    debug_msg "updater_shutdown: checking target $target"

    # check if we need to reboot.
    # note: the leading space before $CHROMEOS_NEED_DIRECT_REBOOT_UPDATE_TARGETS
    #       is required for in_list to work properly on empty list.
    if is_positive $is_allow_reboot &&
       in_list "$target" " $CHROMEOS_NEED_DIRECT_REBOOT_UPDATE_TARGETS"; then
      echo "  ** this update needs to reboot system immediately. **"
      debug_msg "!!!!! going to reboot NOW !!!!!"
      sync; sync; sync
      execute_command "$CHROMEOS_REBOOT_CMD" || err_die "cannot reboot"
    fi
  done
}

# ----------------------------------------------------------------------------
# Main Entry

# if a mode is assigned by parameter, we won't do any further mode detection.
# if multiple modes were listed as parameter, the last one takes effect.
is_mode_assigned=0

# parse command line
for arg in "$@"; do

  # pre-process argument
  arg_name="${arg%%=*}"
  arg_value=""
  arg_check_value=1
  if [ "$arg_name" != "$arg" ]; then
    arg_value="=${arg#*=}"
  fi
  arg_name="$(echo "$arg_name" | sed 's/_/-/g' | sed 's/^--no-/--no/')"
  if [ "${arg_name%%check*}" = "--no" ]; then
    arg_check_value=0
  fi
  arg="${arg_name}${arg_value}"

  case $arg in
    -h | --help )
      # usage_help
      echo "$0 [options]

mode selection (only the last one assigned mode will take effect):
    --mode=MODE          Mode string: startup, bootok, autoupdate, todev,
                         recovery, factory_install, factory_final
    --factory            Force using factory mode (write RO+RW+GBB/VPD together)
    --with-ro-code       Update RW(A/B together) & RO(BSTUB/FVDEV), keep GBB/VPD
    --rw-ab-together     Update R/W firmware, and update A/B parts together
    --rw-only            (default) Update R/W firmware in 2 stage, skip whole RO

mode aliases:
    --factory-mode       Factory Install Mode, alias for --factory
    --recovery-mode      Recovery Install Mode, alias for --with-ab-together
    --os-install-mode    OS Install Mode, alias for --with-ab-together
    --background-mode    Background Update Mode, alias for --background-update

special options:
    --background-update  Skip update that may freeze keyboard/reboot (eg, EC)
    --allow-reboot       Allow updater to reboot device if required
    --write-protect      Enable write protection after update complete
    --targets='list'     Targets to be updated (def: '$CHROMEOS_UPDATE_TARGETS')

checking and verification:
    --always-verify      Force verifying each write to flashrom
    --[no]check-layout   Check if the flash memory layout is compatible
    --[no]check-rootkey  Check if the rootkey is compatible for updating
    --[no]check-vboot    Check if the carried image has proper VBOOT signature

general options:
    --verbose            Print verbose messages
    --debug              Provide debug information
    --debug-help         Detail help message on debug commands
    --help               This help message"
      exit 0
      ;;
    --debug-help )
      echo "Extra parameters by for debugging:

    --debug-trace             Print executed shell commands line-by-line"
      exit 0
      ;;
    --mode=* )
      arg_value="${arg_value#=}"
      case $arg_value in
        startup )
          is_allow_reboot=1
          allow_2stage_update=1
          is_factory=0
          is_rw_only=1
          is_mode_assigned=1
          verbose_msg " * Auto Update in Reboot (A/B 2 stage)"
          ;;
        autoupdate )
          is_background=1
          allow_2stage_update=1
          is_factory=0
          is_rw_only=1
          is_mode_assigned=1
          verbose_msg " * Auto Update in Background (try A/B 2 stage)"
          ;;
        recovery )
          allow_2stage_update=0
          is_factory=0
          is_rw_only=1
          is_mode_assigned=1
          verbose_msg " * (Recovery) Update R/W firmware (A/B at once)"
          ;;
        factory_install )
          allow_2stage_update=0
          is_factory=1
          is_rw_only=0
          is_mode_assigned=1
          verbose_msg " * Enable factory mode"
          ;;
        factory_final | todev | bootok )
          alert "Warning: mode [$arg_value] is not available on this platform."
          exit 0
	  ;;
        * )
          alert "Warning: mode [$arg_value] is not supported."
          exit 0
          ;;
      esac
      ;;
    --factory* )
      allow_2stage_update=0
      is_factory=1
      is_rw_only=0
      is_mode_assigned=1
      verbose_msg " * Enable factory mode"
      ;;
    --with-ro* )
      allow_2stage_update=0
      is_factory=0
      is_rw_only=0
      is_mode_assigned=1
      verbose_msg " * Enable RW+RO Code (bootstub/recovery) mode"
      ;;
    --rw-ab* | --recovery* | --os-install* )
      allow_2stage_update=0
      is_factory=0
      is_rw_only=1
      is_mode_assigned=1
      verbose_msg " * Update R/W firmware (A/B at once)"
      ;;
    --rw-only )
      allow_2stage_update=1
      is_factory=0
      is_rw_only=1
      is_mode_assigned=1
      verbose_msg " * Update only R/W firmware (try A/B 2 stage)"
      ;;
    --background* )
      is_background=1
      verbose_msg " * Enable background update mode"
      ;;
    --allow-reboot* )
      is_allow_reboot=1
      verbose_msg " * Allow rebooting system after updates applied"
      ;;
    --write-protect* | --enable-write-protect* )
      is_enable_write_protect=1
      verbose_msg " * Enable write protection"
      ;;
    --target*=* )
      CHROMEOS_UPDATE_TARGETS="${arg#--target*=}"
      verbose_msg " * Target(s) changed to: $CHROMEOS_UPDATE_TARGETS"
      ;;
    --always-verify )
      is_always_verify=1
      verbose_msg " * Force verification to every write"
      ;;
    --check-layout | --nocheck-layout )
      is_check_layout=$arg_check_value
      ;;
    --check-rootkey | --nocheck-rootkey )
      is_check_rootkey=$arg_check_value
      ;;
    --check-vboot | --nocheck-vboot )
      is_check_vboot=$arg_check_value
      ;;
    --verbose )
      is_verbose=1
      FLAGS_verbose=$FLAGS_TRUE
      ;;
    --debug )
      is_debug=1
      FLAGS_debug=$FLAGS_TRUE
      ;;
    --debug-trace )
      is_debug=1
      FLAGS_debug=$FLAGS_TRUE
      set -x
      ;;
    * )
      echo " ! ERROR: unknown parameter: $arg"
      echo " ! Please invoke with --help for full syntax."
      exit 1
  esac
  shift
done

# detect and adjust modes
assert_str $is_mode_assigned
if [ -n "$IS_CHROMEOS_STARTUP" ]; then
  # allow reboot by default in Chrome OS startup
  is_allow_reboot=1
fi

# load customized information
if [ -r $CUSTOM_SCRIPT_FILENAME ]; then
  echo " * Loading custom script '$CUSTOM_SCRIPT_FILENAME'..."
  . $CUSTOM_SCRIPT_FILENAME
fi

# report and adjust working mode
if is_positive $is_factory; then
  echo " % Firmware Update Mode: Factory (RO+RW+Everything)"
  is_rw_only=0
  # is_background cannot be set to 0 here, because we may still need to check it
  # in factory mode when the 'factory mode' was enforced by an incompatible
  # version of firmware.
elif is_positive $is_rw_only; then
  is_positive $allow_2stage_update &&
    echo " % Firmware Update Mode: RW Only (2 stage update for A/B)" ||
    echo " % Firmware Update Mode: RW Only (A/B together)"
else
  echo " % Firmware Update Mode: RO(code) + RW(A/B together)"
fi

if is_positive $is_debug; then
  is_verbose=1
  FLAGS_verbose=$FLAGS_TRUE
  debug_msg "(mode) is_factory: $is_factory"
  debug_msg "(mode) is_rw_only: $is_rw_only"
  debug_msg "(mode) allow_2stage_update: $allow_2stage_update"
  debug_msg "(opt) is_background: $is_background"
  debug_msg "(opt) is_allow_reboot: $is_allow_reboot"
  debug_msg "(opt) is_always_verify: $is_always_verify"
  debug_msg "(opt) is_enable_write_protect: $is_enable_write_protect"
  debug_msg "(opt) allow_fmap_decode_layout: $allow_fmap_decode_layout"
  debug_msg "(opt) update targets: $CHROMEOS_UPDATE_TARGETS"
  debug_msg "(chk) is_check_layout: $is_check_layout"
  debug_msg "(chk) is_check_rootkey : $is_check_rootkey"
  debug_msg "(chk) is_check_vboot: $is_check_vboot"
  debug_msg "(msg) is_verbose: $is_verbose"
  debug_msg "(msg) is_debug: $is_debug"
fi

for target in $CHROMEOS_UPDATE_TARGETS; do
  case $target in
    bios )
      debug_msg "checking target=bios"
      # update BIOS firmware
      if [ -f $BIOS_IMAGE_FILENAME ]; then
        bios_ro_list="$CHROMEOS_BIOS_FACTORY_RO_LIST"
        is_positive $is_factory || bios_ro_list="$CHROMEOS_BIOS_RO_LIST"
        update_firmware "bios" "BIOS" \
          $BIOS_IMAGE_FILENAME \
          "$bios_ro_list" \
          "$CHROMEOS_BIOS_RW_A_LIST" "$CHROMEOS_BIOS_RW_B_LIST" \
          "$CHROMEOS_BIOS_SKIP_VERIFY_LIST" \
          "$CHROMEOS_BIOS_PRESERVE_LIST" \
          "$CHROMEOS_VBOOT_LIST" \
          "$CHROMEOS_ROOTKEY_SECTION_NAME"
        end_update_firmware "bios"
      fi
      ;;
    ec )
      debug_msg "checking target=ec"
      # update BIOS firmware
      # update EC firmware
      if [ -f $EC_IMAGE_FILENAME ]; then
        update_firmware "ec" "Embedded Controller (EC)" \
          $EC_IMAGE_FILENAME \
          "$CHROMEOS_EC_RO_LIST" \
          "$CHROMEOS_EC_RW_LIST" "" \
          "$CHROMEOS_EC_SKIP_VERIFY_LIST" \
          "$CHROMEOS_EC_PRESERVE_LIST" \
          "" \
          ""
        end_update_firmware "ec"
      fi
      ;;
    * )
      err_die "Unknown target: $target"
      ;;
  esac
done

updater_shutdown
echo " *** Firmware update complete. ***"
