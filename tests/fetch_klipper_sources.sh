#!/bin/bash
# Fetch the real Klipper/Kalico G-code parser, config reader, API server, resonance
# tester and accelerometer modules for tests/test_klipper_contract.py. The GPL sources
# are downloaded, never committed; pinned revisions keep the contract reproducible.
set -euo pipefail

dest=${1:-"$(dirname "$0")/.klipper-src"}
klipper_master=ce7002bedf37e938bb483572949f3703ac6476cb
kalico_main=84a4105726e22c5c15943e791b5cdb3beff52e77

fetch() {
    mkdir -p "$(dirname "$2")"
    curl -fsSL --retry 3 -o "$2" "$1"
}

# klippy.py holds Printer.lookup_object: an unknown name is a config error there,
# which the G-code dispatcher turns into a shutdown; webhooks.py answers info
for ref in v0.10.0 v0.11.0 v0.12.0 v0.13.0 "$klipper_master"; do
    for file in gcode.py klippy.py webhooks.py extras/resonance_tester.py; do
        fetch "https://raw.githubusercontent.com/Klipper3d/klipper/$ref/klippy/$file" \
              "$dest/klipper-$ref/$(basename "$file")"
    done
done
# v0.10's config reader is Python 2 code (no strict mode: sections always merge)
for ref in v0.11.0 v0.12.0 v0.13.0 "$klipper_master"; do
    fetch "https://raw.githubusercontent.com/Klipper3d/klipper/$ref/klippy/configfile.py" \
          "$dest/klipper-$ref/configfile.py"
done
# Kalico keeps Printer in printer.py
for file in gcode.py configfile.py printer.py webhooks.py extras/resonance_tester.py; do
    fetch "https://raw.githubusercontent.com/KalicoCrew/kalico/$kalico_main/klippy/$file" \
          "$dest/kalico-$kalico_main/$(basename "$file")"
done
# the accelerometer modules and the sample stream each registers (Kalico has no bmi160)
for file in adxl345.py lis2dw.py lis3dh.py mpu9250.py icm20948.py bmi160.py; do
    fetch "https://raw.githubusercontent.com/Klipper3d/klipper/$klipper_master/klippy/extras/$file" \
          "$dest/klipper-$klipper_master/$file"
done
for file in adxl345.py lis2dw.py lis3dh.py mpu9250.py icm20948.py; do
    fetch "https://raw.githubusercontent.com/KalicoCrew/kalico/$kalico_main/klippy/extras/$file" \
          "$dest/kalico-$kalico_main/$file"
done
echo "Klipper/Kalico sources in $dest"
