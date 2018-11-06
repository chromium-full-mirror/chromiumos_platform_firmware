#!/bin/bash
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
#
# Merges yaml files for the rw/ro test case.

cd test
cros_config_schema -m  "config.yaml" "config_rw.yaml" -o "config_rw.json"
