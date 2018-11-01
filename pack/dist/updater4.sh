#!/bin/sh
#
# Copyright (c) 2012 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
#
# For factory and auto update, after shell-ball self-extracts, this script is
# called to update BIOS and EC firmware as per how many files are extracted.
# To simply design, THIS SCRIPT MUST BE EXECUTED IN A R/W EXCLUSIVE TEMP FOLDER.
# AND ALL FILENAMES FOR INPUT AND OUTPUT MUST NOT CONTAIN SPACE.
#
# Allowed commands are:
# - standard shell commands
# - flashrom
# - futility
# - crossystem
# All other special commands should be defined by a function in crosutil.sh.
#
# Temporary files should be named as "_*" to prevent confliction

# Updater for firmware v4 (two-stop main, chromeos-ec).
# This is designed for x86/arm platform, using chromeos-ec (software sync).
# Assume SLOT_A and SLOT_B has exactly same contents (and same keyblock).

# THIS FILE IS FROZEN. ANY FURTHER CHANGES SHOULD GO TO UPDATER5.

# ----------------------------------------------------------------------------
# shflags

# $Id: shflags 133 2009-05-10 18:04:51Z kate.ward@forestent.com $
# vim:et:ft=sh:sts=2:sw=2
#
# Copyright 2008 Kate Ward. All Rights Reserved.
# Released under the LGPL (GNU Lesser General Public License)
#
# shFlags -- Advanced command-line flag library for Unix shell scripts.
# http://code.google.com/p/shflags/
#
# Author: kate.ward@forestent.com (Kate Ward)
#
# This module implements something like the google-gflags library available
# from http://code.google.com/p/google-gflags/.
#
# FLAG TYPES: This is a list of the DEFINE_*'s that you can do.  All flags take
# a name, default value, help-string, and optional 'short' name (one-letter
# name).  Some flags have other arguments, which are described with the flag.
#
# DEFINE_string: takes any input, and intreprets it as a string.
#
# DEFINE_boolean: typically does not take any argument: say --myflag to set
#   FLAGS_myflag to true, or --nomyflag to set FLAGS_myflag to false.
#   Alternately, you can say
#     --myflag=true  or --myflag=t or --myflag=0  or
#     --myflag=false or --myflag=f or --myflag=1
#   Passing an option has the same affect as passing the option once.
#
# DEFINE_float: takes an input and intreprets it as a floating point number. As
#   shell does not support floats per-se, the input is merely validated as
#   being a valid floating point value.
#
# DEFINE_integer: takes an input and intreprets it as an integer.
#
# SPECIAL FLAGS: There are a few flags that have special meaning:
#   --help (or -?)  prints a list of all the flags in a human-readable fashion
#   --flagfile=foo  read flags from foo.  (not implemented yet)
#   --              as in getopt(), terminates flag-processing
#
# EXAMPLE USAGE:
#
#   -- begin hello.sh --
#   #! /bin/sh
#   . ./shflags
#   DEFINE_string name 'world' "somebody's name" n
#   FLAGS "$@" || exit $?
#   eval set -- "${FLAGS_ARGV}"
#   echo "Hello, ${FLAGS_name}."
#   -- end hello.sh --
#
#   $ ./hello.sh -n Kate
#   Hello, Kate.
#
# NOTE: Not all systems include a getopt version that supports long flags. On
# these systems, only short flags are recognized.

#==============================================================================
# shFlags
#
# Shared attributes:
#   flags_error: last error message
#   flags_return: last return value
#
#   __flags_longNames: list of long names for all flags
#   __flags_shortNames: list of short names for all flags
#   __flags_boolNames: list of boolean flag names
#
#   __flags_opts: options parsed by getopt
#
# Per-flag attributes:
#   FLAGS_<flag_name>: contains value of flag named 'flag_name'
#   __flags_<flag_name>_default: the default flag value
#   __flags_<flag_name>_help: the flag help string
#   __flags_<flag_name>_short: the flag short name
#   __flags_<flag_name>_type: the flag type
#
# Notes:
# - lists of strings are space separated, and a null value is the '~' char.

# return if FLAGS already loaded
[ -n "${FLAGS_VERSION:-}" ] && return 0
FLAGS_VERSION='1.0.3'

# return values
FLAGS_TRUE=0
FLAGS_FALSE=1
FLAGS_ERROR=2

# reserved flag names
FLAGS_RESERVED='ARGC ARGV ERROR FALSE HELP PARENT RESERVED TRUE VERSION'

_flags_debug() { echo "flags:DEBUG $@" >&2; }
_flags_warn() { echo "flags:WARN $@" >&2; }
_flags_error() { echo "flags:ERROR $@" >&2; }
_flags_fatal() { echo "flags:FATAL $@" >&2; }

# specific shell checks
if [ -n "${ZSH_VERSION:-}" ]; then
  setopt |grep "^shwordsplit$" >/dev/null
  if [ $? -ne ${FLAGS_TRUE} ]; then
    _flags_fatal 'zsh shwordsplit option is required for proper zsh operation'
    exit ${FLAGS_ERROR}
  fi
  if [ -z "${FLAGS_PARENT:-}" ]; then
    _flags_fatal "zsh does not pass \$0 through properly. please declare' \
\"FLAGS_PARENT=\$0\" before calling shFlags"
    exit ${FLAGS_ERROR}
  fi
fi

#
# constants
#

# getopt version
__FLAGS_GETOPT_VERS_STD=0
__FLAGS_GETOPT_VERS_ENH=1
__FLAGS_GETOPT_VERS_BSD=2

getopt >/dev/null 2>&1
case $? in
  0) __FLAGS_GETOPT_VERS=${__FLAGS_GETOPT_VERS_STD} ;;  # bsd getopt
  2)
    # TODO(kward): look into '-T' option to test the internal getopt() version
    if [ "`getopt --version`" = '-- ' ]; then
      __FLAGS_GETOPT_VERS=${__FLAGS_GETOPT_VERS_STD}
    else
      __FLAGS_GETOPT_VERS=${__FLAGS_GETOPT_VERS_ENH}
    fi
    ;;
  *)
    _flags_fatal 'unable to determine getopt version'
    exit ${FLAGS_ERROR}
    ;;
esac

# getopt optstring lengths
__FLAGS_OPTSTR_SHORT=0
__FLAGS_OPTSTR_LONG=1

__FLAGS_NULL='~'

# flag info strings
__FLAGS_INFO_DEFAULT='default'
__FLAGS_INFO_HELP='help'
__FLAGS_INFO_SHORT='short'
__FLAGS_INFO_TYPE='type'

# flag lengths
__FLAGS_LEN_SHORT=0
__FLAGS_LEN_LONG=1

# flag types
__FLAGS_TYPE_NONE=0
__FLAGS_TYPE_BOOLEAN=1
__FLAGS_TYPE_FLOAT=2
__FLAGS_TYPE_INTEGER=3
__FLAGS_TYPE_STRING=4

# set the constants readonly
__flags_constants=`set |awk -F= '/^FLAGS_/ || /^__FLAGS_/ {print $1}'`
for __flags_const in ${__flags_constants}; do
  # skip certain flags
  case ${__flags_const} in
    FLAGS_HELP) continue ;;
    FLAGS_PARENT) continue ;;
  esac
  # set flag readonly
  if [ -z "${ZSH_VERSION:-}" ]; then
    readonly ${__flags_const}
  else  # handle zsh
    case ${ZSH_VERSION} in
      [123].*) readonly ${__flags_const} ;;
      *) readonly -g ${__flags_const} ;;  # declare readonly constants globally
    esac
  fi
done
unset __flags_const __flags_constants

#
# internal variables
#

__flags_boolNames=' '  # space separated list of boolean flag names
__flags_longNames=' '  # space separated list of long flag names
__flags_shortNames=' '  # space separated list of short flag names

__flags_columns=''  # screen width in columns
__flags_opts=''  # temporary storage for parsed getopt flags

#------------------------------------------------------------------------------
# private functions
#

# Define a flag.
#
# Calling this function will define the following info variables for the
# specified flag:
#   FLAGS_flagname - the name for this flag (based upon the long flag name)
#   __flags_<flag_name>_default - the default value
#   __flags_flagname_help - the help string
#   __flags_flagname_short - the single letter alias
#   __flags_flagname_type - the type of flag (one of __FLAGS_TYPE_*)
#
# Args:
#   _flags__type: integer: internal type of flag (__FLAGS_TYPE_*)
#   _flags__name: string: long flag name
#   _flags__default: default flag value
#   _flags__help: string: help string
#   _flags__short: string: (optional) short flag name
# Returns:
#   integer: success of operation, or error
_flags_define()
{
  if [ $# -lt 4 ]; then
    flags_error='DEFINE error: too few arguments'
    flags_return=${FLAGS_ERROR}
    _flags_error "${flags_error}"
    return ${flags_return}
  fi

  _flags_type_=$1
  _flags_name_=$2
  _flags_default_=$3
  _flags_help_=$4
  _flags_short_=${5:-${__FLAGS_NULL}}

  _flags_return_=${FLAGS_TRUE}

  # TODO(kward): check for validity of the flag name (e.g. dashes)

  # check whether the flag name is reserved
  echo " ${FLAGS_RESERVED} " |grep " ${_flags_name_} " >/dev/null
  if [ $? -eq 0 ]; then
    flags_error="flag name (${_flags_name_}) is reserved"
    _flags_return_=${FLAGS_ERROR}
  fi

  # require short option for getopt that don't support long options
  if [ ${_flags_return_} -eq ${FLAGS_TRUE} \
      -a ${__FLAGS_GETOPT_VERS} -ne ${__FLAGS_GETOPT_VERS_ENH} \
      -a "${_flags_short_}" = "${__FLAGS_NULL}" ]
  then
    flags_error="short flag required for (${_flags_name_}) on this platform"
    _flags_return_=${FLAGS_ERROR}
  fi

  # check for existing long name definition
  if [ ${_flags_return_} -eq ${FLAGS_TRUE} ]; then
    if _flags_itemInList "${_flags_name_}" \
        ${__flags_longNames} ${__flags_boolNames}
    then
      flags_error="flag name ([no]${_flags_name_}) already defined"
      _flags_warn "${flags_error}"
      _flags_return_=${FLAGS_FALSE}
    fi
  fi

  # check for existing short name definition
  if [ ${_flags_return_} -eq ${FLAGS_TRUE} \
      -a "${_flags_short_}" != "${__FLAGS_NULL}" ]
  then
    if _flags_itemInList "${_flags_short_}" ${__flags_shortNames}; then
      flags_error="flag short name (${_flags_short_}) already defined"
      _flags_warn "${flags_error}"
      _flags_return_=${FLAGS_FALSE}
    fi
  fi

  # handle default value. note, on several occasions the 'if' portion of an
  # if/then/else contains just a ':' which does nothing. a binary reversal via
  # '!' is not done because it does not work on all shells.
  if [ ${_flags_return_} -eq ${FLAGS_TRUE} ]; then
    case ${_flags_type_} in
      ${__FLAGS_TYPE_BOOLEAN})
        if _flags_validateBoolean "${_flags_default_}"; then
          case ${_flags_default_} in
            true|t|0) _flags_default_=${FLAGS_TRUE} ;;
            false|f|1) _flags_default_=${FLAGS_FALSE} ;;
          esac
        else
          flags_error="invalid default flag value '${_flags_default_}'"
          _flags_return_=${FLAGS_ERROR}
        fi
        ;;

      ${__FLAGS_TYPE_FLOAT})
        if _flags_validateFloat "${_flags_default_}"; then
          :
        else
          flags_error="invalid default flag value '${_flags_default_}'"
          _flags_return_=${FLAGS_ERROR}
        fi
        ;;

      ${__FLAGS_TYPE_INTEGER})
        if _flags_validateInteger "${_flags_default_}"; then
          :
        else
          flags_error="invalid default flag value '${_flags_default_}'"
          _flags_return_=${FLAGS_ERROR}
        fi
        ;;

      ${__FLAGS_TYPE_STRING}) ;;  # everything in shell is a valid string

      *)
        flags_error="unrecognized flag type '${_flags_type_}'"
        _flags_return_=${FLAGS_ERROR}
        ;;
    esac
  fi

  if [ ${_flags_return_} -eq ${FLAGS_TRUE} ]; then
    # store flag information
    eval "FLAGS_${_flags_name_}='${_flags_default_}'"
    eval "__flags_${_flags_name_}_${__FLAGS_INFO_TYPE}=${_flags_type_}"
    eval "__flags_${_flags_name_}_${__FLAGS_INFO_DEFAULT}=\
\"${_flags_default_}\""
    eval "__flags_${_flags_name_}_${__FLAGS_INFO_HELP}=\"${_flags_help_}\""
    eval "__flags_${_flags_name_}_${__FLAGS_INFO_SHORT}='${_flags_short_}'"

    # append flag name(s) to list of names
    __flags_longNames="${__flags_longNames}${_flags_name_} "
    __flags_shortNames="${__flags_shortNames}${_flags_short_} "
    [ ${_flags_type_} -eq ${__FLAGS_TYPE_BOOLEAN} ] && \
        __flags_boolNames="${__flags_boolNames}no${_flags_name_} "
  fi

  flags_return=${_flags_return_}
  unset _flags_default_ _flags_help_ _flags_name_ _flags_return_ _flags_short_ \
      _flags_type_
  [ ${flags_return} -eq ${FLAGS_ERROR} ] && _flags_error "${flags_error}"
  return ${flags_return}
}

