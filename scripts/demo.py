#!/usr/bin/env python3
"""End-to-end demo: seed -> start 3 services -> submit revision -> recompute
-> build package -> download -> independent verification -> prove the package
is immutable while the source keeps updating.

Usage:  python3 scripts/demo.py
Stops with Ctrl+C (services keep running until then so you can open the UI).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

HOME = os.path.join(REPO, "data")
BASE = "http://127.0.0.1:%d"
PORTS = (("ingestd", "impact.ingestd", 8001),
         ("recomputed", "impact.recomputed", 8002),
         ("packagesvc", "impact.packagesvc", 8003))


def http(method, url, payload=None):
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode() or "{}")


def wait_health(port, name, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            http("GET", BASE % port + "/healthz")
            print("  ok %s (:%d)" % (name, port))
            return
        except Exception:
            time.sleep(0.2)
    raise SystemExit("service %s did not start" % name)


def main():
    os.environ["IMPACT_HOME"] = HOME
    shutil.rmtree(HOME, ignore_errors=True)

    from impact import db, fiction
    db.init_db()
    con = db.connect()
    n = fiction.seed_database(con, n=600, seed=42)
    con.close()
    print("1) seeded %d fictional details" % n)

    procs = []
    for name, mod, port in PORTS:
        procs.append(subprocess.Popen([sys.executable, "-m", mod], cwd=REPO,
                                      env=dict(os.environ)))
    try:
        print("2) starting services")
        for name, _mod, port in PORTS:
            wait_health(port, name)
        print("   UI: http://127.0.0.1:8003")

        det = http("GET", BASE % 8003 + "/api/sample-details?limit=5")["details"]
        a, b, c = det[0], det[1], det[2]
        changes = [
            {"detail_id": a["detail_id"], "amount": a["amount"] + 25000, "qty": a["qty"] + 5},
            {"detail_id": b["detail_id"],
             "region": "East" if b["region"] != "East" else "West"},
            {"detail_id": c["detail_id"], "delete": True},
            {"detail_id": "DX900001", "region": "North", "product": "P-07",
             "channel": "online", "amount": 156000, "qty": 30},
        ]
        r = http("POST", BASE % 8003 + "/api/submit", {"changes": changes})
        bid = r["batch_id"]
        print("3) submitted %s (boundary v%d, %d changes)"
              % (bid, r["boundary_version"], r["n_changes"]))

        while True:
            b = http("GET", BASE % 8003 + "/api/batches/" + bid)["batch"]
            if b["status"] == "computed":
                break
            time.sleep(0.2)
        print("4) impact recomputed at the pinned boundary:")
        imp = http("GET", BASE % 8003 + "/api/batches/%s/impact" % bid)["impact"]
        for rid, rep in imp.items():
            t = rep["total"]
            print("   %-15s affected=%2d  Δamount=%+10d  Δqty=%+5d"
                  % (rid, len(rep["rows"]), t["delta"]["amount"], t["delta"]["qty"]))

        http("POST", BASE % 8003 + "/api/packages/%s/build" % bid)
        while True:
            st = http("GET", BASE % 8003 + "/api/packages/%s/status" % bid)
            if st["package"] and st["package"]["status"] == "done":
                break
            time.sleep(0.2)
        pkg = st["package"]
        print("5) package built: generation g%d, sha256 %s..."
              % (pkg["published_generation"], pkg["artifact_sha256"][:16]))

        with urllib.request.urlopen(BASE % 8003 + "/api/packages/%s/download" % bid) as resp:
            data = resp.read()
        out = os.path.join(tempfile.mkdtemp(prefix="impact-demo-"), "pkg.zip")
        with open(out, "wb") as fh:
            fh.write(data)
        xdir = out + ".x"
        with zipfile.ZipFile(out) as z:
            z.extractall(xdir)
        print("6) downloaded %d bytes; running the embedded independent verifier:"
              % len(data))
        proc = subprocess.run([sys.executable, "-I", "verify.py"],
                              cwd=xdir, capture_output=True, text=True)
        tail = proc.stdout.strip().splitlines()
        for line in tail[:3] + ["   ..."] + tail[-4:]:
            print("   " + line)
        if proc.returncode != 0:
            print(proc.stdout)
            print(proc.stderr)
            raise SystemExit("verification FAILED")

        d2 = http("GET", BASE % 8003 + "/api/sample-details?limit=3")["details"][0]
        r2 = http("POST", BASE % 8003 + "/api/submit",
                  {"changes": [{"detail_id": d2["detail_id"],
                                "amount": d2["amount"] + 777}]})
        print("7) source kept updating: %s accepted (boundary v%d)"
              % (r2["batch_id"], r2["boundary_version"]))
        with urllib.request.urlopen(BASE % 8003 + "/api/packages/%s/download" % bid) as resp:
            data2 = resp.read()
        print("8) %s package bytes unchanged after source updates: %s"
              % (bid, data == data2))

        print("\nDemo OK. UI at http://127.0.0.1:8003  (Ctrl+C to stop services)")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    main()
