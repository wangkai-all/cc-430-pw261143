"""Revision ingestion service (independent process).

The ONLY writer of detail_versions: it assigns versions and pins each
batch's input boundary inside one immediate transaction.
"""
from . import config, core, db, httpkit

app = httpkit.App("ingestd")


@app.route("GET", "/healthz")
def health(h, body, query):
    return 200, {"ok": True, "service": "ingestd"}


@app.route("POST", "/api/batches")
def create_batch(h, body, query):
    body = body or {}
    con = db.connect()
    try:
        result = core.accept_batch(con, body.get("changes") or [], body.get("batch_id"))
    except core.Validation as e:
        return 422, {"error": str(e)}
    finally:
        con.close()
    return 201, result


@app.route("GET", "/api/batches")
def list_batches(h, body, query):
    con = db.connect()
    rows = con.execute(
        "SELECT * FROM batches ORDER BY created_at DESC, batch_id DESC LIMIT 200"
    ).fetchall()
    con.close()
    return 200, {"batches": [dict(r) for r in rows]}


@app.route("GET", "/api/batches/<bid>")
def batch_detail(h, body, query, bid):
    con = db.connect()
    batch = con.execute("SELECT * FROM batches WHERE batch_id=?", (bid,)).fetchone()
    if not batch:
        con.close()
        return 404, {"error": "unknown batch"}
    changes = [dict(r) for r in con.execute(
        "SELECT * FROM detail_versions WHERE batch_id=? ORDER BY version", (bid,)).fetchall()]
    con.close()
    return 200, {"batch": dict(batch), "changes": changes}


@app.route("GET", "/api/details")
def details(h, body, query):
    """Current (latest) detail state — used by the source simulator and the UI."""
    limit = min(int(query.get("limit", "50")), 1000)
    offset = int(query.get("offset", "0"))
    con = db.connect()
    version = int(con.execute("SELECT v FROM meta WHERE k='version'").fetchone()["v"])
    rows = core.state_at_version(con, version)
    con.close()
    return 200, {"version": version, "details": rows[offset:offset + limit]}


def main():
    db.init_db()
    httpkit.serve(app, config.bind_host(), config.ingest_port())


if __name__ == "__main__":
    main()
