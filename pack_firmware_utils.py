# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Utility functions for tests.

This includes a few useful functions shared among unit and functional tests.
"""

from __future__ import print_function

import os
import re

# We are looking for KEY="VALUE", or KEY=
RE_KEY_VALUE = re.compile('(?P<key>[A-Z_]+)=("(?P<value>.*)")?$')

def ReadVersions(fname):
  """Read the start of the supplied script to get the version information.

  This picks up various shell variable assignments from the script and
  returns them so their values can be checked.

  Args:
    fname: Filename of script file.

  Returns:
    Dict with:
        key: Shell variable.
        value: Value of that shell variable.
  """
  with open(fname) as fd:
    lines = fd.read(1000).splitlines()[:30]
  # Use strip() where needed since some lines are indented.
  prefixes = ['TARGET', 'STABLE', 'IMAGE', 'KEYSET', 'KEY']
  lines = [line.strip() for line in lines
           if line.strip().split('_')[0] in prefixes or
           line.startswith('UNIBUILD')]
  versions = {}
  for line in lines:
    m = RE_KEY_VALUE.match(line)
    value = m.group('value')
    versions[m.group('key')] = value if value else ''
  return versions

def MakeTestFiles():
  """Create test files that we need."""
  os.system('./make_test_files.sh')
