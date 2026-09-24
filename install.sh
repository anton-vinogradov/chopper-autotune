#!/bin/bash
# Install chopper-autotune on the printer host. The Python environment is built and
# checked first; the Klipper/Moonraker config is touched only after that succeeded,
# so a failed install never leaves macros that have nothing to run.
set -e
repo=chopper-autotune
cfg_name=chopper_autotune.cfg
repo_path=$(dirname "$(realpath "$0")")
config_dir=~/printer_data/config
printer_cfg=$config_dir/printer.cfg
venv=$repo_path/.venv

fail() {
    echo "ERROR: $*"
    echo "install.sh stopped: this run did not change your Klipper config."
    exit 1
}

if [ "$(id -u)" = "0" ]; then
    echo "Script must run from non-root !!!"
    exit 1
fi

g_shell_path=~/klipper/klippy/extras
[ -d "$g_shell_path" ] || fail "Klipper not found: $g_shell_path does not exist"

# numpy wheels for 32-bit Raspberry Pi OS (piwheels) link against OpenBLAS, 64-bit
# wheels carry their own. A failed apt step is not fatal: pip may still succeed.
if command -v apt-get > /dev/null; then
    sudo apt-get update || echo "WARNING: apt-get update failed, continuing"
    sudo apt-get install -y python3-venv libopenblas-dev \
        || echo "WARNING: could not install python3-venv libopenblas-dev, continuing"
fi

python3 -m venv --system-site-packages "$venv" \
    || fail "python3 -m venv failed (on Debian or Raspberry Pi OS: sudo apt-get install python3-venv)"
"$venv/bin/pip" install -q --upgrade pip setuptools || fail "pip could not upgrade itself (no network?)"
"$venv/bin/pip" install -e "$repo_path" || fail "pip could not install chopper-autotune (see the error above)"
"$venv/bin/python" -c 'import numpy, plotly, chopper_autotune.cli' \
    || fail "the Python environment in $venv does not work (see the error above)"
[ -x "$venv/bin/chopper-autotune" ] || fail "$venv/bin/chopper-autotune was not created"
echo "Python environment ready in $venv"

mkdir -p "$config_dir/$repo/datasets"

g_shell_name=gcode_shell_command.py
if [ -f "$g_shell_path/$g_shell_name" ]; then
    echo "$g_shell_name already exists in $g_shell_path, skipping"
else
    cp "$repo_path/$g_shell_name" "$g_shell_path/"
    echo "Copied $g_shell_name to $g_shell_path"
fi

ln -srf "$repo_path/$cfg_name" "$config_dir/"

# Klipper rejects duplicate sections: provide [force_move] via a generated local file
# (never by editing the repo's tracked cfg — update_manager needs a clean git tree)
# and only when the user's config does not declare one already.
force_cfg_name=chopper_force_move.cfg
force_cfg=$config_dir/$force_cfg_name
if grep -rq "^\[force_move\]" "$config_dir" --include="*.cfg" --exclude="$force_cfg_name" 2>/dev/null; then
    rm -f "$force_cfg"
    if [ -f "$printer_cfg" ]; then
        sed -i "/^\[include $force_cfg_name\]$/d" "$printer_cfg"
    fi
    echo "[force_move] already present in your config (FORCE_MOVE must stay enabled there)"
else
    printf '[force_move]\nenable_force_move: True\n' > "$force_cfg"
    if [ -f "$printer_cfg" ] && ! grep -q "^\[include $force_cfg_name\]$" "$printer_cfg"; then
        sed -i "1i\[include $force_cfg_name]" "$printer_cfg"
    fi
fi

if [ -f "$printer_cfg" ] && ! grep -q "^\[include $cfg_name\]$" "$printer_cfg"; then
    sed -i "1i\[include $cfg_name]" "$printer_cfg"
    echo "Included $cfg_name in printer.cfg"
fi

moonraker_conf=$config_dir/moonraker.conf
restart_moonraker=
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
    restart_moonraker=1
fi

# KlipperScreen panel (optional): a one-tap app to launch tuning / demo from the touchscreen.
ks_conf=$config_dir/KlipperScreen.conf
if [ -d ~/KlipperScreen/panels ]; then
    ln -srf "$repo_path/klipperscreen/chopper.py" ~/KlipperScreen/panels/chopper.py
    echo "Linked the Chopper panel into KlipperScreen"
    if [ -f "$ks_conf" ] && ! grep -q "^\[menu __main more chopper\]$" "$ks_conf"; then
        # add one button to the "More" submenu, above the auto-generated (#~#) block KlipperScreen owns
        awk 'function emit(){print "[menu __main more chopper]"; print "name: Chopper";
                              print "icon: fine-tune"; print "panel: chopper"; print ""}
             /^#~#/ && !done {emit(); done=1} {print}
             END{if(!done){print ""; emit()}}' "$ks_conf" > "$ks_conf.tmp" && mv "$ks_conf.tmp" "$ks_conf"
        echo "Added the Chopper button to the KlipperScreen More menu"
    fi
    sudo systemctl restart KlipperScreen 2>/dev/null || true
fi

if [ -n "$restart_moonraker" ]; then
    sudo service moonraker restart || echo "WARNING: could not restart moonraker, restart it yourself"
fi
sudo service klipper restart || echo "WARNING: could not restart klipper, restart it yourself"
if [ -f "$printer_cfg" ] && grep -q "^\[include $cfg_name\]$" "$printer_cfg"; then
    echo "Done. Try: CHOPPER_COLLECT SPEED=55 DRY_RUN=1 from the web console"
else
    echo "WARNING: $printer_cfg not found. Add [include $cfg_name] to your main Klipper config"
    echo "yourself (and [include $force_cfg_name] if $force_cfg exists), then restart Klipper."
fi
