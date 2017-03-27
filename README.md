# ChromeOS Firmware Updater

This repository contains the firmware updater (`chromeos-firmwareupdate`) that
will update firmware images related to verified boot, usually AP (also known as
BIOS or MAIN) and EC.

[TOC]

## Introduction
Auto update is one of the most important feature in Chrome OS. Updating
firmware is one of the most complicated process, since all Chromebooks come
with firmware that implemented [verified
boot](https://www.chromium.org/chromium-os/chromiumos-design-docs/verified-boot)
and must be able to update in background silently.

## Using Firmware Updater

The firmware updater was made as a "shellball", a self-executable file
containing updater logic (shell scripts), utility programs, and firmware images.

### Update manually

Usually you can find the updater in `/usr/sbin/chromeos-firmwareupdate`
on a ChromeOS device (or the rootfs partition of a disk image).

To look at its contents (firmware images and versions):

    chromeos-firmwareupdate -V

Usually for people who wants to "update all my firmware to right states", do:

    chromeos-firmwareupdate --mode=recovery

> The `recovery` mode will try to update RO+RW if your write protection
> is not enabled, otherwise on RW.

If your are not sure about write protection status but you only want RW to be
updated, run:

    chromeos-firmwareupdate --mode=recovery --wp=1

> The `--wp` argument will override you real write protection status.

### Simulating ChromeOS Auto Update

The ChromeOS Auto Update (`update_engine`) runs updater in a different way - a
[two-step trial process](https://www.chromium.org/chromium-os/chromiumos-design-docs/firmware-boot-and-recovery).

If you want to simulate and test that, do:

    chromeos-firmwareupdate --mode=autoupdate --wp=1

## Building Firmware Updater

The updater is provided by the
[`virtual/chromeos-firmware`](https://chromium.googlesource.com/chromiumos/overlays/chromiumos-overlay/+/master/virtual/chromeos-firmware)
package in Chromium OS source tree, which will be replaced and includes the
`chromeos-base/chromeos-firmware-${BOARD}` package in private board overlays.

To build an updater locally, in chroot run:

    emerge-${BOARD} chromeos-firmware-${BOARD}

If your board overlay has defined USE flags `bootimage` or `cros_ec`,
`chromeos-firwmare-${BOARD}` package will add dependency to firmware and EC
source packages (`chromeos-bootimage` and `chromeos-ec`), and have the firmware
images in `/build/${BOARD}/firmware/{image,ec}.bin`. A "local" updater will be
also generated in `/build/${BOARD}/firmware/updater.sh` so you can run it to
test the locally built firmware images.

> In other words, you can remove `bootimage` and `cros_ec` in branches that you
> don't need firmware from source, for example the factory branches or ToT,
> especially if there are external partners who only has access to particular
> board private overlays.

## Manipulating Firmware Updater Packages

The firmware updater packages lives in private board overlays:
`src/private-overlays/overlay-${BOARD}-private/chromeos-base/chromeos-firmware-${BOARD}/chromeos-firmware-${BOARD}-9999.ebuild`.
Find a template here in [chromiumos-base/chromeos-firmware-null](https://chromium.googlesource.com/chromiumos/overlays/chromiumos-overlay/+/master/chromeos-base/chromeos-firmware-null/chromeos-firmware-null-9999.ebuild).

Usually there are few fields you have to fill:

### CROS_FIRMWARE_MAIN_IMAGE
A reference to the Main (AP) firmware image, which usually comes from
`emerge-${BOARD} chromeos-booimage` then `/build/${BOARD}/firmware/image.bin`.

Usually this implies both RO and RW. See `CROS_FIRMWARE_MAIN_RW_IMAGE` below for
more information.

> You have to run `ebuild-${BOARD} chromeos-firmware-${BOARD}.ebuild manifest`
> whenever you've changed the image files (`CROS_FIRMWARE_*_IMAGE`).

### CROS_FIRMWARE_MAIN_RW_IMAGE
A reference to the Main (AP) firmware image and only used for RW sections.

If this value is set, `CROS_FIRMWARE_MAIN_IMAGE` will be used for RO and this
will be used for RW.

### CROS_FIRMWARE_EC_IMAGE
A reference to the Embedded Controller (EC) firmware image, which usually comes
from `emerge-${BOARD} chromeos-ec` then `/build/${BOARD}/firmware/ec.bin`.

## Technical Details

### CROS_FIRMWARE_SCRIPT and packaging
The firmware updater is built by running `pack_firmware.py`, which collects
firmware image and extra files, all files under `pack_dist` folder, archived by
running [`shar`](https://www.gnu.org/software/sharutils/), with a special
bootstrap stub `pack_stub`.

Since the verified boot has been evolved with so much differences, we put the
updating logic in different files according to the generation of firmware:
`pack_dist/updater*.sh`. Most Chromebooks today should use `updater4.sh`.

Usually we will increase a "logic version" when the verified boot has been
changed so much that the updater code for previous versions would almost won't
work. Currently we have defined these versions
(Use [Developer Info](https://www.chromium.org/chromium-os/developer-information-for-chrome-os-devices)
 page to find the mapping from board names to product names):

 - Version 1: (EOL) mario (CR48), using H2C BIOS.
 - Version 2: (EOL) alex and zgb.
 - Version 3: lumpy, stumpy, butterfly, stout, parrot.
 - Version 4: Everything after version 3 until now.
 - Version 5: Was created for vboot2, but now it's merged back to Version 4.

This will be mapped to what you should set in the `CROS_FIRMWARE_SCRIPT` value
in ebuild files.

### Updater logic
Here's a detailed list of how each updater mode works:

![Diagram](_docs:update_diagram.png)

 - `--mode=autoupdate`: Invoked by update_engine when a payload is installed.
   1. Check if WP is enabled.
   2. If WP is enabled, update RW / inactive and exit. After system reboot.
      The `update_engine` will invoke `chromeos-setgoodfirmware` after 60 secs,
      which will update or mark booted RW firmware to active.
   3. If WP is disabled, check if the RO section is same as
      `CROS_FIRMWARE_MAIN_IMAGE`. If yes, go 2. Otherwise, do `--mode=recovery`.

 - `--mode=recovery`: Invoked by recovery shim after installed.
   1. Check if WP is enabled.
   2. If WP is enabled, update both RW/A, RW/B and exit.
   3. If WP is disabled, update whole image except reserved sections (GBB, VPD).
      This includes RO, RW/A, and RW/B.

 - `--mode=factory_install`: Used for factory initial imaging.
   1. Check if WP is enabled, and exit with failure if enabled.
   2. Update whole image except reserved sections (GBB, VPD).
      This includes RO, RW/A, and RW/B.
