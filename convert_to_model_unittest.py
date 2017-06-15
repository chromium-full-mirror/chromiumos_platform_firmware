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

  def runWithModel(self, model, ebuild_model):
    """Run a test with the given model and ebuild model.

    Normally these two are the same (e.g. 'reef') but for 'reef-uni' we use
    the chromeos-firmware-reef ebuild and thus we need to distinguish between
    the ebuild we use ('reef') and the board we use ('reef-uni'). The script
    has special-case code for this so we need to test it.
    """
    converter = convert_to_model.ModelConverter([model])
    converter._srcpath = 'test'
    converter._path_format = 'chromeos-firmware-%(model)s*.ebuild'
    with self.OutputCapturer() as capturer:
      converter.Start()
    self.assertEqual(capturer.GetStdout(), '''&models {
\t%s {
\t\tfirmware {
\t\t\tbcs-overlay = "overlay-reef-private";
\t\t\tbuild-main-rw-image;
\t\t\tec-image = "bcs://Reef_EC.9042.87.0.tbz2";
\t\t\textras = "${FILESDIR}/a_directory", "${FILESDIR}/a_file", "MoreStuff", \
"YetMoreStuff", "will_it_ever_end?", \
"bcs://gru_fw_rev0_8676.0.2016_08_05.tbz2", \
"bcs://gru_ec_rev0_8676.0.2016_08_05.tbz2", "${ROOT}/root", \
"${SYSROOT}/sysroot";
\t\t\tmain-image = "bcs://Reef.9042.87.0.tbz2";
\t\t\tmain-rw-image = "bcs://Reef.9042.85.0.tbz2";
\t\t\tpd-image = "bcs://Reef_PD.9042.80.0.tbz2";
\t\t\tscript = "updater4.sh";
\t\t\tstable-ec-version = "reef_v1.1.5899-b349d2b";
\t\t\tstable-main-version = "Google_Reef.9042.87.0";
\t\t\tstable-pd-version = "reef_v1.1.5800-49d2bb3";
\t\t};
\t};

};

Run this command in chroot to regenerate the manifest:
FIRMWARE_UNIBUILD="%s" ebuild /mnt/host/source/src/\
chromeos-firmware-reef-0.0.1-r56.ebuild manifest
''' % (ebuild_model, model))

  def testEbuild(self):
    """Simple test of processing an ebuild"""
    self.runWithModel('reef', 'reef')
    self.runWithModel('reef-uni', 'reef')


if __name__ == '__main__':
  cros_test_lib.main()
