#!/usr/bin/env python3
"""homepodd -- always-on HomePod mini 2 stereo-pair output for Linux (built on a Steam Machine / SteamOS).

Reads raw 44.1 kHz s16 stereo PCM from stdin (piped in by homepods-service.sh from the 'HomePods'
PipeWire output). Stays idle until real audio arrives, then finds the minis by name, connects,
and streams on their PTP clock. After IDLE_S seconds of silence it releases the minis so other
Apple devices can use them. Exits when stdin ends (systemd restarts it).

Env:  SPEAKERS="Left Name,Right Name"  (AirPlay names, clock-source speaker first; required)
      FALLBACK_IPS="192.168.1.20,192.168.1.21"  (optional, same order; skips the Bonjour scan)
      LAT_MS (default 70)   IDLE_S (default 20)   FMT=be|le (byte order of stdin, default be)
      SENDER_NAME (name the minis see, default: hostname)
      sudo ./homepodd.py --dry     offline self-test (no speakers needed)
"""
import array, asyncio, os, plistlib, random, signal, socket, struct, sys, threading, time
from uuid import uuid4

DRY = "--dry" in sys.argv
args = [a for a in sys.argv[1:] if a != "--dry"]
LAT_MS = int(os.environ.get("LAT_MS", args[0] if args else "70"))
SPEAKERS = [s.strip() for s in os.environ.get("SPEAKERS", "").split(",") if s.strip()]
FALLBACK_IPS = [ip.strip() for ip in os.environ.get("FALLBACK_IPS", "").split(",") if ip.strip()]
SENDER_NAME = os.environ.get("SENDER_NAME") or socket.gethostname()
IDLE_S = float(os.environ.get("IDLE_S", "20"))
FMT = os.environ.get("FMT", "be")
IPS = list(FALLBACK_IPS)
SECS = 20  # only used by the dry-run tone
CLOCK_MODE = os.environ.get("CLOCK", "gm")
DUP = int(os.environ.get("DUP", "0"))
DUP_MS = float(os.environ.get("DUP_MS", "3"))

SR, CH, FPP = 44100, 2, 352                  # 44.1k stereo s16, 352 frames (8 ms) per packet
PKT_BYTES = FPP * CH * 2
LAT = int(SR * LAT_MS / 1000)                # latency in samples
MY_CLOCK = bytes.fromhex("aabbccfffe5ea111")
SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35)
stats = {"ptp": 0, "late_sends": 0, "syncs": 0, "pkts": 0, "underruns": 0, "trims": 0, "sessions": 0}
PRIMARY = None   # set per session to the first speaker's IP

