#!/usr/bin/env python
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Decodes a file from given offset as INT32 in native endianess."""

import struct
import sys


def decode_int32(file_path, offset=0):
  """Opens file and read 4 bites as INT32 in given offset.

  Args:
    file_path: A string to the file to read.
    offset: An integer indicating offset to skip.

  Returns:
    An integer as decoded number.
  """
  with open(file_path) as f:
    f.seek(offset)
    return struct.unpack('<I', f.read(4))[0]


def main(argv):
  """Main function for command line invocation."""
  if len(argv) < 2 or len(argv) > 3:
    exit('Usage: %s file_path [offset]' % argv[0])

  file_path = argv[1]
  offset = int(argv[2], 0) if len(argv) > 2 else 0
  print("%d" % decode_int32(file_path, offset))


if __name__ == '__main__':
  main(sys.argv)
