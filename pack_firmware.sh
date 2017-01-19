#!/usr/bin/env bash

# Copyright (c) 2012 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

# This script packages firmware images into an executale "shell-ball".
# It requires:
#  - at least one firmware image (*.bin, should be AP or EC or ...)
#  - pack_dist/updater.sh main script
#  - pack_stub as the template/stub script for output
#  - any other additional files used by updater.sh in pack_dist folder

script_base="$(dirname "$0")"
UTILS="$(readlink -f "$script_base")/utils"
SHFLAGS_FILE="$script_base/lib/shflags/shflags"
. "$SHFLAGS_FILE"

# DEFINE_string name default_value description flag
DEFINE_string bios_image "" "Path of input AP (BIOS) firmware image" "b"
DEFINE_string bios_rw_image "" "Path of input BIOS RW firmware image" "w"
DEFINE_string ec_image "" "Path of input Embedded Controller firmware image" "e"
DEFINE_string ec_version "IGNORE" "Version of input EC firmware image"
DEFINE_string pd_image "" "Path of input Power Delivery firmware image" "p"
DEFINE_string pd_version "IGNORE" "Version of input PD firmware image"
DEFINE_string script "updater.sh" "File name of main script file"
DEFINE_string output "" "Path of output filename" "o"
DEFINE_string extra "" "Directory list (separated by :) of files to be merged"
DEFINE_boolean remove_inactive_updaters ${FLAGS_TRUE} \
  "Remove inactive updater scripts"
DEFINE_boolean create_bios_rw_image ${FLAGS_FALSE} \
  "Resign and generate a BIOS RW image"
DEFINE_boolean merge_bios_rw_image ${FLAGS_TRUE} \
  "Merge the --bios_rw_image into --bios_image RW sections."

# stable settings
DEFINE_string stable_main_version "" "Version of stable main firmware"
DEFINE_string stable_ec_version "" "Version of stable EC firmware"
DEFINE_string stable_pd_version "" "Version of stable PD firmware"

# embedded tools
DEFINE_string tools "flashrom mosys crossystem gbb_utility vpd dump_fmap" \
  "List of tool programs to be bundled into updater"
DEFINE_string tool_base "" \
  "Default source locations for tools programs (delimited by colon)"

# deprecated parameters
DEFINE_string bios_version "(deprecated)" "Please don't use this."
DEFINE_boolean unstable ${FLAGS_FALSE} "(deprecated)"
DEFINE_boolean early_mp_fullupdate ${FLAGS_FALSE}  "(deprecated)"
DEFINE_string mp_main_version "" "(deprecated)"
DEFINE_string mp_ec_version "" "(deprecated)"
DEFINE_string platform "" "(deprecated)"

# Parse command line
FLAGS "$@" || exit 1
eval set -- "${FLAGS_ARGV}" || exit 1

# we need following tools to be inside a package:
#  - $FLAGS_tools: native binary tools
#  - pack_dist/${FLAGS_script}
#  - all files in pack_dist (utilities and resources)

stub_file="$script_base/pack_stub"
pack_dist="$script_base/pack_dist"
main_script="$pack_dist/${FLAGS_script}"
tmpbase=""
TMPDIRS=()

# helper utilities

do_cleanup() {
  local dir
  for dir in "${TMPDIRS[@]}"; do
    rm -rf "${dir}"
  done
  TMPDIRS=()
}

die() {
  if [ -n "$1" ]; then
    echo "ERROR: $1" >&2
  fi
  exit 1
}

has_command() {
  type "$1" >/dev/null 2>&1
}

find_tool() {
  local toolbase_list="$(echo "$FLAGS_tool_base" | tr ':' '\n')"
  local toolpath
  # TODO(hungte) old_bins is introduced by issue chromiumos:224734.
  # Remove it when the transition is complete.
  toolpath="$(echo "$toolbase_list" |
              while read toolbase; do
                if [ -f "$toolbase/old_bins/$1" ]; then
                  readlink -f "$toolbase/old_bins/$1"
                  exit $FLAGS_TRUE
                elif [ -f "$toolbase/$1" ]; then
                  readlink -f "$toolbase/$1"
                  exit $FLAGS_TRUE
                fi
              done)"
  [ -n "$toolpath" ] && echo "$toolpath"
}