# ---------------------------------------------------------------- PTP follower
def ptp_ts():
    t = time.time_ns()
    return struct.pack(">HII", (t // 10**9) >> 32, (t // 10**9) & 0xFFFFFFFF, t % 10**9)

def ptp_header(mtype, length, seq, control, flags=0, log_interval=0, clock=MY_CLOCK, port=1):
    return (bytes([0x10 | mtype, 2]) + struct.pack(">HBB", length, 0, 0) + struct.pack(">H", flags)
            + bytes(12) + clock + struct.pack(">H", port) + struct.pack(">HBb", seq, control, log_interval))

def announce(seq):
    body = (ptp_ts() + struct.pack(">hB", 37, 0) + bytes([250, 248, 0xFE]) + struct.pack(">H", 0xFFFF)
            + bytes([250]) + MY_CLOCK + struct.pack(">H", 0) + bytes([0xA0]))
    return ptp_header(11, 34 + len(body), seq, 5, flags=0x0008) + body

def delay_req(seq):  # zero identity + port 0: HomePods answer these
    return ptp_header(1, 44, seq, 1, log_interval=0x7F, clock=bytes(8), port=0) + ptp_ts()

def ts10(b):
    hi, lo, ns = struct.unpack(">HII", b)
    return ((hi << 32) | lo) * 10**9 + ns

class PTPClock:
    """device_time(local) = local + offset. Offset comes from the PRIMARY speaker's Sync/Follow_Up
    (min-delay filter). All minis in the pair follow the same grandmaster, so one clock serves all."""
    def __init__(self):
        self.reset()
    def reset(self):
        self.sync_rx, self.samples = {}, []
        self.gm = None; self.port_clock = None; self.seen = set()

    def on_event(self, sock):
        while True:
            try: data, anc, _, addr = sock.recvmsg(2048, 1024)
            except BlockingIOError: return
            rx = None
            for lvl, typ, cd in anc:
                if lvl == socket.SOL_SOCKET and typ == SO_TIMESTAMPNS:
                    s, ns = struct.unpack("qq", cd[:16]); rx = s * 10**9 + ns
            self.seen.add(addr[0])
            if addr[0] == PRIMARY and len(data) >= 34 and data[0] & 0x0F == 0:
                self.sync_rx[struct.unpack(">H", data[30:32])[0]] = rx or time.time_ns()

    def on_general(self, sock):
        while True:
            try: data, addr = sock.recvfrom(2048)
            except BlockingIOError: return
            self.seen.add(addr[0])
            if addr[0] != PRIMARY or len(data) < 44: continue
            t = data[0] & 0x0F
            if t == 8:
                rx = self.sync_rx.pop(struct.unpack(">H", data[30:32])[0], None)
                if rx is None: continue
                corr = struct.unpack(">q", data[8:16])[0] >> 16
                self.samples.append((rx, ts10(data[34:44]) + corr - rx))
                self.samples = self.samples[-32:]
                self.port_clock = data[20:28]; stats["ptp"] += 1
            elif t == 11 and len(data) >= 64:
                self.gm = data[53:61]

    @property
    def ready(self): return len(self.samples) >= 10
    def offset(self): return max(o for _, o in self.samples[-16:])
    def device_now(self): return time.time_ns() + self.offset()
    def clock_id(self):
        cid = self.gm if CLOCK_MODE == "gm" else self.port_clock
        return cid or self.port_clock or bytes(8)

def rtp_header(first, seq, ts, ssrc):
    return struct.pack(">BBHII", 0x80, 0xE0 if first else 0x60, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)

def ptp_sync_packet(seq, current_rtp, device_ns, next_rtp, clock_id, first):
    secs, ns = divmod(device_ns, 10**9)
    frac = (ns << 32) // 10**9
    return struct.pack(">BBHIIII", 0x90 if first else 0x80, 0xD7, seq & 0xFFFF, current_rtp & 0xFFFFFFFF,
                       secs & 0xFFFFFFFF, frac, next_rtp & 0xFFFFFFFF) + clock_id

class Control(asyncio.DatagramProtocol):
    """Per-speaker control socket: answers retransmit requests from that speaker's backlog."""
    def __init__(self, rx): self.rx, self.tr = rx, None
    def connection_made(self, tr): self.tr = tr
    def datagram_received(self, data, addr):
        if len(data) >= 8 and data[1] & 0x7F == 0x55:
            self.rx.resend_req += 1
            lost, n = struct.unpack(">HH", data[4:8])
            for i in range(n):
                pkt = self.rx.backlog.get((lost + i) & 0xFFFF)
                if pkt and self.tr:
                    self.tr.sendto(b"\x80\xd6" + pkt[2:4] + pkt, addr); self.rx.resent += 1

def body(resp):
    try: return plistlib.loads(resp.body) if resp.body else {}
    except Exception: return {}

class Speaker:
    """One AirPlay 2 session (RTSP + event channel + audio/control sockets) to one mini."""
    def __init__(self, ip):
        self.ip, self.backlog, self.resend_req, self.resent, self.send_err = ip, {}, 0, 0, 0
        self.conn = self.rtsp = self.ev = self.fb = None

    async def connect(self):
        from pyatv.auth.hap_pairing import TRANSIENT_CREDENTIALS
        from pyatv.auth.hap_channel import setup_channel
        from pyatv.protocols.airplay.auth import verify_connection
        from pyatv.protocols.airplay.channels import EventChannel
        from pyatv.support.http import http_connect
        from pyatv.support.rtsp import RtspSession
        self.conn = await http_connect(self.ip, 7000)
        self.me = self.conn.local_ip
        self.verifier = await verify_connection(TRANSIENT_CREDENTIALS, self.conn)
        self.rtsp = RtspSession(self.conn)
        sid = str(uuid4()).upper()
        peer = {"Addresses": [self.me], "ID": sid, "SupportsClockPortMatchingOverride": True}
        base = await self.rtsp.exchange("SETUP", allow_error=True, body={
            "deviceID": "AA:BB:CC:DD:EE:FF", "sessionUUID": sid,
            "timingProtocol": "PTP", "timingPeerInfo": peer, "timingPeerList": [peer],
            "isMultiSelectAirPlay": True, "groupContainsGroupLeader": False,
            "macAddress": "AA:BB:CC:DD:EE:FF", "model": "Mac17,9", "name": SENDER_NAME,  # the minis only accept low-latency PTP streams from "Macs"
            "osName": "macOS", "osVersion": "27.2", "osBuildVersion": "24K122",
            "senderSupportsRelay": False, "sourceVersion": "1005.13.1", "statsCollectionEnabled": False})
        b = body(base)
        print(f"[{self.ip}] base SETUP {base.code}")
        if base.code != 200: raise RuntimeError(f"{self.ip}: base SETUP failed ({base.code})")
        if b.get("eventPort"):
            self.ev, _ = await setup_channel(EventChannel, self.verifier, self.ip, b["eventPort"], "Events-Salt",
                                             "Events-Read-Encryption-Key", "Events-Write-Encryption-Key")

    async def setup_stream(self, all_ips):
        from pyatv.support.chacha20 import Chacha20Cipher8byteNonce
        loop = asyncio.get_running_loop()
        p = await self.rtsp.exchange("SETPEERS", allow_error=True, content_type="/peer-list-changed",
                                     body=plistlib.dumps(list(all_ips) + [self.me], fmt=plistlib.FMT_BINARY))
        shk = os.urandom(32)
        csock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); csock.bind((self.me, 0))
        self.ctrl_tr, _ = await loop.create_datagram_endpoint(lambda: Control(self), sock=csock)
        st = await self.rtsp.exchange("SETUP", allow_error=True, body={"streams": [{
            "audioFormat": 0x800, "audioMode": "default", "ct": 1, "spf": FPP, "sr": SR,
            "controlPort": csock.getsockname()[1], "isMedia": True,
            "latencyMin": LAT, "latencyMax": max(LAT * 2, 88200), "shk": shk, "type": 96,
            "supportsDynamicStreamID": False, "streamConnectionID": self.rtsp.session_id}]})
        sb = body(st)
        print(f"[{self.ip}] SETPEERS {p.code}, stream SETUP {st.code} {sb}")
        if st.code != 200 or not sb.get("streams"): raise RuntimeError(f"{self.ip}: stream SETUP failed")
        s0 = sb["streams"][0]; self.data_port, self.ctrl_port = s0["dataPort"], s0["controlPort"]
        rec = await self.rtsp.exchange("RECORD", allow_error=True, headers={"Range": "npt=0-"})
        print(f"[{self.ip}] RECORD {rec.code}")
        self.cipher = Chacha20Cipher8byteNonce(shk, shk)
        self.dsock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); self.dsock.bind((self.me, 0))
        self.dsock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        async def feedback():
            while True:
                await asyncio.sleep(2)
                try: await self.rtsp.feedback(allow_error=True)
                except Exception: pass
        self.fb = asyncio.create_task(feedback())

    def send_audio(self, seq, hdr, audio):
        nonce = self.cipher.out_nonce   # each speaker has its own key, so encrypt per speaker
        pkt = hdr + self.cipher.encrypt(audio, aad=hdr[4:12]) + nonce[-8:]
        try: self.dsock.sendto(pkt, (self.ip, self.data_port))
        except OSError: self.send_err += 1
        self.backlog[seq & 0xFFFF] = pkt
        if len(self.backlog) > 1000: self.backlog.pop(next(iter(self.backlog)))

    def send_ctrl(self, pkt): self.ctrl_tr.sendto(pkt, (self.ip, self.ctrl_port))

    async def close(self):
        if self.fb: self.fb.cancel()
        try: await self.rtsp.exchange("TEARDOWN", allow_error=True)
        except Exception: pass
        if self.ev: self.ev.close()
        if self.conn: self.conn.close()

