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
FMAP_REGIONS=( "FW_MAIN_A" "FW_MAIN_B" )

trap 'rm -rf "${TMPDIR}"' EXIT

# Replaces the EC RW in the ap_file with the EC RW from the ec_file.
replace_ecrw() {
  local ap_file
  local ec_file
  local region
  ap_file="$1"
  ec_file="$2"

  futility dump_fmap "${ec_file}" -x RW_FW:"${TMPDIR}/ecrw"
  openssl dgst -sha256 -binary "${TMPDIR}/ecrw" > "${TMPDIR}/ecrw.hash"
  for region in "${FMAP_REGIONS[@]}"
  do
    cbfstool "${ap_file}" remove -r "${region}" -n ecrw
    cbfstool "${ap_file}" remove -r "${region}" -n ecrw.hash
    cbfstool "${ap_file}" expand -r "${region}"
    cbfstool "${ap_file}" add -r "${region}" -t raw \
      -c LZMA -f "${TMPDIR}/ecrw" -n ecrw
    cbfstool "${ap_file}" add -r "${region}" -t raw \
      -c none -f "${TMPDIR}/ecrw.hash" -n ecrw.hash
  done
}

# Updates the version in both the ap_file and ec_file, and stores the result
# in outdir.
update_version() {
  local ap_file
  local ec_file
  local version
  local outdir
  ap_file="$1"
  ec_file="$2"
  version="$3"
  outdir="$4"

  mkdir "${outdir}"
  # Extract the old fwid for the size
  futility dump_fmap "${ap_file}" -x RW_FWID_A:"${TMPDIR}/fwid_a"
  # Set the fwid
  echo -n "Google_Ciri.${version}" >"${TMPDIR}/fwid_new"
  truncate -r "${TMPDIR}/fwid_a" "${TMPDIR}/fwid_new"
  # Load into fmap for A and B
  futility load_fmap "${ap_file}" -o "${outdir}/image-ciri.bin" \
    RO_FRID:"${TMPDIR}/fwid_new" RW_FWID_A:"${TMPDIR}/fwid_new" \
    RW_FWID_B:"${TMPDIR}/fwid_new"
  # Extract the old EC FWID for the size
  futility dump_fmap "${ec_file}" -x RW_FWID:"${TMPDIR}/ec_fwid"
  # Set the EC FWID
  echo -n "ciri-${version}" >"${TMPDIR}/ec_fwid_new"
  truncate -r "${TMPDIR}/ec_fwid" "${TMPDIR}/ec_fwid_new"
  # Load EC FWID into ec.bin
  futility load_fmap "${ec_file}" -o "${outdir}/ec.bin" \
    RO_FRID:"${TMPDIR}/ec_fwid_new" RW_FWID:"${TMPDIR}/ec_fwid_new"
  # Replace the ecrw in the AP image
  replace_ecrw "${outdir}/image-ciri.bin" "${outdir}/ec.bin"
  # Print the versions
  futility update --manifest -i "${outdir}/image-ciri.bin" -e "${outdir}/ec.bin"
}

# Merge yaml files for the rw/ro test case.
cd test
cros_config_schema -m  "config.yaml" "config_rw.yaml" -o "config_rw.json"

# Copy all *.tbz2 to IMAGEDIR as symlinks.
mkdir -p "${IMAGEDIR}"
rm -f "${IMAGEDIR}"/*
ln -sr "${FUNCTESTDIR}"/*.tbz2 "${IMAGEDIR}"

# Generate the simple archives from compressed individual files
gunzip -c "${FUNCTESTDIR}/image-ciri.bin.gz" >"${TMPDIR}/image-ciri.bin"
gunzip -c "${FUNCTESTDIR}/ec.bin.gz" >"${TMPDIR}/ec.bin"
gunzip -c "${FUNCTESTDIR}/ec.config.gz" >"${TMPDIR}/ec.config"

# AP 15705.0.0
tar -C "${TMPDIR}" -cjf "${IMAGEDIR}/Ciri.15705.0.0.tbz2" image-ciri.bin
tar -C "${TMPDIR}" -cjf "${IMAGEDIR}/Ciri_Wrong_Version.15555.0.0.tbz2" \
  image-ciri.bin
# EC 15705.0.0 with no component_manifest.json
tar -C "${TMPDIR}" -cjf "${IMAGEDIR}/Ciri_EC_No_CM.15705.0.0.tbz2" ec.bin \
  ec.config
# EC 15705.0.0 with an incorrect component_manifest.json
cp "${FUNCTESTDIR}"/ec_cm-ciri.15820.json "${TMPDIR}/component_manifest.json"
tar -C "${TMPDIR}" -cjf "${IMAGEDIR}/Ciri_EC_Wrong_Version.15705.0.0.tbz2" \
  component_manifest.json ec.bin ec.config
# EC 15705.0.0 with a correct component_manifest.json
cp "${FUNCTESTDIR}"/ec_cm-ciri.15705.json "${TMPDIR}/component_manifest.json"
tar -C "${TMPDIR}" -cjf "${IMAGEDIR}/Ciri_EC.15705.0.0.tbz2" \
  component_manifest.json ec.bin ec.config

# Generate Ciri.15706.0.0.tbz2
update_version "${TMPDIR}/image-ciri.bin" "${TMPDIR}/ec.bin" 15706.0.0 \
  "${TMPDIR}/Ciri.15706.0.0"
tar -C "${TMPDIR}/Ciri.15706.0.0" -cjf "${IMAGEDIR}/Ciri.15706.0.0.tbz2" \
  image-ciri.bin

# Generate Ciri_EC.15821.0.0.tbz2
update_version "${TMPDIR}/image-ciri.bin" "${TMPDIR}/ec.bin" 15821.0.0 \
  "${TMPDIR}/Ciri.15821.0.0"
cp "${FUNCTESTDIR}"/ec_cm-ciri.15821.json \
  "${TMPDIR}/Ciri.15821.0.0/component_manifest.json"
gunzip -c "${FUNCTESTDIR}/ec.config.gz" >"${TMPDIR}/Ciri.15821.0.0/ec.config"
tar -C "${TMPDIR}/Ciri.15821.0.0" -cjf "${IMAGEDIR}/Ciri_EC.15821.0.0.tbz2" \
  component_manifest.json ec.bin ec.config

# Generate Ciri_EC.15820.0.0.tbz2
update_version "${TMPDIR}/image-ciri.bin" "${TMPDIR}/ec.bin" 15820.0.0 \
  "${TMPDIR}/Ciri.15820.0.0"
cp "${FUNCTESTDIR}"/ec_cm-ciri.15820.json \
  "${TMPDIR}/Ciri.15820.0.0/component_manifest.json"
gunzip -c "${FUNCTESTDIR}/ec.config.gz" >"${TMPDIR}/Ciri.15820.0.0/ec.config"
tar -C "${TMPDIR}/Ciri.15820.0.0" -cjf "${IMAGEDIR}/Ciri_EC.15820.0.0.tbz2" \
  component_manifest.json ec.bin ec.config
