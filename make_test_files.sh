#!/bin/bash
# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
#
# Script to generate files need for tests.

set -ex

ROOTDIR="$(dirname "$(realpath "$0")")"
FUNCTESTDIR="${ROOTDIR}/functest"
IMAGEDIR="${FUNCTESTDIR}/images"
TMPDIR="$(mktemp -d)"

# Merge yaml files for the rw/ro test case.
cd test
cros_config_schema -m  "config.yaml" "config_rw.yaml" -o "config_rw.json"

# Copy all *.tbz2 to IMAGEDIR as symlinks.
mkdir -p "${IMAGEDIR}"
rm -f "${IMAGEDIR}"/*
ln -sr "${FUNCTESTDIR}"/*.tbz2 "${IMAGEDIR}"

patch_tar() {
  local oldtar="$1"
  local newtar="$2"
  local filename="$3"
  local patchfile="$4"
  local tmpdir="${TMPDIR}/${newtar}"

  if [[ -f "${IMAGEDIR}/${newtar}" ]]; then
    echo "ERROR: Target ${IMAGEDIR}/${newtar}" already exists
    exit 1
  fi

  # cd to an empty dir because ${oldtar} may contain multiple files.
  mkdir "${tmpdir}" && cd "${tmpdir}"

  tar -xf "${FUNCTESTDIR}/${oldtar}"
  bspatch "${filename}" "${filename}" "${FUNCTESTDIR}/${patchfile}"
  tar -jcf "${IMAGEDIR}/${newtar}" ./*
}

# Generate Ciri.15706.0.0.tbz2.
patch_tar "Ciri.15705.0.0.tbz2" "Ciri.15706.0.0.tbz2" \
  "image-ciri.bin" "image-ciri.15706.bspatch"