# Return valid getopt options using currently defined list of long options.
#
# This function builds a proper getopt option string for short (and long)
# options, using the current list of long options for reference.
#
# Args:
#   _flags_optStr: integer: option string type (__FLAGS_OPTSTR_*)
# Output:
#   string: generated option string for getopt
# Returns:
#   boolean: success of operation (always returns True)
_flags_genOptStr()
{
  _flags_optStrType_=$1

  _flags_opts_=''

  for _flags_flag_ in ${__flags_longNames}; do
    _flags_type_=`_flags_getFlagInfo ${_flags_flag_} ${__FLAGS_INFO_TYPE}`
    case ${_flags_optStrType_} in
      ${__FLAGS_OPTSTR_SHORT})
        _flags_shortName_=`_flags_getFlagInfo \
            ${_flags_flag_} ${__FLAGS_INFO_SHORT}`
        if [ "${_flags_shortName_}" != "${__FLAGS_NULL}" ]; then
          _flags_opts_="${_flags_opts_}${_flags_shortName_}"
          # getopt needs a trailing ':' to indicate a required argument
          [ ${_flags_type_} -ne ${__FLAGS_TYPE_BOOLEAN} ] && \
              _flags_opts_="${_flags_opts_}:"
        fi
        ;;

      ${__FLAGS_OPTSTR_LONG})
        _flags_opts_="${_flags_opts_:+${_flags_opts_},}${_flags_flag_}"
        # getopt needs a trailing ':' to indicate a required argument
        [ ${_flags_type_} -ne ${__FLAGS_TYPE_BOOLEAN} ] && \
            _flags_opts_="${_flags_opts_}:"
        ;;
    esac
  done

  echo "${_flags_opts_}"
  unset _flags_flag_ _flags_opts_ _flags_optStrType_ _flags_shortName_ \
      _flags_type_
  return ${FLAGS_TRUE}
}

# Returns flag details based on a flag name and flag info.
#
# Args:
#   string: long flag name
#   string: flag info (see the _flags_define function for valid info types)
# Output:
#   string: value of dereferenced flag variable
# Returns:
#   integer: one of FLAGS_{TRUE|FALSE|ERROR}
_flags_getFlagInfo()
{
  _flags_name_=$1
  _flags_info_=$2

  _flags_nameVar_="__flags_${_flags_name_}_${_flags_info_}"
  _flags_strToEval_="_flags_value_=\"\${${_flags_nameVar_}:-}\""
  eval "${_flags_strToEval_}"
  if [ -n "${_flags_value_}" ]; then
    flags_return=${FLAGS_TRUE}
  else
    # see if the _flags_name_ variable is a string as strings can be empty...
    # note: the DRY principle would say to have this function call itself for
    # the next three lines, but doing so results in an infinite loop as an
    # invalid _flags_name_ will also not have the associated _type variable.
    # Because it doesn't (it will evaluate to an empty string) the logic will
    # try to find the _type variable of the _type variable, and so on. Not so
    # good ;-)
    _flags_typeVar_="__flags_${_flags_name_}_${__FLAGS_INFO_TYPE}"
    _flags_strToEval_="_flags_type_=\"\${${_flags_typeVar_}:-}\""
    eval "${_flags_strToEval_}"
    if [ "${_flags_type_}" = "${__FLAGS_TYPE_STRING}" ]; then
      flags_return=${FLAGS_TRUE}
    else
      flags_return=${FLAGS_ERROR}
      flags_error="invalid flag name (${_flags_nameVar_})"
    fi
  fi

  echo "${_flags_value_}"
  unset _flags_info_ _flags_name_ _flags_strToEval_ _flags_type_ _flags_value_ \
      _flags_nameVar_ _flags_typeVar_
  [ ${flags_return} -eq ${FLAGS_ERROR} ] && _flags_error "${flags_error}"
  return ${flags_return}
}

# check for presense of item in a list. passed a string (e.g. 'abc'), this
# function will determine if the string is present in the list of strings (e.g.
# ' foo bar abc ').
#
# Args:
#   _flags__str: string: string to search for in a list of strings
#   unnamed: list: list of strings
# Returns:
#   boolean: true if item is in the list
_flags_itemInList()
{
  _flags_str_=$1
  shift

  echo " ${*:-} " |grep " ${_flags_str_} " >/dev/null
  if [ $? -eq 0 ]; then
    flags_return=${FLAGS_TRUE}
  else
    flags_return=${FLAGS_FALSE}
  fi

  unset _flags_str_
  return ${flags_return}
}

# Returns the width of the current screen.
#
# Output:
#   integer: width in columns of the current screen.
_flags_columns()
{
  if [ -z "${__flags_columns}" ]; then
    # determine the value and store it
    if eval stty size >/dev/null 2>&1; then
      # stty size worked :-)
      set -- `stty size`
      __flags_columns=$2
    elif eval tput cols >/dev/null 2>&1; then
      set -- `tput cols`
      __flags_columns=$1
    else
      __flags_columns=80  # default terminal width
    fi
  fi
  echo ${__flags_columns}
}

# Validate a boolean.
#
# Args:
#   _flags__bool: boolean: value to validate
# Returns:
#   bool: true if the value is a valid boolean
_flags_validateBoolean()
{
  _flags_bool_=$1

  flags_return=${FLAGS_TRUE}
  case "${_flags_bool_}" in
    true|t|0) ;;
    false|f|1) ;;
    *) flags_return=${FLAGS_FALSE} ;;
  esac

  unset _flags_bool_
  return ${flags_return}
}

# Validate a float.
#
# Args:
#   _flags__float: float: value to validate
# Returns:
#   bool: true if the value is a valid float
_flags_validateFloat()
{
  _flags_float_=$1

  if _flags_validateInteger ${_flags_float_}; then
    flags_return=${FLAGS_TRUE}
  else
    flags_return=${FLAGS_TRUE}
    case ${_flags_float_} in
      -*)  # negative floats
        _flags_test_=`expr "${_flags_float_}" : '\(-[0-9][0-9]*\.[0-9][0-9]*\)'`
        ;;
      *)  # positive floats
        _flags_test_=`expr "${_flags_float_}" : '\([0-9][0-9]*\.[0-9][0-9]*\)'`
        ;;
    esac
    [ "${_flags_test_}" != "${_flags_float_}" ] && flags_return=${FLAGS_FALSE}
  fi

  unset _flags_float_ _flags_test_
  return ${flags_return}
}

# Validate an integer.
#
# Args:
#   _flags__integer: interger: value to validate
# Returns:
#   bool: true if the value is a valid integer
_flags_validateInteger()
{
  _flags_int_=$1

  flags_return=${FLAGS_TRUE}
  case ${_flags_int_} in
    -*)  # negative ints
      _flags_test_=`expr "${_flags_int_}" : '\(-[0-9][0-9]*\)'`
      ;;
    *)  # positive ints
      _flags_test_=`expr "${_flags_int_}" : '\([0-9][0-9]*\)'`
      ;;
  esac
  [ "${_flags_test_}" != "${_flags_int_}" ] && flags_return=${FLAGS_FALSE}

  unset _flags_int_ _flags_test_
  return ${flags_return}
}

# Parse command-line options using the standard getopt.
#
# Note: the flag options are passed around in the global __flags_opts so that
# the formatting is not lost due to shell parsing and such.
#
# Args:
#   @: varies: command-line options to parse
# Returns:
#   integer: a FLAGS success condition
_flags_getoptStandard()
{
  flags_return=${FLAGS_TRUE}
  _flags_shortOpts_=`_flags_genOptStr ${__FLAGS_OPTSTR_SHORT}`

  # check for spaces in passed options
  for _flags_opt_ in "$@"; do
    # note: the silliness with the x's is purely for ksh93 on Ubuntu 6.06
    _flags_match_=`echo "x${_flags_opt_}x" |sed 's/ //g'`
    if [ "${_flags_match_}" != "x${_flags_opt_}x" ]; then
      flags_error='the available getopt does not support spaces in options'
      flags_return=${FLAGS_ERROR}
      break
    fi
  done

  if [ ${flags_return} -eq ${FLAGS_TRUE} ]; then
    __flags_opts=`getopt ${_flags_shortOpts_} $@ 2>&1`
    _flags_rtrn_=$?
    if [ ${_flags_rtrn_} -ne ${FLAGS_TRUE} ]; then
      _flags_warn "${__flags_opts}"
      flags_error='unable to parse provided options with getopt.'
      flags_return=${FLAGS_ERROR}
    fi
  fi

  unset _flags_match_ _flags_opt_ _flags_rtrn_ _flags_shortOpts_
  return ${flags_return}
}

# Parse command-line options using the enhanced getopt.
#
# Note: the flag options are passed around in the global __flags_opts so that
# the formatting is not lost due to shell parsing and such.
#
# Args:
#   @: varies: command-line options to parse
# Returns:
#   integer: a FLAGS success condition
_flags_getoptEnhanced()
{
  flags_return=${FLAGS_TRUE}
  _flags_shortOpts_=`_flags_genOptStr ${__FLAGS_OPTSTR_SHORT}`
  _flags_boolOpts_=`echo "${__flags_boolNames}" \
      |sed 's/^ *//;s/ *$//;s/ /,/g'`
  _flags_longOpts_=`_flags_genOptStr ${__FLAGS_OPTSTR_LONG}`

  __flags_opts=`getopt \
      -o ${_flags_shortOpts_} \
      -l "${_flags_longOpts_},${_flags_boolOpts_}" \
      -- "$@" 2>&1`
  _flags_rtrn_=$?
  if [ ${_flags_rtrn_} -ne ${FLAGS_TRUE} ]; then
    _flags_warn "${__flags_opts}"
    flags_error='unable to parse provided options with getopt.'
    flags_return=${FLAGS_ERROR}
  fi

  unset _flags_boolOpts_ _flags_longOpts_ _flags_rtrn_ _flags_shortOpts_
  return ${flags_return}
}

