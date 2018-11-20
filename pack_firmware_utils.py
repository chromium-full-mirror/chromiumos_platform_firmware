# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Utility functions for tests.

This includes a few useful functions shared among unit and functional tests.
"""

from __future__ import print_function

import json
import re
import subprocess

# We are looking for KEY="VALUE", or KEY=
RE_KEY_VALUE = re.compile('(?P<key>[A-Z_]+)=("(?P<value>.*)")?$')

def ReadSetVars(fname):
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
  prefixes = ['TARGET', 'IMAGE', 'SIGNATURE']
  lines = [line.strip() for line in lines
           if line.strip().split('_')[0] in prefixes]
  versions = {}
  for line in lines:
    m = RE_KEY_VALUE.match(line)
    value = m.group('value')
    versions[m.group('key')] = value if value else ''
  return versions

def ReadVersions(fname):
  """Read the version of images in an updater archive.

  Args:
    fname: Filename of script file.

  Returns:
    Dict with:
        key: Version name.
        value: Version value.
  """
  raw_data = subprocess.check_output(['sh', fname, '--manifest'])
  data = json.loads(raw_data)
  if len(data) == 1:
    data = data.values()[0]
    ro_fwid = data['host']['versions']['ro']
    rw_fwid = data['host']['versions']['rw']
    platform = ro_fwid.partition('.')[0]
    ec_id = data['ec']['versions']['ro'] if 'ec' in data else ''
    pd_id = data['pd']['versions']['ro'] if 'pd' in data else ''
  else:
    ro_fwid = rw_fwid = platform = ec_id = pd_id = ''

  versions = {
      'TARGET_RO_FWID': ro_fwid,
      'TARGET_FWID': rw_fwid,
      'TARGET_ECID': ec_id,
      'TARGET_PDID': pd_id,
      'TARGET_PLATFORM': platform,
  }
  return versions

def MakeTestFiles():
  """Create test files that we need."""
  subprocess.check_call(['./make_test_files.sh'], shell=True)
