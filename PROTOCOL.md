# Protocol notes

The wire-level details behind [homepodd.py](homepodd.py): what a Mac sends a HomePod mini 2
pair for low-latency playback, and how this project reproduces it. Everything here is
what the code does today and what the minis accepted on the firmware it was built
against. None of it is documented by Apple, so treat it as observations, not a spec.

Byte offsets are 0-based. Multi-byte fields are big-endian unless noted.

## Session flow

For each mini, in order (the first mini in `SPEAKERS` is the clock source):

1. **Connect** to TCP port 7000.
2. **Transient pairing** (HAP, no PIN) with pyatv's `TRANSIENT_CREDENTIALS`. The mini only
   allows this when the Home app's *Allow Speaker & TV Access* lets your machine in.
3. **Base `SETUP`** (binary plist body). These fields matter:
   | key | value | why |
   |---|---|---|
   | `timingProtocol` | `PTP` | instead of the NTP-style timing regular AirPlay 2 senders use |
   | `timingPeerInfo` / `timingPeerList` | `{Addresses: [our IP], ID: <session UUID>, SupportsClockPortMatchingOverride: true}` | tells the mini who its PTP peer is |
   | `model` | `Mac17,9` | the minis only take low-latency PTP sessions from a sender that says it's a Mac |
   | `osName` / `osVersion` / `osBuildVersion` | `macOS` / `27.2` / `24K122` | same reason |
   | `isMultiSelectAirPlay` | `true` | |
   | `groupContainsGroupLeader` | `false` | |
   | `deviceID`, `macAddress` | `AA:BB:CC:DD:EE:FF` | fixed placeholders; the minis accepted them |

   If the reply has an `eventPort`, open the encrypted event channel on it
   (`Events-Salt`, `Events-Read-Encryption-Key`, `Events-Write-Encryption-Key`).
4. **Wait for PTP** to lock (see below): 10 Sync/Follow_Up pairs from the primary mini.
   homepodd gives up after 5 s and retries the whole session.
5. **`SETPEERS`** (`Content-Type: /peer-list-changed`) with a binary plist list of every
   mini's IP plus ours. This puts both minis on one timing group.
6. **Stream `SETUP`** with one stream:
   | key | value |
   |---|---|
   | `type` | `96` (realtime audio) |
   | `ct` | `1` (uncompressed PCM) |
   | `audioFormat` | `0x800` (PCM, 44.1 kHz, 16-bit, stereo) |
   | `sr` / `spf` | `44100` / `352` |
   | `latencyMin` | `LAT_MS` in samples (70 ms → 3087) |
   | `latencyMax` | `max(2 × latencyMin, 88200)` |
   | `shk` | 32 random bytes: this session's audio key |
   | `controlPort` | our UDP control socket for this mini |
   | `isMedia`, `audioMode`, `supportsDynamicStreamID`, `streamConnectionID` | `true`, `default`, `false`, the RTSP session id |

   The reply's `streams[0]` has the mini's `dataPort` and `controlPort`.
7. **`RECORD`** with `Range: npt=0-`.
8. Every 2 s, an RTSP **`FEEDBACK`** keeps the session alive.
9. On release, **`TEARDOWN`**, then close the event channel and the connection.

## PTP: following the mini's clock

The mini is the PTP **grandmaster** and the sender follows it. homepodd binds UDP **319**
(event) and **320** (general) on all interfaces, which is why it runs as root.

**Keepalive.** Once a second, for each mini:
- an **Announce** to port 320 with our own clock identity (`aa:bb:cc:ff:fe:5e:a1:11`) and
  deliberately weak values (priority1 250, clock class 248, accuracy `0xFE`, variance
  `0xFFFF`, priority2 250, time source `0xA0`, internal oscillator), so we never win the
  best-master election;
- a **Delay_Req** to port 319 with a zero clock identity and port 0. The minis answer
  these.

**Offset.** For the primary mini only:
- Each **Sync** (message type 0, port 319) is timestamped by the kernel on arrival
  (`SO_TIMESTAMPNS`) and stored under its sequence id (bytes 30–31).
