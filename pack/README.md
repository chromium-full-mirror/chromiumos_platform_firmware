# Chrome OS Firmware Updater Package

This folder contains resources for packaging new Chrome OS firmware updater.

## Format

The package is using [shar](https://www.gnu.org/software/sharutils/) format file
with UNIX shell stub as SFX (self-extracting archive) program.

The SFX stub program is [sfx.sh](./sfx.sh).

## Contents

-   The package file will always start with the SFX stub program.
-   The [packer](../pack_firmware.py) program will put firmware images and
    extra tools or files into package.
-   For package with single target platform (i.e., no Unified Build),
    all firmware images should be put in top level, named as:
    -   `bios.bin`: AP (host) firmware image
    -   `ec.bin`: Embedded Controller firmware image
    -   `pd.bin`: CrOS PD (running CrOS EC software) firmware image
-   For package with multiple target platforms (i.e., Unified Build),
    each platform will have its definition file `setvars.sh` (see
    [setvars.template](./setvars.template)) in `models/${MODEL}` folder,
    with at least following fields:
    -   `IMAGE_MAIN`: Path to AP (host) firmware image
    -   `IMAGE_EC`: Path to Embedded Controller firmware image
    -   `IMAGE_PD`: Path to CrOS PD firmware image
    -   `SIGNATURE_ID`: Signing key name for this model, just put model name.
-   For platforms that supports White Label (LOEM), the keys for each LOEM
    must be put in `keyset` folder, with a `whitelabel_tag` as suffix
    (which `${model}-${loem}` in Unified Build, otherwise just `${loem}`):
    -   `rootkey.$WLTAG`: The firmware root key
    -   `vblock_A.$WLTAG`: The vblock in A section
    -   `vblock_B.$WLTAG`: The vblock in A section

## Create initial package

Assume you have all contents ready in `$PATH_TO_DIR`, just do:
```sh
cp -f sfx.sh chromeos-firmwareupdate
./chromeos-firmwareupdate --repack $PATH_TO_DIR
```

## Update package contents

Run command:
```sh
chromeos-firmwareupdate --repack $PATH_TO_DIR
```

## Extract package contents

Run command:
```sh
chromeos-firmwareupdate --unpack # extracts to a new temp folder
```

Or add a destination path:
```sh
chromeos-firmwareupdate --unpack $PATH_TO_DIR
```

## Check contents info

Run command:
```sh
cromeos-firmwareupdate -V
```
