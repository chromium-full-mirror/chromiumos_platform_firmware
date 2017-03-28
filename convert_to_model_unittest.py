#!/usr/bin/env python2
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Unit tests for convert_to_model.py.

This converts a sample ebuild as a simple test of operation.
"""

from __future__ import print_function

from chromite.lib import cros_test_lib

import convert_to_model


# We need to poke around in internal members of PackFirmware.
# pylint: disable=protected-access


class TestUnit(cros_test_lib.OutputTestCase):
  """Test cases for common program flows."""

  def testEbuild(self):
    """Simple test of processing an ebuild"""
    converter = convert_to_model.ModelConverter(['reef'])
    converter._srcpath = 'test'
    converter._path_format = 'chromeos-firmware-%(model)s*.ebuild'
    with self.OutputCapturer() as capturer:
      converter.Start()
    self.assertEqual(capturer.GetStdout(), '''&models {
\treef {
\t\tbcs-overlay = "overlay-reef-private";
\t\tbuild-main-rw-image;
\t\tec-image = "bcs://Reef_EC.9042.87.0.tbz2";
\t\textras = "${FILESDIR}/a_directory", "${FILESDIR}/a_file", "MoreStuff", \
"YetMoreStuff", "will_it_ever_end?", \
"bcs://gru_fw_rev0_8676.0.2016_08_05.tbz2", \
"bcs://gru_ec_rev0_8676.0.2016_08_05.tbz2", "${ROOT}/root", \
"${SYSROOT}/sysroot";
\t\tmain-image = "bcs://Reef.9042.87.0.tbz2";
\t\tmain-rw-image = "bcs://Reef.9042.85.0.tbz2";
\t\tpd-image = "bcs://Reef_PD.9042.80.0.tbz2";
\t\tscript = "updater4.sh";
\t\tstable-ec-version = "reef_v1.1.5899-b349d2b";
\t\tstable-main-version = "Google_Reef.9042.87.0";
\t\tstable-pd-version = "reef_v1.1.5800-49d2bb3";
\t};
\t
};
''')


if __name__ == '__main__':
  cros_test_lib.main()
