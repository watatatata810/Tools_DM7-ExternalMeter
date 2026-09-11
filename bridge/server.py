"""DM7 External Meter bridge.

Connects to a Yamaha DM7 over Remote Control Protocol (TCP 49280), keeps
channel labels/colors in memory, and relays meter frames to browser clients
over WebSocket. Serves the web UI from ../web.

usage: python server.py [--host 192.168.1.121] [--port 8000] [--interval 50]
                        [--bind-ip <local IPv4 used to reach the DM7>] [--listen <IPv4 for the web UI>]
       python server.py --list-nics | --scan

Without --host the bridge scans the local subnet(s) for consoles answering on
TCP 49280 (auto-discovery). One hit -> connect; several -> the web UI asks.
The chosen host is remembered in bridge/config.json.

The DM7 stops a meter stream 10 s after mtrstart (measured 2026-09-11), so active
subscriptions are re-sent every METER_KEEPALIVE_S seconds.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("bridge")
HERE = Path(__file__).resolve().parent
FROZEN = getattr(sys, "frozen", False)
# PyInstaller one-file: bundled data lives under sys._MEIPASS; config sits next to the executable
RES = Path(getattr(sys, "_MEIPASS", HERE.parent)) if FROZEN else HERE.parent
WEB = RES / "web"
TABLE = RES / "tools" / "dm7_levelwt_table.json"
CONFIG = (Path(sys.executable).resolve().parent if FROZEN else HERE) / "config.json"

RCP_PORT = 49280
# Meter sources exposed by the DM7 (from DM7 RCP mtrinfo, verified 2026-09-11)
SOURCES = {
    "InCh": {"count": 72, "pickoffs": ["PreHPF", "PreFader", "PostOn"]},
    "Mix": {"count": 48, "pickoffs": ["PreEQ", "PreFader", "PostOn"]},
    "Mtrx": {"count": 12, "pickoffs": ["PreEQ", "PreFader", "PostOn"]},
    "St": {"count": 4, "pickoffs": ["PreEQ", "PreFader", "PostOn"]},
}
LABEL_PARAMS = ["Label/Name", "Label/Color", "Fader/On"]
METER_KEEPALIVE_S = 5      # DM7 auto-stops a meter stream 10 s after mtrstart
RX_TIMEOUT_S = 15          # no bytes from the console for this long -> reconnect
SCAN_TIMEOUT_S = 0.6       # per-host TCP connect timeout during discovery
SCAN_TOTAL_S = 8.0         # whole discovery is cut off after this (partial results are kept)
SCAN_CONCURRENCY = 128
RECONNECT_DELAYS_S = (2, 4, 7, 10)   # back-off between reconnect attempts (last value repeats)
OFFLINE_RESCAN_S = 30      # offline this long -> rescan and let the UI offer the list (never auto-switch)

RE_GET = re.compile(r'^OK get MIXER:Current/(\w+)/([\w/]+) (\d+) (\d+) (.*)$')
RE_NOTIFY_SET = re.compile(r'^NOTIFY set MIXER:Current/(\w+)/([\w/]+) (\d+) (\d+) (.*)$')
RE_QUOTED = re.compile(r'^"((?:[^"\\]|\\.)*)"')


def parse_value(raw: str):
    """First value of an RCP reply. `OK set`/`NOTIFY set` repeat the value twice
    (e.g. `... 23 0 "Red" "Red"`), so only the leading token is taken."""
    raw = raw.strip()
    m = RE_QUOTED.match(raw)
    if m:
        return m.group(1).replace('\\"', '"')
    tok = raw.split(" ", 1)[0]
    try:
        return int(tok)
    except ValueError:
        return tok


# ---------------------------------------------------------------- discovery
def local_networks(bind_ip: str | None = None) -> list[ipaddress.IPv4Network]:
    """IPv4 networks of this machine (psutil gives real prefixes; fallback assumes /24).
    Loopback and link-local (169.254.x) are skipped; with bind_ip only that NIC's network is used.
    Scans are bounded to /22 (1022 hosts) at most."""
    nets: dict[str, ipaddress.IPv4Network] = {}
    try:
        import psutil
        for _name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET and a.netmask:
                    nets[a.address] = ipaddress.IPv4Interface(f"{a.address}/{a.netmask}").network
    except ImportError:
        for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = ai[4][0]
            nets[ip] = ipaddress.IPv4Interface(f"{ip}/24").network
    out = []
    for ip, net in nets.items():
        if bind_ip and ip != bind_ip:
            continue
        if net.is_loopback or net.is_link_local:
            continue
        if net.prefixlen < 22:
            net = ipaddress.IPv4Interface(f"{ip}/22").network
        out.append(net)
    return out


async def probe_console(ip: str, bind_ip: str | None, timeout: float = SCAN_TIMEOUT_S) -> dict | None:
    kw = {"local_addr": (bind_ip, 0)} if bind_ip else {}
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(ip, RCP_PORT, **kw), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    info = {"ip": ip}
    try:
        w.write(b"devinfo productname\ndevinfo devicename\n")
        await w.drain()
        for _ in range(2):
            line = (await asyncio.wait_for(r.readline(), timeout=1.5)).decode(errors="replace").strip()
            if line.startswith("OK devinfo "):
                _, _, key, val = line.split(" ", 3)
                info[key] = parse_value(val)
    except (OSError, asyncio.TimeoutError, ValueError):
        pass
    finally:
        w.close()
    return info if "productname" in info else None


async def scan_consoles(bind_ip: str | None = None) -> list[dict]:
    nets = local_networks(bind_ip)
    hosts = [str(h) for net in nets for h in net.hosts()]
    log.info("scanning %d hosts on %s for TCP %d", len(hosts), [str(n) for n in nets], RCP_PORT)
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def one(ip):
        async with sem:
            return await probe_console(ip, bind_ip)

    t0 = time.time()
    tasks = [asyncio.ensure_future(one(ip)) for ip in hosts]
    done, pending = await asyncio.wait(tasks, timeout=SCAN_TOTAL_S) if tasks else (set(), set())
    for p in pending:
        p.cancel()
    found = [d for f in done if not f.cancelled() and f.exception() is None and (d := f.result())]
    found.sort(key=lambda d: ipaddress.IPv4Address(d["ip"]))
    log.info("scan done in %.1fs%s: %d console(s) %s", time.time() - t0,
             " (cut off, %d hosts unanswered)" % len(pending) if pending else "", len(found), found)
    return found


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict):
    try:
        CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning("cannot save config: %s", e)


# ---------------------------------------------------------------- bridge
class DM7Bridge:
    def __init__(self, host: str | None, rcp_port: int, interval_ms: int, bind_ip: str | None = None,
                 fixed_host: bool = False):
        self.host, self.rcp_port, self.interval_ms, self.bind_ip = host, rcp_port, interval_ms, bind_ip
        self.fixed_host = fixed_host          # --host given: no discovery, no switching
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.connected = False
        self.device = {}
        self.devices: list[dict] = []         # last discovery result
        self.scanning = False
        self.offline_since: float | None = time.monotonic()   # None while connected
        self.attempts = 0
        self.offline_rescan_at: float | None = None
        self.last_error: str | None = None
        self.host_changed = asyncio.Event()
        # meta[source][param] = list of values indexed by channel
        self.meta = {s: {p: [None] * d["count"] for p in LABEL_PARAMS} for s, d in SOURCES.items()}
        self.clients: dict[WebSocket, set[str]] = {}
        self.active: set[str] = set()  # meter addresses currently started on the console
        self.last: dict[str, list[int]] = {}

    # ---------- discovery / target ----------
    def status(self) -> dict:
        offline_for = None if self.connected or self.offline_since is None else round(time.monotonic() - self.offline_since, 1)
        devices = self.devices
        if self.connected and self.host and not any(d["ip"] == self.host for d in devices):
            # remembered/fixed host without a scan yet: list at least the console we are on
            devices = [{"ip": self.host, **{k: v for k, v in self.device.items() if k in ("productname", "devicename")}}] + devices
        return {"connected": self.connected, "host": self.host, "fixed": self.fixed_host,
                "scanning": self.scanning, "devices": devices, "device": self.device,
                "offline_for": offline_for, "attempts": self.attempts, "last_error": self.last_error,
                "bind_ip": self.bind_ip, "bind_ip_present": (self.bind_ip is None) or any(ip == self.bind_ip for _n, ip in list_nics()),
                "offline_long": bool(self.host and not self.connected and offline_for is not None and offline_for >= OFFLINE_RESCAN_S)}

    async def push_status(self):
        await self.broadcast({"type": "status", **self.status()})

    async def scan(self) -> list[dict]:
        if self.scanning:
            return self.devices
        self.scanning = True
        await self.push_status()
        try:
            self.devices = await scan_consoles(self.bind_ip)
        finally:
            self.scanning = False
        await self.push_status()
        return self.devices

    async def set_host(self, host: str | None):
        if self.fixed_host or host == self.host:
            return
        self.host = host
        self.offline_since = time.monotonic()
        self.attempts = 0
        self.offline_rescan_at = None
        cfg = load_config()
        cfg["host"] = host
        save_config(cfg)
        self.host_changed.set()           # wakes run(); a live session notices the change and drops
        if self.writer:
            self.writer.close()
        await self.push_status()

    async def _auto_select(self):
        """No host yet: scan; connect if exactly one console answers."""
        await self.scan()
        if len(self.devices) == 1 and not self.host:
            log.info("auto-selecting %s", self.devices[0])
            await self.set_host(self.devices[0]["ip"])

    # ---------- RCP connection ----------
    async def run(self):
        while True:
            if not self.host:
                await self._auto_select()
                if not self.host:
                    try:
                        await asyncio.wait_for(self.host_changed.wait(), timeout=20)
                    except asyncio.TimeoutError:
                        pass
                    self.host_changed.clear()
                    continue
            self.host_changed.clear()
            try:
                await self._session()
            except (OSError, asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError) as e:
                log.warning("RCP connection lost: %s", e)
                self.last_error = (f"NIC {self.bind_ip} が見つかりません (ケーブル / Wi-Fi / IP 設定を確認)" if isinstance(e, OSError) and getattr(e, "errno", None) in (10049, 99, 49)   # EADDRNOTAVAIL: Windows / Linux / macOS
                                   else "接続がタイムアウトしました" if isinstance(e, asyncio.TimeoutError) else str(e))
            if self.connected or self.offline_since is None:
                self.offline_since = time.monotonic()
            self.connected = False
            self.active.clear()
            self.device = {}
            self.attempts += 1
            await self.push_status()
            # long offline: rescan so the UI can show what is reachable (no automatic switch)
            off = time.monotonic() - self.offline_since
            if self.host and off >= OFFLINE_RESCAN_S and (self.offline_rescan_at is None or time.monotonic() - self.offline_rescan_at > 60):
                self.offline_rescan_at = time.monotonic()
                asyncio.create_task(self.scan())
            delay = RECONNECT_DELAYS_S[min(self.attempts - 1, len(RECONNECT_DELAYS_S) - 1)]
            try:
                await asyncio.wait_for(self.host_changed.wait(), timeout=delay)   # a new target skips the wait
            except asyncio.TimeoutError:
                pass

    async def _session(self):
        host = self.host
        log.info("connecting to %s:%d%s", host, self.rcp_port, f" via {self.bind_ip}" if self.bind_ip else "")
        kw = {"local_addr": (self.bind_ip, 0)} if self.bind_ip else {}
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(host, self.rcp_port, **kw), timeout=5)
        self.connected = True
        self.last_error = None
        self.offline_since = None
        self.attempts = 0
        self.offline_rescan_at = None
        keepalive = asyncio.create_task(self._keepalive())
        for k in ("productname", "devicename", "protocolver"):
            await self.send(f"devinfo {k}")
        await self.request_all_meta()
        await self.push_status()
        await self.sync_subscriptions()
        try:
            while True:
                if self.host != host:
                    raise ConnectionError("target changed")
                try:
                    line = await asyncio.wait_for(self.reader.readline(), timeout=RX_TIMEOUT_S)
                except asyncio.TimeoutError:
                    raise ConnectionError(f"no data from console for {RX_TIMEOUT_S}s")
                if not line:
                    raise ConnectionError("EOF from console")
                await self.handle_line(line.decode(errors="replace").rstrip("\r\n"))
        finally:
            keepalive.cancel()
            self.writer.close()

    async def _keepalive(self):
        """Re-arm active meter streams before the console's 10 s auto-stop; ping when idle."""
        while True:
            await asyncio.sleep(METER_KEEPALIVE_S)
            if self.active:
                for addr in list(self.active):
                    await self.send(f"mtrstart MIXER:Current/{addr} {self.interval_ms}")
            else:
                await self.send("devinfo productname")

    async def send(self, cmd: str):
        if self.writer is None or self.writer.is_closing():
            return
        self.writer.write((cmd + "\n").encode())
        await self.writer.drain()

    async def request_all_meta(self):
        for src, d in SOURCES.items():
            for p in LABEL_PARAMS:
                cmds = "".join(f"get MIXER:Current/{src}/{p} {i} 0\n" for i in range(d["count"]))
                self.writer.write(cmds.encode())
        await self.writer.drain()

    async def handle_line(self, line: str):
        if line.startswith("NOTIFY mtr "):
            # NOTIFY mtr MIXER:Current/InCh/PostOn levelwt 03 03 ...
            parts = line.split()
            addr = parts[2].replace("MIXER:Current/", "")
            vals = [int(x, 16) for x in parts[4:]]
            self.last[addr] = vals
            await self.broadcast_meter(addr, vals)
            return
        m = RE_GET.match(line) or RE_NOTIFY_SET.match(line)
        if m:
            src, param, x, _y, raw = m.groups()
            if src in self.meta and param in self.meta[src]:
                self.meta[src][param][int(x)] = parse_value(raw)
                if line.startswith("NOTIFY"):
                    await self.broadcast({"type": "meta", "meta": self.meta})
            return
        if line.startswith("NOTIFY sscurrent") or line.startswith("NOTIFY ssrecall"):
            # scene recall changes many labels at once without per-parameter notifies -> refetch
            await self.request_all_meta()
            asyncio.get_running_loop().call_later(
                1.5, lambda: asyncio.ensure_future(self.broadcast({"type": "meta", "meta": self.meta})))
            return
        if line.startswith("OK devinfo "):
            _, _, key, val = line.split(" ", 3)
            self.device[key] = parse_value(val)
            if key == "devicename":
                await self.push_status()
            return
        if line.startswith("ERROR"):
            log.warning("console: %s", line)

    # ---------- subscriptions ----------
    def wanted(self) -> set[str]:
        return set().union(*self.clients.values()) if self.clients else set()

    async def sync_subscriptions(self):
        if not self.connected:
            return
        want = self.wanted()
        for addr in want - self.active:
            await self.send(f"mtrstart MIXER:Current/{addr} {self.interval_ms}")
        for addr in self.active - want:
            await self.send(f"mtrstop MIXER:Current/{addr}")
        self.active = set(want)

    # ---------- websocket ----------
    async def add_client(self, ws: WebSocket):
        self.clients[ws] = set()
        await ws.send_json({"type": "hello", "sources": SOURCES, "interval": self.interval_ms, **self.status()})
        await ws.send_json({"type": "meta", "meta": self.meta})

    async def remove_client(self, ws: WebSocket):
        self.clients.pop(ws, None)
        await self.sync_subscriptions()

    async def set_client_subs(self, ws: WebSocket, addrs: list[str]):
        valid = {f"{s}/{p}" for s, d in SOURCES.items() for p in d["pickoffs"]}
        self.clients[ws] = {a for a in addrs if a in valid}
        await self.sync_subscriptions()
        for addr in self.clients[ws]:
            if addr in self.last:
                await ws.send_json({"type": "mtr", "addr": addr, "v": self.last[addr]})

    async def broadcast(self, msg: dict):
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                self.clients.pop(ws, None)

    async def broadcast_meter(self, addr: str, vals: list[int]):
        payload = json.dumps({"type": "mtr", "addr": addr, "v": vals})
        for ws, subs in list(self.clients.items()):
            if addr in subs:
                try:
                    await ws.send_text(payload)
                except Exception:
                    self.clients.pop(ws, None)


