#!/bin/bash
cfg_name=chopper_autotune.cfg

sed -i "/^\[include $cfg_name\]$/d" ~/printer_data/config/printer.cfg
rm -f ~/printer_data/config/$cfg_name

# remove the KlipperScreen panel and its menu button if they were installed
ks_conf=~/printer_data/config/KlipperScreen.conf
rm -f ~/KlipperScreen/panels/chopper.py
if [ -f "$ks_conf" ] && grep -q "^\[menu __main chopper\]$" "$ks_conf"; then
    sed -i "/^\[menu __main chopper\]$/,/^$/d" "$ks_conf"
    sudo systemctl restart KlipperScreen 2>/dev/null || true
fi

sudo service klipper restart
echo "Uninstalled; datasets in ~/printer_data/config/chopper-autotune are kept"
