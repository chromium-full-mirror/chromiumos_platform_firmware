#!/bin/sh
#
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
#
# Updater calling 'futility update'.
echo "Starting firmware updater v5..."

# Process legacy options.
ARGS=""
for arg in "$@"; do
  case "${arg}" in
    --noupdate_ec|--noupdate_pd)
      arg="--host_only"
      ;;
    --nocheck_keys)
      arg="--force"
      ;;
    --update_ec|--update_pd|--update_main|--check_keys)
      continue
      ;;
  esac
  ARGS="${ARGS} ${arg}"
done

echo ">> futility update -a . ${ARGS}"
exec futility update -a . ${ARGS}
