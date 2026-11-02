#!/bin/bash
# Puts networking back exactly how it was before ap-setup.sh.
DIR="$(cd "$(dirname "$0")" && pwd)"
CONF="$DIR/../homepod.conf"
[ -f "$CONF" ] || { echo "Missing homepod.conf (copy homepod.conf.example and fill in the Wi-Fi section)."; exit 1; }
. "$CONF"
ETH_IF="${ETH_IF:-enp5s0}"; WLAN_IF="${WLAN_IF:-wlan0}"
WIRED_CON="${WIRED_CON:-Steam Wired Connection}"; WIFI_CLIENT_CON="${WIFI_CLIENT_CON:-$SSID}"
sudo -v
sudo nmcli con down homepod-ap >/dev/null 2>&1 || true
sudo nmcli con delete homepod-ap homepod-br0-eth homepod-br0 >/dev/null 2>&1 || true
sudo nmcli con mod "$WIRED_CON" connection.autoconnect yes
sudo nmcli con up "$WIRED_CON" >/dev/null
sudo nmcli con mod "$WIFI_CLIENT_CON" connection.autoconnect yes 2>/dev/null || true
for i in $(seq 20); do ip -4 addr show $ETH_IF | grep -q "inet " && break; sleep 1; done
ip -4 -br addr show $ETH_IF
sudo systemctl restart homepods 2>/dev/null || true
echo "Back to normal: Ethernet direct, Wi-Fi client re-enabled, no access point."
