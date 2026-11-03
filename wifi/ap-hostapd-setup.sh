#!/bin/bash
# Step 2 of the optional dedicated Wi-Fi: hosts your home SSID on 5 GHz (channel 36 by default) with hostapd,
# bridged to Ethernet, and ONLY lets the HomePod minis join (MAC allow list). Every other device is turned
# away and stays on your normal router/mesh. If this machine is off, the minis just fall back to the router.
# Starts automatically at boot.  Needs: wifi/ap-setup.sh run first (for br0), and a hostapd binary.
# Usage: wifi/ap-hostapd-setup.sh [channel]   (default 36)
# Undo:  wifi/ap-hostapd-undo.sh
set -e
CH="${1:-36}"
DIR="$(cd "$(dirname "$0")" && pwd)"
CONF="$DIR/../homepod.conf"
[ -f "$CONF" ] || { echo "Missing homepod.conf (copy homepod.conf.example and fill in the Wi-Fi section)."; exit 1; }
. "$CONF"
ETH_IF="${ETH_IF:-enp5s0}"; WLAN_IF="${WLAN_IF:-wlan0}"
WIRED_CON="${WIRED_CON:-Steam Wired Connection}"; WIFI_CLIENT_CON="${WIFI_CLIENT_CON:-$SSID}"
HOSTAPD="${HOSTAPD:-$(command -v hostapd || echo "$DIR/hostapd")}"
read -r -a MINIS <<< "$MINI_MACS"
COUNTRY="${COUNTRY:-US}"

echo "Checking hostapd runs on this system..."
if ! "$HOSTAPD" -v 2>&1 | grep -q "hostapd v"; then
  echo "hostapd won't start - missing libraries:"; ldd "$HOSTAPD" | grep "not found"; exit 1
fi
"$HOSTAPD" -v 2>&1 | head -1
ip link show br0 >/dev/null 2>&1 || { echo "Bridge br0 missing - run ap-setup.sh first."; exit 1; }

read -r -s -p "Enter the $SSID Wi-Fi password (not shown): " PSK; echo
[ ${#PSK} -ge 8 ] || { echo "Password must be at least 8 characters."; exit 1; }
sudo -v

echo "[1/5] Stop NetworkManager's access point and keep it away from wlan0..."
sudo nmcli con down homepod-ap >/dev/null 2>&1 || true
sudo nmcli con delete homepod-ap >/dev/null 2>&1 || true
printf '[keyfile]\nunmanaged-devices=interface-name:%s\n' "$WLAN_IF" | sudo tee /etc/NetworkManager/conf.d/zz-homepod-unmanaged.conf >/dev/null
sudo nmcli general reload conf 2>/dev/null || sudo systemctl reload NetworkManager
sudo systemctl disable --now iwd >/dev/null 2>&1 || true

echo "[2/5] Write hostapd config (password stored root-only in /etc/homepod)..."
sudo mkdir -p /etc/homepod && sudo chmod 700 /etc/homepod
printf '%s\n' "${MINIS[@]}" | sudo tee /etc/homepod/accept.mac >/dev/null
sudo tee /etc/homepod/hostapd.conf >/dev/null <<CONF
interface=$WLAN_IF
bridge=br0
driver=nl80211
ctrl_interface=/run/hostapd
ssid=$SSID
country_code=$COUNTRY
ieee80211d=1
hw_mode=a
channel=$CH
ieee80211n=1
wmm_enabled=1
auth_algs=1
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase=$PSK
macaddr_acl=1
accept_mac_file=/etc/homepod/accept.mac
CONF
sudo chmod 600 /etc/homepod/hostapd.conf /etc/homepod/accept.mac

echo "[3/5] Install boot service..."
sudo tee /etc/systemd/system/homepod-ap.service >/dev/null <<UNIT
[Unit]
Description=Dedicated Wi-Fi for the HomePod minis (hostapd, minis-only allow list)
After=NetworkManager.service network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStartPre=/bin/sh -c 'iw dev $WLAN_IF info >/dev/null 2>&1 || iw phy phy0 interface add $WLAN_IF type managed; for i in \$\$(seq 30); do ip link show br0 >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1'
ExecStart=$HOSTAPD /etc/homepod/hostapd.conf
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload

echo "[4/5] Start it..."
sudo systemctl enable --now homepod-ap.service
sleep 4

echo "[5/5] Status:"
systemctl is-active homepod-ap.service
iw dev $WLAN_IF info | grep -E "type|channel"
echo
echo "Now unplug both minis ~10 s, plug back in, wait ~30 s, then:"
echo "  iw dev $WLAN_IF station dump | grep Station"
echo "Live log: journalctl -u homepod-ap -f"
