"""Explanation package service (independent process).

Hosts the web UI, proxies revision submissions to ingestd, serves impact
drill-down data, and builds / cancels / resumes / downloads packages.
Builder threads poll the build_tasks queue; publish is generation-fenced.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request

from . import config, core, db, httpkit, package_builder
from .reports import REPORTS

app = httpkit.App("packagesvc")
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def _static(name, ctype):
    def handler(h, body, query):
        with open(os.path.join(WEB, name), "rb") as fh:
            return 200, fh.read(), {"Content-Type": ctype}
    return handler


app.route("GET", "/")(_static("index.html", "text/html; charset=utf-8"))
app.route("GET", "/app.js")(_static("app.js", "application/javascript; charset=utf-8"))
app.route("GET", "/style.css")(_static("style.css", "text/css; charset=utf-8"))


@app.route("GET", "/healthz")
def health(h, body, query):
    return 200, {"ok": True, "service": "packagesvc"}


# --- proxy: revision submission goes to the ingestion service ---------------

def _proxy_ingest(path, method="GET", payload=None):
    req = urllib.request.Request(
        config.ingest_base() + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {"error": "ingestd error %s" % e.code}
    except Exception as e:
        return 502, {"error": "ingestd unreachable: %s" % e}


@app.route("POST", "/api/submit")
def submit(h, body, query):
    status, payload = _proxy_ingest("/api/batches", "POST", body or {})
    return status, payload


# --- read models (shared SQLite is read-safe from every service) ------------

@app.route("GET", "/api/batches")
def batches(h, body, query):
    con = db.connect()
    rows = con.execute(
        """
        SELECT b.batch_id, b.boundary_version, b.n_changes, b.status, b.created_at,
               b.computed_at, p.status AS pkg_status, p.generation,
               p.published_generation, p.artifact_sha256
        FROM batches b LEFT JOIN packages p ON p.batch_id = b.batch_id
        ORDER BY b.created_at DESC, b.batch_id DESC LIMIT 200
        """
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


@app.route("GET", "/api/batches/<bid>/impact")
def impact(h, body, query, bid):
    con = db.connect()
    batch = con.execute("SELECT * FROM batches WHERE batch_id=?", (bid,)).fetchone()
    if not batch:
        con.close()
        return 404, {"error": "unknown batch"}
    data = core.load_impact(con, bid)
    reports = {rid: {"title": REPORTS[rid]["title"],
                     "dimensions": REPORTS[rid]["dimensions"]} for rid in REPORTS}
    con.close()
    return 200, {"batch": dict(batch), "reports": reports, "impact": data}


@app.route("GET", "/api/sample-details")
def sample_details(h, body, query):
    limit = min(int(query.get("limit", "5")), 500)
    con = db.connect()
    version = int(con.execute("SELECT v FROM meta WHERE k='version'").fetchone()["v"])
    rows = core.state_at_version(con, version)[:limit]
    con.close()
    return 200, {"version": version, "details": rows}


# --- package lifecycle ------------------------------------------------------

@app.route("POST", "/api/packages/<bid>/build")
def build(h, body, query, bid):
    con = db.connect()
    try:
        task, created = package_builder.request_build(con, bid)
        return (201 if created else 200), {"task": task, "created": created}
    except KeyError:
        return 404, {"error": "unknown batch"}
    except package_builder.NotReady as e:
        return 409, {"error": "batch not computed yet (status=%s)" % e}
    finally:
        con.close()


@app.route("POST", "/api/packages/<bid>/cancel")
def cancel(h, body, query, bid):
    con = db.connect()
    task = package_builder.request_cancel(con, bid)
    con.close()
    if task is None:
        return 409, {"error": "no active build for this batch"}
    return 200, {"task": task}


@app.route("GET", "/api/packages/<bid>/status")
def pkg_status(h, body, query, bid):
    con = db.connect()
    st = package_builder.package_status(con, bid)
    con.close()
    return 200, st


@app.route("GET", "/api/packages/<bid>/download")
def download(h, body, query, bid):
    con = db.connect()
    pkg = con.execute("SELECT * FROM packages WHERE batch_id=?", (bid,)).fetchone()
    con.close()
    if not pkg or not pkg["artifact_path"] or not os.path.exists(pkg["artifact_path"]):
        return 404, {"error": "no published package for this batch"}
    with open(pkg["artifact_path"], "rb") as fh:
        data = fh.read()
    fname = os.path.basename(pkg["artifact_path"])
    return 200, data, {
        "Content-Type": "application/zip",
        "Content-Disposition": 'attachment; filename="%s"' % fname,
        "X-Sha256": pkg["artifact_sha256"] or "",
    }


# --- builder worker threads --------------------------------------------------

def _builder_loop(idx):
    while True:
        con = db.connect()
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT task_id FROM build_tasks WHERE status='pending' ORDER BY task_id LIMIT 1"
        ).fetchone()
        if row:
            con.execute(
                "UPDATE build_tasks SET status='running', updated_at=? WHERE task_id=?",
                (db.now_iso(), row["task_id"]),
            )
        con.commit()
        con.close()
        if not row:
            time.sleep(config.poll_interval())
            continue
        package_builder.run_build_task(row["task_id"])


def main():
    db.init_db()
    for i in range(config.build_workers()):
        threading.Thread(target=_builder_loop, args=(i,), daemon=True).start()
    httpkit.serve(app, config.bind_host(), config.package_port())


if __name__ == "__main__":
    main()
