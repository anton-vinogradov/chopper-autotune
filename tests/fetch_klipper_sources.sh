#!/bin/bash
# Fetch the real Klipper/Kalico G-code parser and API server for
# tests/test_klipper_contract.py. The GPL sources are downloaded, never committed;
# pinned revisions keep the contract reproducible.
set -euo pipefail

dest=${1:-"$(dirname "$0")/.klipper-src"}
klipper_master=ce7002bedf37e938bb483572949f3703ac6476cb
kalico_main=84a4105726e22c5c15943e791b5cdb3beff52e77

fetch() {
    mkdir -p "$(dirname "$2")"
    curl -fsSL --retry 3 -o "$2" "$1"
}

for ref in v0.10.0 v0.11.0 v0.12.0 v0.13.0 "$klipper_master"; do
    fetch "https://raw.githubusercontent.com/Klipper3d/klipper/$ref/klippy/gcode.py" \
          "$dest/klipper-$ref/gcode.py"
done
fetch "https://raw.githubusercontent.com/Klipper3d/klipper/$klipper_master/klippy/webhooks.py" \
      "$dest/klipper-$klipper_master/webhooks.py"
fetch "https://raw.githubusercontent.com/KalicoCrew/kalico/$kalico_main/klippy/gcode.py" \
      "$dest/kalico-$kalico_main/gcode.py"
echo "Klipper/Kalico sources in $dest"