# Dynamically parse a getopt result and set appropriate variables.
#
# This function does the actual conversion of getopt output and runs it through
# the standard case structure for parsing. The case structure is actually quite
# dynamic to support any number of flags.
#
# Args:
#   argc: int: original command-line argument count
#   @: varies: output from getopt parsing
# Returns:
#   integer: a FLAGS success condition
_flags_parseGetopt()
{
  _flags_argc_=$1
  shift

  flags_return=${FLAGS_TRUE}

  if [ ${__FLAGS_GETOPT_VERS} -ne ${__FLAGS_GETOPT_VERS_ENH} ]; then
    set -- $@
  else
    # note the quotes around the `$@' -- they are essential!
    eval set -- "$@"
  fi

  # provide user with number of arguments to shift by later
  # NOTE: the FLAGS_ARGC variable is obsolete as of 1.0.3 because it does not
  # properly give user access to non-flag arguments mixed in between flag
  # arguments. Its usage was replaced by FLAGS_ARGV, and it is being kept only
  # for backwards compatibility reasons.
  FLAGS_ARGC=`expr $# - 1 - ${_flags_argc_}`

  # handle options. note options with values must do an additional shift
  while true; do
    _flags_opt_=$1
    _flags_arg_=${2:-}
    _flags_type_=${__FLAGS_TYPE_NONE}
    _flags_name_=''

    # determine long flag name
    case "${_flags_opt_}" in
      --) shift; break ;;  # discontinue option parsing

      --*)  # long option
        _flags_opt_=`expr "${_flags_opt_}" : '--\(.*\)'`
        _flags_len_=${__FLAGS_LEN_LONG}
        if _flags_itemInList "${_flags_opt_}" ${__flags_longNames}; then
          _flags_name_=${_flags_opt_}
        else
          # check for negated long boolean version
          if _flags_itemInList "${_flags_opt_}" ${__flags_boolNames}; then
            _flags_name_=`expr "${_flags_opt_}" : 'no\(.*\)'`
            _flags_type_=${__FLAGS_TYPE_BOOLEAN}
            _flags_arg_=${__FLAGS_NULL}
          fi
        fi
        ;;

      -*)  # short option
        _flags_opt_=`expr "${_flags_opt_}" : '-\(.*\)'`
        _flags_len_=${__FLAGS_LEN_SHORT}
        if _flags_itemInList "${_flags_opt_}" ${__flags_shortNames}; then
          # yes. match short name to long name. note purposeful off-by-one
          # (too high) with awk calculations.
          _flags_pos_=`echo "${__flags_shortNames}" \
              |awk 'BEGIN{RS=" ";rn=0}$0==e{rn=NR}END{print rn}' \
                  e=${_flags_opt_}`
          _flags_name_=`echo "${__flags_longNames}" \
              |awk 'BEGIN{RS=" "}rn==NR{print $0}' rn="${_flags_pos_}"`
        fi
        ;;
    esac

    # die if the flag was unrecognized
    if [ -z "${_flags_name_}" ]; then
      flags_error="unrecognized option (${_flags_opt_})"
      flags_return=${FLAGS_ERROR}
      break
    fi

    # set new flag value
    [ ${_flags_type_} -eq ${__FLAGS_TYPE_NONE} ] && \
        _flags_type_=`_flags_getFlagInfo \
            "${_flags_name_}" ${__FLAGS_INFO_TYPE}`
    case ${_flags_type_} in
      ${__FLAGS_TYPE_BOOLEAN})
        if [ ${_flags_len_} -eq ${__FLAGS_LEN_LONG} ]; then
          if [ "${_flags_arg_}" != "${__FLAGS_NULL}" ]; then
            eval "FLAGS_${_flags_name_}=${FLAGS_TRUE}"
          else
            eval "FLAGS_${_flags_name_}=${FLAGS_FALSE}"
          fi
        else
          _flags_strToEval_="_flags_val_=\
\${__flags_${_flags_name_}_${__FLAGS_INFO_DEFAULT}}"
          eval "${_flags_strToEval_}"
          if [ ${_flags_val_} -eq ${FLAGS_FALSE} ]; then
            eval "FLAGS_${_flags_name_}=${FLAGS_TRUE}"
          else
            eval "FLAGS_${_flags_name_}=${FLAGS_FALSE}"
          fi
        fi
        ;;

      ${__FLAGS_TYPE_FLOAT})
        if _flags_validateFloat "${_flags_arg_}"; then
          eval "FLAGS_${_flags_name_}='${_flags_arg_}'"
        else
          flags_error="invalid float value (${_flags_arg_})"
          flags_return=${FLAGS_ERROR}
          break
        fi
        ;;

      ${__FLAGS_TYPE_INTEGER})
        if _flags_validateInteger "${_flags_arg_}"; then
          eval "FLAGS_${_flags_name_}='${_flags_arg_}'"
        else
          flags_error="invalid integer value (${_flags_arg_})"
          flags_return=${FLAGS_ERROR}
          break
        fi
        ;;

      ${__FLAGS_TYPE_STRING})
        eval "FLAGS_${_flags_name_}='${_flags_arg_}'"
        ;;
    esac

    # handle special case help flag
    if [ "${_flags_name_}" = 'help' ]; then
      if [ ${FLAGS_help} -eq ${FLAGS_TRUE} ]; then
        flags_help
        flags_error='help requested'
        flags_return=${FLAGS_FALSE}
        break
      fi
    fi

    # shift the option and non-boolean arguements out.
    shift
    [ ${_flags_type_} != ${__FLAGS_TYPE_BOOLEAN} ] && shift
  done

  # give user back non-flag arguments
  FLAGS_ARGV=''
  while [ $# -gt 0 ]; do
    FLAGS_ARGV="${FLAGS_ARGV:+${FLAGS_ARGV} }'$1'"
    shift
  done

  unset _flags_arg_ _flags_len_ _flags_name_ _flags_opt_ _flags_pos_ \
      _flags_strToEval_ _flags_type_ _flags_val_
  return ${flags_return}
}

#------------------------------------------------------------------------------
# public functions
#

# A basic boolean flag. Boolean flags do not take any arguments, and their
# value is either 1 (false) or 0 (true). For long flags, the false value is
# specified on the command line by prepending the word 'no'. With short flags,
# the presense of the flag toggles the current value between true and false.
# Specifying a short boolean flag twice on the command results in returning the
# value back to the default value.
#
# A default value is required for boolean flags.
#
# For example, lets say a Boolean flag was created whose long name was 'update'
# and whose short name was 'x', and the default value was 'false'. This flag
# could be explicitly set to 'true' with '--update' or by '-x', and it could be
# explicitly set to 'false' with '--noupdate'.
DEFINE_boolean() { _flags_define ${__FLAGS_TYPE_BOOLEAN} "$@"; }

# Other basic flags.
DEFINE_float()   { _flags_define ${__FLAGS_TYPE_FLOAT} "$@"; }
DEFINE_integer() { _flags_define ${__FLAGS_TYPE_INTEGER} "$@"; }
DEFINE_string()  { _flags_define ${__FLAGS_TYPE_STRING} "$@"; }

# Parse the flags.
#
# Args:
#   unnamed: list: command-line flags to parse
# Returns:
#   integer: success of operation, or error
FLAGS()
{
  # define a standard 'help' flag if one isn't already defined
  [ -z "${__flags_help_type:-}" ] && \
      DEFINE_boolean 'help' false 'show this help' 'h'

  # parse options
  if [ $# -gt 0 ]; then
    if [ ${__FLAGS_GETOPT_VERS} -ne ${__FLAGS_GETOPT_VERS_ENH} ]; then
      _flags_getoptStandard "$@"
    else
      _flags_getoptEnhanced "$@"
    fi
    flags_return=$?
  else
    # nothing passed; won't bother running getopt
    __flags_opts='--'
    flags_return=${FLAGS_TRUE}
  fi

  if [ ${flags_return} -eq ${FLAGS_TRUE} ]; then
    _flags_parseGetopt $# "${__flags_opts}"
    flags_return=$?
  fi

  [ ${flags_return} -eq ${FLAGS_ERROR} ] && _flags_fatal "${flags_error}"
  return ${flags_return}
}

# This is a helper function for determining the `getopt` version for platforms
# where the detection isn't working. It simply outputs debug information that
# can be included in a bug report.
#
# Args:
#   none
# Output:
#   debug info that can be included in a bug report
# Returns:
#   nothing
flags_getoptInfo()
{
  # platform info
  _flags_debug "uname -a: `uname -a`"
  _flags_debug "PATH: ${PATH}"

  # shell info
  if [ -n "${BASH_VERSION:-}" ]; then
    _flags_debug 'shell: bash'
    _flags_debug "BASH_VERSION: ${BASH_VERSION}"
  elif [ -n "${ZSH_VERSION:-}" ]; then
    _flags_debug 'shell: zsh'
    _flags_debug "ZSH_VERSION: ${ZSH_VERSION}"
  fi

  # getopt info
  getopt >/dev/null
  _flags_getoptReturn=$?
  _flags_debug "getopt return: ${_flags_getoptReturn}"
  _flags_debug "getopt --version: `getopt --version 2>&1`"

  unset _flags_getoptReturn
}

# Returns whether the detected getopt version is the enhanced version.
#
# Args:
#   none
# Output:
#   none
# Returns:
#   bool: true if getopt is the enhanced version
flags_getoptIsEnh()
{
  test ${__FLAGS_GETOPT_VERS} -eq ${__FLAGS_GETOPT_VERS_ENH}
}

# Returns whether the detected getopt version is the standard version.
#
# Args:
#   none
# Returns:
#   bool: true if getopt is the standard version
flags_getoptIsStd()
{
  test ${__FLAGS_GETOPT_VERS} -eq ${__FLAGS_GETOPT_VERS_STD}
}

# This is effectively a 'usage()' function. It prints usage information and
# exits the program with ${FLAGS_FALSE} if it is ever found in the command line
# arguments. Note this function can be overridden so other apps can define
# their own --help flag, replacing this one, if they want.
#
# Args:
#   none
# Returns:
#   integer: success of operation (always returns true)
flags_help()
{
  if [ -n "${FLAGS_HELP:-}" ]; then
    echo "${FLAGS_HELP}" >&2
  else
    echo "USAGE: ${FLAGS_PARENT:-$0} [flags] args" >&2
  fi
  if [ -n "${__flags_longNames}" ]; then
    echo 'flags:' >&2
    for flags_name_ in ${__flags_longNames}; do
      flags_flagStr_=''
      flags_boolStr_=''

      flags_default_=`_flags_getFlagInfo \
          "${flags_name_}" ${__FLAGS_INFO_DEFAULT}`
      flags_help_=`_flags_getFlagInfo \
          "${flags_name_}" ${__FLAGS_INFO_HELP}`
      flags_short_=`_flags_getFlagInfo \
          "${flags_name_}" ${__FLAGS_INFO_SHORT}`
      flags_type_=`_flags_getFlagInfo \
          "${flags_name_}" ${__FLAGS_INFO_TYPE}`

      [ "${flags_short_}" != "${__FLAGS_NULL}" ] \
          && flags_flagStr_="-${flags_short_}"

      if [ ${__FLAGS_GETOPT_VERS} -eq ${__FLAGS_GETOPT_VERS_ENH} ]; then
        [ "${flags_short_}" != "${__FLAGS_NULL}" ] \
            && flags_flagStr_="${flags_flagStr_},"
        [ ${flags_type_} -eq ${__FLAGS_TYPE_BOOLEAN} ] \
            && flags_boolStr_='[no]'
        flags_flagStr_="${flags_flagStr_}--${flags_boolStr_}${flags_name_}:"
      fi

      case ${flags_type_} in
        ${__FLAGS_TYPE_BOOLEAN})
          if [ ${flags_default_} -eq ${FLAGS_TRUE} ]; then
            flags_defaultStr_='true'
          else
            flags_defaultStr_='false'
          fi
          ;;
        ${__FLAGS_TYPE_FLOAT}|${__FLAGS_TYPE_INTEGER})
          flags_defaultStr_=${flags_default_} ;;
        ${__FLAGS_TYPE_STRING}) flags_defaultStr_="'${flags_default_}'" ;;
      esac
      flags_defaultStr_="(default: ${flags_defaultStr_})"

      flags_helpStr_="  ${flags_flagStr_}  ${flags_help_} ${flags_defaultStr_}"
      flags_helpStrLen_=`expr "${flags_helpStr_}" : '.*'`
      flags_columns_=`_flags_columns`
      if [ ${flags_helpStrLen_} -lt ${flags_columns_} ]; then
        echo "${flags_helpStr_}" >&2
      else
        echo "  ${flags_flagStr_}  ${flags_help_}" >&2
        # note: the silliness with the x's is purely for ksh93 on Ubuntu 6.06
        # because it doesn't like empty strings when used in this manner.
        flags_emptyStr_="`echo \"x${flags_flagStr_}x\" \
            |awk '{printf "%"length($0)-2"s", ""}'`"
        flags_helpStr_="  ${flags_emptyStr_}  ${flags_defaultStr_}"
        flags_helpStrLen_=`expr "${flags_helpStr_}" : '.*'`
        if [ ${__FLAGS_GETOPT_VERS} -eq ${__FLAGS_GETOPT_VERS_STD} \
            -o ${flags_helpStrLen_} -lt ${flags_columns_} ]; then
          # indented to match help string
          echo "${flags_helpStr_}" >&2
        else
          # indented four from left to allow for longer defaults as long flag
          # names might be used too, making things too long
          echo "    ${flags_defaultStr_}" >&2
        fi
      fi
    done
  fi

  unset flags_boolStr_ flags_default_ flags_defaultStr_ flags_emptyStr_ \
      flags_flagStr_ flags_help_ flags_helpStr flags_helpStrLen flags_name_ \
      flags_columns_ flags_short_ flags_type_
  return ${FLAGS_TRUE}
}

# Reset shflags back to an uninitialized state.
#
# Args:
#   none
# Returns:
#   nothing
flags_reset()
{
  for flags_name_ in ${__flags_longNames}; do
    flags_strToEval_="unset FLAGS_${flags_name_}"
    for flags_type_ in \
        ${__FLAGS_INFO_DEFAULT} \
        ${__FLAGS_INFO_HELP} \
        ${__FLAGS_INFO_SHORT} \
        ${__FLAGS_INFO_TYPE}
    do
      flags_strToEval_=\
"${flags_strToEval_} __flags_${flags_name_}_${flags_type_}"
    done
    eval ${flags_strToEval_}
  done

  # reset internal variables
  __flags_boolNames=' '
  __flags_longNames=' '
  __flags_shortNames=' '

  unset flags_name_ flags_type_ flags_strToEval_
}

# ----------------------------------------------------------------------------
# Common Command Line Arguments

modes="(autoupdate | legacy | recovery | factory_install | output)"

DEFINE_string mode "" "Updater mode ${modes}" "m"
DEFINE_string wp "" "Override write protection state (0/1)." ""

DEFINE_boolean debug $FLAGS_FALSE "Enable debug messages." "d"
DEFINE_boolean verbose $FLAGS_TRUE "Enable verbose messages." "v"
DEFINE_boolean dry_run $FLAGS_FALSE "Enable dry-run mode." ""
DEFINE_boolean force $FLAGS_FALSE "Try to force update." ""
DEFINE_boolean allow_reboot $FLAGS_TRUE \
  "Allow rebooting system immediately if required."
DEFINE_string customization_id "" \
  "Customization ID for keysets." ""
DEFINE_string signature_id "" "Signature ID pointing to the keyset to use." ""
DEFINE_string model "" \
  "Overrides the model used; instead of using 'mosys platform model'" ""

DEFINE_boolean update_ec $FLAGS_TRUE "Enable updating Embedded Firmware." ""
DEFINE_boolean update_pd $FLAGS_TRUE "Enable updating PD Firmware." ""
DEFINE_boolean update_main $FLAGS_TRUE "Enable updating Main Firmware." ""
DEFINE_boolean host_only $FLAGS_FALSE "Only update Main Firmware." ""

