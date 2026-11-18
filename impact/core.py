"""Core domain logic: boundary-pinned revision acceptance and impact recompute.

The same revision batch is always evaluated against ONE input boundary:
  baseline = state of every detail at `version <= boundary_version`
  new      = baseline with exactly this batch's revisions applied
Later batches (higher versions) never leak into this batch's numbers, no
matter when the recompute actually runs.
"""
import json
import re

from . import db
from .reports import REPORTS, MEASURES

FIELDS = ("detail_id", "region", "product", "channel", "amount", "qty")
DIM_FIELDS = ("region", "product", "channel")
MEASURE_FIELDS = ("amount", "qty")

_DETAIL_ID_RX = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


class Validation(Exception):
    """Client-supplied revision batch is invalid."""


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _key_of(row, dims):
    return tuple(row[d] for d in dims)


# ---------------------------------------------------------------------------
# State reconstruction (replay from the append-only version history)
# ---------------------------------------------------------------------------

def state_at_version(con, version):
    """Full detail state as of `version` (deleted rows excluded)."""
    rows = con.execute(
        """
        SELECT dv.detail_id, dv.region, dv.product, dv.channel, dv.amount, dv.qty
        FROM detail_versions dv
        JOIN (SELECT detail_id, MAX(version) AS v
              FROM detail_versions WHERE version <= ?
              GROUP BY detail_id) m
          ON m.detail_id = dv.detail_id AND m.v = dv.version
        WHERE dv.deleted = 0
        ORDER BY dv.detail_id
        """,
        (version,),
    ).fetchall()
    return [dict(r) for r in rows]


def _latest_at(con, detail_id, version):
    row = con.execute(
        "SELECT * FROM detail_versions WHERE detail_id=? AND version<=? "
        "ORDER BY version DESC LIMIT 1",
        (detail_id, version),
    ).fetchone()
    if row is None or row["deleted"]:
        return None
    return {f: row[f] for f in FIELDS}


# ---------------------------------------------------------------------------
# Revision acceptance (runs inside ingestd; single writer of detail_versions)
# ---------------------------------------------------------------------------

def _validate_change(ch):
    if not isinstance(ch, dict):
        raise Validation("each change must be an object")
    did = ch.get("detail_id")
    if not isinstance(did, str) or not _DETAIL_ID_RX.match(did):
        raise Validation("detail_id must match [A-Za-z0-9_-]{1,64}")
    for f in DIM_FIELDS:
        if ch.get(f) is not None:
            v = ch[f]
            if not isinstance(v, str) or not 1 <= len(v) <= 40:
                raise Validation("%s: %s must be a string of 1..40 chars" % (did, f))
    for f in MEASURE_FIELDS:
        if ch.get(f) is not None:
            v = ch[f]
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise Validation("%s: %s must be a non-negative integer" % (did, f))
    if "delete" in ch and not isinstance(ch["delete"], bool):
        raise Validation("%s: delete must be a boolean" % did)


