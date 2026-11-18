#!/usr/bin/env python3
"""Source simulator: keeps submitting small revision batches to ingestd,
proving that already-published packages do not change when the source
keeps updating. Stop with Ctrl+C."""
import argparse
import json
import os
import random
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def http(method, url, payload=None):
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode() or "{}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingest-base", default=os.environ.get("IMPACT_INGEST_BASE",
                                                            "http://127.0.0.1:8001"))
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    print("source simulator -> %s (every %.1fs, Ctrl+C to stop)"
          % (args.ingest_base, args.interval))
    while True:
        try:
            det = http("GET", args.ingest_base + "/api/details?limit=200")["details"]
            if det:
                picks = rng.sample(det, k=min(rng.randint(1, 3), len(det)))
                changes = []
                for d in picks:
                    changes.append({
                        "detail_id": d["detail_id"],
                        "amount": max(0, d["amount"] + rng.randint(-5000, 8000)),
                        "qty": max(0, d["qty"] + rng.randint(-3, 5)),
                    })
                # de-duplicate in case a detail was sampled twice
                uniq = {c["detail_id"]: c for c in changes}
                r = http("POST", args.ingest_base + "/api/batches",
                         {"changes": list(uniq.values())})
                print("  submitted %s (%d changes, boundary v%d)"
                      % (r["batch_id"], r["n_changes"], r["boundary_version"]), flush=True)
        except Exception as e:
            print("  error: %s" % e, flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