DEFINE_string output_dir "" \
  "Sets the output directory for files generated using --mode=output" "o"

DEFINE_boolean check_keys $FLAGS_TRUE "Check firmware keys before updating." ""
DEFINE_boolean check_rw_compatible $FLAGS_TRUE \
  "Check if RW firmware is compatible with current RO" ""
DEFINE_boolean check_platform $FLAGS_TRUE \
  "Bypass firmware updates if the system platform name is different" ""
# Required for factory compatibility
DEFINE_boolean factory $FLAGS_FALSE "Equivalent to --mode=factory_install"

# ----------------------------------------------------------------------------
# Common Utilities

# Reports some critical message in stderr
alert() {
  echo "$*" 1>&2
}

# Reports error message and exit(1)
# NOTE: top level script does not exit if call to die is inside a
# sub-shell (ex: (func-call), $(func-call)) or if-blocks.
die() {
  alert "ERROR: $*"
  exit 1
}

# Alias for die, backward compatibility for custom scripts in board overlay
err_die() {
  die "$@"
}

# Reports error message and exit(3)
# NOTE: top level script does not exit if call to die is inside a
# sub-shell (ex: (func-call), $(func-call)) or if-blocks.
die_need_reboot() {
  alert "ERROR: $*"
  exit 3
}

# Reports error message and exit(4)
# NOTE: top level script does not exit if call to die is inside a
# sub-shell (ex: (func-call), $(func-call)) or if-blocks.
die_need_ro_update() {
  alert "ERROR: $*"
  exit 4
}

# Prints a message if in verbose mode
verbose_msg() {
  if [ "$FLAGS_verbose" = "$FLAGS_TRUE" ] ||
     [ "$FLAGS_debug" = "$FLAGS_TRUE" ]; then
    echo "$*"
  fi
}

# Prints a message if in debug mode
debug_msg() {
  if [ "$FLAGS_debug" = "$FLAGS_TRUE" ]; then
    alert " (DEBUG) $*"
  fi
}

# Helps functions to check if there were given parameters in correct form.
# Syntax: check_param "prototype" "$@"
#         prototype is a string in "funcname(arg1,arg2,arg3)" form.
check_param() {
  local original_format="$1"
  local format="$(echo "$1" | sed 's/[(,) ]/ /g; s/  */ /g;')"
  local has_wild_arg=0
  local i=0 param=""

  # truncate the function name
  shift
  format="$(echo "$format" | sed 's/^[^ ]* //')"

  # check parameter numbers
  i=0
  for param in $format ; do
    if [ "$param" = "..." ]; then
      # ignore everything after '...'
      has_wild_arg=1
      break
    fi
    i=$(($i + 1))
  done
  if [ $has_wild_arg = 1 ]; then
    [ $# -ge $i ] || die "$original_format: need $i+ params (got $#): $@"
  else
    [ $# -eq $i ] || die "$original_format: need $i params (got $#): $@"
  fi

  # check if we have any empty parameters
  i=1
  for param in "$@"; do
    local param_name=$(echo "$format" | cut -d" " -f $i)
    # forr params started with 'opt_', allow it to be empty.
    if [ -z "$param" -a "${param_name##opt_*}" != "" ]; then
      shift # remove the invoke command name
      die "check_param: $original_format: " \
              "param '$param_name' is empty: '$@'"
    fi
    i=$(($i + 1))
  done
}

# Quick check and setup for basic envoronments.
set_flags() {
  # Adjust WP flags.
  case "$FLAGS_wp" in
    on | ON | true | TRUE | 1)
      FLAGS_wp=true
      ;;
    off | OFF | false | FALSE | 0)
      FLAGS_wp=false
      ;;
    "")
      FLAGS_wp=
      ;;
    *)
      die "Invalid option for --wp: ${FLAGS_wp}"
      ;;
  esac

  if [ "${FLAGS_host_only}" = "${FLAGS_TRUE}" ]; then
    FLAGS_update_ec=${FLAGS_FALSE}
    FLAGS_update_pd=${FLAGS_FALSE}
    debug_msg "Ignore EC/PD images bundled in updater."
  fi

  # Adjust update_* flags according to known images.
  if [ ! -s "$IMAGE_MAIN" ]; then
    FLAGS_update_main=${FLAGS_FALSE}
    verbose_msg "No main firmware bundled in updater, ignored."
  fi
  if [ ! -s "$IMAGE_EC" ]; then
    FLAGS_update_ec=${FLAGS_FALSE}
    debug_msg "No EC firmware bundled in updater, ignored."
  fi
  if [ ! -s "$IMAGE_PD" ]; then
    FLAGS_update_pd=${FLAGS_FALSE}
    debug_msg "No PD firmware bundled in updater, ignored."
  fi
}

# Executes a command, and provide messages if it failed.
silent_invoke() {
  local ret=$FLAGS_TRUE
  if [ "${FLAGS_dry_run}" = "${FLAGS_TRUE}" ]; then
    return $ret
  fi
  "$@" >_exec.stdout 2>_exec.stderr || ret=$?
  if [ "$ret" != "0" ]; then
    alert " Execution failed ($ret): $*"
    alert " Messages:"
    cat _exec.stdout >&2
    cat _exec.stderr >&2
  fi
  rm -f _exec.stdout
  rm -f _exec.stderr
  return $ret
}

# Prints a verbose message before calling silent_invoke.
invoke() {
  verbose_msg " * invoke: $*"
  silent_invoke "$@" || die "Execution FAILED."
}

alert_write_protection() {
  if ! cros_is_hardware_write_protected;  then
    echo "
    WARNING: Hardware write protection may be still enabled, and a RO update is
             scheduled. The update may fail.
    "
  fi
}

# Prints a standard "unofficial HWID" warning message
alert_unofficial_hwid() {
  # TODO(hungte) maybe we don't need this anymore - simply check if keys are
  # compatible is enough.
  echo "
  Your system is using an unofficial version of $1 firmware,
  and is forbidden to invoke this update.

  Please restore your original firmware with correct HWID,
  or invoke this update in factory mode.
  "
}

# Prints a standard "unkonwn platform" error message
alert_unknown_platform() {
  # Sytnax: alert_unknown_platform CURRENT EXPECTED
  echo "
  Sorry, this firmware update is only for $2 platform.
  Your system ($1) is either incompatible or using an
  unknown version of firmware.
  "
}

# Prints a "incompatible" warning message
alert_incompatible_firmware() {
  # Syntax: alert_incompatible_firmware reboot_will_autoupdate
  echo "
  Your current RO firmware is NOT compatible with the new RW firmware, and needs
  a RO update.  Please make sure you've DISABLED write protection for such
  special update."

  if [ "$1" = "${FLAGS_TRUE}" ]; then
    echo "
  A RO autoupdate is also scheduled in next boot.
  "
  fi

  echo "
  If you want to start a manual update immediately, please use command:

  sudo chromeos-firmwareupdate --mode=recovery --wp=0
  "
}

alert_incompatible_rootkey() {
  alert "
  Incompatible firmware image (Root key is different).

  You may need to disable hardware write protection and perform a factory
  install by '--mode=factory_install' or recovery by '--mode=recovery'.
  "
}

alert_incompatible_tpmkey() {
  alert "
  Incompatible firmware image (Rollback - older than keys stored in TPM).

  Please update with latest recovery image and firmware, or restart a
  factory setup process to reset TPM key version.
  "
}

# ----------------------------------------------------------------------------
# ChromeOS Firmware Unpacking Utilities

# Folders for preparing and unpacking firmware
# Layout: DIR_[TYPE]/[TYPE]/[SECTION], DIR_[TYPE]/[IMAGE]
DIR_CURRENT="_current"
DIR_TARGET="_target"

# Parameters for flashrom command
TARGET_OPT_MAIN="-p host"
TARGET_OPT_EC="-p ec"
TARGET_OPT_PD="-p ec:dev=1"
WRITE_OPT="--fast-verify"

# Image and key files.
IMAGE_MAIN="bios.bin"
IMAGE_MAIN_RW="bios_rw.bin"  # optional image.
IMAGE_EC="ec.bin"
IMAGE_PD="pd.bin"
KEYSET_DIR="keyset"

# Overrides this with any function name to perform special tasks when EC is
# updated (Ex, notify EC to check battery firmware updates)
CUSTOMIZATION_EC_POST_UPDATE=""
CUSTOMIZATION_PD_POST_UPDATE=""

# Unpacks target image, in full image and unpacked form.
crosfw_unpack_image() {
  check_param "crosfw_unpack_image(type, image, target_opt)" "$@"
  local type_name="$1" image_name="$2" target_opt="$3"
  debug_msg "preparing $type_name firmware images..."
  mkdir -p "$DIR_TARGET/$type_name"
  if [ -n "$(dirname ${image_name})" ]; then
    mkdir -p "${DIR_TARGET}/$(dirname ${image_name})"
  fi
  cp -f "$image_name" "$DIR_TARGET/$image_name"
  ( cd "$DIR_TARGET/$type_name";
    futility dump_fmap -x "../$image_name" >/dev/null 2>&1) ||
    die "Invalid firmware image (missing FMAP) in $image_name."
}

# Unpacks image from current system EEPROM, in full and/or unpacked form.
crosfw_unpack_current_image() {
  check_param "crosfw_unpack_current_image(type,image,target_opt,...)" "$@"
  local type_name="$1" image_name="$2" target_opt="$3"
  shift; shift; shift
  debug_msg "trying to read $type_name firmware from system EEPROM..."
  mkdir -p "$DIR_CURRENT/$type_name"
  local list="" i=""
  for i in $@ ; do
    list="$list -i $i"
  done
  if [ -n "$(dirname ${image_name})" ]; then
    mkdir -p "${DIR_CURRENT}/$(dirname ${image_name})"
  fi
  invoke flashrom ${target_opt} ${list} -r "${DIR_CURRENT}/${image_name}"
  # current may not have FMAP... (ex, from factory setup)
  ( cd "$DIR_CURRENT/$type_name";
    futility dump_fmap -x "../$image_name" >/dev/null 2>&1) || true
}

# Copies VPD data by merging sections from current system into target firmware
# image file.
crosfw_dupe_vpd() {
  check_param "crosfw_dupe_vpd(vpd_list, output, opt_input)" "$@"
  # Preserve VPD when updating an existing system (maya be legacy firmware or
  # ChromeOS firmware). The system may not have FMAP, so we need to use
  # emulation mode otherwise reading sections may fail.
  local vpd_list="$1"
  local output="$2"
  local input="$3"
  debug_msg "crosfw_dupe_vpd: Duplicating VPD... ($vpd_list)"

  # TODO(hungte) Speed up by reading only partitions specified in vpd_list
  # unless active firmware is nonchrome.
  if [ -z "$input" ]; then
    input="_vpd_temp.bin"
    debug_msg "Reading active firmware..."
    silent_invoke flashrom $TARGET_OPT_MAIN -r "$input" ||
      die "Failed to read current main firmware."
  fi
  local size_input="$(cros_get_file_size "$input")"
  local size_output="$(cros_get_file_size "$output")"

  if [ -z "$size_input" ] || [ "$size_input" = "0" ]; then
    die "Invalid current main firmware. Abort."
  fi
  if [ "$size_input" != "$size_output" ]; then
    die "Incompatible firmware image size ($size_input != $size_output)."
  fi

  # Build command for VPD list
  local vpd_list_cmd="" vpd_name=""
  local is_trusted_vpd=1
  local param="dummy:emulate=VARIABLE_SIZE,image=$output,size=$size_input"

  if futility dump_fmap "$input" >/dev/null 2>&1; then
    # Trust the FMAP in input
    debug_msg "Using VPD location from input FMAP"
    is_trusted_vpd=
    local tmpdir="_dupe_vpd.tmp"
    local input_path="$(readlink -f "$input")"
    (
      mkdir "$tmpdir"
      cd "$tmpdir"
      futility dump_fmap -x "$input_path"
    ) >/dev/null 2>&1
    for vpd_name in $vpd_list; do
      vpd_list_cmd="$vpd_list_cmd -i $vpd_name:$tmpdir/$vpd_name"
    done
  else
    debug_msg "Using VPD location from target FMAP"
    for vpd_name in $vpd_list; do
      vpd_list_cmd="$vpd_list_cmd -i $vpd_name"
    done
  fi

  debug_msg "Preserving VPD: -p $param $vpd_list_cmd"
  if ! silent_invoke flashrom -p "$param" $vpd_list_cmd -w "$input"; then
    if [ -n "$is_trusted_vpd" ]; then
      die "Failed to dupe VPD. Please check target firmware image."
    else
      alert "Warning: corrupted VPD in current firmware - reset."
    fi
  fi

  # Check if VPD is valid. Reset if required.
  debug_msg "Checking VPD partitions..."
  for vpd_name in $vpd_list; do
    if ! silent_invoke vpd -i "$vpd_name" -f "$output"; then
      alert "Warning: corrupted VPD ($vpd_name) - Reset."
      silent_invoke vpd -O -i "$vpd_name" -f "$output"
    fi
  done

  debug_msg "crosfw_dupe_vpd: $output updated."
}