def load_pcm(path):
    if path is None:  # 440 Hz test tone
        import math
        return b"".join(struct.pack(">hh", v, v) for v in
                        (int(6000 * math.sin(2 * math.pi * 440 * i / SR)) for i in range(SR * SECS)))
    import miniaudio
    d = miniaudio.decode_file(path, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=CH, sample_rate=SR)
    s = d.samples; s.byteswap(); return s.tobytes()  # AirPlay PCM is big-endian (pyatv byteswaps too)

# ---------------------------------------------------------------- live source
def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

# ---------------------------------------------------------------- live source
class LiveSource:
    """stdin -> byte buffer (background thread). take() returns one packet; trims if the buffer
    drifts too far ahead, returns silence on underrun. is_silent() tells whether recent audio is
    digital silence (nothing playing to the HomePods output)."""
    TARGET, MAX_TARGET = 4, 6                # cushion in packets (8 ms each): starts ~32 ms, grows to
                                             # ~48 ms if the capture keeps arriving late
    def __init__(self, stream=None):
        self.buf = bytearray(); self.lock = threading.Lock(); self.eof = False
        self.swap = FMT != "be"
        self.stream = stream if stream is not None else sys.stdin.buffer
        self.last_sound = 0.0                # monotonic time we last saw non-silent audio
        self.rebuffer = False                # after an underrun: output silence until cushion refills
        threading.Thread(target=self._reader, daemon=True).start()
    def _reader(self):
        fd = self.stream.fileno()                # raw fd reads: no buffered-reader lock, so no
        while True:                              # "could not acquire lock at shutdown" abort on exit
            try: chunk = os.read(fd, 65536)
            except OSError: chunk = b""
            if not chunk: self.eof = True; return
            if any(chunk[i] not in (0, 255) for i in range(0, len(chunk), 97)) and max(
                    abs(v) for v in array.array("h", chunk[:len(chunk) // 2 * 2])[::7]) > 8:
                self.last_sound = time.monotonic()
            with self.lock:
                self.buf += chunk
                if len(self.buf) > 200 * PKT_BYTES:          # nobody is consuming (idle): keep it short
                    del self.buf[:len(self.buf) - self.TARGET * PKT_BYTES - (len(self.buf) % 4)]
    def sound_recent(self, secs): return time.monotonic() - self.last_sound < secs
    def reset(self):
        with self.lock: del self.buf[:]
    def level(self):
        with self.lock: return len(self.buf) // PKT_BYTES
    def take(self):
        with self.lock:
            if len(self.buf) > (self.TARGET + 8) * PKT_BYTES:      # drifted too far behind live
                cut = len(self.buf) - self.TARGET * PKT_BYTES
                cut -= cut % 4; del self.buf[:cut]; stats["trims"] += 1
            if self.rebuffer:                                     # refilling after an underrun
                if len(self.buf) < self.TARGET * PKT_BYTES: return bytes(PKT_BYTES)
                self.rebuffer = False
            if len(self.buf) < PKT_BYTES:                         # ran dry: grow cushion, refill once
                stats["underruns"] += 1
                if self.TARGET < self.MAX_TARGET:
                    self.TARGET += 1; log(f"  capture ran dry -> cushion now {self.TARGET} packets (~{self.TARGET*8} ms)")
                self.rebuffer = True
                return bytes(PKT_BYTES)
            out = bytes(self.buf[:PKT_BYTES]); del self.buf[:PKT_BYTES]
        if self.swap:
            a = array.array("h", out); a.byteswap(); out = a.tobytes()
        return out

# ---------------------------------------------------------------- speaker discovery
LAST_GOOD = None   # IPs that worked last session (skips the Bonjour scan next time)

async def reachable(ip, timeout=0.6):
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(ip, 7000), timeout)
        w.close(); return True
    except Exception:
        return False

async def find_speakers():
    """Fast path: last-good (or fallback) IPs if they answer on port 7000. Slow path: Bonjour scan by name."""
    for ips in (LAST_GOOD, FALLBACK_IPS):
        if ips and all(await asyncio.gather(*(reachable(ip) for ip in ips))):
            return list(ips)
    try:
        import pyatv
        found = await pyatv.scan(asyncio.get_running_loop(), timeout=3)
        by_name = {c.name: str(c.address) for c in found}
        ips = [by_name[n] for n in SPEAKERS if n in by_name]
        if len(ips) == len(SPEAKERS):
            log(f"found speakers by name: {ips}")
            return ips
        log(f"only found {[n for n in SPEAKERS if n in by_name]} by name; trying fallback IPs")
    except Exception as e:
        log(f"discovery failed ({e}); trying fallback IPs")
    return list(FALLBACK_IPS)

# ---------------------------------------------------------------- streaming
async def stream(source, speakers, clock, stop):
    """Shared PTP timeline: every speaker gets the same RTP timestamps anchored to one PTP clock. Returns when stop is set, stdin ends, or audio has been
    silent for IDLE_S seconds."""
    seq0, ts0, ssrc = random.randrange(2**16), random.randrange(2**32), random.randrange(2**32)
    source.reset()
    for _ in range(200):
        if source.level() >= source.TARGET: break
        await asyncio.sleep(0.005)
    dev0 = clock.device_now()
    seq, sync_k, first, i = seq0, 0, True, 0
    last = dict(stats)
    while not stop.is_set() and not source.eof:
        if i % 125 == 0 and not source.sound_recent(IDLE_S):
            return "idle"
        if i and i % 7500 == 0:          # once a minute while streaming
            d = {k: stats[k] - last[k] for k in ("late_sends", "underruns", "trims")}; last = dict(stats)
            log(f"  last 60 s: late {d['late_sends']} underruns {d['underruns']} trims {d['trims']} "
                f"buffer {source.level()}/{source.TARGET} resend req {[sp.resend_req for sp in speakers]} "
                f"clock offset {clock.offset()/1e6:.3f} ms")
        due_dev = dev0 + (i * FPP * 10**9) // SR
        delay = (due_dev - clock.device_now()) / 1e9
        if delay > 0: await asyncio.sleep(delay)
        elif delay < -0.004: stats["late_sends"] += 1
        while i * FPP >= sync_k * SR:
            anchor_rtp, anchor_dev = ts0 + sync_k * SR, dev0 + sync_k * 10**9
            pkt = ptp_sync_packet(sync_k, anchor_rtp - LAT, anchor_dev, anchor_rtp,
                                  clock.clock_id(), first and sync_k == 0)
            for sp in speakers: sp.send_ctrl(pkt)
            sync_k += 1; stats["syncs"] += 1
        hdr = rtp_header(first, seq, ts0 + i * FPP, ssrc)
        audio = source.take()
        for sp in speakers: sp.send_audio(seq, hdr, audio)
        stats["pkts"] += 1; first = False; seq += 1; i += 1
    return "eof" if source.eof else "stop"

async def session(source, clock, ev_s, gen_s, stop):
    """One play session: discover, connect, lock clock, stream until idle/eof/stop, release."""
    global PRIMARY, IPS, LAST_GOOD
    IPS = await find_speakers()
    if not IPS:
        log("couldn't find the speakers - check SPEAKERS / FALLBACK_IPS"); return "error"
    PRIMARY = IPS[0]; clock.reset()
    log(f"audio detected -> connecting to {', '.join(f'{n} ({ip})' for n, ip in zip(SPEAKERS, IPS))}")
    speakers = [Speaker(ip) for ip in IPS]
    async def ptp_keepalive():
        n = 0
        while True:
            for ip in IPS:
                gen_s.sendto(announce(n), (ip, 320)); ev_s.sendto(delay_req(n), (ip, 319))
            n += 1; await asyncio.sleep(1)
    ka = None
    try:
        for sp in speakers: await sp.connect()
        ka = asyncio.create_task(ptp_keepalive())
        for _ in range(50):
            if clock.ready: break
            await asyncio.sleep(0.1)
        if not clock.ready:
            log("no PTP from", PRIMARY, "- will retry"); return "error"
        for sp in speakers: await sp.setup_stream(IPS)
        stats["sessions"] += 1; LAST_GOOD = list(IPS)
        log(f"streaming (latency {LAT_MS} ms)")
        why = await stream(source, speakers, clock, stop)
        log(f"session ended: {why}  | late {stats['late_sends']} underruns {stats['underruns']} trims {stats['trims']} "
            f"resend req {[sp.resend_req for sp in speakers]}")
        return why
    except Exception as e:
        log(f"session error: {type(e).__name__}: {e}"); return "error"
    finally:
        if ka: ka.cancel()
        for sp in speakers:
            try: await sp.close()
            except Exception: pass

async def dry_run():
    """Offline: fake speakers + fake live source with sound -> silence, checks idle release."""
    global IDLE_S, PRIMARY
    IDLE_S = 1.0; PRIMARY = "a"
    print("DRY RUN: 2 s of tone then silence; expect streaming, then idle release ~1 s after the tone stops.")
    from pyatv.support.chacha20 import Chacha20Cipher8byteNonce
    class FakeClock:
        def device_now(self): return time.time_ns() + 123_456_789
        def clock_id(self): return bytes.fromhex("aabbccfffe000001")
    class FakeSpeaker(Speaker):
        def __init__(self, ip):
            super().__init__(ip); k = os.urandom(32)
            self.cipher = Chacha20Cipher8byteNonce(k, k); self.n = 0; self.data_port = 1
            self.dsock = type("S", (), {"sendto": lambda s, p, a: setattr(self, "n", self.n + 1)})()
        def send_ctrl(self, pkt): pass
    r, w = os.pipe()
    tone = load_pcm(None)[:SR * 4 * 2]               # 2 s of (big-endian) tone
    feed = tone + bytes(SR * 4 * 3)                  # then 3 s of silence
    def writer():
        f = os.fdopen(w, "wb", buffering=0); t0 = time.monotonic(); pos = 0
        while pos < len(feed):
            target = int((time.monotonic() - t0) * SR) * 4
            if target > pos: f.write(feed[pos:target]); pos = target
            time.sleep(0.002)
        time.sleep(0.5); f.close()
    threading.Thread(target=writer, daemon=True).start()
    src = LiveSource(os.fdopen(r, "rb", buffering=0))
    while not src.sound_recent(0.5): await asyncio.sleep(0.01)
    t0 = time.monotonic()
    sps = [FakeSpeaker("a"), FakeSpeaker("b")]
    why = await stream(src, sps, FakeClock(), asyncio.Event())
    print(f"  stream returned '{why}' after {time.monotonic() - t0:.2f}s (expect 'idle' after ~3 s)")
    print(f"  packets per speaker {[s.n for s in sps]}, late {stats['late_sends']}, underruns {stats['underruns']}, trims {stats['trims']}")

async def main():
    if DRY: return await dry_run()
    if not SPEAKERS and not FALLBACK_IPS:
        sys.exit("Set SPEAKERS (AirPlay names, e.g. SPEAKERS=\"Left,Right\") or FALLBACK_IPS first.")
    log(f"homepodd starting | speakers {SPEAKERS} | latency {LAT_MS} ms | idle release {IDLE_S:.0f} s")
    source = LiveSource()
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, stop.set)
    clock = PTPClock(); socks = []
    for port, cb in ((319, clock.on_event), (320, clock.on_general)):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
        s.bind(("0.0.0.0", port)); s.setblocking(False)
        loop.add_reader(s, cb, s); socks.append(s)
    ev_s, gen_s = socks
    log("idle - waiting for audio on the 'HomePods' output")
    while not stop.is_set() and not source.eof:
        if not source.sound_recent(0.5):
            await asyncio.sleep(0.05); continue
        why = await session(source, clock, ev_s, gen_s, stop)
        if why == "error": await asyncio.sleep(3)
        elif why == "idle": log("released the minis - waiting for audio")
    log("input ended" if source.eof else "stopping", stats)

asyncio.run(main())
sys.stdout.flush()
os._exit(0)   # skip interpreter teardown (daemon reader thread may still be blocked in read)
