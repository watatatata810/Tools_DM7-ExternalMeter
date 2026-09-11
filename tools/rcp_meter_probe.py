import socket, time, json, statistics, datetime
import os
tbl = json.load(open(os.path.join(os.path.dirname(__file__), "dm7_levelwt_table.json")))
s = socket.create_connection(("192.168.1.121", 49280), timeout=5); s.settimeout(0.2)
print("start", datetime.datetime.now().strftime("%H:%M:%S.%f"))
s.sendall(b"mtrstart MIXER:Current/InCh/PostOn 50\nmtrstart MIXER:Current/St/PostOn 50\nmtrstart MIXER:Current/Mix/PostOn 50\n")
buf=b""; t0=time.time(); samples={}
while time.time()-t0 < 3.0:
    try:
        d=s.recv(65536)
        if not d: break
        buf+=d
    except socket.timeout: pass
    while b"\n" in buf:
        line,buf=buf.split(b"\n",1); p=line.decode().split()
        if len(p)>4 and p[1]=="mtr":
            samples.setdefault(p[2],[]).append([int(x,16) for x in p[4:]])
s.sendall(b"mtrstop MIXER:Current/InCh/PostOn\nmtrstop MIXER:Current/St/PostOn\nmtrstop MIXER:Current/Mix/PostOn\n"); time.sleep(0.3); s.close()
print("end", datetime.datetime.now().strftime("%H:%M:%S.%f"))
for addr, rows in samples.items():
    n=len(rows[0]); print(f"\n{addr}: {len(rows)} frames, {n} values")
    for i in range(n):
        col=[r[i] for r in rows]; mx=max(col); med=int(statistics.median(col))
        if mx>7: print(f"  idx {i:2d} (ch{i+1:2d}): max raw {mx:3d} -> {tbl[str(mx)]:6.1f} dB | median raw {med:3d} -> {tbl[str(med)]:6.1f} dB")