# Utility to return if main firmware write protection is enabled.
is_mainfw_write_protected() {
  if [ -n "$FLAGS_wp" ]; then
    verbose_msg "Warning: MAIN WP state overridden as ${FLAGS_wp}"
    "${FLAGS_wp}"
  else
    cros_is_hardware_write_protected &&
      cros_is_software_write_protected "$TARGET_OPT_MAIN"
  fi
}

# Utility to return if EC firmware write protection is enabled.
is_ecfw_write_protected() {
  if [ -n "$FLAGS_wp" ]; then
    verbose_msg "Warning: EC WP state overridden as ${FLAGS_wp}"
    "${FLAGS_wp}"
  else
    cros_is_hardware_write_protected &&
      cros_is_software_write_protected "$TARGET_OPT_EC"
  fi
}

# Utility to return if PD firmware write protection is enabled.
is_pdfw_write_protected() {
  if [ -n "$FLAGS_wp" ]; then
    verbose_msg "Warning: PD WP state overridden as ${FLAGS_wp}"
    "${FLAGS_wp}"
  else
    cros_is_hardware_write_protected &&
      cros_is_software_write_protected "$TARGET_OPT_PD"
  fi
}

crosfw_dup2_mainfw() {
  # Syntax: crosfw_dup2_mainfw SLOT_FROM SLOT_TO
  #    Duplicates a slot to another place on system live firmware
  local slot_from="$1" slot_to="$2"
  # Writing in flashrom, even if there's nothing to write, is still much slower
  # than reading because it needs to read + verify whole image. So we should
  # only write firmware on if there's something changed.
  local temp_from="_dup2_temp_from"
  local temp_to="_dup2_temp_to"
  local temp_image="_dup2_temp_image"
  debug_msg "invoking: crosfw_dup2_mainfw($@)"
  local opt_read_slots="-i $slot_from:$temp_from -i $slot_to:$temp_to"
  invoke flashrom ${TARGET_OPT_MAIN} ${opt_read_slots} -r "${temp_image}"
  if [ -s "$temp_from" ] && cros_compare_file "$temp_from" "$temp_to"; then
    debug_msg "crosfw_dup2_mainfw: slot content is the same, no need to copy."
  else
    slot="$slot_to:$temp_from"
    invoke flashrom ${TARGET_OPT_MAIN} ${WRITE_OPT} -w "${temp_image}" \
      -i "${slot}"
  fi
}

# Compares two slots from current and target folder.
crosfw_is_equal_slot() {
  check_param "crosfw_is_equal_slot(type, slot, ...)" "$@"
  local type_name="$1" slot_name="$2" slot2_name="$3"
  [ "$#" -lt 4 ] || die "crosfw_is_equal_slot: internal error"
  [ -n "$slot2_name" ] || slot2_name="$slot_name"
  local current="$DIR_CURRENT/$type_name/$slot_name"
  local target="$DIR_TARGET/$type_name/$slot2_name"
  cros_compare_file "$current" "$target"
}

# Gets the hash from the current slot
crosfw_slot_hash() {
  check_param "crosfw_slot_hash(type, slot)" "$@"
  local type_name="$1" slot_name="$2"
  [ "$#" -lt 3 ] || die "crosfw_slot_hash: internal error"
  local current="$DIR_CURRENT/$type_name/$slot_name"
  cros_get_file_hash "$current"
}

# ----------------------------------------------------------------------------
# General Updater

# Update Main Firmware (BIOS, AP)
crosfw_update_main() {
  # Syntax: crosfw_update_main SLOT FIRMWARE_SOURCE_TYPE
  #    Write assigned type (normal/developer) of firmware source into
  #    assigned slot (returns directly if the target is already filled
  #    with correct data)
  # Syntax: crosfw_update_main SLOT
  #    Write assigned slot from MAIN_TARGET_IMAGE.
  # Syntax: crosfw_update_main
  #    Write complete MAIN_TARGET_IMAGE

  local slot="$1"
  local source_type="$2"
  debug_msg "invoking: crosfw_update_main($@)"
  # TODO(hungte) verify if slot is valid.
  [ -s "$IMAGE_MAIN" ] || die "missing firmware image: $IMAGE_MAIN"
  if [ "$slot" = "" ]; then
    invoke flashrom ${TARGET_OPT_MAIN} ${WRITE_OPT} -w "${IMAGE_MAIN}"
  elif [ "$source_type" = "" ]; then
    invoke flashrom ${TARGET_OPT_MAIN} ${WRITE_OPT} -w "${IMAGE_MAIN}" \
      -i "${slot}"
  else
    local section_file="$DIR_TARGET/$TYPE_MAIN/$source_type"
    [ -s "$section_file" ] || die "crosfw_update_main: missing $section_file"
    slot="$slot:$section_file"
    invoke flashrom ${TARGET_OPT_MAIN} ${WRITE_OPT} -w "${IMAGE_MAIN}" \
      -i "${slot}"
  fi
}

# Update Embedded Controller Firmware
crosfw_update_ec() {
  local slot="$1"
  debug_msg "invoking: crosfw_update_ec($@)"
  # Syntax: crosfw_update_ec SLOT
  #    Update assigned slot with proper firmware.
  # Syntax: crosfw_update_ec
  #    Write complete MAIN_TARGET_IMAGE
  # TODO(hungte) verify if slot is valid.
  [ -s "$IMAGE_EC" ] || die "missing firmware image: $IMAGE_EC"
  [ -z "$CUSTOMIZATION_EC_PRE_UPDATE" ] || "$CUSTOMIZATION_EC_PRE_UPDATE"
  if [ -n "$slot" ]; then
    invoke flashrom ${TARGET_OPT_EC} ${WRITE_OPT} -w "${IMAGE_EC}" -i "${slot}"
  else
    invoke flashrom ${TARGET_OPT_EC} ${WRITE_OPT} -w "${IMAGE_EC}"
  fi
  [ -z "$CUSTOMIZATION_EC_POST_UPDATE" ] || "$CUSTOMIZATION_EC_POST_UPDATE"
}

# Update Embedded Controller PD Firmware
crosfw_update_pd() {
  local slot="$1"
  debug_msg "invoking: crosfw_update_pd($@)"
  # Syntax: crosfw_update_pd SLOT
  #    Update assigned slot with proper firmware.
  # Syntax: crosfw_update_pd
  #    Write complete MAIN_TARGET_IMAGE
  [ -s "$IMAGE_PD" ] || die "missing firmware image: $IMAGE_PD"
  [ -z "$CUSTOMIZATION_PD_PRE_UPDATE" ] || "$CUSTOMIZATION_PD_PRE_UPDATE"
  if [ -n "$slot" ]; then
    invoke flashrom ${TARGET_OPT_PD} ${WRITE_OPT} -w "${IMAGE_PD}" -i "${slot}"
  else
    invoke flashrom ${TARGET_OPT_PD} ${WRITE_OPT} -w "${IMAGE_PD}"
  fi
  [ -z "$CUSTOMIZATION_PD_POST_UPDATE" ] || "$CUSTOMIZATION_PD_POST_UPDATE"
}

# Note following "preserve" utilities will change $IMAGE_MAIN so any processing
# to the file (ex, prepare_main_image) must be invoked AFTER this call.
crosfw_preserve_hwid() {
  [ -s "$IMAGE_MAIN" ] || die "preserve_hwid: no main firmware."
  silent_invoke futility gbb -s --hwid="$HWID" "$IMAGE_MAIN"
}

crosfw_preserve_vpd() {
  crosfw_dupe_vpd "RO_VPD RW_VPD" "$IMAGE_MAIN" ""
}

crosfw_preserve_bmpfv() {
  if [ -z "$HWID" ]; then
    debug_msg "crosfw_preserve_bmpfv: Running non-ChromeOS firmware. Skip."
    return
  fi
  debug_msg "Preseving main firmware bitmap volume data..."
  [ -s "$IMAGE_MAIN" ] || die "crosfw_preserve_bmpfv: no main firmware."
  # Preserves V1, V2 bitmap volumes.
  silent_invoke flashrom $TARGET_OPT_MAIN -i GBB:_gbb.bin -r _temp.rom
  silent_invoke futility gbb -g --bmpfv=_bmpfv.bin _gbb.bin
  [ -s "_bmpfv.bin" ] || die "crosfw_preserve_bmpfv: invalid bmpfv"
  silent_invoke futility gbb -s --bmpfv=_bmpfv.bin "$IMAGE_MAIN"
}

crosfw_preserve_gbb() {
  if [ -z "$HWID" ]; then
    debug_msg "crosfw_preserve_gbb: Running non-ChromeOS firmware. Skip."
    return
  fi
  debug_msg "Preseving main firmware GBB data..."
  [ -s "$IMAGE_MAIN" ] || die "crosfw_preserve_gbb: no main firmware."
  silent_invoke flashrom $TARGET_OPT_MAIN -i GBB:_gbb.bin -r _temp.rom

  # Preseves flags (--flags output format: "flags: 0x0000001")
  local flags="$(futility gbb -g --flags _gbb.bin 2>/dev/null |
                 sed -nr 's/^flags: ([x0-9]+)/\1/p')"
  debug_msg "Current firmware flags: $flags"
  if [ -n "$flags" ]; then
    silent_invoke futility gbb -s --flags=$((flags)) "$IMAGE_MAIN"
  fi
}

crosfw_preserve_nvram() {
  if [ -z "$HWID" ]; then
    debug_msg "crosfw_preserve_nvram: Running non-ChromeOS firmware. Skip."
    return
  fi
  [ -s "$IMAGE_MAIN" ] || die "crosfw_preserve_nvram: no main firmware."
  local offset=$(futility dump_fmap -p "$IMAGE_MAIN" RW_NVRAM | cut -d' ' -f2)
  local size=$(futility dump_fmap -p "$IMAGE_MAIN" RW_NVRAM | cut -d' ' -f3)
  if [ -z "$size" ]; then
    debug_msg "crosfw_preserve_nvram: System doesn't have flash NVRAM. Skip."
    return
  fi

  debug_msg "Preseving main firmware NVRAM data..."
  silent_invoke flashrom $TARGET_OPT_MAIN -r -i RW_NVRAM:_nvram.rom
  if [ "$size" != $(cros_get_file_size _nvram.rom) ]; then
    verbose_msg "RW_NVRAM size changed, cannot preserve old state!"
    return
  fi
  silent_invoke dd seek=$offset count=$size bs=1 conv=notrunc \
     if=_nvram.rom of="$IMAGE_MAIN"
}

# ----------------------------------------------------------------------------
# ChromeOS Specific Utilities

# Retrieves MD5 hash of a given file.
cros_get_file_hash() {
  md5sum -b "$1" 2>/dev/null | sed 's/ .*//'
}

# Compares two files on Chrome OS (there may be no cmp/diff)
cros_compare_file() {
  local hash1="$(cros_get_file_hash "$1")"
  local hash2="$(cros_get_file_hash "$2")"
  debug_msg "cros_compare_file($1, $2): $hash1, $hash2"
  [ -n "$hash1" ] && [ "$hash1" = "$hash2" ]
}

# Compares two version string on Chrome OS
cros_compare_version() {
  local base="$1"
  local target="$2"

  # Return directly if exact match.
  if [ "$base" = "$target" ]; then
    echo "0"
    return
  fi

  # Now, compare each token by magic "sort -V" (--version-sort).
  local prior="$( (echo "$base"; echo "$target") | sort -V | head -n 1)"
  if [ "$prior" = "$base" ]; then
    echo "-1"
  else
    echo "1"
  fi
}

# Shortcut to compare version compatibility.
# Ex: cros_version_greater_than "$mp_fwid" "$RO_FWID" && die "Need update"
cros_version_greater_than() {
  [ "$(cros_compare_version "$1" "$2")" = "1" ]
}

# Gets file size.
cros_get_file_size() {
  [ -e "$1" ] || die "cros_get_file_size: invalid file: $1"
  stat -c "%s" "$1" 2>/dev/null
}

# Gets a Chrome OS system property (must exist).
cros_get_prop() {
  crossystem "$@" || die "cannot get crossystem property: $@"
}

# Sets a Chrome OS system property.
cros_set_prop() {
  if [ "${FLAGS_dry_run}" = "${FLAGS_TRUE}" ]; then
    alert "dry_run: cros_set_prop $@"
    return ${FLAGS_TRUE}
  fi
  crossystem "$@" || die "cannot SET crossystem property: $@"
}

# Queries a Chrome OS system property, return error if not available.
cros_query_prop() {
  crossystem "$@" || debug_msg "FAILURE: crossystem $@"
}

# Sets the "firmare B tries" counter
cros_set_fwb_tries() {
  cros_set_prop fwb_tries="$1"
}

cros_get_fwb_tries() {
  cros_query_prop fwb_tries
}

