#!/usr/bin/env python
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Merges one file to specified location of another file."""

import sys


def merge_file(large_path, small_path, large_offset, small_offset=0,
               size=None):
  """Merges file in small_path to large_path in given offset.

  Args:
    large_path: A string for path of the file to merged to.
    small_path: A string for path of the file to merge from.
    large_offset: The offset to write in large_path.
    small_offset: The offset to read in small_path.
    size: Count of bytes to read/write. None to read whole small_path.
  """
  with open(large_path, 'r+') as output:
    output.seek(large_offset)
    with open(small_path) as source:
      source.seek(small_offset)
      output.write(source.read() if size is None else source.read(size))


def main(argv):
  """Main function for command line invocation."""
  if len(argv) < 4 or len(argv) > 6:
    exit('Usage: %s large_path small_path large_offset [small_offset [size]]' %
         argv[0])

  large_path = argv[1]
  small_path = argv[2]
  large_offset = int(argv[3], 0)
  small_offset = int(argv[4], 0) if len(argv) > 4 else 0
  size = int(argv[5], 0) if len(argv) > 5 else None
  merge_file(large_path, small_path, large_offset, small_offset, size)
  print('Merged file %s@%d:%s to %s@%d.' %
        (small_path, small_offset, '*' if size is None else size, large_path,
         large_offset))


if __name__ == '__main__':
  main(sys.argv)
