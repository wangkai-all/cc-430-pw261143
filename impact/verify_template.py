#!/usr/bin/env python3
"""
Impact explanation package - independent verifier.

Recomputes EVERY number in this package from the embedded evidence
(the input-boundary snapshot + the revision list) and checks file
integrity against the manifest. Requires Python 3.8+ standard library
only. No network access, no source systems, no trust in the producer.

Usage:
    python3 verify.py        # exit code 0 = package consistent
"""
import hashlib
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))


def _read(rel):
    with open(os.path.join(ROOT, rel), "rb") as fh:
        return fh.read()


def _json(rel):
    return json.loads(_read(rel).decode("utf-8"))


def _jsonl(rel):
    out = []
    for line in _read(rel).decode("utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _sha256(rel):
    return hashlib.sha256(_read(rel)).hexdigest()


def _key(row, dims):
    return tuple(row[d] for d in dims)


def _sums(rows, dims):
    acc = defaultdict(lambda: [0, 0])
    for r in rows:
        k = _key(r, dims)
        acc[k][0] += r["amount"]
        acc[k][1] += r["qty"]
    return acc


def main():
    failures = []

    def check(cond, msg):
        print("  [%s] %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            failures.append(msg)

    manifest = _json("manifest.json")
    print("package: batch=%s boundary_version=%s generated_at=%s"
          % (manifest["batch_id"], manifest["boundary_version"], manifest["generated_at"]))

    print("[1/4] file integrity (sha256 vs manifest)")
    for rel in sorted(manifest["files"]):
        want = manifest["files"][rel]
        got = _sha256(rel) if os.path.exists(os.path.join(ROOT, rel)) else "<missing>"
        check(got == want, "sha256 %-38s %s..." % (rel, want[:16]))

    print("[2/4] evidence internal consistency")
    baseline = _jsonl("evidence/baseline_details.jsonl")
    revisions = _jsonl("evidence/revisions.jsonl")
    check(len(baseline) == manifest["evidence"]["baseline_rows"],
          "baseline row count = %d" % len(baseline))
    check(len(revisions) == manifest["evidence"]["revision_rows"],
          "revision row count = %d" % len(revisions))
    by_id = {}
    dup = False
    for r in baseline:
        if r["detail_id"] in by_id:
            dup = True
        by_id[r["detail_id"]] = r
    check(not dup, "baseline detail_ids are unique")
    for rev in revisions:
        did = rev["detail_id"]
        old = by_id.get(did)
        if rev["old"] is None:
            check(old is None, "rev %s: add has no baseline row" % did)
        else:
            check(old is not None and all(old[f] == rev["old"][f]
                  for f in ("region", "product", "channel", "amount", "qty")),
                  "rev %s: old state matches baseline evidence" % did)
        if rev["op"] == "delete":
            check(rev["new"] is None, "rev %s: delete carries no new state" % did)
        else:
            check(rev["new"] is not None, "rev %s: upsert carries a new state" % did)

    print("[3/4] recompute aggregates from evidence and compare")
    state = dict(by_id)
    for rev in revisions:
        state.pop(rev["detail_id"], None)
        if rev["new"] is not None:
            state[rev["detail_id"]] = rev["new"]
    revs = [(r["detail_id"], r["old"], r["new"]) for r in revisions]
    zero = [0, 0]

    for rid in sorted(manifest["reports"]):
        dims = manifest["reports"][rid]["dimensions"]
        impact = _json("impact/%s.json" % rid)
        check(impact["batch_id"] == manifest["batch_id"], "%s: batch_id matches manifest" % rid)
        check(impact["boundary_version"] == manifest["boundary_version"],
              "%s: boundary_version matches manifest" % rid)
        base_sums = _sums(baseline, dims)
        new_sums = _sums(state.values(), dims)
        changed = set(k for k in set(base_sums) | set(new_sums)
                      if base_sums.get(k, zero) != new_sums.get(k, zero))
        rows = impact["affected_rows"]
        got_keys = set(_key(row["key"], dims) for row in rows)
        check(got_keys == changed,
              "%s: affected key set == recomputed delta set (%d keys)" % (rid, len(changed)))
        for row in rows:
            k = _key(row["key"], dims)
            o = base_sums.get(k, zero)
            n = new_sums.get(k, zero)
            label = "%s[%s]" % (rid, "/".join(str(x) for x in k))
            check([row["old"]["amount"], row["old"]["qty"]] == list(o), "%s: old values" % label)
            check([row["new"]["amount"], row["new"]["qty"]] == list(n), "%s: new values" % label)
            check(row["delta"]["amount"] == n[0] - o[0] and row["delta"]["qty"] == n[1] - o[1],
                  "%s: delta == new - old" % label)
            expected = []
            for did, ro, rn in revs:
                co = ro if (ro and _key(ro, dims) == k) else None
                cn = rn if (rn and _key(rn, dims) == k) else None
                if co or cn:
                    expected.append({
                        "detail_id": did,
                        "old": ({"amount": co["amount"], "qty": co["qty"]} if co else None),
                        "new": ({"amount": cn["amount"], "qty": cn["qty"]} if cn else None),
                    })
            check(sorted(row["contributions"], key=lambda c: c["detail_id"])
                  == sorted(expected, key=lambda c: c["detail_id"]),
                  "%s: contributions match revision evidence" % label)
        t = impact["report_total"]
        check(t["old"]["amount"] == sum(v[0] for v in base_sums.values())
              and t["old"]["qty"] == sum(v[1] for v in base_sums.values()),
              "%s: report total (old) recomputed" % rid)
        check(t["new"]["amount"] == sum(v[0] for v in new_sums.values())
              and t["new"]["qty"] == sum(v[1] for v in new_sums.values()),
              "%s: report total (new) recomputed" % rid)
        check(t["delta"]["amount"] == t["new"]["amount"] - t["old"]["amount"]
              and t["delta"]["qty"] == t["new"]["qty"] - t["old"]["qty"],
              "%s: report total delta consistent" % rid)

    print("[4/4] manifest totals cross-check")
    for rid in sorted(manifest["reports"]):
        impact = _json("impact/%s.json" % rid)
        mt = manifest["totals"][rid]
        t = impact["report_total"]
        check(mt["old_amount"] == t["old"]["amount"] and mt["old_qty"] == t["old"]["qty"]
              and mt["new_amount"] == t["new"]["amount"] and mt["new_qty"] == t["new"]["qty"],
              "%s: manifest totals == impact totals" % rid)

    if failures:
        print("\nVERIFICATION FAILED: %d check(s) failed" % len(failures))
        return 1
    print("\nVERIFICATION PASSED: package is complete and internally consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