# Reboots the system immediately
cros_reboot() {
  verbose_msg "Rebooting system..."
  if [ "${FLAGS_dry_run}" = "${FLAGS_TRUE}" ]; then
    alert "dry_run: reboot"
    return ${FLAGS_TRUE}
  elif [ "${FLAGS_allow_reboot}" = "${FLAGS_FALSE}" ]; then
    alert "Rebooting from updater is disabled."
    return ${FLAGS_TRUE}
  fi
  sync
  # For systems with low speed disk storage, more few seconds for hardware to
  # complete I/O.
  sleep 1
  reboot
  # 'reboot' command terminates immediately, so we must block here to prevent
  # further execution.
  sleep 1d
}

# Returns if the hardware write-protection switch is enabled.
cros_is_hardware_write_protected() {
  local ret=""
  # In current design, hardware write protection is one single switch for all
  # targets (BIOS & EC). On some platforms, wpsw_cur is not availble and only
  # wpsw_boot can be trusted. NOTE: if wpsw_* all gives error, we should treat
  # wpsw as "protected", just to be safe.
  case "$(cros_query_prop wpsw_cur)" in
    "0" )
      ret=$FLAGS_FALSE
      ;;
    "1" )
      ret=$FLAGS_TRUE
      ;;
  esac
  [ "$ret" = "" ] && case "$(cros_query_prop wpsw_boot)" in
    "0" )
      alert "Warning: wpsw_cur is not availble, using wpsw_boot (0)"
      ret=$FLAGS_FALSE
      ;;
    "1" )
      alert "Warning: wpsw_cur is not availble, using wpsw_boot (1)"
      ret=$FLAGS_TRUE
      ;;
    * )
      # All wp* failed.
      alert "Warning: wpsw_boot/cur all failed. Assuming HW write protected."
      ret=$FLAGS_TRUE
      ;;
  esac
  return $ret
}

# Returns if the software write-protection register is enabled.
cros_is_software_write_protected() {
  local opt="$1"
  # Do not pipe flashrom stdout to grep to prevent SIGPIPE
  FLASHROM_OUT=$(flashrom $opt --wp-status 2>/dev/null)
  echo "${FLASHROM_OUT}" | grep -q "write protect is enabled"
}

# Reports write protection status
cros_report_wp_status() {
  local test_main="$1" test_ec="$2" test_pd="$3"
  local wp_hw="off" wp_sw_main="off" wp_sw_ec="off" wp_sw_pd="off"
  cros_is_hardware_write_protected && wp_hw="ON"
  local message="Hardware: $wp_hw, Software:"
  if [ "$test_main" = $FLAGS_TRUE ]; then
    cros_is_software_write_protected "$TARGET_OPT_MAIN" && wp_sw_main="ON"
    message="$message Main=$wp_sw_main"
  fi

  # On many 3rd party EC implementations, checking write protection
  # (--wp-status) may hang device for up to 2~3 seconds, so we want to prevent
  # querying WP status in modes that does not touch EC.
  case "$FLAGS_mode" in
    autoupdate )
      test_ec=$FLAGS_FALSE
      test_pd=$FLAGS_FALSE
      ;;
  esac

  if [ "$test_ec" = $FLAGS_TRUE ]; then
    cros_is_software_write_protected "$TARGET_OPT_EC" && wp_sw_ec="ON"
    message="$message EC=$wp_sw_ec"
  fi

  if [ "$test_pd" = $FLAGS_TRUE ]; then
    cros_is_software_write_protected "$TARGET_OPT_PD" && wp_sw_pd="ON"
    message="$message PD=$wp_sw_pd"
  fi
  echo "$message"
}

# Reports the information from given key file.
cros_report_key() {
  local key_file="$1"
  local key_hash="$(futility vbutil_key --unpack "$key_file" |
                    sed -nr 's/^Key sha1sum: *([^ ]*)$/\1/p')"
  local label=""
  case "$key_hash" in
    b11d74edd286c144e1135b49e7f0bc20cf041f10 )
      label="DEV-signed rootkey"
      ;;
    "" )
      label="Unknown (failed to unpack key)"
      ;;
    * )
      ;;
  esac
  echo "$key_hash $label"
}

# Checks if the root keys (from Google Binary Block) are the same.
cros_check_same_root_keys() {
  check_param "cros_check_same_root_keys(current, target)" "$@"
  local keyfile1="_gk1"
  local keyfile2="_gk2"
  local keyfile1_strip="${keyfile1}_strip"
  local keyfile2_strip="${keyfile2}_strip"
  local ret=${FLAGS_TRUE}

  # current(1) may not contain root key, but target(2) MUST have a root key
  if silent_invoke futility gbb -g --rootkey="$keyfile1" "$1"; then
    silent_invoke futility gbb -g --rootkey="$keyfile2" "$2" ||
      die "Cannot find ChromeOS GBB RootKey in $2."
    # to workaround key paddings...
    cat $keyfile1 | sed 's/\xff*$//g; s/\x00*$//g;' >$keyfile1_strip
    cat $keyfile2 | sed 's/\xff*$//g; s/\x00*$//g;' >$keyfile2_strip
    if ! cros_compare_file "$keyfile1_strip" "$keyfile2_strip"; then
      ret=$FLAGS_FALSE
      alert "Current key: $(cros_report_key "$keyfile1")"
      alert "Target  key: $(cros_report_key "$keyfile2")"
    fi
  else
    debug_msg "warning: cannot get rootkey from $1"
    ret=$FLAGS_ERROR
  fi
  return $ret
}

# Gets the vbutil_firmware information of a RW firmware image.
cros_get_rw_firmware_info() {
  check_param "cros_get_rw_firmware_info(vblock, fw_main, image)" "$@"
  local vblock="$1"
  local fw_main="$2"
  local image="$3"

  local rootkey="_rootkey"
  silent_invoke futility gbb -g --rootkey="$rootkey" "$image" ||
    return

  local fw_info
  fw_info="$(futility vbutil_firmware \
               --verify "$vblock" \
               --signpubkey "$rootkey" \
               --fv "$fw_main")" || fw_info=""
  echo "$fw_info"
}

# Checks if the firmare key and version are allowed by TPM.
cros_check_tpm_key_version() {
  check_param "cros_check_tpm_key_version(fw_info)" "$@"
  local fw_info="$1"

  local tpm_fwver="$(cros_query_prop tpm_fwver)"
  if [ -z "$tpm_fwver" ]; then
    die "Failed to retrieve TPM information."
  fi
  tpm_fwver="$((tpm_fwver))"
  debug_msg "tpm_fwver: $tpm_fwver"

  local data_key_version="$(
    echo "$fw_info" | sed -n '/^ *Data key version:/s/.*:[ \t]*//p')"
  debug_msg "data_key_version: $data_key_version"
  local firmware_version="$(
    echo "$fw_info" | sed -n '/^ *Firmware version:/s/.*:[ \t]*//p')"
  debug_msg "firmware_version: $firmware_version"
  if [ -z "$data_key_version" ] || [ -z "$firmware_version" ]; then
    die "Cannot verify firmware key version from target image."
  fi

  local fw_key_version="$((
    (data_key_version << 16) | (firmware_version & 0xFFFF) ))"
  debug_msg "fw_key_version: $fw_key_version"

  if [ "$tpm_fwver" -gt "$fw_key_version" ]; then
    fw_key_version="$(printf "0x%x" $fw_key_version)"
    tpm_fwver="$(printf "0x%x" $tpm_fwver)"
    alert "Firmware key ($fw_key_version) will be rejected by TPM ($tpm_fwver)."
    return $FLAGS_FALSE
  fi
  return $FLAGS_TRUE
}

# Gets the flag of firmware preamble data.
cros_get_firmware_preamble_flags() {
  check_param "cros_get_firmware_preamble_flags(fw_info)" "$@"
  local fw_info="$1"

  local preamble_flags="$(
    echo "$fw_info" | sed -n '/^ *Preamble flags:/s/.*:[ \t]*//p')"
  debug_msg "preamble_flags: $preamble_flags"
  echo "$preamble_flags"
}

# Returns if firmware was boot with VBSD_LF_USE_RO_NORMAL flag.
cros_is_ro_normal_boot() {
  local VBSD_LF_USE_RO_NORMAL=0x08
  local vdat_flags="$(cros_get_prop vdat_flags 2>/dev/null)"
  [ "$((vdat_flags & VBSD_LF_USE_RO_NORMAL))" -gt "0" ]
}

# Clears ChromeOS related NVData (usually stored on NVRAM/CMOS, storing firmware
# related settings and cookies).
cros_clear_nvdata() {
  mosys nvram clear >/dev/null 2>&1 ||
    debug_msg " - (NVData not cleared)."
}

# Adds path to PATH if given tool is available (and supports empty param).
cros_add_tool_path() {
  local path="$1"
  local tool="$2"
  if [ -x "$path/$tool" ] && "$path/$tool" >/dev/null 2>&1; then
    debug_msg "$path/$tool works fine."
    PATH="$path:$PATH"; export PATH
    return $FLAGS_TRUE
  fi
  return $FLAGS_FALSE
}

# Configures PATH to use the bundled executable binaries.
cros_setup_path() {
  local base="$(readlink -f "$SCRIPT_BASE")"
  if cros_add_tool_path "$base/bin" "crossystem"; then
    debug_msg "Using programs in $base/bin."
    return
  fi

  debug_msg "Using programs in system."
}

# Reset lock file variable.
LOCK_FILE=""

cros_acquire_lock() {
  LOCK_FILE="${1:-/tmp/chromeos-firmwareupdate-running}"
  debug_msg "cros_acquire_lock: Set lock file to $LOCK_FILE."
  # TODO(hungte) Use flock to help locking in better way.
  if [ -r "$LOCK_FILE" ]; then
    local pid="$(cat "$LOCK_FILE")"
    if [ -z "$pid" ]; then
      # For legacy updaters or corrupted systems, PID is empty.
      die "Firmware Updater already running ($LOCK_FILE) with unknown process."
    else
      ps "$pid" >/dev/null 2>&1 &&
        die "
          Firmware Updater already running ($LOCK_FILE).
          Please wait for process $pid to finish and retry later."
    fi
    alert "Warning: Removing expired session lock: $LOCK_FILE ($pid)"
  fi
  echo "$PPID" >"$LOCK_FILE"
  # Clean up on regular or error exits.
  trap cros_release_lock EXIT
}

cros_release_lock() {
  if [ -n "$LOCK_FILE" ]; then
    rm -f "$LOCK_FILE" || alert "Warning: failed to release $LOCK_FILE."
  fi
}

# Utility function to override RW firmware image by version.
# Example: cros_override_rw_firmware_by_version "$RO_FWID" "$TARGET_FWID"
cros_override_rw_firmware_by_version() {
  local current_id="$1"
  local min_ro_id="$2"
  local is_factory=""

  # RW update only happens in autoupdate and recovery (without WP) modes.
  case "$FLAGS_mode" in
    factory_install )
      return
      ;;
    recovery )
      # For recovery mode, if WP is not enabled, we will work in
      # factory/incompatible update mode.
      is_mainfw_write_protected || return ${FLAGS_TRUE}
      ;;
    autoupdate )
      true
      ;;
    * )
      debug_msg "Warning: unknown mode for RW version test: $FLAGS_mode"
      return
  esac

  debug_msg "cros_override_rw_firmware_by_version($current_id, $min_ro_id)"
  if cros_version_greater_than "$min_ro_id" "$current_id"; then
    is_mainfw_write_protected || alert "
      You are updating a device with deprecated RO firmware $current_id.
      For safety concern, we don't push RO updates by default.
      If you are willing to take the RISK OF BRICKING DEVICE, you can manually
      invoke firmware updater (and make sure write protection is disabled) as:

       chromeos-firmwareupdate --mode=recovery --wp=0
      "
    [ -s "$IMAGE_MAIN_RW" ] && cp -f "$IMAGE_MAIN_RW" "$IMAGE_MAIN" &&
      crosfw_preserve_hwid &&
      alert "Updating with RW firmware (slower boot time)." ||
      die "Failed to load RW firmware. Can't update."
  fi
}

cros_check_compatible_platform() {
  local image="$1"
  local platform="$2"

  # Always pass if image (target) does not specify platform.
  if [ -z "$1" ]; then
    return 0
  fi

  # White list or modify according to known exceptions.
  # TODO(hungte) Move this to per-board customization. For now it's executed
  # before customization is loaded.
  case "${image}" in
    Google_Snow )
      platform="${platform%_Rev[0-9]}"
      ;;
  esac

  debug_msg "cros_check_compatible_platform image=${image} platform=${platform}"
  # Return the test result.
  [ "${image}" = "${platform}" ]
}

# Set up variables which control the firmware updater's operation.
# This is called when the firmware update package has been unpacked.
cros_set_unibuild_vars() {
  local model="$1"

  MODEL_DIR="models/${model}"

  local fname="${MODEL_DIR}/setvars.sh"
  [ -e "${fname}" ] || die "Cannot find model script ${fname}"

  . "./${fname}"
  verbose_msg "Model is ${model}"
}

# Parse command line
FLAGS "$@" || exit 1
eval set -- "$FLAGS_ARGV"

# Use bundled tools with highest priority, to prevent dependency when updating
cros_setup_path

