#!/bin/bash
# Run by systemd (as root) at boot. Creates the 'HomePods' output in the desktop user's PipeWire session, makes it
# the default, captures it and pipes it into homepodd. A watchdog checks every 3 s whether PipeWire's
# socket was recreated (it restarts on Gaming/Desktop mode switches); if so, or if the pipeline dies,
# everything exits and systemd restarts this script, which recreates the output.
DIR="$(cd "$(dirname "$0")" && pwd)"
USERNAME="${HOMEPOD_USER:-deck}"             # the user whose desktop audio gets captured
PYTHON="${PYTHON:-$DIR/.venv/bin/python}"
USERID=$(id -u "$USERNAME")
export XDG_RUNTIME_DIR=/run/user/$USERID
AS_USER=(runuser -u "$USERNAME" -- env XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR")
pa() { "${AS_USER[@]}" pactl "$@"; }

for i in $(seq 60); do                       # wait for the user's audio session (up to ~2 min)
  [ -S "$XDG_RUNTIME_DIR/pulse/native" ] && pa info >/dev/null 2>&1 && break
  sleep 2
done

if ! pa list short sinks | grep -q homepods; then
  pa load-module module-null-sink sink_name=homepods rate=44100 channels=2 \
    sink_properties=device.description=HomePods >/dev/null && echo "created HomePods output"
fi
pa set-default-sink homepods && echo "HomePods set as default output"

"${AS_USER[@]}" parec -d homepods.monitor --raw --format=s16be --rate=44100 --channels=2 --latency-msec=10 \
  | FMT=be "$PYTHON" "$DIR/homepodd.py" &
PIPE=$!

SOCK="$XDG_RUNTIME_DIR/pulse/native"
INODE=$(stat -c %i "$SOCK" 2>/dev/null)
while kill -0 $PIPE 2>/dev/null; do          # watchdog (no logins -> no log spam)
  sleep 3
  if [ "$(stat -c %i "$SOCK" 2>/dev/null)" != "$INODE" ]; then
    echo "audio system restarted (Gaming/Desktop switch?) - restarting"
    break
  fi
done
pkill -P $$ 2>/dev/null; pkill -f "parec -d homepods.monitor" 2>/dev/null
sleep 1
exit 1   # non-zero so systemd always restarts us