def build_app(bridge: DM7Bridge) -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(bridge.run())
        yield
        task.cancel()

    app = FastAPI(lifespan=lifespan)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        await bridge.add_client(ws)
        try:
            while True:
                msg = await ws.receive_json()
                t = msg.get("type")
                if t == "subscribe":
                    await bridge.set_client_subs(ws, msg.get("addrs", []))
                elif t == "scan":
                    asyncio.create_task(bridge.scan())
                elif t == "select":
                    await bridge.set_host(msg.get("host") or None)
        except WebSocketDisconnect:
            pass
        finally:
            await bridge.remove_client(ws)

    @app.get("/api/status")
    async def api_status():
        return JSONResponse(bridge.status())

    @app.post("/api/scan")
    async def api_scan():
        return JSONResponse(await bridge.scan())

    @app.get("/table.json")
    async def table():
        return FileResponse(TABLE)

    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
    return app


def list_nics() -> list[tuple[str, str]]:
    """(interface name, IPv4) pairs. Uses psutil when available, else a hostname lookup."""
    try:
        import psutil
        return [(name, a.address) for name, addrs in psutil.net_if_addrs().items()
                for a in addrs if a.family == socket.AF_INET]
    except ImportError:
        return [("?", ai[4][0]) for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-nics", action="store_true", help="print local IPv4 addresses and exit")
    ap.add_argument("--scan", action="store_true", help="scan for consoles, print them and exit")
    ap.add_argument("--bind-ip", default=os.environ.get("DM7_BIND_IP"),
                    help="local IPv4 to use as the source of the DM7 connection (pins the NIC; also limits the scan)")
    ap.add_argument("--listen", default=os.environ.get("LISTEN", "0.0.0.0"),
                    help="IPv4 the web UI listens on (127.0.0.1 = this PC only)")
    ap.add_argument("--host", default=os.environ.get("DM7_HOST"),
                    help="DM7 IPv4. Omit to auto-discover (last choice is remembered in bridge/config.json)")
    ap.add_argument("--forget", action="store_true", help="ignore the remembered host and rescan")
    ap.add_argument("--rcp-port", type=int, default=RCP_PORT)
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--interval", type=int, default=50, help="meter interval ms (40-1000)")
    ap.add_argument("--no-browser", action="store_true", help="do not open the web UI in the default browser")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.list_nics:
        for name, ip in list_nics():
            print(f"{ip:16s} {name}")
        return
    if args.scan:
        for d in asyncio.run(scan_consoles(args.bind_ip)):
            print(f"{d['ip']:16s} {d.get('productname', '?'):8s} {d.get('devicename', '')}")
        return
    host = args.host
    fixed = bool(host)
    if not host and not args.forget:
        host = load_config().get("host")
    bridge = DM7Bridge(host, args.rcp_port, args.interval, args.bind_ip, fixed_host=fixed)
    url = f"http://{'127.0.0.1' if args.listen in ('0.0.0.0', '') else args.listen}:{args.port}/"
    log.info("web UI on %s  (target: %s)", url, host or "auto-discover")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(build_app(bridge), host=args.listen, port=args.port, log_level="warning")
    except OSError as e:
        log.error("cannot listen on %s:%d (%s). Another instance running? Use --port to change.", args.listen, args.port, e)
        if FROZEN:
            input("Press Enter to close...")


if __name__ == "__main__":
    main()