# This setup must occur before anything else since the unibuild
# variables need to be set before the rest of the initialization.
if [ -n "${UNIBUILD}" ]; then
  model="${FLAGS_model}"
  if [ "${FLAGS_mode}" = "output" ]; then
    [ -n "${model}" ] || die \
      "Model (--model) must be specified with --mode=output"
    [ -n "${FLAGS_output_dir}" ] || die \
      "Output directory (--output_dir) must be specified with --mode=output"
  fi

  if [ -z "${model}" ]; then
    model="$(mosys platform model)"
    [ -n "${model}" ] || die "Cannot get model from mosys."
  fi
  # Image and key files were set in the shared crosfw.sh file. Set them again
  # here, since we want to use the files specific to the chosen model.
  cros_set_unibuild_vars "${model}"
fi


# ----------------------------------------------------------------------------
# Customization Section

# Customization script file name - do not change this.
# You have to create a file with this name to put your customization.
CUSTOMIZATION_SCRIPT="updater_custom.sh"

# Customization script main entry - do not change this.
# You have to define a function with this name to run your customization.
CUSTOMIZATION_MAIN="updater_custom_main"

# ----------------------------------------------------------------------------
# Constants

# Slot names defined by ChromeOS Firmware Specification
SLOT_A="RW_SECTION_A"
SLOT_B="RW_SECTION_B"
SLOT_RO="RO_SECTION"
SLOT_RW_SHARED="RW_SHARED"
SLOT_LEGACY="RW_LEGACY"
SLOT_EC_RO="EC_RO"
SLOT_EC_RW="EC_RW"
SLOT_PD_RO="EC_RO"
SLOT_PD_RW="EC_RW"

# Main = Application (AP) or SoC firmware, sometimes considered as "BIOS"
TYPE_MAIN="main"
# EC = Embedded Controller
TYPE_EC="ec"
# PD = Power Delivery
TYPE_PD="pd"

# ----------------------------------------------------------------------------
# Global Variables

# Current system identifiers (may be empty if running on non-ChromeOS systems)
HWID="$(crossystem hwid 2>/dev/null)" || HWID=""

# Compare following values with TARGET_*
# (should be passed by wrapper as environment variables)
FWID="$(crossystem fwid 2>/dev/null)" || FWID=""
RO_FWID="$(crossystem ro_fwid 2>/dev/null)" || RO_FWID=""
PLATFORM="${FWID%%.*}"

# Determine EC version
SYSFS_CROS_EC="/sys/class/chromeos/cros_ec/version"
if [ -e "${SYSFS_CROS_EC}" ]; then
  ECID="$(sed -n 's/^RW version: *//p' ${SYSFS_CROS_EC})"
else
  ECINFO="$(mosys -k ec info 2>/dev/null)" || ECINFO=""
  ECID="$(eval "$ECINFO"; echo "$fw_version")"
fi

# Determine PD version
SYSFS_CROS_PD="/sys/class/chromeos/cros_pd/version"
if [ -e "${SYSFS_CROS_PD}" ]; then
  PDID="$(sed -n 's/^RW version: *//p' ${SYSFS_CROS_PD})"
else
  PDINFO="$(mosys -k pd info 2>/dev/null)" || PDINFO=""
  PDID="$(eval "$PDINFO"; echo "$fw_version")"
fi

# ----------------------------------------------------------------------------
# Helper functions

# Verifies if current system is installed with compatible rootkeys
check_compatible_keys() {
  local current_image="$DIR_CURRENT/$IMAGE_MAIN"
  local target_image="$DIR_TARGET/$IMAGE_MAIN"
  if [ "${FLAGS_check_keys}" = ${FLAGS_FALSE} ] ||
     [ "${FLAGS_force}" = "${FLAGS_TRUE}" ]; then
    debug_msg "check_compatible_keys: ignored."
    return $FLAGS_TRUE
  fi
  if ! cros_check_same_root_keys "$current_image" "$target_image"; then
    alert_incompatible_rootkey
    die "Incompatible Rootkey."
  fi

  # Get RW firmware information
  local fw_info
  fw_info="$(cros_get_rw_firmware_info "$DIR_TARGET/$TYPE_MAIN/VBLOCK_A" \
                                       "$DIR_TARGET/$TYPE_MAIN/FW_MAIN_A" \
                                       "$target_image")" || fw_info=""
  [ -n "$fw_info" ] || die "Failed to get RW firmware information"

  # Check TPM
  if ! cros_check_tpm_key_version "$fw_info"; then
    alert_incompatible_tpmkey
    die "Incompatible TPM Key."
  fi

  # Warn for RO-normal updates
  local flag_ro_normal_boot=1
  local current_flags="$(cros_get_firmware_preamble_flags "$fw_info")"
  if [ "$((current_flags & flag_ro_normal_boot))" = "$flag_ro_normal_boot" ]
  then
    alert "
    WARNING: FIRMWARE IMAGE TO BE UPDATED IS SIGNED WITH 'RO-NORMAL' FLAG.
    THIS IS A KEY-BLOCK-ONLY UPDATE WITHOUT FIRMWARE CODE CHANGE.
    YOUR FWID (ACTIVE FIRMWARE ID) WON'T CHANGE AFTER APPLYING THIS UPDATE.
    "
  fi
}

need_update_legacy() {
  debug_msg "Checking whether legacy update is needed..."
  if [ "${FLAGS_force}" = ${FLAGS_FALSE} ] && \
     crosfw_is_equal_slot "${TYPE_MAIN}" "${SLOT_LEGACY}"; then
    debug_msg "${SLOT_LEGACY} not changed."
    return "${FLAGS_FALSE}"
  fi

  local sentinel_name='cros_allow_auto_update'
  local current_file=".legacy.current"
  local target_file=".legacy.target"
  rm -f "${current_file}" "${target_file}"

  # cbfstool may not exist on certain old systems. Try our best shot.
  cbfstool "${DIR_CURRENT}/${TYPE_MAIN}/${SLOT_LEGACY}" extract \
    -n "${sentinel_name}" -f "${current_file}" >/dev/null 2>&1 || true
  cbfstool "${DIR_TARGET}/${TYPE_MAIN}/${SLOT_LEGACY}" extract \
    -n "${sentinel_name}" -f "${target_file}" >/dev/null 2>&1 || true

  # We should update only if both target and current CBFS allows update.
  if ! [ -f "${target_file}" ]; then
    debug_msg "cbfstool failure or target not for auto update. Ignore."
    return "${FLAGS_FALSE}"
  fi
  if ! [ -f "${current_file}" ]; then
    debug_msg "cbfstool failure or old/custom legacy firmware. Ignore update."
    return "${FLAGS_FALSE}"
  fi
  return "${FLAGS_TRUE}"
}

check_and_update_legacy() {
  if need_update_legacy; then
    verbose_msg "${SLOT_LEGACY} is changed and will be updated."
    crosfw_update_main "${SLOT_LEGACY}"
  fi
}

need_update_main_ro() {
  debug_msg "Checking need_update_main_ro"
  if [ "${TARGET_RO_FWID}" != "${RO_FWID}" ]; then
    debug_msg "TARGET_RO_FWID != RO_FWID."
    return ${FLAGS_TRUE}
  fi

  prepare_main_image
  prepare_main_current_image

  cp -f "${DIR_CURRENT}/${TYPE_MAIN}/${SLOT_RO}" .ro_from
  cp -f "${DIR_TARGET}/${TYPE_MAIN}/${SLOT_RO}" .ro_to
  cp -f "${DIR_CURRENT}/${TYPE_MAIN}/GBB" .gbb_from
  cp -f "${DIR_TARGET}/${TYPE_MAIN}/GBB" .gbb_to

  # HWID should be already preserved.
  local file
  for file in .ro_from .ro_to .gbb_from .gbb_to; do
    futility gbb -s --flags=0 "${file}" >/dev/null 2>&1
  done

  if ! cros_compare_file .gbb_from .gbb_to; then
    verbose_msg "RO GBB Contents (probably root keys) are different."
    return $FLAGS_TRUE
  fi

  if ! cros_compare_file .ro_from .ro_to; then
    verbose_msg "RO Contents are different."
    return $FLAGS_TRUE
  fi
  return $FLAGS_FALSE
}

need_update_main() {
  if [ "$TARGET_FWID" != "$FWID" ]; then
    return $FLAGS_TRUE
  fi

  prepare_main_image
  prepare_main_current_image

  if is_vboot2; then
    # Slot A and B are not guaranteed to be the same with vboot2, and we do not
    # always boot out of slot A.  This check must first determine what the
    # active slot is and compare that instead of always comparing A to B.
    local mainfw_act="$(cros_get_prop mainfw_act)"
    local vblock=""

    if [ "$mainfw_act" = "A" ]; then
      vblock="VBLOCK_A"
    elif [ "$mainfw_act" = "B" ]; then
      vblock="VBLOCK_B"
    else
      die "autoupdate: unexpected active firmware ($_mainfw_act)..."
    fi

    # Compare VBLOCK from current and target slot (normal firmware).
    if ! crosfw_is_equal_slot "$TYPE_MAIN" "$vblock" "$vblock"; then
      debug_msg "$vblock differs between current and target"
      return $FLAGS_TRUE
    fi
  else
    # Compare VBLOCK from current A slot and target B slot (normal firmware).
    if ! crosfw_is_equal_slot "$TYPE_MAIN" "VBLOCK_A" "VBLOCK_B"; then
      debug_msg "VBLOCK in A and B differ"
      return $FLAGS_TRUE
    fi
  fi
}

prepare_main_image() {
  crosfw_unpack_image "$TYPE_MAIN" "$IMAGE_MAIN" "$TARGET_OPT_MAIN"
}

prepare_main_current_image() {
  # Reading main current image is slow so we do want to cache.
  local cached_file="${DIR_CURRENT}/.cached"
  if [ -e "${cached_file}" ]; then
    debug_msg "prepare_main_current_image: Use existing cache."
  else
    crosfw_unpack_current_image "$TYPE_MAIN" "$IMAGE_MAIN" "$TARGET_OPT_MAIN"
    touch "${cached_file}"
  fi
}

is_write_protection_disabled() {
  if [ "${FLAGS_update_main}" = ${FLAGS_TRUE} ]; then
    is_mainfw_write_protected && return $FLAGS_FALSE || true
  fi

  if [ "${FLAGS_update_ec}" = ${FLAGS_TRUE} ]; then
    is_ecfw_write_protected && return $FLAGS_FALSE || true
  fi

  if [ "${FLAGS_update_pd}" = ${FLAGS_TRUE} ]; then
    is_pdfw_write_protected && return $FLAGS_FALSE || true
  fi

  return $FLAGS_TRUE
}

clear_update_cookies() {
  # Always success because the system may not have crossystem ready yet if we're
  # trying to recover a broken firmware or after transition from legacy firmware
  ( cros_set_fwb_tries 0
    cros_set_prop recovery_request=0 ) >/dev/null 2>&1 ||
      debug_msg "clear_update_cookies: there were some errors, ignored."
}

silent_sh() {
  # Calls given commands and ignores any error (mostly for factory
  # installations, when the firmware is still non-Chrome).
  ( "$@" ) >/dev/null 2>&1 ||
    debug_msg "Failed calling: $@"
}

enable_dev_boot() {
  cros_set_prop dev_boot_usb=1 dev_boot_signed_only=0
}

disable_dev_boot() {
  # The firmware will decide and reset default values of dev_boot_usb and
  # dev_boot_signed_only on reoot when user turned off developer switch (i.e.,
  # normal mode). It's safe to set dev_boot_usb to zero here, but
  # dev_boot_signed_only may expect different default values, so we leave it
  # untouched and let firmware decide.
  cros_set_prop dev_boot_usb=0
}

