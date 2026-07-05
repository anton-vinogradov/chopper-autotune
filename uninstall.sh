#!/bin/bash
cfg_name=chopper_autotune.cfg
force_cfg_name=chopper_force_move.cfg

sed -i "/^\[include $cfg_name\]$/d; /^\[include $force_cfg_name\]$/d" ~/printer_data/config/printer.cfg
rm -f ~/printer_data/config/$cfg_name ~/printer_data/config/$force_cfg_name

# remove the update_manager registration install.sh added, or Moonraker will
# error on the missing repo path at every start after the directory is deleted
moonraker_conf=~/printer_data/config/moonraker.conf
if [ -f "$moonraker_conf" ] && grep -q "^\[update_manager chopper-autotune\]$" "$moonraker_conf"; then
    sed -i "/^\[update_manager chopper-autotune\]$/,/^\s*$/d" "$moonraker_conf"
    sudo service moonraker restart
fi

# remove the KlipperScreen panel and its menu button if they were installed
ks_conf=~/printer_data/config/KlipperScreen.conf
rm -f ~/KlipperScreen/panels/chopper.py
if [ -f "$ks_conf" ] && grep -q "^\[menu __main more chopper\]$" "$ks_conf"; then
    sed -i "/^\[menu __main more chopper\]$/,/^$/d" "$ks_conf"
    sudo systemctl restart KlipperScreen 2>/dev/null || true
fi

sudo service klipper restart
echo "Uninstalled; datasets in ~/printer_data/config/chopper-autotune are kept."
echo "Left ~/klipper/klippy/extras/gcode_shell_command.py in place (other tools may use it)."
