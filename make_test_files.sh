#!/bin/sh
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
#
# Creates a few binary files needed by tests.

cd test
for fname in *.dts; do
  dtc  "${fname}" -o "${fname%.*}.dtb" -O dtb
done