load_keyset() {
  if ! [ -d "$KEYSET_DIR" ]; then
    debug_msg "No keysets folder."
    return
  fi
  local keyid="" whitelabel_tag=""

  # Allow overriding keys from command line.
  # TODO(hungte) In future if cros_config is available on ALL (including legacy)
  # systems, we can consider again moving all these logic to cros_config.
  if [ -n "${FLAGS_signature_id}" ]; then
    keyid="${FLAGS_signature_id}"
  elif [ -n "${FLAGS_customization_id}" ]; then
    # TODO(hungte) Remove FLAGS_customization_id in future.
    # Match how VPD customization_id is processed below.
    keyid="${FLAGS_customization_id%%-*}"
  else
    # For Unified build, the keyset is use by each model and white-label.
    # For Non-Unified build, keyset is used only for white-label devices.
    # Legacy devices use 'customization_id' (upper cased) while newer devices
    # use 'whitelabel_tag' (lower cased).
    if [ -n "${UNIBUILD}" ]; then
      local model="$(mosys platform model)"
      debug_msg "Unified Build: Default signature=${SIGNATURE_ID}"
      # TODO(hungte) Search some white-label-specific property in cros_config
      # to make sure this is a white label device.
      keyid="${SIGNATURE_ID##sig-id-in-*}"
      if [ -n "${SIGNATURE_ID}" ] && [ -z "${keyid}" ]; then
        debug_msg "Trying to find signature from VPD 'whitelabel_tag'"
        whitelabel_tag="$(vpd -g whitelabel_tag 2>/dev/null || true)"
        if [ -n "${whitelabel_tag}" ]; then
          keyid="${model}-${whitelabel_tag}"
        else
          # This is for RMA, early builds or factory first time installation.
          # TODO(hungte) Only allow this in special condition, for example if
          # write protection is disabled.
          debug_msg "Fallback to model of Unified Build."
          keyid="${model}"
        fi
      fi
    else
      # Non-Unified Build. Find 'whitelabel_tag' or 'customization_id'.
      debug_msg "Trying to find signature from VPD 'whitelabel_tag'"
      whitelabel_tag="$(vpd -g whitelabel_tag 2>/dev/null || true)"
      if [ -n "${whitelabel_tag}" ]; then
        # The signerbot is simply using keyset names (UPPERCASED) in non-unified
        # build so we have to convert whitelabel_tag.
        # In some environments like netboot initramfs, the `tr` is provided by
        # busybox and do not support POSIX names like :lower: and :upper:, so we
        # have to use [a-z] explicitly.
        keyid="$(echo "${whitelabel_tag}"| tr '[a-z]' '[A-Z]')"
      else
        # `customization_id` comes in format LOEM-SERIES and we have to drop
        # [-SERIES] for finding right signed files.
        debug_msg "Trying to find signature from VPD 'customization_id'"
        local customization_id="$(vpd -g customization_id)"
        keyid="${customization_id%%-*}"
      fi
    fi
  fi

  if [ -z "${keyid}" ]; then
    if [ -n "${UNIBUILD}" ]; then
      die "Unibuild: No signature ID in VPD/cmdline. Cannot continue."
    else
      verbose_msg "No customization_id. Use default keys."
    fi
    return
  fi

  debug_msg "Keysets detected, using [$keyid]"
  local rootkey="$KEYSET_DIR/rootkey.$keyid"
  local vblock_a="$KEYSET_DIR/vblock_A.$keyid"
  local vblock_b="$KEYSET_DIR/vblock_B.$keyid"
  if ! [ -s "$rootkey" ]; then
    die "Failed to load keysets for signature [$keyid]."
  fi
  # Override keys
  futility gbb -s --rootkey="$rootkey" "$IMAGE_MAIN" ||
    die "Failed to update rootkey from keyset [$keyid]."
  local size_input="$(cros_get_file_size "$IMAGE_MAIN")"
  local param="dummy:emulate=VARIABLE_SIZE,image=$IMAGE_MAIN,size=$size_input"
  silent_invoke flashrom -p "$param" -w "$IMAGE_MAIN" \
    -i "VBLOCK_A:$vblock_a" -i "VBLOCK_B:$vblock_b" ||
    die "Failed to update VBLOCK from keyset [$keyid]."
  # TODO(hungte) Verify key correctness after building new image files.
  verbose_msg "Firmware keys changed to set [$keyid]."
}

is_vboot2() {
  crossystem 'fw_vboot2?1'
}

# ----------------------------------------------------------------------------
# Core logic in different modes

# For unified builds that support separate root/fw keys per model,
# this option allows creation of a targeted model specific bios/ec image, which
# can then be used to automate the factory imaging process without requiring
# an firmware install specific logic.
mode_output() {
  cp "${IMAGE_MAIN}" "${FLAGS_output_dir}"
  cp "${IMAGE_EC}" "${FLAGS_output_dir}"
  verbose_msg "Firmware images generated to: ${FLAGS_output_dir}"
}

# Update Engine - Received Update
mode_autoupdate() {
  if [ "${FLAGS_update_main}" = "${FLAGS_FALSE}" ]; then
    echo "No main firmware to upload, abort."
    return 1
  fi

  if ! is_mainfw_write_protected && need_update_main_ro; then
    mode_recovery
    return
  fi

  # Quick check if we need to update
  if [ "${FLAGS_force}" = ${FLAGS_TRUE} ] || need_update_main; then
    verbose_msg "Firmware update available: ${TARGET_FWID}. "
  else
    verbose_msg "Latest RW firmware already installed. No need to update."
    check_and_update_legacy
    return
  fi

  local mainfw_act="$(cros_get_prop mainfw_act)"
  local update_slot="${SLOT_B}"
  local prop_name=fwb_tries
  local try_next=

  if is_vboot2; then
    if [ "$mainfw_act" = "A" ]; then
      update_slot="$SLOT_B"
      try_next=B
    elif [ "$mainfw_act" = "B" ]; then
      update_slot="$SLOT_A"
      try_next=A
    else
      die "autoupdate: unexpected active firmware ($_mainfw_act)..."
    fi
    prop_name=fw_try_count
  else  # vboot1
    if [ "$mainfw_act" = "B" ]; then
      # mainfw_act is only updated at system reboot; if two updates (both with
      # firmware updates) are pushed in a row, next update will be executed
      # while mainfw_act is still B. Since we don't use RW BIOS directly when
      # updater is running, it should be safe to update in this case.
      debug_msg "mainfw_act=B, checking if we can still update FW B..."
      prepare_main_current_image
      cros_compare_file "$DIR_CURRENT/$TYPE_MAIN/$SLOT_A" \
                        "$DIR_CURRENT/$TYPE_MAIN/$SLOT_B" &&
        alert "Installing updates while mainfw_act is B (should be safe)." ||
        die_need_reboot "Done (retry update next boot)"
    elif [ "$mainfw_act" != "A" ]; then
      die "autoupdate: unexpected active firmware ($mainfw_act)..."
    fi
  fi

  prepare_main_image
  prepare_main_current_image
  check_compatible_keys
  crosfw_update_main "$update_slot"
  check_and_update_legacy

  if [ -n "$try_next" ]; then
    cros_set_prop "fw_try_next=$try_next"
  fi

  # Try to determine EC software sync by checking $ECID. We can't rely on
  # $TARGET_ECID, $FLAGS_update_ec or $IMAGE_EC because in future there may be
  # no EC binary blobs in updater (integrated inside main firmware image).
  # Note since $ECID uses mosys, devices not using ChromeOS EC may still
  # report an ID (but they should use updater v3 instead).
  if [ -n "$ECID" ] && [ "$ECID" != "$TARGET_ECID" ]; then
    # EC software sync may need extra reboots.
    cros_set_prop "${prop_name}=8"
    verbose_msg "On reboot EC update may occur."
  else
    cros_set_prop "${prop_name}=6"
  fi
}

# Recovery Installer
mode_recovery() {
  local prefix="recovery"
  [ "${FLAGS_mode}" = "recovery" ] || prefix="${FLAGS_mode}(recovery)"
  if [ "${FLAGS_update_main}" = ${FLAGS_TRUE} ]; then
    if ! is_mainfw_write_protected; then
      # TODO(hungte) check and alert if the original mode is autoupdate, and TPM
      # rollback detected.
      verbose_msg "$prefix: update RO+RW"
      crosfw_preserve_vpd
      crosfw_preserve_gbb
      crosfw_preserve_nvram
      crosfw_update_main
    else
      # TODO(hungte) check if FMAP is not changed
      verbose_msg "$prefix: update main/RW:A,B,SHARED,LEGACY"
      prepare_main_image
      prepare_main_current_image
      check_compatible_keys
      crosfw_update_main "$SLOT_A"
      crosfw_update_main "$SLOT_B"
      crosfw_update_main "$SLOT_RW_SHARED"
      # Some old platforms like Daisy may not have SLOT_LEGACY.
      if [ -f "${DIR_TARGET}/${TYPE_MAIN}/${SLOT_LEGACY}" ]; then
        crosfw_update_main "$SLOT_LEGACY"
      fi
    fi
  fi

  if [ "${FLAGS_update_ec}" = ${FLAGS_TRUE} ]; then
    if ! is_ecfw_write_protected; then
      verbose_msg "$prefix: update ec/RO+RW"
      crosfw_update_ec
    fi
    # If EC is write-protected, software sync will lock RW EC after kernel
    # starts (left firmware boot stage). We can't "recovery" EC RW in this case.
    # Ref: crosbug.com/p/16087.
    verbose_msg "$prefix: EC may be restored or updated in next boot."
  fi

  if [ "${FLAGS_update_pd}" = ${FLAGS_TRUE} ]; then
    if ! is_pdfw_write_protected; then
      verbose_msg "$prefix: update pd/RO+RW"
      crosfw_update_pd
    fi
    verbose_msg "$prefix: PD may be restored or updated in next boot."
  fi

  clear_update_cookies
}

# Factory Installer
mode_factory_install() {
  # Everything executed here must assume the system may be not using ChromeOS
  # firmware.
  is_write_protection_disabled ||
    die_need_ro_update "You need to first disable hardware write protection."

  if [ "${FLAGS_update_main}" = ${FLAGS_TRUE} ]; then
    # We may preserve bitmap here, just like recovery mode. However if there's
    # some issue (or incompatible stuff) found in bitmap, we will need a method
    # to update the bitmaps.
    crosfw_preserve_vpd || verbose_msg "Warning: cannot preserve VPD."
    crosfw_update_main
  fi
  if [ "${FLAGS_update_ec}" = ${FLAGS_TRUE} ]; then
    crosfw_update_ec
  fi
  if [ "${FLAGS_update_pd}" = ${FLAGS_TRUE} ]; then
    crosfw_update_pd
  fi
  cros_clear_nvdata
  silent_sh enable_dev_boot
  clear_update_cookies
}

# Force update RW_LEGACY
mode_legacy() {
  echo "Updating ${SLOT_LEGACY} firmware..."
  crosfw_update_main "${SLOT_LEGACY}"
}

# ----------------------------------------------------------------------------
# Main Entry

main() {
  cros_acquire_lock
  set_flags

  # factory compatibility
  if [ "${FLAGS_factory}" = ${FLAGS_TRUE} ] ||
     [ "${FLAGS_mode}" = "factory" ]; then
    FLAGS_mode=factory_install
  fi
  local ro_type=""
  cros_is_ro_normal_boot && ro_type="$ro_type[RO_NORMAL]"

  verbose_msg "Starting $TARGET_PLATFORM firmware updater v4 (${FLAGS_mode})..."
  local package_info="$TARGET_FWID"
  if [ "$TARGET_FWID" != "$TARGET_RO_FWID" ]; then
    package_info="RO:$TARGET_RO_FWID RW:$TARGET_FWID"
  fi
  local current_info="RO:$RO_FWID $ro_type, ACT:$FWID"
  if [ -n "${TARGET_ECID}" ]; then
    package_info="$package_info / EC:$TARGET_ECID"
  fi
  if [ -n "$ECID" ]; then
    current_info="$current_info / EC:$ECID"
  fi
  if [ -n "${TARGET_PDID}" ]; then
    package_info="$package_info / PD:$TARGET_PDID"
  fi
  if [ -n "$PDID" ]; then
    current_info="$current_info / PD:$PDID"
  fi
  verbose_msg " - Updater package: [$package_info]"
  verbose_msg " - Current system:  [$current_info]"

  if [ "${FLAGS_update_main}" = ${FLAGS_TRUE} -a -n "${HWID}" ]; then
    # always preserve HWID for current system, if available.
    crosfw_preserve_hwid
    debug_msg "preserved HWID as: $HWID."
  fi

  local wpmsg="$(cros_report_wp_status $FLAGS_update_main \
                 $FLAGS_update_ec $FLAGS_update_pd)"
  verbose_msg " - Write protection: $wpmsg"

  # Check platform except in factory_install mode.
  if [ "${FLAGS_check_platform}" = ${FLAGS_TRUE} ] &&
     [ "${FLAGS_mode}" != "factory_install" ] &&
     [ "${FLAGS_mode}" != "output" ] &&
     ! cros_check_compatible_platform "${TARGET_PLATFORM}" "${PLATFORM}"; then
    alert_unknown_platform "$PLATFORM" "$TARGET_PLATFORM"
    exit 1
  fi

  # Since CL:798773, all additional files are in bin/.
  # TODO(hungte) Remove this hack when we have separated extra programs and data.
  if [ ! -r "${CUSTOMIZATION_SCRIPT}" ] && [ -r "bin/${CUSTOMIZATION_SCRIPT}" ]
  then
    CUSTOMIZATION_SCRIPT="bin/${CUSTOMIZATION_SCRIPT}"
  fi

  # load customization
  if [ -r "$CUSTOMIZATION_SCRIPT" ]; then
    debug_msg "loading customization..."
    . ./$CUSTOMIZATION_SCRIPT
    # invoke customization
    debug_msg "starting customized updater main..."
    $CUSTOMIZATION_MAIN
  fi

  load_keyset

  result="completed"
  case "${FLAGS_mode}" in
    autoupdate | recovery | factory_install | output | legacy)
      mode_"${FLAGS_mode}" || result="failed"
      ;;

    "" )
      die "Please assign updater mode by --mode option."
      ;;

    * )
      die "Unknown mode: ${FLAGS_mode}"
      ;;
  esac
  verbose_msg "Firmware update (${FLAGS_mode}) ${result}."
}

# Exit on error
set -e

# Main Entry
main
