#!/bin/sh
# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

# A tiny stub for ZIP based Chrome OS firmware updater package.
#
# Developer friendly features (-V, --manifest, --{un,re}pack) may depend on
# zip/unzip, which will be available on test images or non-Chrome OS.
# For any features that will run on a standard Chrome OS release image,
# it can only invoke `futility` and should never depend on zip/unzip.

: "${TMP_TEMPLATE="fwupdater.XXXXXX"}"
: "${USE_LZMA_ZIP:=}"
# Use a standard timestamp for the version files so that we get the same
# exact sharball each time. Otherwise the changing timestamps creates small
# differences.
: "${TOUCH_TS:=201701010000}"

warn() {
  echo "$*" >&2
}

die() {
  warn "ERROR: $*"
  exit 1
}

has_program() {
  type "$1" >/dev/null 2>&1
}

need_program() {
  has_program "$1" || die "Need program '$1' to continue."
}

is_lzma_zip() {
  [ -n "${USE_LZMA_ZIP}" ] || [ -e "$1"/LZMA_ZIP ]
}

do_compress() {
  local archive="$1"
  local src="$2"
  local use_7z="false"

  if is_lzma_zip "${src}"; then
    if has_program 7z; then
      use_7z="true"
    else
      warn "WARNING: 7z is not available. Repack without LZMA."
    fi
  fi

  if ${use_7z}; then
    echo 1 > "${src}/LZMA_ZIP"
    touch -t "${TOUCH_TS}" "${src}/LZMA_ZIP"
    7z a -tzip -mm=lzma "${archive}" "${src}"
  else
    zip -r -X "${archive}" "${src}"
  fi
}

do_decompress() {
  local archive="$1"
  local dest="$2"

  # We don't know the archive format without unpacking first.  Try 7z first
  # because it supports more formats.
  if has_program 7z; then
    7z x "${archive}" -o"${dest}"
  else
    need_program unzip
    if ! unzip -o "${archive}" -d "${dest}"; then
      die "Need 7z to unpack this archive."
    fi
  fi
}

repack() {
  local dest="$(readlink -f "$1")"
  local src="$2"

  if [ ! -d "${src}" ]; then
    die "Does not exist: ${src}"
  elif [ ! -e "${dest}" ]; then
    die "Cannot find $1 -> ${dest}"
  fi

  need_program futility
  futility update -a "${src}" --manifest > "${src}/manifest.json"

  touch -t "${TOUCH_TS}" "${src}/VERSION" "${src}/manifest.json"

  # TODO(hungte) Use futility to check content and initial repack in future.
  need_program zip

  # Currently the libzip inside futility will discard SFX if we try to update
  # so we need to always create a new archive.
  local cut_mark="$(sed -n '/^##CUTHERE##/=' "${dest}")"
  if ! [ "${cut_mark}" -gt 0 ]; then
    die "SFX File corrupted: ${dest}"
  fi
  sed -i "$((cut_mark + 1)),\$d" "${dest}"

  local tmp="$(mktemp --tmpdir "${TMP_TEMPLATE}")"
  # We need some extra work to get reproducible zip archives.
  # -X: Disable extra fields (e.g. atime).  We don't need them.
  # TZ=UTC: Force stable timezone for timestamps rather than the active one.
  (cd "${src}" && TZ=UTC do_compress "${tmp}.zip" .) \
    || die "Failed archiving: ${dest}"
  cat "${tmp}.zip" >>"${dest}"
  rm -f "${tmp}" "${tmp}.zip"
  zip -A "${dest}"
  echo "OK: Repacked (updated): ${src} -> ${dest}"
}

unpack() {
  local src="$1"
  local dest="$2"
  if [ ! -e "${src}" ]; then
    die "Does not exist: ${src}"
  elif [ -z "${dest}" ]; then
    die "Need a destination folder to extract contents."
  fi

  mkdir -p "${dest}" || die "Failed to create folder: ${dest}"
  echo "Extracting to: ${dest}"
  # Force a stable timezone rather than the active one.
  if ! has_program futility ||
     ! TZ=UTC futility update -a "${src}" --unpack "${dest}"; then
    TZ=UTC do_decompress "${src}" "${dest}"
  fi
  echo "OK: Unpacked: ${dest}"
}

