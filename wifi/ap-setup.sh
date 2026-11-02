#!/bin/bash
# Step 1 of the optional dedicated Wi-Fi: bridges the wired connection (br0, keeping the Ethernet MAC so
# your IP doesn't change) and starts a NetworkManager access point with your home SSID on its own channel.
# Run wifi/ap-hostapd-setup.sh afterwards to switch the AP to hostapd with a minis-only allow list.
# Usage: wifi/ap-setup.sh [channel]   (default 36)      Undo: wifi/ap-undo.sh
set -e
CH="${1:-36}"
DIR="$(cd "$(dirname "$0")" && pwd)"
CONF="$DIR/../homepod.conf"
[ -f "$CONF" ] || { echo "Missing homepod.conf (copy homepod.conf.example and fill in the Wi-Fi section)."; exit 1; }
. "$CONF"
ETH_IF="${ETH_IF:-enp5s0}"; WLAN_IF="${WLAN_IF:-wlan0}"
WIRED_CON="${WIRED_CON:-Steam Wired Connection}"; WIFI_CLIENT_CON="${WIFI_CLIENT_CON:-$SSID}"
WIRED="$WIRED_CON"; WIFI_CLIENT="$WIFI_CLIENT_CON"; ETH=$ETH_IF; WLAN=$WLAN_IF
MAC=$(cat /sys/class/net/$ETH/address)

echo "This hosts '$SSID' on 5 GHz channel $CH from the Steam Machine, bridged to Ethernet."
read -r -s -p "Enter the $SSID Wi-Fi password (not shown): " PSK; echo
[ ${#PSK} -ge 8 ] || { echo "Password must be at least 8 characters."; exit 1; }
sudo -v

echo "[1/4] Stop using Wi-Fi as a client..."
sudo nmcli con mod "$WIFI_CLIENT" connection.autoconnect no 2>/dev/null || true
sudo nmcli con down "$WIFI_CLIENT" 2>/dev/null || true

echo "[2/4] Create bridge br0 (keeps the Ethernet MAC so your IP doesn't change)..."
sudo nmcli con delete homepod-ap homepod-br0-eth homepod-br0 >/dev/null 2>&1 || true
sudo nmcli con add type bridge ifname br0 con-name homepod-br0 bridge.stp no bridge.mac-address "$MAC" \
  ipv4.method auto ipv6.method auto connection.autoconnect yes >/dev/null
sudo nmcli con add type ethernet ifname $ETH con-name homepod-br0-eth controller br0 port-type bridge \
  connection.autoconnect yes >/dev/null
sudo nmcli con mod "$WIRED" connection.autoconnect no

echo "[3/4] Move Ethernet onto the bridge (network blips for a few seconds)..."
sudo nmcli con up homepod-br0-eth >/dev/null
sudo nmcli con up homepod-br0 >/dev/null || true
for i in $(seq 30); do ip -4 addr show br0 | grep -q "inet " && break; sleep 1; done
ip -4 -br addr show br0

echo "[4/4] Start the access point on channel $CH..."
sudo nmcli con add type wifi ifname $WLAN con-name homepod-ap ssid "$SSID" controller br0 port-type bridge \
  802-11-wireless.mode ap 802-11-wireless.band a 802-11-wireless.channel "$CH" \
  wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp \
  wifi-sec.psk "$PSK" connection.autoconnect yes >/dev/null
sudo nmcli con up homepod-ap

echo
iw dev $WLAN info | grep -E "type|channel"
echo
echo "Done. Now unplug both minis for ~10 s and plug them back in, then check who's connected:"
echo "  iw dev $WLAN station dump | grep Station"
echo "(your minis: $MINI_MACS)"
sudo systemctl restart homepods 2>/dev/null || true
