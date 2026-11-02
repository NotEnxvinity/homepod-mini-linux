#!/bin/bash
# Removes the HomePods boot service. Leaves the repo folder (and .venv) alone.
sudo systemctl disable --now homepods.service 2>/dev/null
sudo rm -f /etc/systemd/system/homepods.service
sudo systemctl daemon-reload
MOD=$(pactl list short modules 2>/dev/null | awk '/sink_name=homepods/{print $1}')
[ -n "$MOD" ] && pactl unload-module "$MOD"
echo "Removed. Pick another audio output in your sound settings if it didn't switch on its own."
