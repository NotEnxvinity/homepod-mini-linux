# homepod-mini-linux

Low-latency AirPlay 2 audio from Linux to a **HomePod mini 2 stereo pair**. It's built and tested on a Valve Steam Machine running SteamOS, in both Desktop and Gaming Mode.

When the HomePod mini 2 launched in October 2026, Apple added a low-latency mode that lets the pair act as a Mac's default speakers. Only Macs get it. This project reverse engineers how a Mac talks to the minis and does the same thing from Linux, so games, YouTube, and everything else on the system play through the pair, perfectly in sync.

**Result:** stereo, in sync, roughly **90–100 ms** total delay, starting automatically at boot.

> Not affiliated with or endorsed by Apple or Valve. This uses undocumented protocols that a firmware update could break.

## How it works

```
Apps ──▶ PipeWire "HomePods" output ──▶ parec (raw PCM) ──▶ homepodd.py ──▶ HomePod mini (L)
                                                               │  ▲
                                                               │  └── PTP clock (mini is grandmaster)
                                                               └────▶ HomePod mini (R)
```

1. `homepods-service.sh` creates a virtual **HomePods** output in PipeWire and makes it the default. It captures that output as raw 44.1 kHz stereo PCM.
2. `homepodd.py` sits idle until real audio shows up. Then it finds the minis (cached IPs first, Bonjour by name as the fallback), opens an AirPlay 2 session to each one, and streams.
3. After `IDLE_S` seconds of silence, it releases the minis so your iPhone or Mac can use them again.
4. A watchdog restarts everything when SteamOS switches between Gaming and Desktop Mode, since PipeWire restarts on each switch.

### What the Mac actually does (the reverse engineering)

