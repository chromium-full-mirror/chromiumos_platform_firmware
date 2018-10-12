#!/bin/sh
#
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
#
# Updater calling 'futility update'.

SCRIPT_BASE="$(dirname "$0")"
. "$SCRIPT_BASE/common.sh"

# Parse command line
FLAGS "$@" || exit 1
eval set -- "$FLAGS_ARGV"

# Use bundled tools with highest priority, to prevent dependency when updating
cros_setup_path

# This setup must occur before anything else since the unibuild
# variables need to be set before the rest of the initialization.
if [ -n "${UNIBUILD}" ]; then
  model="${FLAGS_model}"
  if [ "${FLAGS_mode}" = "output" ]; then
    [ -n "${model}" ] || die \
      "Model (--model) must be specified with --mode=output"
    [ -n "${FLAGS_output_dir}" ] || die \
      "Output directory (--output_dir) must be specified with --mode=output"
  fi

  if [ -z "${model}" ]; then
    model="$(mosys platform model)"
    [ -n "${model}" ] || die "Cannot get model from mosys."
  fi
  # Image and key files were set in the shared crosfw.sh file. Set them again
  # here, since we want to use the files specific to the chosen model.
  cros_set_unibuild_vars "${model}"
fi

FWID="$(crossystem fwid 2>/dev/null)" || FWID=""
PLATFORM="${FWID%%.*}"
UPDATER_QUIRKS=""

# For updater5, we don't load per-board customization; instead everything
# must be done as "quirks" and defined here.
case "${PLATFORM#Google_}" in
  Whirlwind | Arkham | Storm | Gale)
    UPDATER_QUIRKS="enlarge_image"
    ;;
  Sentry | Lars | Chell )
    UPDATER_QUIRKS="unlock_me_for_update"
    ;;
  Poppy)
    UPDATER_QUIRKS="min_platform_version=6"
    ;;
  Scarlet)
    UPDATER_QUIRKS="min_platform_version=1"
    ;;
esac

load_keyset() {
  if ! [ -d "$KEYSET_DIR" ]; then
    debug_msg "No keysets folder."
    return
  fi
  local keyid="" whitelabel_tag=""

  # Allow overriding keys from command line.
  # TODO(hungte) In future if cros_config is available on ALL (including legacy)
  # systems, we can consider again moving all these logic to cros_config.
  if [ -n "${FLAGS_signature_id}" ]; then
    keyid="${FLAGS_signature_id}"
  elif [ -n "${FLAGS_customization_id}" ]; then
    # TODO(hungte) Remove FLAGS_customization_id in future.
    # Match how VPD customization_id is processed below.
    keyid="${FLAGS_customization_id%%-*}"
  else
    # For Unified build, the keyset is use by each model and white-label.
    # For Non-Unified build, keyset is used only for white-label devices.
    # Legacy devices use 'customization_id' (upper cased) while newer devices
    # use 'whitelabel_tag' (lower cased).
    if [ -n "${UNIBUILD}" ]; then
      local model="$(mosys platform model)"
      debug_msg "Unified Build: Default signature=${SIGNATURE_ID}"
      # TODO(hungte) Search some white-label-specific property in cros_config
      # to make sure this is a white label device.
      keyid="${SIGNATURE_ID##sig-id-in-*}"
      if [ -n "${SIGNATURE_ID}" ] && [ -z "${keyid}" ]; then
        debug_msg "Trying to find signature from VPD 'whitelabel_tag'"
        whitelabel_tag="$(vpd -g whitelabel_tag 2>/dev/null || true)"
        if [ -n "${whitelabel_tag}" ]; then
          keyid="${model}-${whitelabel_tag}"
        else
          # This is for RMA, early builds or factory first time installation.
          # TODO(hungte) Only allow this in special condition, for example if
          # write protection is disabled.
          debug_msg "Fallback to model of Unified Build."
          keyid="${model}"
        fi
      fi
    else
      # Non-Unified Build. Find 'whitelabel_tag' or 'customization_id'.
      debug_msg "Trying to find signature from VPD 'whitelabel_tag'"
      whitelabel_tag="$(vpd -g whitelabel_tag 2>/dev/null || true)"
      if [ -n "${whitelabel_tag}" ]; then
        # The signerbot is simply using keyset names (UPPERCASED) in non-unified
        # build so we have to convert whitelabel_tag.
        # In some environments like netboot initramfs, the `tr` is provided by
        # busybox and do not support POSIX names like :lower: and :upper:, so we
        # have to use [a-z] explicitly.
        keyid="$(echo "${whitelabel_tag}"| tr '[a-z]' '[A-Z]')"
      else
        # `customization_id` comes in format LOEM-SERIES and we have to drop
        # [-SERIES] for finding right signed files.
        debug_msg "Trying to find signature from VPD 'customization_id'"
        local customization_id="$(vpd -g customization_id)"
        keyid="${customization_id%%-*}"
      fi
    fi
  fi

  if [ -z "${keyid}" ]; then
    if [ -n "${UNIBUILD}" ]; then
      die "Unibuild: No signature ID in VPD/cmdline. Cannot continue."
    else
      echo "No customization_id. Use default keys."
    fi
    return
  fi

  debug_msg "Keysets detected, using [$keyid]"
  local rootkey="$KEYSET_DIR/rootkey.$keyid"
  local vblock_a="$KEYSET_DIR/vblock_A.$keyid"
  local vblock_b="$KEYSET_DIR/vblock_B.$keyid"
  if ! [ -s "$rootkey" ]; then
    die "Failed to load keysets for signature [$keyid]."
  fi
  # Override keys
  futility gbb -s --rootkey="$rootkey" "$IMAGE_MAIN" ||
    die "Failed to update rootkey from keyset [$keyid]."
  futility load_fmap "${IMAGE_MAIN}" VBLOCK_A:${vblock_a} VBLOCK_B:${vblock_b} \
    || die "Failed to update VBLOCK from keyset [$keyid]."
  echo "Firmware keys changed to set [$keyid]."
}