def accept_batch(con, changes, batch_id=None):
    """Validate and persist one revision batch, pinning its input boundary.

    The boundary, the new detail versions, the batch row and the recompute
    job are written in ONE immediate transaction, so the boundary is exactly
    'all data accepted before this batch'.
    """
    if not isinstance(changes, list) or not changes:
        raise Validation("changes must be a non-empty list")
    if len(changes) > 1000:
        raise Validation("too many changes in one batch (max 1000)")
    seen = set()
    for ch in changes:
        _validate_change(ch)
        if ch["detail_id"] in seen:
            raise Validation("duplicate detail_id in batch: %s" % ch["detail_id"])
        seen.add(ch["detail_id"])
    if batch_id is not None and not _DETAIL_ID_RX.match(batch_id):
        raise Validation("invalid batch_id")

    con.execute("BEGIN IMMEDIATE")
    try:
        boundary = int(con.execute("SELECT v FROM meta WHERE k='version'").fetchone()["v"])
        if batch_id is None:
            n = con.execute("SELECT COUNT(*) c FROM batches").fetchone()["c"] + 1
            batch_id = "B-%04d" % n
            while con.execute("SELECT 1 FROM batches WHERE batch_id=?", (batch_id,)).fetchone():
                n += 1
                batch_id = "B-%04d" % n
        elif con.execute("SELECT 1 FROM batches WHERE batch_id=?", (batch_id,)).fetchone():
            raise Validation("batch_id already exists: %s" % batch_id)

        for i, ch in enumerate(changes):
            did = ch["detail_id"]
            old = _latest_at(con, did, boundary)
            if ch.get("delete"):
                if old is None:
                    raise Validation("%s: delete requested but detail does not exist at boundary" % did)
                con.execute(
                    "INSERT INTO detail_versions"
                    " (detail_id, version, region, product, channel, amount, qty, deleted, batch_id)"
                    " VALUES (?,?,?,?,?,?,?,1,?)",
                    (did, boundary + i + 1, old["region"], old["product"], old["channel"],
                     old["amount"], old["qty"], batch_id),
                )
            else:
                merged = dict(old) if old else {"detail_id": did}
                for f in DIM_FIELDS + MEASURE_FIELDS:
                    if ch.get(f) is not None:
                        merged[f] = ch[f]
                missing = [f for f in DIM_FIELDS + MEASURE_FIELDS if merged.get(f) is None]
                if missing:
                    raise Validation("%s: missing fields for upsert: %s" % (did, ", ".join(missing)))
                if old is not None and all(merged[f] == old[f] for f in DIM_FIELDS + MEASURE_FIELDS):
                    raise Validation("%s: no-op change (identical to boundary state)" % did)
                con.execute(
                    "INSERT INTO detail_versions"
                    " (detail_id, version, region, product, channel, amount, qty, deleted, batch_id)"
                    " VALUES (?,?,?,?,?,?,?,0,?)",
                    (did, boundary + i + 1, merged["region"], merged["product"], merged["channel"],
                     merged["amount"], merged["qty"], batch_id),
                )

        con.execute("UPDATE meta SET v=? WHERE k='version'", (boundary + len(changes),))
        now = db.now_iso()
        con.execute(
            "INSERT INTO batches (batch_id, boundary_version, n_changes, status, created_at)"
            " VALUES (?,?,?,'received',?)",
            (batch_id, boundary, len(changes), now),
        )
        con.execute(
            "INSERT INTO recompute_jobs (batch_id, status, updated_at) VALUES (?, 'pending', ?)",
            (batch_id, now),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    return {"batch_id": batch_id, "boundary_version": boundary,
            "n_changes": len(changes), "status": "received"}


# ---------------------------------------------------------------------------
# Impact recompute (runs inside recomputed)
# ---------------------------------------------------------------------------

def _revisions_of(con, batch_id, state):
    changes = con.execute(
        "SELECT * FROM detail_versions WHERE batch_id=? ORDER BY version", (batch_id,)
    ).fetchall()
    revisions = []
    for ch in changes:
        old = state.get(ch["detail_id"])
        new = None if ch["deleted"] else {f: ch[f] for f in FIELDS}
        revisions.append({"detail_id": ch["detail_id"], "old": old, "new": new})
    return revisions


def compute_impact(con, batch_id):
    """Recompute every report for a batch at its pinned boundary.

    Stores only the *affected* aggregate rows plus the revised details'
    contributions (delta storage) — not every intermediate contribution.
    Commits per report so a worker crash can resume from the job's progress.
    Idempotent: existing rows for the batch are replaced first.
    """
    batch = con.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    if batch is None:
        raise KeyError("unknown batch " + batch_id)
    boundary = batch["boundary_version"]
    baseline = state_at_version(con, boundary)
    state = {r["detail_id"]: r for r in baseline}
    revisions = _revisions_of(con, batch_id, state)

    con.execute("DELETE FROM impact_rows WHERE batch_id=?", (batch_id,))
    con.execute("DELETE FROM report_diffs WHERE batch_id=?", (batch_id,))
    con.commit()

    done_reports = []
    for rid, spec in REPORTS.items():
        dims = spec["dimensions"]
        by_key = {}
        tot_a = tot_q = 0
        for r in baseline:
            by_key.setdefault(_key_of(r, dims), []).append(r)
            tot_a += r["amount"]
            tot_q += r["qty"]

        affected = {}
        for rev in revisions:
            if rev["old"] is not None:
                affected[_key_of(rev["old"], dims)] = True
            if rev["new"] is not None:
                affected[_key_of(rev["new"], dims)] = True

        d_a = d_q = 0
        for key in sorted(affected):
            members = by_key.get(key, [])
            old_a = sum(m["amount"] for m in members)
            old_q = sum(m["qty"] for m in members)
            contribs = []
            for rev in revisions:
                co = rev["old"] if (rev["old"] is not None and _key_of(rev["old"], dims) == key) else None
                cn = rev["new"] if (rev["new"] is not None and _key_of(rev["new"], dims) == key) else None
                if co or cn:
                    contribs.append({
                        "detail_id": rev["detail_id"],
                        "old": ({"amount": co["amount"], "qty": co["qty"]} if co else None),
                        "new": ({"amount": cn["amount"], "qty": cn["qty"]} if cn else None),
                    })
            contribs.sort(key=lambda c: c["detail_id"])
            new_a = old_a - sum(c["old"]["amount"] for c in contribs if c["old"]) \
                        + sum(c["new"]["amount"] for c in contribs if c["new"])
            new_q = old_q - sum(c["old"]["qty"] for c in contribs if c["old"]) \
                        + sum(c["new"]["qty"] for c in contribs if c["new"])
            # A revision may touch a key without changing its sums for THIS
            # report (e.g. a region move is invisible to product_sales).
            # Only genuinely changed aggregates are "affected".
            if (new_a, new_q) == (old_a, old_q):
                continue
            key_obj = {d: v for d, v in zip(dims, key)}
            con.execute(
                "INSERT INTO impact_rows"
                " (batch_id, report_id, key_json, old_amount, old_qty, new_amount, new_qty, contribs)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (batch_id, rid, json.dumps(key_obj, sort_keys=True, ensure_ascii=False),
                 old_a, old_q, new_a, new_q, json.dumps(contribs, ensure_ascii=False)),
            )
            d_a += new_a - old_a
            d_q += new_q - old_q

        con.execute(
            "INSERT INTO report_diffs (batch_id, report_id, old_amount, old_qty, new_amount, new_qty)"
            " VALUES (?,?,?,?,?,?)",
            (batch_id, rid, tot_a, tot_q, tot_a + d_a, tot_q + d_q),
        )
        done_reports.append(rid)
        con.execute(
            "UPDATE recompute_jobs SET progress=?, updated_at=? WHERE batch_id=?",
            (json.dumps(done_reports), db.now_iso(), batch_id),
        )
        con.commit()


