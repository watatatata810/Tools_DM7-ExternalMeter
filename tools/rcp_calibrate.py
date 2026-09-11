"""DM7 meter calibration: sample all InCh meters, auto-detect the hottest channel, log raw vs expected dB.
usage: python rcp_calibrate.py <expected_dBFS> [channel(1-based)]
"""
import socket, time, json, os, sys, statistics, csv, datetime
HOST, PORT = "192.168.1.121", 49280
HERE = os.path.dirname(os.path.abspath(__file__))
tbl = json.load(open(os.path.join(HERE, "dm7_levelwt_table.json")))
expected = float(sys.argv[1]); fixed_ch = int(sys.argv[2]) if len(sys.argv) > 2 else None
PICK = ["PreHPF", "PreFader", "PostOn"]
s = socket.create_connection((HOST, PORT), timeout=5); s.settimeout(0.2)
for p in PICK: s.sendall(f"mtrstart MIXER:Current/InCh/{p} 50\n".encode())
buf = b""; t0 = time.time(); samples = {p: [] for p in PICK}
while time.time() - t0 < 2.5:
    try:
        d = s.recv(65536)
        if not d: break
        buf += d
    except socket.timeout: pass
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1); f = line.decode().split()
        if len(f) > 4 and f[1] == "mtr":
            samples[f[2].rsplit("/", 1)[1]].append([int(x, 16) for x in f[4:]])
for p in PICK: s.sendall(f"mtrstop MIXER:Current/InCh/{p}\n".encode())
time.sleep(0.2); s.close()
pre = samples["PreHPF"]
if not pre: sys.exit("no PreHPF frames received")
n = len(pre[0]); med = [int(statistics.median(r[i] for r in pre)) for i in range(n)]
ch = fixed_ch - 1 if fixed_ch else max(range(n), key=lambda i: med[i])
print(f"expected {expected:+.1f} dBFS  -> detected ch{ch+1} (PreHPF median raw {med[ch]}); frames={len(pre)}")
row = {"time": datetime.datetime.now().isoformat(timespec="seconds"), "expected_dB": expected, "ch": ch + 1}
for p in PICK:
    col = [r[ch] for r in samples[p]]
    mode = statistics.mode(col); lo, hi = min(col), max(col)
    print(f"  {p:9s} raw mode {mode:3d} (0x{mode:02x}, range {lo}-{hi})  table -> {tbl[str(mode)]:+.1f} dB  diff {tbl[str(mode)]-expected:+.1f}")
    row[f"{p}_raw"] = mode; row[f"{p}_tbl"] = tbl[str(mode)]
others = sorted(((med[i], i + 1) for i in range(n) if i != ch), reverse=True)[:3]
print("  next hottest channels (PreHPF median raw):", others)
log = os.path.join(HERE, "calibration_log.csv"); new = not os.path.exists(log)
with open(log, "a", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(row)); (w.writeheader() if new else None); w.writerow(row)