# ----------------------------------------------------------------------------

# For unified builds that support separate root/fw keys per model,
# this option allows creation of a targeted model specific bios/ec image, which
# can then be used to automate the factory imaging process without requiring
# an firmware install specific logic.
mode_output() {
  cp -f "${IMAGE_MAIN}" "${FLAGS_output_dir}"
  cp -f "${IMAGE_EC}" "${FLAGS_output_dir}"
  echo "Firmware images generated to: ${FLAGS_output_dir}"
}

# ----------------------------------------------------------------------------
# Main Entry

main() {
  cros_acquire_lock
  set_flags

  # factory compatibility
  if [ "${FLAGS_factory}" = ${FLAGS_TRUE} ] ||
     [ "${FLAGS_mode}" = "factory" ]; then
    FLAGS_mode=factory_install
  fi

  echo "Starting $TARGET_PLATFORM firmware updater v5 (${FLAGS_mode})..."
  load_keyset

  local args="-i ${IMAGE_MAIN}"
  if [ -s "${IMAGE_EC}" ] && [ "${FLAGS_update_ec}" = "${FLAGS_TRUE}" ]; then
    args="${args} --ec_image ${IMAGE_EC}"
  fi
  if [ -s "${IMAGE_PD}" ] && [ "${FLAGS_update_pd}" = "${FLAGS_TRUE}" ]; then
    args="${args} --pd_image ${IMAGE_PD}"
  fi
  if [ -n "${FLAGS_wp}" ]; then
    args="${args} --wp=${FLAGS_wp}"
  fi
  if [ "${FLAGS_force}" = "${FLAGS_TRUE}" ] ||
     [ "${FLAGS_check_keys}" = "${FLAGS_FALSE}" ]; then
    args="${args} --force"
  fi
  if [ "${FLAGS_debug}" = "${FLAGS_TRUE}" ]; then
    args="${args} --debug"
  fi
  if [ -n "${UPDATER_QUIRKS}" ]; then
    echo "*** Quirks for platform ${PLATFORM}: ${UPDATER_QUIRKS}"
    args="${args} --quirks ${UPDATER_QUIRKS}"
  fi
  debug_msg "args: ${args}"

  result="completed"
  case "${FLAGS_mode}" in
    autoupdate )
      futility update -t ${args} || result="failed"
      ;;
    recovery )
      futility update ${args} || result="failed"
      ;;
    factory_install )
      futility update --mode=factory ${args} || result="failed"
      ;;
    legacy )
      futility update --mode=legacy -i "${IMAGE_MAIN}" || result="failed"
      ;;
    output )
      mode_output || result="failed"
      ;;

    "" )
      die "Please assign updater mode by --mode option."
      ;;

    * )
      die "Unknown mode: ${FLAGS_mode}"
      ;;
  esac

  local msg="Firmware update (${FLAGS_mode}) ${result}."
  if [ "${result}" = "failed" ]; then
    die "${msg}"
  fi
  echo "${msg}"
}

# Exit on error
set -e

# Main Entry
main
