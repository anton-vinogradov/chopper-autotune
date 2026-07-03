#!/bin/bash
set -e
repo=chopper-autotune
cfg_name=chopper_autotune.cfg
repo_path=$(dirname "$(realpath "$0")")

if [ "$(id -u)" = "0" ]; then
    echo "Script must run from non-root !!!"
    exit 1
fi

mkdir -p ~/printer_data/config/$repo/datasets

g_shell_path=~/klipper/klippy/extras
g_shell_name=gcode_shell_command.py
if [ -f "$g_shell_path/$g_shell_name" ]; then
    echo "$g_shell_name already exists in $g_shell_path, skipping"
else
    cp "$repo_path/$g_shell_name" "$g_shell_path/"
    echo "Copied $g_shell_name to $g_shell_path"
fi

ln -srf "$repo_path/$cfg_name" ~/printer_data/config/

printer_cfg=~/printer_data/config/printer.cfg
if [ -f "$printer_cfg" ] && ! grep -q "^\[include $cfg_name\]$" "$printer_cfg"; then
    sed -i "1i\[include $cfg_name]" "$printer_cfg"
    echo "Included $cfg_name in printer.cfg"
fi

moonraker_conf=~/printer_data/config/moonraker.conf
if [ -f "$moonraker_conf" ] && ! grep -q "^\[update_manager $repo\]$" "$moonraker_conf"; then
    {
        echo ""
        echo "[update_manager $repo]"
        echo "type: git_repo"
        echo "path: $repo_path"
        echo "origin: https://github.com/anton-vinogradov/$repo.git"
        echo "primary_branch: main"
        echo "managed_services: klipper"
    } >> "$moonraker_conf"
    echo "Added [update_manager $repo] to moonraker.conf"
    sudo service moonraker restart
fi

if command -v apt-get > /dev/null; then
    sudo apt-get update
    sudo apt-get install -y libatlas-base-dev libopenblas-dev
fi

python3 -m venv --system-site-packages "$repo_path/.venv"
"$repo_path/.venv/bin/pip" install -q --upgrade pip setuptools
"$repo_path/.venv/bin/pip" install -e "$repo_path"

sudo service klipper restart
echo "Done. Try: CHOPPER_COLLECT SPEED=55 DRY_RUN=1 from the web console"