- Its **Follow_Up** (type 8, port 320) carries the precise origin timestamp (bytes 34–43:
  48-bit seconds plus 32-bit nanoseconds) and the correction field (bytes 8–15,
  nanoseconds × 2¹⁶).
- One sample is `origin + correction − local_arrival`, which is the true offset minus that
  packet's network delay. homepodd keeps the last 32 and uses the **largest of the last
  16**: the sample with the least delay is the most accurate (a min-delay filter).
- `device_now = local_time + offset` is "now" on the mini's clock.

**Clock identity.** The grandmaster identity comes from the primary mini's Announce
(bytes 53–60) and goes into every timing packet. `CLOCK=port` uses the Sync's source port
identity (bytes 20–27) instead.

Both minis follow the same grandmaster, so one offset serves the pair.

## Audio packets (to each mini's `dataPort`)

One packet is 352 stereo frames (8 ms), 1444 bytes:

| bytes | field |
|---|---|
| 0 | `0x80` (RTP v2) |
| 1 | `0xE0` on the first packet of a session (marker bit + payload type 96), `0x60` after |
| 2–3 | sequence number (random start, +1 per packet) |
| 4–7 | RTP timestamp (random start, +352 per packet) |
| 8–11 | SSRC (random per session) |
| 12–1419 | 1408 bytes of s16 big-endian stereo PCM, encrypted |
| 1420–1435 | Poly1305 tag (16 bytes) |
| 1436–1443 | nonce: the 8-byte little-endian packet counter |

Encryption is **ChaCha20-Poly1305** with the session's `shk` as the key, the nonce
`00 00 00 00 ‖ counter (LE)`, and **bytes 4–11 of the header (timestamp + SSRC) as the
additional authenticated data**. Each mini has its own `shk`, so the same PCM is encrypted
once per mini.

Packets go out when the mini's clock reaches each one's due time: packet *i* is due at
`start + i × 352 / 44100` s on the PTP clock. Pacing against the mini's clock, not the local
one, keeps the sender from drifting off the timeline the minis play on.

## Timing packets (to each mini's `controlPort`)

Once per second of audio, each mini gets the same 28-byte packet that ties an RTP
timestamp to a PTP time:

| bytes | field |
|---|---|
| 0 | `0x90` for the very first one (extension bit set), `0x80` after |
| 1 | `0xD7` |
| 2–3 | sequence (counts these packets) |
| 4–7 | RTP timestamp to be **heard** at the PTP time below: `anchor − latency` |
| 8–11 | PTP time, seconds |
| 12–15 | PTP time, fraction of a second (× 2³²) |
| 16–19 | `anchor`: the RTP timestamp being **sent** at that PTP time |
| 20–27 | grandmaster clock identity |

Because both minis get the same anchors on the same clock, they play in lockstep. The
`latency` subtracted here is the delay you hear, and why `LAT_MS` sets it.

## Retransmits (on our control socket)

A mini that misses packets sends a request with `byte1 & 0x7F == 0x55`, the first missing
sequence number in bytes 4–5 and a count in bytes 6–7. homepodd answers each one it still
has with `0x80 0xD6`, the original sequence number, and the whole original encrypted packet.
Each mini has its own backlog of the last 1000 packets (about 8 s).

## Capture and buffering

`parec` reads the `HomePods` PipeWire sink's monitor as raw s16 big-endian 44.1 kHz stereo,
with a 10 ms capture latency, and pipes it to homepodd. homepodd keeps a small cushion between capture and
send:
- It starts at **4 packets (32 ms)**. Each time capture runs dry it sends silence, grows the
  cushion by one packet (up to 6, 48 ms) and waits for it to refill.
- If capture gets more than 8 packets ahead of the cushion (the local and PTP clocks drift
  apart), it drops the excess to stay near live.
- Audio counts as "present" when sampled bytes aren't all `0x00`/`0xFF` and some sample
  exceeds ±8. After `IDLE_S` seconds without any, the session is torn down.

The end-to-end delay is roughly `LAT_MS` + the cushion + capture latency, which is where
the measured 90–100 ms at `LAT_MS=70` comes from.