extract_frid() {
  local image_file="$(readlink -f "$1")"
  local default_frid="$2"
  local section_name="${3:-RO_FRID}"
  local tmpdir="$(mktemp -d)"
  TMPDIRS+=("$tmpdir")
  ( cd "$tmpdir"
    dump_fmap -x "$image_file" >/dev/null 2>&1 || true
    [ -s "$section_name" ] && cat "$section_name" || echo "$default_frid" )
}

my_md5() {
  local path="$1"
  # Strip the path in /build/.../work
  md5sum -b "${path}" | sed 's"/build/.*/work/""'
}

get_preamble_flags() {
  local image_file="$(readlink -f "$1")"
  local tmpdir="$(mktemp -d)"
  TMPDIRS+=("$tmpdir")
  local preamble_flags="$(
    cd "$tmpdir"
    dump_fmap -x "$image_file" >/dev/null 2>&1 || true
    gbb_utility --rootkey=rootkey.bin GBB >/dev/null 2>&1 || true
    vbutil_firmware --verify VBLOCK_A --signpubkey rootkey.bin --fv FW_MAIN_A |
      grep  "^ *Preamble flags:" |
      sed 's/^ *Preamble flags:[t \t]*//' || true)"
  echo "$preamble_flags"
}

set_preamble_flags() {
  # TODO(hungte) Check if input file is also using dev keys.
  local input_file="$(readlink -f "$1")"
  local output_file="$(readlink -f "$2")"
  local preamble_flags="$3"
  local keydir="/usr/share/vboot/devkeys"
  resign_firmwarefd.sh "$input_file" "$output_file" \
    "$keydir/firmware_data_key.vbprivk" \
    "$keydir/firmware.keyblock" \
    "$keydir/dev_firmware_data_key.vbprivk" \
    "$keydir/dev_firmware.keyblock" \
    "$keydir/kernel_subkey.vbpubk" \
    0 "$preamble_flags"
}

check_rw_firmware() {
  local preamble_flags="$(get_preamble_flags "$1")"
  [ -z "$preamble_flags" ] &&
    die "Failed to detect firmware preamble flags."
  [ "$((preamble_flags & 0x01))" = 1 ] &&
    die "Firmware image ($image_file) is NOT a RW-firmware."
}

create_rw_firmware() {
  local ro_image_file="$1"
  local rw_image_file="$2"
  local preamble_flags="$(get_preamble_flags "$ro_image_file")"
  [ -z "$preamble_flags" ] &&
    die "Failed to detect firmware preamble flags."
  [ "$((preamble_flags & 0x01))" = 0 ] &&
    die "Firmware image ($ro_image_file) is NOT a RO_NORMAL firmware."
  set_preamble_flags "$ro_image_file" "$rw_image_file" \
    "$((preamble_flags ^ 0x01))" ||
    die "Failed to resign and create RW image from RO image ($ro_image_file)."
  echo "RW firmware image created: $rw_image_file"
}

clone_firmware_section() {
  local src="$1"
  local dest="$2"
  local section="$3"

  local info_src="$(dump_fmap -p $src $section)"
  local info_dest="$(dump_fmap -p $dest $section)"

  local size_src="${info_src##* }"
  local size_dest="${info_dest##* }"
  local offset_src="$(echo "$info_src" | cut -d ' ' -f 2)"
  local offset_dest="$(echo "$info_dest" | cut -d ' ' -f 2)"

  [ "$size_src" -gt 0 ] || die "Firmware section $section is invalid."

  if [ "$size_src" != "$size_dest" ]; then
    die "Firmware section $section size is different, cannot clone."
  fi

  if [ "$offset_src" != "$offset_dest" ]; then
    die "Firmware section $section is not in same location, cannot clone."
  fi
  "$UTILS/merge_file.py" "$dest" "$src" "$offset_dest" "$offset_src" \
    "$size_src" || die "Failed to clone firmware section."
}

