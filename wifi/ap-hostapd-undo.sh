#!/bin/bash
# Removes the dedicated Wi-Fi completely and restores the original networking (iwd, Ethernet direct, no bridge).
DIR="$(cd "$(dirname "$0")" && pwd)"
CONF="$DIR/../homepod.conf"
[ -f "$CONF" ] || { echo "Missing homepod.conf (copy homepod.conf.example and fill in the Wi-Fi section)."; exit 1; }
. "$CONF"
ETH_IF="${ETH_IF:-enp5s0}"; WLAN_IF="${WLAN_IF:-wlan0}"
WIRED_CON="${WIRED_CON:-Steam Wired Connection}"; WIFI_CLIENT_CON="${WIFI_CLIENT_CON:-$SSID}"
sudo -v || exit 1
sudo systemctl disable --now homepod-ap.service 2>/dev/null
sudo rm -f /etc/systemd/system/homepod-ap.service
sudo rm -rf /etc/homepod
sudo rm -f /etc/NetworkManager/conf.d/zz-homepod-unmanaged.conf /etc/NetworkManager/conf.d/zz-homepod-wifi-backend.conf
sudo systemctl daemon-reload
sudo systemctl enable --now iwd
sudo systemctl restart NetworkManager
sleep 5
sudo nmcli con delete homepod-ap homepod-br0-eth homepod-br0 >/dev/null 2>&1
sudo nmcli con mod "$WIRED_CON" connection.autoconnect yes
sudo nmcli con up "$WIRED_CON" >/dev/null
sudo nmcli con mod "$WIFI_CLIENT_CON" connection.autoconnect yes 2>/dev/null
sudo iwctl known-networks "$WIFI_CLIENT_CON" set-property AutoConnect yes 2>/dev/null
sudo systemctl restart homepods 2>/dev/null
echo "Restored: iwd back on, Ethernet direct, Wi-Fi client re-enabled, no access point."
