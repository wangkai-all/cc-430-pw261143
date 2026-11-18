"""Dependency recompute service (independent process).

Polls the recompute queue and recomputes every registered report for each
accepted batch at its pinned input boundary. Crashes are safe: progress is
committed per report and compute_impact is idempotent.
"""
import threading
import time
import traceback

from . import config, core, db, httpkit

app = httpkit.App("recomputed")


@app.route("GET", "/healthz")
def health(h, body, query):
    con = db.connect()
    pending = con.execute(
        "SELECT COUNT(*) c FROM recompute_jobs WHERE status IN ('pending','running')"
    ).fetchone()["c"]
    con.close()
    return 200, {"ok": True, "service": "recomputed", "queue": pending}


def _worker():
    while True:
        con = db.connect()
        try:
            job = con.execute(
                "SELECT * FROM recompute_jobs WHERE status='pending' ORDER BY updated_at LIMIT 1"
            ).fetchone()
            if not job:
                time.sleep(config.poll_interval())
                continue
            bid = job["batch_id"]
            now = db.now_iso()
            con.execute(
                "UPDATE recompute_jobs SET status='running', attempts=attempts+1, updated_at=?"
                " WHERE batch_id=?",
                (now, bid),
            )
            con.execute("UPDATE batches SET status='computing' WHERE batch_id=?", (bid,))
            con.commit()
            try:
                core.compute_impact(con, bid)
                now = db.now_iso()
                con.execute(
                    "UPDATE recompute_jobs SET status='done', updated_at=? WHERE batch_id=?",
                    (now, bid),
                )
                con.execute(
                    "UPDATE batches SET status='computed', computed_at=? WHERE batch_id=?",
                    (now, bid),
                )
                con.commit()
                print("[recomputed] computed impact for %s" % bid, flush=True)
            except Exception as e:  # noqa: BLE001 - record and keep serving
                traceback.print_exc()
                con.rollback()
                now = db.now_iso()
                con.execute(
                    "UPDATE recompute_jobs SET status='failed', error=?, updated_at=?"
                    " WHERE batch_id=?",
                    (str(e), now, bid),
                )
                con.execute("UPDATE batches SET status='failed' WHERE batch_id=?", (bid,))
                con.commit()
        finally:
            con.close()


def main():
    db.init_db()
    threading.Thread(target=_worker, daemon=True).start()
    httpkit.serve(app, config.bind_host(), config.worker_port())


if __name__ == "__main__":
    main()