merge_rw_firmware() {
  local ro_image_file="$1"
  local rw_image_file="$2"

  clone_firmware_section "$rw_image_file" "$ro_image_file" RW_SECTION_A
  clone_firmware_section "$rw_image_file" "$ro_image_file" RW_SECTION_B
}

extract_ecrw_fmap() {
  local rw_image_file="$1"

  dump_fmap -x "$rw_image_file" EC_MAIN_A >/dev/null 2>&1
  # EC_MAIN_A starts with a section header: [count][offset][size].
  local count="$("$UTILS/decode_int32.py" EC_MAIN_A 0)"
  local offset="$("$UTILS/decode_int32.py" EC_MAIN_A 4)"
  local size="$("$UTILS/decode_int32.py" EC_MAIN_A 8)"
  if [ "$count" != 1 -o "$offset" != 12 ]; then
    die "Unexpected EC_MAIN_A ($count,$offset). Cannot merge EC RW."
  fi
  # To make sure files to be merged are both prepared, merge_file.py will only
  # accept existing files, so we have to create ecrw now.
  touch ecrw
  "$UTILS/merge_file.py" ecrw EC_MAIN_A 0 $offset $size ||
    die "Failed to merge firmware from FMAP."
}

extract_ecrw_cbfs() {
  local rw_image_file="$1"
  local cbfs_name="$2"

  cbfstool "$rw_image_file" extract -n "$cbfs_name" -f ecrw -r FW_MAIN_A
}

extract_ecrw() {
  local rw_image_file="$1"

  if [ -n "$(dump_fmap -p "$rw_image_file" EC_MAIN_A)" ]; then
    extract_ecrw_fmap "$@"
  else
    extract_ecrw_cbfs "$@"
  fi
}

merge_rw_ec_firmware() {
  local ec_image_file="$1"
  local rw_image_file="$2"
  local cbfs_name="$3"
  local offset size

  local tmpdir="$(mktemp -d)"
  TMPDIRS+=("$tmpdir")
  (cd "$tmpdir"
   extract_ecrw "$rw_image_file" "$cbfs_name" ||
     die "Failed to extract ecrw from $rw_image_file."
   map=($(dump_fmap -p "$ec_image_file" EC_RW))
   offset="${map[1]}"
   size="${map[2]}"
   [ "$size" -ge "$(stat -c"%s" ecrw)" ] ||
     die "New RW payload larger than preserved FMAP section, cannot merge."
   "$UTILS/merge_file.py" "$ec_image_file" ecrw "$offset" ||
     die "Failed to change firmware section.") ||
     die "Failed to merge RW firmware to $ec_image_file."
}

trap do_cleanup EXIT

# check tool: we need uuencode to create the shell ball.
has_command shar || die "You need shar (sharutils)"
has_command uuencode || die "You need uuencode (sharutils)"

# check required basic files
for X in "$main_script" "$stub_file"; do
  if [ ! -r "$X" ]; then
    die "Cannot find required file: $X"
  fi
done

# check tool programs
flashrom_bin=""
for X in $FLAGS_tools; do
  if ! find_tool "$X" >/dev/null; then
    die "Cannot find tool program to bundle: $X"
  fi
  if [ "$X" = "flashrom" ]; then
    flashrom_bin="$(find_tool $X)"
  fi
done

# check input: fail if no any images were assigned
bios_bin="${FLAGS_bios_image}"
bios_version="IGNORE"
bios_rw_bin="${FLAGS_bios_rw_image}"
ec_bin="${FLAGS_ec_image}"
ec_version="${FLAGS_ec_version}"
pd_bin="${FLAGS_pd_image}"
pd_version="${FLAGS_pd_version}"
if [ "$bios_bin" = "" -a "$ec_bin" = "" -a "$pd_bin" = "" ]; then
  die "must assign at least one of BIOS or EC or PD image."
fi

# create temporary folder to store files
tmpbase="$(mktemp -d)" || die "Cannot create temporary folder."
TMPDIRS+=("${tmpbase}")

