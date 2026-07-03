#!/bin/bash
cfg_name=chopper_autotune.cfg

sed -i "/^\[include $cfg_name\]$/d" ~/printer_data/config/printer.cfg
rm -f ~/printer_data/config/$cfg_name
sudo service klipper restart
echo "Uninstalled; datasets in ~/printer_data/config/chopper-autotune are kept"