Regular AirPlay 2 from Linux (PipeWire's RAOP module, OwnTone, etc.) works with HomePods, but it buffers about 2 seconds. That's fine for music and useless for games. To find out what Apple does differently, I watched a Mac stream to the pair using `log stream` on Apple's AirPlay subsystems plus packet captures, and found:

- **PTP timing instead of NTP-style timing.** The Mac sets up the session with `timingProtocol: PTP`, and the **mini acts as the PTP grandmaster**. The Mac follows the speaker's clock instead of making the speaker estimate the sender's clock. That's where the tight sync comes from.
- **The realtime audio stream.** Audio goes as stream type `96`: uncompressed 16-bit PCM in 352-frame (8 ms) RTP packets, encrypted with ChaCha20-Poly1305 using a per-session key from `SETUP`, with a small `latencyMin` instead of the usual multi-second buffer.
- **Timing announcements on the control channel.** About once a second, the sender ties an RTP timestamp to a time on the mini's PTP clock. Both minis use the same timeline, so they play in lockstep.
- **Mac-only gating.** The minis accept this kind of session from a sender that identifies itself as a Mac. `homepodd.py` identifies itself as one during `SETUP`.

`homepodd.py` puts all of that together: a small PTP follower that locks onto the primary mini's Sync and Follow_Up messages (with kernel receive timestamps), pairing, RTSP, and encryption from [pyatv](https://github.com/postlund/pyatv), and a scheduler that sends each packet at its exact PTP time to both speakers. It also answers retransmit requests from a per-speaker backlog.

The byte-level details (SETUP fields, the PTP offset filter, packet layouts, encryption and buffering) are in [PROTOCOL.md](PROTOCOL.md).

## Requirements

- Linux with **PipeWire** (with `pactl`/`parec`) and **systemd**. SteamOS 3 works out of the box.
- Python 3.10+ (the installer creates a virtualenv and installs pyatv).
- Two HomePod mini 2s in a stereo pair, updated to the latest firmware. A single mini works too.
- In the Home app, set **Allow Speaker & TV Access** to *Everyone on the same network* (or a setting that lets your machine connect).
- UDP ports **319/320** (PTP) need to be free. The service runs as root so it can bind them.

## Install

```bash
git clone https://github.com/NotEnxvinity/homepod-mini-linux.git
cd homepod-mini-linux
cp homepod.conf.example homepod.conf
nano homepod.conf        # set SPEAKERS to your minis' names from the Home app
./install.sh             # or ./install.sh 90  to set latency in ms
```

Then play something. The **HomePods** output is already the default.

| Command | What it does |
|---|---|
| `systemctl status homepods` | Is it running? |
| `journalctl -u homepods -f` | Live log: sessions, buffer health, clock offset |
| `./install.sh <ms>` | Change latency (raise it if you hear dropouts) |
| `./uninstall.sh` | Remove the service |
| `.venv/bin/python homepodd.py --dry` | Offline self-test, no speakers or root needed |

### Tuning

- `LAT_MS` (default 70): the delay the minis are told to buffer. Lower is snappier but more sensitive to Wi-Fi hiccups. Total end-to-end delay ends up about 20–30 ms above this.
- `FALLBACK_IPS`: put your minis' IPs here (and reserve them in your router) to skip the Bonjour scan, so audio starts faster.
- `IDLE_S` (default 20): seconds of silence before the minis are released.

## Optional: dedicated Wi-Fi for the minis

On a busy mesh network I got occasional dropouts whenever something else hammered the same channel, like a speed test or a big download. The fix was giving the minis their own access point. The Steam Machine's built-in Wi-Fi isn't otherwise used, since it's wired to the router, so it hosts the **same SSID and password** on its own 5 GHz channel, bridged to Ethernet, with a **MAC allow list** that only admits the minis:

- Every other device gets turned away and stays on the normal router or mesh.
- The minis stay on your normal network with the same IPs, and your iPhone and Mac can still reach them.
- If the Steam Machine is off, the minis just roam back to the router.

```bash
# fill in the Wi-Fi section of homepod.conf first (SSID, MINI_MACS, interface names)
wifi/ap-setup.sh            # bridge Ethernet as br0 (keeps your IP)
wifi/ap-hostapd-setup.sh    # hostapd AP with the minis-only allow list, starts at boot
# then unplug both minis for ~10 s and plug them back in
iw dev wlan0 station dump | grep Station   # should list exactly your minis
```

Undo everything with `wifi/ap-hostapd-undo.sh`, or `wifi/ap-undo.sh` if you only ran step one. The Wi-Fi password is entered at run time and stored only in root-only `/etc/homepod/`, never in this folder.

SteamOS doesn't ship `hostapd`. Either install it from your distro, or extract the binary from Arch Linux's `hostapd` package and put it at `wifi/hostapd`. The script checks for missing libraries before changing anything. This part changes your network configuration, so read the scripts before you run them.

## Files

| File | Purpose |
|---|---|
| `homepodd.py` | The sender: PTP follower, AirPlay 2 sessions, realtime stream, idle release |
| `PROTOCOL.md` | Wire-level notes: session setup, PTP, packet formats, buffering |
| `homepods-service.sh` | Run by systemd: creates and captures the PipeWire output, watchdog |
| `install.sh` / `uninstall.sh` | Virtualenv, systemd unit, config |
| `homepod.conf.example` | All settings, copied to `homepod.conf` (git-ignored) |
| `wifi/` | Optional dedicated access point scripts and their undo scripts |

## Credits

- [pyatv](https://github.com/postlund/pyatv) by Pierre Ståhl for HAP pairing, RTSP, and encryption, and the AirPlay 2 protocol docs that made this possible.
- [Shairport Sync](https://github.com/mikebrady/shairport-sync), [OwnTone](https://github.com/owntone/owntone-server), and [openairplay](https://openairplay.github.io/airplay-spec/) for years of public AirPlay research.

Built by [Enxvinity](https://github.com/NotEnxvinity) in one very long Sunday, the day after the minis arrived.

## License

MIT