version_file="$tmpbase/VERSION"
if [ -n "$flashrom_bin" ]; then
  flashrom_ver="$(
    strings "$flashrom_bin" |
    grep '^[0-9\.]\+ \+: \+[a-z0-9]\+ \+: \+.\+UTC')"
  echo "
flashrom(8): $(md5sum -b "$flashrom_bin")
             $(file -b "$flashrom_bin")
             $flashrom_ver" >>"$version_file"
fi
echo "" >>"$version_file"

# Image file must follow names defined in pack_dist/updater*.sh:
IMAGE_MAIN="bios.bin"
IMAGE_MAIN_RW="bios_rw.bin"
IMAGE_EC="ec.bin"
IMAGE_PD="pd.bin"

# copy firmware image files
if [ "$bios_bin" != "" ]; then
  bios_version="$(extract_frid "$bios_bin" "IGNORE")"
  bios_rw_version="$bios_version"
  cp -pf "$bios_bin" "$tmpbase/$IMAGE_MAIN" || die "cannot get BIOS image"
  echo "BIOS image:   $(my_md5 "$bios_bin")" >> "$version_file"
  [ "$bios_version" = "IGNORE" ] ||
    echo "BIOS version: $bios_version" >> "$version_file"
else
  FLAGS_merge_bios_rw_image=$FLAGS_FALSE
fi
if [ -z "$bios_rw_bin" -a "$FLAGS_create_bios_rw_image" = "$FLAGS_TRUE" ]; then
  bios_rw_bin="$tmpbase/$IMAGE_MAIN_RW"
  create_rw_firmware "$bios_bin" "$bios_rw_bin"
  # create_rw_firmware is made only for RO_NORMAL images and can't be merged.
  FLAGS_merge_bios_rw_image=$FLAGS_FALSE
fi
if [ "$bios_rw_bin" != "" ]; then
  check_rw_firmware "$bios_rw_bin"
  bios_rw_version="$(extract_frid "$bios_rw_bin" "IGNORE")"

  if [ "$FLAGS_merge_bios_rw_image" = "$FLAGS_TRUE" ]; then
    merge_rw_firmware "$tmpbase/$IMAGE_MAIN" "$bios_rw_bin"
  elif [ "$bios_rw_bin" != "$tmpbase/$IMAGE_MAIN_RW" ]; then
    cp -pf "$bios_rw_bin" "$tmpbase/$IMAGE_MAIN_RW" || die "cannot get RW BIOS"
  fi

  echo "BIOS (RW) image:   $(my_md5 "$bios_rw_bin")" >> "$version_file"
  [ "$bios_rw_version" = "IGNORE" ] ||
    echo "BIOS (RW) version: $bios_rw_version" >>"$version_file"
else
  FLAGS_merge_bios_rw_image="$FLAGS_FALSE"
fi
if [ "$ec_bin" != "" ]; then
  ec_version="$(extract_frid "$ec_bin" "$FLAGS_ec_version")"
  # Since mosys r430, trailing spaces reported by mosys is always scrubbed.
  ec_version="$(echo "$ec_version" | sed 's/ *$//')"
  cp -pf "$ec_bin" "$tmpbase/$IMAGE_EC" || die "cannot get EC image"
  echo "EC image:     $(my_md5 "$ec_bin")" >> "$version_file"
  [ "$ec_version" = "IGNORE" ] ||
    echo "EC version:   $ec_version" >>"$version_file"

  if [ "$FLAGS_merge_bios_rw_image" = "$FLAGS_TRUE" ]; then
    # Read EC RW from merged RW image.
    merge_rw_ec_firmware "$tmpbase/$IMAGE_EC" "$tmpbase/$IMAGE_MAIN" "ecrw"
    ec_rw_version="$(extract_frid "$tmpbase/$IMAGE_EC" "" "RW_FWID")"
    echo "EC (RW) version:   $ec_rw_version" >>"$version_file"
  fi
