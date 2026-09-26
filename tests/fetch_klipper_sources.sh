#!/bin/bash
# Fetch the real Klipper/Kalico G-code parser, config reader, API server, resonance
# tester and accelerometer modules, and Beacon's probe module, for
# tests/test_klipper_contract.py: the supported ones, the current Klipper release and
# master, and Kalico. The GPL sources are downloaded, never committed; pinned revisions
# keep the contract reproducible.
set -euo pipefail

dest=${1:-"$(dirname "$0")/.klipper-src"}
klipper_master=ce7002bedf37e938bb483572949f3703ac6476cb
kalico_main=84a4105726e22c5c15943e791b5cdb3beff52e77
beacon_master=3eb0134607664734ccf5cd94cb51840e1c8b27b3

fetch() {
    mkdir -p "$(dirname "$2")"
    curl -fsSL --retry 3 -o "$2" "$1"
}

# klippy.py holds Printer.lookup_object: an unknown name is a config error there,
# which the G-code dispatcher turns into a shutdown; webhooks.py answers info;
# force_move.py tells a supported Klipper (collect.require_current_klipper)
for ref in v0.13.0 "$klipper_master"; do
    for file in gcode.py klippy.py webhooks.py configfile.py extras/resonance_tester.py \
                extras/force_move.py; do
        fetch "https://raw.githubusercontent.com/Klipper3d/klipper/$ref/klippy/$file" \
              "$dest/klipper-$ref/$(basename "$file")"
    done
done
# Kalico keeps Printer in printer.py
for file in gcode.py configfile.py printer.py webhooks.py extras/resonance_tester.py \
            extras/force_move.py; do
    fetch "https://raw.githubusercontent.com/KalicoCrew/kalico/$kalico_main/klippy/$file" \
          "$dest/kalico-$kalico_main/$(basename "$file")"
done
# the accelerometer modules, the sample stream each registers and bulk_sensor.py that
# serves it to API clients (Kalico has no bmi160)
for file in adxl345.py lis2dw.py lis3dh.py mpu9250.py icm20948.py bmi160.py bulk_sensor.py; do
    fetch "https://raw.githubusercontent.com/Klipper3d/klipper/$klipper_master/klippy/extras/$file" \
          "$dest/klipper-$klipper_master/$file"
done
for file in adxl345.py lis2dw.py lis3dh.py mpu9250.py icm20948.py bulk_sensor.py; do
    fetch "https://raw.githubusercontent.com/KalicoCrew/kalico/$kalico_main/klippy/extras/$file" \
          "$dest/kalico-$kalico_main/$file"
done
# the accelerometer built into a Beacon probe: its own stream endpoint and format
fetch "https://raw.githubusercontent.com/beacon3d/beacon_klipper/$beacon_master/beacon.py" \
      "$dest/beacon-$beacon_master/beacon.py"
echo "Klipper/Kalico/Beacon sources in $dest"