# ---------------------------------------------------------------------------
# Read models (UI drill-down + package builder)
# ---------------------------------------------------------------------------

def load_impact(con, batch_id):
    out = {}
    for rid in REPORTS:
        rows = []
        for r in con.execute(
            "SELECT * FROM impact_rows WHERE batch_id=? AND report_id=? ORDER BY key_json",
            (batch_id, rid),
        ).fetchall():
            old = {"amount": r["old_amount"], "qty": r["old_qty"]}
            new = {"amount": r["new_amount"], "qty": r["new_qty"]}
            rows.append({
                "key": json.loads(r["key_json"]),
                "old": old,
                "new": new,
                "delta": {"amount": new["amount"] - old["amount"],
                          "qty": new["qty"] - old["qty"]},
                "contributions": json.loads(r["contribs"]),
            })
        diff = con.execute(
            "SELECT * FROM report_diffs WHERE batch_id=? AND report_id=?", (batch_id, rid)
        ).fetchone()
        total = None
        if diff:
            o = {"amount": diff["old_amount"], "qty": diff["old_qty"]}
            n = {"amount": diff["new_amount"], "qty": diff["new_qty"]}
            total = {"old": o, "new": n,
                     "delta": {"amount": n["amount"] - o["amount"], "qty": n["qty"] - o["qty"]}}
        out[rid] = {"rows": rows, "total": total}
    return out


def impact_file_obj(batch, report_id, impact):
    spec = REPORTS[report_id]
    return {
        "report_id": report_id,
        "title": spec["title"],
        "dimensions": spec["dimensions"],
        "measures": list(MEASURES),
        "batch_id": batch["batch_id"],
        "boundary_version": batch["boundary_version"],
        "affected_rows": impact[report_id]["rows"],
        "report_total": impact[report_id]["total"],
    }


def evidence_for_batch(con, batch_id):
    """The self-contained evidence slice embedded into a package:
    the full baseline at the pinned boundary + this batch's revisions."""
    batch = con.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    if batch is None:
        raise KeyError("unknown batch " + batch_id)
    baseline = state_at_version(con, batch["boundary_version"])
    state = {r["detail_id"]: r for r in baseline}
    changes = con.execute(
        "SELECT * FROM detail_versions WHERE batch_id=? ORDER BY version", (batch_id,)
    ).fetchall()
    revisions = []
    for ch in changes:
        old = state.get(ch["detail_id"])
        new = None if ch["deleted"] else {f: ch[f] for f in FIELDS}
        revisions.append({
            "detail_id": ch["detail_id"],
            "op": "delete" if ch["deleted"] else "upsert",
            "old": old,
            "new": new,
        })
    return baseline, revisions