fi
if [ "$pd_bin" != "" ]; then
  pd_version="$(extract_frid "$pd_bin" "$FLAGS_pd_version")"
  cp -pf "$pd_bin" "$tmpbase/$IMAGE_PD" || die "cannot get PD image"
  echo "PD image:     $(my_md5 "$pd_bin")" >> "$version_file"
  [ "$pd_version" = "IGNORE" ] ||
    echo "PD version:   $pd_version" >>"$version_file"

  if [ "$FLAGS_merge_bios_rw_image" = "$FLAGS_TRUE" ]; then
    # Read EC RW from merged RW image.
    merge_rw_ec_firmware "$tmpbase/$IMAGE_PD" "$tmpbase/$IMAGE_MAIN" "pdrw"
    pd_rw_version="$(extract_frid "$tmpbase/$IMAGE_PD" "" "RW_FWID")"
    echo "PD (RW) version:   $pd_rw_version" >>"$version_file"
  fi
fi

# Set platform to first field of firmware version (ex: Google_Link.1234 ->
# Google_Link).
FLAGS_platform="${bios_version%%.*}"

# copy tool programs and main resources from pack_dist.
# WARNING: do not put any files with dot in prefix ( eg: .blah )
cp -pf "$SHFLAGS_FILE" "$tmpbase"/. || die "cannot copy shflags"
for X in $FLAGS_tools; do
  # Use static version if available.
  tool_file="$(find_tool "$X")"
  [ -e "$tool_file"_s ] && tool_file="$tool_file"_s
  cp -pf "$tool_file" "$tmpbase/$X" || die "cannot copy $X"
  chmod a+rx "$tmpbase/$X"
done
cp -rfp "$pack_dist"/* "$tmpbase" || die "cannot copy pack_dist folder"

# remove inactive updater scripts
if [ "${FLAGS_remove_inactive_updaters}" = $FLAGS_TRUE ]; then
  (cd "$tmpbase"
   inactive_list="$(ls updater*.sh | sed "s/\b$FLAGS_script\b//")"
   rm -f $inactive_list
  )
fi

# adjust file permission
chmod a+rx "$tmpbase"/*.sh

# copy extra files. if $FLAGS_extra is a folder, copy all content inside.
extra_list="$(echo "${FLAGS_extra}" | tr ':' '\n')"
for extra in $extra_list; do
  if [ -d "$extra" ]; then
    cp -r "$extra"/* "$tmpbase" ||
      die "cannot copy extra files from folder $extra"
    echo "Extra files from folder: $extra" >> "$version_file"
  elif [ "$extra" != "" ]; then
    cp -r "$extra" "$tmpbase" ||
      die "Cannot copy extra files $extra"
    echo "Extra file: $extra" >> "$version_file"
  fi
done
echo "" >>"$version_file"
chmod -R a+r "$tmpbase"/*

# package temporary folder into ouput file
[ -n "${FLAGS_output}" ] || die "Missing output file."
output="${FLAGS_output}"

cp -f "$stub_file" "$output"

# Our substitution strings may contain '/', which will confuse sed
# Instead, use ascii char 1 (SOH/Start of Heading) as sed delimiter char
dc=$'\001'
sed -in "
  s${dc}REPLACE_RO_FWID${dc}${bios_version}${dc};
  s${dc}REPLACE_FWID${dc}${bios_rw_version}${dc};
  s${dc}REPLACE_ECID${dc}${ec_version}${dc};
  s${dc}REPLACE_PDID${dc}${pd_version}${dc};
  s${dc}REPLACE_PLATFORM${dc}${FLAGS_platform}${dc};
  s${dc}REPLACE_SCRIPT${dc}${FLAGS_script}${dc};
  s${dc}REPLACE_STABLE_FWID${dc}${FLAGS_stable_main_version}${dc};
  s${dc}REPLACE_STABLE_ECID${dc}${FLAGS_stable_ec_version}${dc};
  s${dc}REPLACE_STABLE_PDID${dc}${FLAGS_stable_pd_version}${dc};
  " "$output"
sh "$output" --sb_repack "$tmpbase" ||
  die "Failed to archive firmware package"

# provide more information since output is not stdout
chmod a+rx "$output"
cat "$tmpbase/VERSION"*
echo ""
echo "Packed output image is: $output"