# Reports current system information
report_system() {
  local self="$1"
  if ! crossystem fwid >/dev/null 2>&1; then
    return
  fi

  # The firmware manifest key in futility comes from the model name,
  # which can be obtained via "cros_config / name".  This is given
  # here for informational purposes.  Check the output of "crosid" for
  # what futility uses.
  local model="$(cros_config /firmware image-name || cros_config / name || echo unknown)"
  local manifest="$(sh "${self}" --manifest --fast 2>/dev/null)"
  local package="$(echo "${manifest}" |
    jq -c ".${model}.host.versions" 2>/dev/null)"
  local swwp="1"
  if [ "$(futility flash --wp-status --ignore-hw)" = "WP status: disabled" ]; then
    swwp=0
  fi
  if [ -z "${package}" ]; then
    package="${manifest:-unknown}"
  else
    package="RO=$(echo "${package}" | jq -r .ro)    RW=$(
                  echo "${package}" | jq -r .rw)"
  fi

  echo "
  Machine Model: ${model}
  Write Protect: HW=$(crossystem wpsw_cur) SW=${swwp}
  Last Boot Version: RO=$(crossystem ro_fwid) ACT/$(
                           crossystem mainfw_act)=$(crossystem fwid)
  Firmware Updater:  ${package}
  "
}

usage() {
  local prog="$1"
  echo "Usage: ${prog} [options]

-V:              print human readable version information
--manifest:      print machine readable (JSON) version information
--unpack DEST:   unpack the contents to DEST
--repack SRC:    repack with contents from SRC

"
  if has_program futility; then
    echo "If no options are given, prints firmware info and exits."
    echo "Other options will be passed to 'futility update' command:"
    echo ""
    exec futility update --help
  else
    echo "You need to install 'futility' for other commands and options."
  fi
}

main() {
  set -e

  case "$1" in
    -V)
      # TODO(hungte) Replace by generating a human readable output by
      # processing the output of --manifest.
      warn "WARNING: The output format of -V may change anytime."
      warn "WARNING: For machine-friendly version, please use --manifest"
      if has_program 7z; then
        7z e -so "$0" VERSION
        echo "Package Contents:"
        exec 7z l "$0"
      # We plan to support non-zip formats in the future.  When a file is not
      # in zip format, the unzip command still writes error messages to
      # stdout/stderr even with the `-qq` flag.  Therefore, we suppress output
      # by redirecting to /dev/null.
      elif has_program unzip && unzip -t "$0" >/dev/null 2>&1 ; then
        unzip -p "$0" VERSION
        echo "Package Contents:"
        exec unzip -l "$0"
      elif has_program futility; then
        local tmp="$(mktemp -d --tmpdir "${TMP_TEMPLATE}")"
        futility update -a "$0" --unpack "${tmp}" >/dev/null 2>&1 || true
        cat "${tmp}/VERSION" || true
        echo "Package Contents:"
        (cd "${tmp}" && find . -type f) || true
        rm -rf "${tmp}"
      else
        die "Need program '7z', 'unzip' or 'futility' to continue."
      fi
      ;;
    --manifest)
      if has_program futility && futility update -a "$0" "$@"; then
        return 0
      fi
      need_program unzip
      exec unzip -p "$0" manifest.json
      ;;

    --unpack)
      unpack "$0" "$2"
      ;;
    --repack)
      repack "$0" "$2"
      ;;
    --repack=*)
      repack "$0" "${1##*=}"
      ;;

    # Legacy options.
    --sb_extract)
      warn "'$1' is deprecated. Please use '--unpack' in future."
      unpack "$0" "$2"
      ;;
    --sb_repack)
      warn "'$1' is deprecated. Please use '--repack' in future."
      repack "$0" "$2"
      ;;

    --help | -h)
      usage "$0"
      ;;

    *)
      # This is what a release Chrome OS will run and can only invoke futility.
      report_system "$0"
      if [ -z "$*" ]; then
        die "When invoked as $0, you need to specify at least one mode (-m)."
      fi
      need_program futility
      exec futility update -a "$0" "$@"
      ;;
  esac
}
main "$@"
exit
# Contents below are ZIP data.
##CUTHERE##################################################################
