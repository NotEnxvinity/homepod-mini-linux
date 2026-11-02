#!/bin/bash
# One-time install: sets up the Python environment and a systemd service so the 'HomePods'
# output starts automatically at boot.
#
# Usage:  cp homepod.conf.example homepod.conf   # then edit it
#         ./install.sh [latency_ms]               # re-run any time to change latency or config
# Remove: ./uninstall.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
CONF="$DIR/homepod.conf"
[ -f "$CONF" ] || { echo "Missing $CONF - copy homepod.conf.example to homepod.conf and edit it first."; exit 1; }
# shellcheck disable=SC1090
. "$CONF"
LAT="${1:-${LAT_MS:-70}}"
[ -n "$SPEAKERS$FALLBACK_IPS" ] || { echo "Set SPEAKERS (or FALLBACK_IPS) in homepod.conf."; exit 1; }
HOMEPOD_USER="${HOMEPOD_USER:-$(id -un)}"

if [ ! -x "$DIR/.venv/bin/python" ]; then
  echo "Creating Python environment in $DIR/.venv ..."
  python3 -m venv "$DIR/.venv"
  "$DIR/.venv/bin/pip" install --quiet --upgrade pip
  "$DIR/.venv/bin/pip" install --quiet -r "$DIR/requirements.txt"
fi

chmod +x "$DIR/homepodd.py" "$DIR/homepods-service.sh"
sudo tee /etc/systemd/system/homepods.service >/dev/null <<UNIT
[Unit]
Description=HomePod mini stereo output (AirPlay 2 / PTP low latency)
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
Environment=LAT_MS=$LAT
Environment="SPEAKERS=$SPEAKERS"
Environment="FALLBACK_IPS=$FALLBACK_IPS"
Environment=IDLE_S=${IDLE_S:-20}
Environment=HOMEPOD_USER=$HOMEPOD_USER
ExecStart=$DIR/homepods-service.sh
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable homepods.service
sudo systemctl restart homepods.service
echo
echo "Installed. Latency: ${LAT} ms. It's running now and will start at every boot."
echo "  status:     systemctl status homepods"
echo "  live log:   journalctl -u homepods -f"
echo "  latency:    ./install.sh <ms>   (re-run with a new number)"
echo "  remove:     ./uninstall.sh"
