# Chrome OS Firmware Updater Package

This folder contains resources for packaging new Chrome OS firmware updater.

## Format

The package is a ZIP format file with simple UNIX shell stub as SFX
(self-extracting archive) program.

The SFX stub program is [sfx2.sh](./sfx2.sh).

## Contents

-   The package file will always start with the SFX stub program.
-   The [packer](../pack_firmware.py) program will put firmware images and
    extra tools or files into package.
-   The package will contain a signing instruction file `signer_config.csv`
    with at least following columns:
    -   `model_name`: Platform model name.
    -   `firmware_image`: Path to the AP (host) firmware image.
    -   `key_id`: Signing key name, usually same as the model name.
    -   `ec_image`: Path to the Embedded Controller firmware image.
-   For platforms that supports Custom Label (LOEM), the keys for each LOEM
    must be put in `keyset` folder, with a `customlabel_tag` as suffix
    (`${model}-${loem}`):
    -   `rootkey.$CLTAG`: The firmware root key
    -   `vblock_A.$CLTAG`: The vblock in A section
    -   `vblock_B.$CLTAG`: The vblock in A section

## Create initial package

Assume you have all contents ready in `$PATH_TO_DIR`, just do:
```sh
cp -f sfx2.sh chromeos-firmwareupdate
./chromeos-firmwareupdate --repack $PATH_TO_DIR
```

## Update package contents

Run command:
```sh
chromeos-firmwareupdate --repack $PATH_TO_DIR
```

If ZIP programs are available, you may also update directly:
```sh
(cd $PATH_TO_DIR; zip PATH_TO/chromeos-firmwareupdate .)
```

## Extract package contents

Extract to a destination folder:
```sh
chromeos-firmwareupdate --unpack $PATH_TO_DIR
```

The package is using ZIP format and can also be extracted using other tools:
```sh
unzip chromeos-firmwareupdate
```

## Check contents info

For debugging, run command:
```sh
cromeos-firmwareupdate -V
```

For machine friendly parsing, run following command to get JSON output:
```sh
cromeos-firmwareupdate --manifest
```
