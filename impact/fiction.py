"""Fictional upstream dataset generator (deterministic, no real data)."""
import random

from . import core, db

REGIONS = ["North", "South", "East", "West", "Central", "Lakes"]
PRODUCTS = ["P-%02d" % i for i in range(1, 11)]
CHANNELS = ["online", "retail", "wholesale"]


def generate_details(n=600, seed=42):
    rng = random.Random(seed)
    rows = []
    for i in range(1, n + 1):
        rows.append({
            "detail_id": "D%06d" % i,
            "region": rng.choice(REGIONS),
            "product": rng.choice(PRODUCTS),
            "channel": rng.choice(CHANNELS),
            "amount": rng.randrange(1000, 500000),   # cents
            "qty": rng.randrange(1, 100),
        })
    return rows


def seed_database(con, n=600, seed=42):
    """Load the fictional dataset as batch 'SEED' (versions 1..n) and compute
    its impact so the initial load is itself fully explainable."""
    existing = con.execute("SELECT COUNT(*) c FROM detail_versions").fetchone()["c"]
    if existing:
        return 0
    rows = generate_details(n, seed)
    now = db.now_iso()
    con.execute("BEGIN IMMEDIATE")
    for i, r in enumerate(rows, start=1):
        con.execute(
            "INSERT INTO detail_versions"
            " (detail_id, version, region, product, channel, amount, qty, deleted, batch_id)"
            " VALUES (?,?,?,?,?,?,?,0,'SEED')",
            (r["detail_id"], i, r["region"], r["product"], r["channel"], r["amount"], r["qty"]),
        )
    con.execute("UPDATE meta SET v=? WHERE k='version'", (n,))
    con.execute(
        "INSERT INTO batches (batch_id, boundary_version, n_changes, status, created_at, computed_at)"
        " VALUES ('SEED', 0, ?, 'computed', ?, ?)",
        (n, now, now),
    )
    con.execute(
        "INSERT INTO recompute_jobs (batch_id, status, progress, updated_at)"
        " VALUES ('SEED', 'done', '[]', ?)",
        (now,),
    )
    con.commit()
    core.compute_impact(con, "SEED")
    return n
