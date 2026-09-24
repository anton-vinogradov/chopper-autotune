#!/bin/bash
# CI smoke test of install.sh in a clean Debian container, run as root: installs as a
# normal user against fake Klipper/Moonraker directories. A failed install must
# leave the config untouched; a good one must give a working environment, wired
# config and a clean git tree (Moonraker refuses to update a modified repo).
set -euo pipefail
src=$(realpath "$(dirname "$0")/..")

apt-get update
apt-get install -y --no-install-recommends sudo git python3 ca-certificates
id pi > /dev/null 2>&1 || useradd -m pi
echo 'pi ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/pi
# no init system in a container: the restarts only have to succeed
printf '#!/bin/sh\necho "service $*"\n' > /usr/local/sbin/service
chmod +x /usr/local/sbin/service

home=/home/pi
repo=$home/chopper-autotune
config=$home/printer_data/config
rm -rf "$repo" "$home/klipper" "$home/printer_data"
cp -a "$src" "$repo"
rm -rf "$repo/.venv" "$repo/tests/.klipper-src"
mkdir -p "$home/klipper/klippy/extras" "$config"
printf '[printer]\nkinematics: corexy\n' > "$config/printer.cfg"
printf '[server]\nhost: 0.0.0.0\n' > "$config/moonraker.conf"
chown -R pi:pi "$home"
as_pi() { sudo -u pi -H env "$@"; }

echo "=== 1. no package index: install.sh must fail and leave the config alone"
before=$(sha256sum "$config/printer.cfg" "$config/moonraker.conf")
if as_pi PIP_INDEX_URL=http://127.0.0.1:9/simple PIP_RETRIES=0 PIP_TIMEOUT=2 \
        bash "$repo/install.sh" > /tmp/fail.log 2>&1; then
    cat /tmp/fail.log
    echo "FAIL: install.sh succeeded without a package index"
    exit 1
fi
grep -q '^ERROR: ' /tmp/fail.log || { cat /tmp/fail.log; echo "FAIL: no ERROR line"; exit 1; }
[ "$before" = "$(sha256sum "$config/printer.cfg" "$config/moonraker.conf")" ] \
    || { echo "FAIL: config changed by a failed install"; exit 1; }
[ ! -e "$config/chopper_autotune.cfg" ] || { echo "FAIL: cfg linked by a failed install"; exit 1; }

echo "=== 2. normal install"
as_pi bash "$repo/install.sh"
as_pi "$repo/.venv/bin/python" -c 'import numpy, plotly, chopper_autotune.cli'
grep -qx '\[include chopper_autotune.cfg\]' "$config/printer.cfg"
grep -qx '\[include chopper_force_move.cfg\]' "$config/printer.cfg"
grep -qx '\[update_manager chopper-autotune\]' "$config/moonraker.conf"
[ -L "$config/chopper_autotune.cfg" ]
[ -f "$home/klipper/klippy/extras/gcode_shell_command.py" ]
dirty=$(as_pi git -C "$repo" status --porcelain)
[ -z "$dirty" ] || { echo "FAIL: install left the repo modified: $dirty"; exit 1; }
# no Klipper runs here, so status fails to connect; it must not say "not installed"
status_out=$(as_pi bash "$repo/status.sh" 2>&1 || true)
if grep -q 'not installed' <<< "$status_out"; then
    echo "FAIL: the launcher does not see the installed program: $status_out"
    exit 1
fi

echo "=== 3. a second run changes nothing"
as_pi bash "$repo/install.sh"
[ "$(grep -cx '\[include chopper_autotune.cfg\]' "$config/printer.cfg")" = 1 ]
[ "$(grep -cx '\[update_manager chopper-autotune\]' "$config/moonraker.conf")" = 1 ]
echo "install smoke test passed: $(grep ^PRETTY_NAME= /etc/os-release) $(uname -m)"
