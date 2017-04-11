#!/usr/bin/env python2
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Converts information in firmware ebuilds to a master configuration.

This tool converts information in a firmware ebuild, such as:

  CROS_FIRMWARE_BCS_OVERLAY="overlay-reef-private"
  CROS_FIRMWARE_MAIN_IMAGE="bcs://Reef.9042.72.0.tbz2"
  CROS_FIRMWARE_EC_IMAGE="bcs://Reef_EC.9042.72.0.tbz2"
  CROS_FIRMWARE_STABLE_MAIN_VERSION="Google_Reef.9042.72.0"
  CROS_FIRMWARE_STABLE_EC_VERSION="reef_v1.1.5891-37adb2a"
  CROS_FIRMWARE_SCRIPT="updater4.sh"

and converts it to a firmware node in the master configuration:

  firmware {
          bcs-overlay = "overlay-reef-private";
          main-image = "bcs://Reef.9042.72.0.tbz2";
          ec-image = "bcs://Reef_EC.9042.72.0.tbz2";
          stable-main-version = "Google_Reef.9042.72.0";
          stable-ec-version = "reef_v1.1.5891-37adb2a";
          script = "updater4.sh";
  };

It operates on one or more models.
"""

from __future__ import print_function

import glob
import os
import re
import sys

from chromite.lib import commandline
from chromite.lib import constants
from chromite.lib import cros_logging as logging
from chromite.lib import osutils

CROS_FIRMWARE = 'CROS_FIRMWARE_'
BCS_PREFIX = 'bcs://'


RE_VARIANT_NAME = re.compile('overlay-([a-z]+)-private')

# Translates from shell variable to property. See binding document at
# src/platform2/chromeos-config/README.md
TRANSLATE_TO_PROPERTY = {
    'BCS_OVERLAY': 'bcs-overlay',
    'MAIN_IMAGE': 'main-image',
    'MAIN_RW_IMAGE': 'main-rw-image',
    'EC_IMAGE': 'ec-image',
    'PD_IMAGE': 'pd-image',
    'SCRIPT': 'script',
    'PLATFORM': '',
    'STABLE_MAIN_VERSION': 'stable-main-version',
    'STABLE_EC_VERSION': 'stable-ec-version',
    'STABLE_PD_VERSION': 'stable-pd-version',
    'BUILD_MAIN_RW_IMAGE': 'build-main-rw-image',
    'EXTRA_LIST': 'extras',
}


def _ParseArgs(argv):
  """Parse the available arguments.

  Invalid arguments or -h cause this function to print a message and exit.

  Args:
    argv: List of string arguments (excluding program name / argv[0]).

  Returns:
    argparse.Namespace object containing the attributes.
  """
  parser = commandline.ArgumentParser(description=__doc__)
  parser.add_argument('-a', '--all', action='store_true',
                      help='Convert all models that can be found')
  parser.add_argument('-m', '--model', type=str, dest='models',
                      action='append', help='Model name to convert')
  opts = parser.parse_args(argv)
  if not (opts.models or opts.all):
    raise parser.error('Please use -m to specify model(s) or -a for all')
  opts.Freeze()
  return opts


class ModelConverterError(Exception):
  """Exception returned by FirmwarePacker when something goes wrong"""


class ModelConverter(object):
  """Handles conversion from an ebuild to a master configuration node.

  Private members:
    _args: argparse.Namespace object containing parsed arguments.
    _indent: Current indent level for output, in terms of number of tabs.
    _models: List of models to generate output for, or None to do all.
    _path_format: Format string to use to find the path to an ebuild, given its
        model name.
    _srcpath: Path source Chrome OS source code ('src' directory).
    _urls: List of URLs to fetch from GCS.
  """

  def __init__(self, models):
    self._models = models
    self._indent = 0
    self._srcpath = os.path.realpath(os.path.join(constants.SOURCE_ROOT, 'src'))
    self._urls = []
    self._path_format = ('private-overlays/overlay-%(model)s-private/'
                         'chromeos-base/chromeos-firmware-%(model)s/'
                         'chromeos-firmware-%(model)s*.ebuild')

  def _AddLine(self, line):
    """Add a line to the output.

    This keeps track of indenting automatically. Indentation increases for
    lines ending in '{' and descreases for lines ending with '};'

    Args:
      line: Line of text to add.
    """
    if line == '};':
      self._indent -= 1
    indent_tabs = '\t' * self._indent if line else ''
    print('%s%s' % (indent_tabs, line))
    if line.endswith('{'):
      self._indent += 1

  @staticmethod
  def _RaiseModelError(model, msg):
    """Helper function to raise a ModelConverterError.

    Args:
      model: Model name the error relates to.
      msg: Message string to include in the exception.
    """
    raise ModelConverterError('Model: %s: %s' % (model, msg))

  def _AddFile(self, model, fname):
    """Add a new URL to our list of files to download.

    This adds the full URL to the given file, based on known locations in GCS.

    Args:
      model: Name of model (e.g. "reef").
      fname: Filename of file to download
    """
    url = ('gs://chromeos-binaries/HOME/bcs-%s-private/overlay-%s-private/'
           'chromeos-base/chromeos-firmware-%s/%s' %
           (model, model, model, fname))
    self._urls.append(url)

  def _OutputNode(self, model, shell_vars):
    """Output a firmware node for a model.

    This outputs each property on its own line.

    Args:
      model: Name of mode (and therefore node).
      shell_vars: Shell varaible dict:
          key: Shell variable name (e.g. CROS_FIRMWARE_MAIN_IMAGE)
          value: Value of variable (e.g. 'bcs://Reef.9042.87.0.tbz2')
    """
    for var, value in sorted(shell_vars.iteritems()):
      if not var.startswith(CROS_FIRMWARE):
        continue
      var = var[len(CROS_FIRMWARE):]
      prop = TRANSLATE_TO_PROPERTY.get(var)
      if prop is None:
        self._RaiseModelError(model, 'Shell variable %s is unknown' % var)
      if prop and value:
        # This boolean property has no value.
        if var == 'BUILD_MAIN_RW_IMAGE':
          self._AddLine('%s;' % prop)

        # This property needs processing to turn it into a proper string list.
        elif var == 'EXTRA_LIST':
          self._AddLine('%s = "%s";' % (prop, '", "'.join(value.split(';'))))

        # Normal properties just have a property name and string value.
        else:
          self._AddLine('%s = "%s";' % (prop, value))
        if value.startswith(BCS_PREFIX):
          self._AddFile(model, value[len(BCS_PREFIX):])

  def _ProcessModel(self, model):
    """Process an ebuild for a model.

    This finds the ebuild for the given model and outputs a configuration node
    for it.

    Args:
      model: Model to process.

    Returns:
      Partial pathname of the ebuild that was processed (the part after
          self._src).
    """
    pathname = os.path.join(self._srcpath, self._path_format % {'model': model})
    ebuild = [fname for fname in glob.glob(pathname) if '9999' not in fname]
    if not ebuild:
      self._RaiseModelError(model, 'Could not find ebuild at "%s"' % pathname)
    if len(ebuild) != 1:
      self._RaiseModelError(model, 'Expected a single ebuild, found: %s' %
                            '\n'.join(ebuild))
    fname = ebuild[0]
    env = {}
    for var in ['FILESDIR', 'ROOT', 'SYSROOT']:
      env[var] = '${%s}' % var
    shell_vars = osutils.SourceEnvironment(
        fname,
        [CROS_FIRMWARE + s for s in TRANSLATE_TO_PROPERTY.keys()],
        env=env)

    self._AddLine('%s {' % model)
    self._AddLine('firmware {')

    # This is only used in zgb and is deprecated, so skip it.
    if '%sEC_VERSION' % CROS_FIRMWARE in shell_vars:
      self._RaiseModelError(model, 'Manual %sEC_VERSION is not supported' %
                            CROS_FIRMWARE)
    self._OutputNode(model, shell_vars)
    self._AddLine('};')
    self._AddLine('};')
    self._AddLine('')
    return fname[len(self._srcpath) + 1:]

  def Start(self):
    """Start processing ebuilds.

    Args:
      argv: Program arguments (without program name).
    """
    if self._models:
      self._AddLine('&models {')

      # Process all models, saving the first one's ebuild path. The first one
      # should be the reference board.
      ebuild_pathname = None
      for model in self._models:
        fname = self._ProcessModel(model)
        if not ebuild_pathname:
          ebuild_pathname = fname
      self._AddLine('};')
      print()
      print('Run this command in chroot to regenerate the manifest:')
      print('FIRMWARE_UNIBUILD="%s" ebuild %s manifest' %
            (' '.join(self._models),
             os.path.join(constants.CHROOT_SOURCE_ROOT, 'src',
                          ebuild_pathname)))
    else:
      pathname = os.path.join(self._srcpath,
                              'private-overlays/overlay-*-private')
      for dirname in sorted(glob.glob(pathname)):
        m = RE_VARIANT_NAME.search(dirname)
        if m:
          model = m.group(1)
          try:
            self._ProcessModel(model)
          except ModelConverterError as e:
            logging.error(str(e))
    return 0


def main(argv):
  args = _ParseArgs(argv)
  conv = ModelConverter(args.models)
  conv.Start()


if __name__ == '__main__':
  sys.exit(main(sys.argv[1:]))
