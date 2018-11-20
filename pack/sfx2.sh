#!/bin/sh
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

# A tiny stub for ZIP based Chrome OS firmware updater package.
#
# Developer friendly features (-V, --manifest, --{un,re}pack) may depend on
# zip/unzip, which will be available on test images or non-Chrome OS.
# For any features that will run on a standard Chrome OS release image,
# it can only invoke `futility` and should never depend on zip/unzip.

: "${TMP_TEMPLATE="fwupdater.XXXXXX"}"

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

  # Use a standard timestamp for the version files so that we get the same
  # exact sharball each time. Otherwise the changing timestamps creates small
  # differences.
  touch -t 201701010000 "${src}/VERSION" "${src}/manifest.json"

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
  (cd "${src}" && zip -r "${tmp}.zip" .) || die "Failed archiving: ${dest}"
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
  if ! has_program futility ||
     ! futility update -a "${src}" --unpack "${dest}"; then
    need_program unzip
    unzip -o "${src}" -d "${dest}"
  fi
  echo "OK: Unpacked: ${dest}"
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
      # TODO(hungte) See if we should add -V in futility in future.
      need_program unzip
      warn "For machine-friendly version, please use --manifest"
      exec unzip -p "$0" VERSION
      ;;
    --manifest)
      if has_program futility && futility update -a "$0" --manifest; then
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
      need_program futility
      exec futility update -a "$0" "$@"
      ;;
  esac
}
main "$@"
exit
# Contents below are ZIP data.
##CUTHERE##################################################################
