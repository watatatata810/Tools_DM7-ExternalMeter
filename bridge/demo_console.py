"""Simulated DM7 consoles for `server.py --demo` (UI / layout work without a console).

Each FakeConsole is a TCP server on 127.0.0.1 that speaks the subset of the DM7
Remote Control Protocol the bridge uses, so the bridge's real session, keepalive,
reconnect and discovery code paths all run unchanged:

  devinfo productname|devicename|protocolver  -> OK devinfo <key> "<value>"
  get MIXER:Current/<src>/<param> <x> 0        -> OK get MIXER:Current/<src>/<param> <x> 0 <value>
  mtrstart MIXER:Current/<src>/<pick> <ms>     -> NOTIFY mtr ... levelwt <hex> ... every max(ms, 50) ms,
                                                  stopping 10 s after the last mtrstart (as the DM7 does)
  mtrstop MIXER:Current/<src>/<pick>

Label changes are pushed as `NOTIFY set ... <value> <value>` (the value twice, as the
DM7 does). Display IPs are TEST-NET-1 (192.0.2.x, RFC 5737) so they can never hit
a real device; server.py maps them to the local ports.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from pathlib import Path

log = logging.getLogger("demo")

MTR_AUTO_STOP_S = 10.0     # DM7 stops a meter stream 10 s after mtrstart (measured 2026-09-11)
MTR_MIN_INTERVAL_MS = 50   # 40 ms requested -> ~50 ms measured on the DM7
FLOOR_RAW = 0x03           # -145 dB, what an idle / muted channel reports
OVER_RAW = 0xFF
PATTERNS = ("music", "static", "sweep", "silent")
STATES = ("online", "off", "unreachable")   # off = connection refused, unreachable = connect times out
COLORS = ["Blue", "Orange", "Yellow", "Purple", "Cyan", "Magenta", "Red", "Green", "White", "Pink", "SkyBlue", "Off"]

# (name, colour, role) — roles drive the "music" pattern. Empty names exercise the UI fallback ("IN 12").
_INCH = [
    ("Kick In", "Red", "kick"), ("Kick Out", "Red", "kick"), ("Snare Top", "Red", "snare"), ("Snare Btm", "Red", "snare"),
    ("Hi-Hat", "Orange", "hat"), ("Tom 1", "Orange", "tom"), ("Tom 2", "Orange", "tom"), ("Floor Tom", "Orange", "tom"),
    ("OH L", "Orange", "cymbal"), ("OH R", "Orange", "cymbal"), ("Bass DI", "Purple", "bass"), ("Bass Mic", "Purple", "bass"),
    ("E.Gt 1", "Green", "gtr"), ("E.Gt 2", "Green", "gtr"), ("A.Gt", "Green", "gtr"), ("Keys L", "Yellow", "keys"),
    ("Keys R", "Yellow", "keys"), ("Synth", "Yellow", "keys"), ("Vo Main", "Blue", "vocal"), ("Cho 1", "SkyBlue", "vocal"),
    ("Cho 2", "SkyBlue", "vocal"), ("Wireless MC Handheld 1", "Cyan", "speech"), ("Wireless MC Handheld 2", "Cyan", "speech"),
    ("Lavalier Presenter", "Cyan", "speech"), ("Click", "White", "click"), ("Talkback", "Magenta", "silent"),
    ("PC Audio L", "Pink", "keys"), ("PC Audio R", "Pink", "keys"), ("", "Off", "silent"), ("", "Off", "silent"),
]
_MIX = ["Mon Vo", "Mon Gt", "Mon Bass", "Mon Keys", "Mon Drum", "IEM Vo L", "IEM Vo R", "IEM Gt L", "IEM Gt R",
        "Side Fill L", "Side Fill R", "FX Rev", "FX Delay", "Sub Group Drums", "Sub Group Vocals", "Record Feed L", "Record Feed R"]
_MTRX = ["Main L", "Main R", "Sub", "Front Fill", "Delay 1", "Delay 2", "Lobby", "Broadcast L", "Broadcast R"]
_ST = ["Stereo L", "Stereo R", "Mono", ""]


def _load_table(path: Path) -> list[tuple[int, float]]:
    t = json.loads(path.read_text(encoding="utf-8"))
    return sorted((int(k), v) for k, v in t.items() if int(k) < OVER_RAW)   # 255 maps to the OVER sentinel (1000)


class World:
    """State shared by all fake consoles: signal pattern, channel labels, label churn."""

    def __init__(self, table_path: Path, sources: dict, pattern: str = "music"):
        self.sources = sources
        self.table = _load_table(table_path)
        self.pattern = pattern if pattern in PATTERNS else "music"
        self.auto_labels = False
        self.t0 = time.monotonic()
        self.rng = random.Random(7)
        self.meta = self._initial_meta()

    # ---------- labels ----------
    def _initial_meta(self) -> dict:
        meta: dict = {}
        for src, d in self.sources.items():
            n = d["count"]
            names, colors, roles, on = [], [], [], []
            for i in range(n):
                if src == "InCh":
                    name, col, role = _INCH[i] if i < len(_INCH) else ("", "Off", "silent")
                elif src == "Mix":
                    name, col, role = (_MIX[i], COLORS[i % 8], "bus") if i < len(_MIX) else ("", "Off", "silent")
                elif src == "Mtrx":
                    name, col, role = (_MTRX[i], "Green", "bus") if i < len(_MTRX) else ("", "Off", "silent")
                else:
                    name, col, role = (_ST[i], "White", "bus") if _ST[i] else ("", "Off", "silent")
                names.append(name); colors.append(col); roles.append(role)
                on.append(0 if role == "silent" and src == "InCh" else 1)   # OFF channels: PostOn stays at the floor, like the DM7
            meta[src] = {"Label/Name": names, "Label/Color": colors, "Fader/On": on, "_role": roles}
        return meta

    def value(self, src: str, param: str, x: int):
        return self.meta[src][param][x]

    def random_change(self) -> list[tuple[str, str, int, object]]:
        """Rename / recolour one channel that is likely on screen (low InCh / St / Mix numbers)."""
        src = self.rng.choice(["InCh", "InCh", "InCh", "Mix", "St"])
        x = self.rng.randrange(min(12, self.sources[src]["count"]))
        if self.rng.random() < 0.5:
            cur = self.meta[src]["Label/Color"][x]
            new = self.rng.choice([c for c in COLORS if c != cur])
            self.meta[src]["Label/Color"][x] = new
            return [(src, "Label/Color", x, new)]
        base = self.meta[src]["Label/Name"][x].split(" #")[0] or f"{src} {x + 1}"
        new = f"{base} #{self.rng.randrange(100)}"
        self.meta[src]["Label/Name"][x] = new
        return [(src, "Label/Name", x, new)]

    def scene_recall(self):
        """Swap every name to an alternate set (as a scene recall would)."""
        for src, d in self.meta.items():
            d["Label/Name"] = [(f"{nm} (B)" if nm and not nm.endswith(" (B)") else nm[:-4] if nm.endswith(" (B)") else nm)
                               for nm in d["Label/Name"]]

    # ---------- levels ----------
    def db_to_raw(self, db: float) -> int:
        best = FLOOR_RAW
        for raw, v in self.table:
            if v <= db and raw > best:
                best = raw
        return best

    def frame(self, src: str, pick: str) -> list[int]:
        n = self.sources[src]["count"]
        t = time.monotonic() - self.t0
        on = self.meta[src]["Fader/On"]
        roles = self.meta[src]["_role"]
        # pickoff offsets: HPF removes some low end, the fader trims a little more
        off = {"PreHPF": 2.0, "PreEQ": 1.0, "PreFader": 0.0, "PostOn": -4.0}.get(pick, 0.0) if self.pattern != "static" else 0.0
        out = []
        for i in range(n):
            if pick == "PostOn" and not on[i]:
                out.append(FLOOR_RAW); continue
            db = self._level(src, i, roles[i], t)
            if db is None:
                out.append(OVER_RAW); continue
            out.append(self.db_to_raw(min(db + off, 0.0)) if db > -140 else FLOOR_RAW)
        return out

    def _level(self, src: str, i: int, role: str, t: float) -> float | None:
        """dB for one channel; None = OVER."""
        if self.pattern == "silent":
            return -145.0
        if self.pattern == "static":
            # fixed ladder across the whole scale, including the colour thresholds and OVER (screenshot-stable)
            ladder = [-3.0, -9.0, -15.0, -18.0, -24.0, -30.0, -40.0, -50.0, -58.0, -70.0, -6.0, -12.0, 0.0, None]
            return ladder[(i + {"InCh": 0, "Mix": 3, "Mtrx": 6, "St": 1}[src]) % len(ladder)]
        if self.pattern == "sweep":
            # every channel ramps -60 -> OVER over 12 s, slightly staggered so peaks and colours can be followed
            ph = ((t + i * 0.25) % 12.0) / 12.0
            db = -60.0 + ph * 63.0
            return None if db > 0.5 else db
        return self._music(role, i, t)

    def _music(self, role: str, i: int, t: float) -> float | None:
        beat = 0.5                                     # 120 BPM
        pb = (t % beat) / beat                         # phase within the beat
        bar = int(t / beat) % 4
        wob = 2.0 * math.sin(t * (0.9 + i * 0.11) + i)
        if role == "silent":
            return -145.0
        if role == "kick":
            return -4.0 - 50.0 * pb + wob if pb < 0.6 else -70.0
        if role == "snare":
            return (-6.0 - 40.0 * pb + wob) if bar in (1, 3) and pb < 0.7 else -62.0
        if role == "hat":
            p8 = (t % (beat / 2)) / (beat / 2)
            return -16.0 - 25.0 * p8 + wob
        if role == "tom":
            return (-8.0 - 40.0 * pb) if int(t / 2) % 4 == i % 4 and pb < 0.6 else -64.0
        if role == "cymbal":
            return -20.0 - 6.0 * pb + wob
        if role == "click":
            return -12.0 - 60.0 * pb if pb < 0.15 else -90.0
        if role in ("vocal", "speech"):
            phrase = math.sin(t * 0.37 + i) > (-0.2 if role == "vocal" else 0.1)   # phrases with breaths / pauses
            if not phrase:
                return -75.0
            syl = abs(math.sin(t * (5.5 if role == "vocal" else 4.0) + i * 1.7))
            db = -24.0 + 16.0 * syl + wob
            if i == 18 and (t % 9.0) < 0.35:           # Vo Main clips briefly every 9 s
                return None
            return db
        if role in ("bass", "keys", "gtr"):
            return -18.0 + 5.0 * math.sin(t * 1.3 + i) + 3.0 * abs(math.sin(t * 7.0 + i)) + wob
        # buses / matrices / stereo: a mix of everything
        return -14.0 + 4.0 * math.sin(t * 0.8 + i * 0.7) + 3.0 * abs(math.sin(t * 6.1 + i)) + wob


class FakeConsole:
    def __init__(self, world: World, display_ip: str, productname: str, devicename: str):
        self.world, self.ip = world, display_ip
        self.info = {"productname": productname, "devicename": devicename, "protocolver": "1.4.0"}
        self.state = "online"
        self.port: int | None = None
        self.server: asyncio.base_events.Server | None = None
        self.conns: set[_Conn] = set()

    def describe(self) -> dict:
        return {"ip": self.ip, "port": self.port, "state": self.state, **self.info}

    async def start(self):
        if self.server:
            return
        # keep the same port across power cycles so the bridge's reconnect target stays valid
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", self.port or 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop_listening(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def set_state(self, state: str):
        if state not in STATES or state == self.state:
            return
        self.state = state
        log.info("demo console %s (%s) -> %s", self.info["devicename"], self.ip, state)
        if state == "online":
            await self.start()
            return
        await self.stop_listening()
        for c in list(self.conns):
            c.close(abrupt=(state == "off"))     # off: EOF right away; unreachable: silence (bridge RX timeout)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        c = _Conn(self, reader, writer)
        self.conns.add(c)
        try:
            await c.run()
        finally:
            self.conns.discard(c)

    async def broadcast(self, line: str):
        for c in list(self.conns):
            await c.send(line)


class _Conn:
    def __init__(self, console: FakeConsole, reader, writer):
        self.console, self.reader, self.writer = console, reader, writer
        self.streams: dict[str, asyncio.Task] = {}
        self.stream_until: dict[str, float] = {}
        self.silent = False

    async def send(self, line: str):
        if self.silent or self.writer.is_closing():
            return
        try:
            self.writer.write((line + "\n").encode())
            await self.writer.drain()
        except (ConnectionError, OSError):
            self.close(abrupt=True)

    def close(self, abrupt: bool):
        for t in self.streams.values():
            t.cancel()
        self.streams.clear()
        if abrupt:
            self.writer.close()
        else:
            self.silent = True        # connection stays up but nothing arrives, like a pulled cable

    async def run(self):
        try:
            while True:
                line = await self.reader.readline()
                if not line:
                    break
                if not self.silent:
                    await self.handle(line.decode(errors="replace").strip())
        except (ConnectionError, OSError):
            pass
        finally:
            self.close(abrupt=True)

    async def handle(self, line: str):
        parts = line.split()
        if not parts:
            return
        cmd = parts[0]
        if cmd == "devinfo" and len(parts) >= 2:
            val = self.console.info.get(parts[1])
            if val is not None:
                await self.send(f'OK devinfo {parts[1]} "{val}"')
            return
        if cmd == "get" and len(parts) >= 4 and parts[1].startswith("MIXER:Current/"):
            src, _, param = parts[1][len("MIXER:Current/"):].partition("/")
            meta = self.console.world.meta.get(src)
            x = int(parts[2])
            if meta and param in meta and 0 <= x < len(meta[param]):
                await self.send(f"OK get {parts[1]} {x} {parts[3]} {_fmt(meta[param][x])}")
            else:
                await self.send("ERROR get InvalidArgument")   # format unverified; the bridge only logs ERROR lines
            return
        if cmd == "mtrstart" and len(parts) >= 2:
            addr = parts[1].replace("MIXER:Current/", "")
            interval = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 100
            self.stream_until[addr] = time.monotonic() + MTR_AUTO_STOP_S
            if addr not in self.streams:
                self.streams[addr] = asyncio.create_task(self._stream(addr, max(interval, MTR_MIN_INTERVAL_MS) / 1000))
            return
        if cmd == "mtrstop" and len(parts) >= 2:
            addr = parts[1].replace("MIXER:Current/", "")
            t = self.streams.pop(addr, None)
            if t:
                t.cancel()
            return

    async def _stream(self, addr: str, period: float):
        src, _, pick = addr.partition("/")
        if src not in self.console.world.sources:
            return
        try:
            while time.monotonic() < self.stream_until.get(addr, 0):
                vals = self.console.world.frame(src, pick)
                await self.send(f"NOTIFY mtr MIXER:Current/{addr} levelwt " + " ".join(f"{v:02x}" for v in vals))
                await asyncio.sleep(period)
        finally:
            if self.streams.get(addr) is asyncio.current_task():
                self.streams.pop(addr, None)


def _fmt(v) -> str:
    return f'"{v}"' if isinstance(v, str) else str(v)


class DemoRig:
    """The fake consoles + the knobs the control page turns."""

    def __init__(self, table_path: Path, sources: dict, pattern: str = "music"):
        self.world = World(table_path, sources, pattern)
        self.consoles = [
            FakeConsole(self.world, "192.0.2.11", "DM7", "DEMO-FOH"),
            FakeConsole(self.world, "192.0.2.12", "DM7", "DEMO-MON"),
        ]
        self._auto_task: asyncio.Task | None = None

    async def start(self):
        for c in self.consoles:
            await c.start()
            log.info("demo console %s listening on 127.0.0.1:%d as %s", c.info["devicename"], c.port, c.ip)

    def by_ip(self, ip: str | None) -> FakeConsole | None:
        return next((c for c in self.consoles if c.ip == ip), None)

    def state(self) -> dict:
        return {"pattern": self.world.pattern, "patterns": list(PATTERNS), "auto_labels": self.world.auto_labels,
                "consoles": [c.describe() for c in self.consoles]}

    async def notify_changes(self, changes):
        for src, param, x, val in changes:
            line = f"NOTIFY set MIXER:Current/{src}/{param} {x} 0 {_fmt(val)} {_fmt(val)}"
            for c in self.consoles:
                await c.broadcast(line)

    async def label_change(self):
        await self.notify_changes(self.world.random_change())

    async def scene_recall(self):
        self.world.scene_recall()
        for c in self.consoles:
            await c.broadcast("NOTIFY sscurrent_ex MIXER:Lib/Scene 0 0")   # format unverified; the bridge only checks startswith

    def set_auto_labels(self, on: bool):
        self.world.auto_labels = on
        if on and not self._auto_task:
            self._auto_task = asyncio.create_task(self._auto_loop())
        elif not on and self._auto_task:
            self._auto_task.cancel()
            self._auto_task = None

    async def _auto_loop(self):
        while True:
            await asyncio.sleep(5)
            await self.label_change()
