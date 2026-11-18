"""Package builder: turns a computed batch into a self-contained,
independently verifiable explanation package (zip).

Properties:
  * Checkpointed per report file  -> cancel now, resume later without rework.
  * Generation fencing            -> a stale/late build can never replace a
                                     newer package (compare-and-swap publish).
  * Self-checking                 -> the embedded verifier must PASS on the
                                     staged package before anything publishes.
  * Immutable once published      -> all content derives from the pinned
                                     input boundary; later source updates
                                     cannot change an existing package.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile

from . import config, core, db
from .reports import REPORTS

VERIFY_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_template.py")

README_TEMPLATE = """\
Impact Explanation Package / 影响解释包
=======================================
Batch:            {batch_id}
Input boundary:   detail version <= {boundary_version}
                  (every report in this package uses THIS boundary,
                   not "the latest data" at build time)
Generated:        {generated_at} (build generation {generation})

Contents / 内容
  manifest.json                    manifest + sha256 of every payload file
  evidence/baseline_details.jsonl  ALL detail rows as of the input boundary
  evidence/revisions.jsonl         this revision batch (old/new state per change)
  impact/<report>.json             affected aggregates: old / new / delta
                                   + per-detail contributions (drill-down)
  verify.py                        independent verifier (stdlib only)

Independent verification / 独立复核
  python3 verify.py
  Recomputes every aggregate from evidence/ and compares it with impact/.
  No network or source-system access is needed. The source may keep
  changing after download; this package's content cannot change.
"""


class Cancelled(Exception):
    pass


class Superseded(Exception):
    pass


class BuildError(Exception):
    pass


class NotReady(Exception):
    pass


# ---------------------------------------------------------------------------
# Build / cancel requests (called by packagesvc HTTP handlers)
# ---------------------------------------------------------------------------

def request_build(con, batch_id):
    """Idempotent: returns the active task if one exists, else starts a new
    generation (this is also how a cancelled/failed build is resumed)."""
    batch = con.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    if batch is None:
        raise KeyError(batch_id)
    if batch["status"] != "computed":
        raise NotReady(batch["status"])
    con.execute("BEGIN IMMEDIATE")
    try:
        active = con.execute(
            "SELECT * FROM build_tasks WHERE batch_id=? AND status IN ('pending','running')"
            " ORDER BY task_id DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
        if active:
            con.commit()
            return dict(active), False
        pkg = con.execute("SELECT * FROM packages WHERE batch_id=?", (batch_id,)).fetchone()
        gen = (pkg["generation"] if pkg else 0) + 1
        now = db.now_iso()
        cur = con.execute(
            "INSERT INTO build_tasks (batch_id, generation, status, created_at, updated_at)"
            " VALUES (?,?, 'pending', ?, ?)",
            (batch_id, gen, now, now),
        )
        if pkg:
            con.execute(
                "UPDATE packages SET generation=?, status='building', updated_at=? WHERE batch_id=?",
                (gen, now, batch_id),
            )
        else:
            con.execute(
                "INSERT INTO packages (batch_id, generation, status, updated_at)"
                " VALUES (?,?,'building',?)",
                (batch_id, gen, now),
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    task = con.execute("SELECT * FROM build_tasks WHERE task_id=?", (cur.lastrowid,)).fetchone()
    return dict(task), True


def request_cancel(con, batch_id):
    con.execute("BEGIN IMMEDIATE")
    try:
        task = con.execute(
            "SELECT * FROM build_tasks WHERE batch_id=? AND status IN ('pending','running')"
            " ORDER BY task_id DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
        if task is None:
            con.commit()
            return None
        now = db.now_iso()
        if task["status"] == "pending":
            # Never started: cancel outright (generation-guarded).
            con.execute(
                "UPDATE build_tasks SET status='cancelled', cancel_requested=1, updated_at=?"
                " WHERE task_id=?",
                (now, task["task_id"]),
            )
            con.execute(
                "UPDATE packages SET status='cancelled', updated_at=?"
                " WHERE batch_id=? AND generation=?",
                (now, batch_id, task["generation"]),
            )
        else:
            # Running: the builder observes this flag at its next step.
            con.execute(
                "UPDATE build_tasks SET cancel_requested=1, updated_at=? WHERE task_id=?",
                (now, task["task_id"]),
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    return dict(con.execute("SELECT * FROM build_tasks WHERE task_id=?",
                            (task["task_id"],)).fetchone())


def package_status(con, batch_id):
    pkg = con.execute("SELECT * FROM packages WHERE batch_id=?", (batch_id,)).fetchone()
    task = con.execute(
        "SELECT * FROM build_tasks WHERE batch_id=? ORDER BY task_id DESC LIMIT 1", (batch_id,)
    ).fetchone()
    cps = con.execute(
        "SELECT COUNT(*) c FROM build_checkpoints WHERE batch_id=?", (batch_id,)
    ).fetchone()["c"]
    return {
        "package": dict(pkg) if pkg else None,
        "latest_task": dict(task) if task else None,
        "checkpoints": cps,
    }


# ---------------------------------------------------------------------------
# Builder (runs inside packagesvc worker threads)
# ---------------------------------------------------------------------------

def _staging_dir(batch_id, gen):
    return os.path.join(config.package_dir(), "staging", "%s__g%d" % (batch_id, gen))


def _sha256_path(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _progress(con, task_id, done, total):
    con.execute(
        "UPDATE build_tasks SET done_steps=?, total_steps=?, updated_at=? WHERE task_id=?",
        (done, total, db.now_iso(), task_id),
    )
    con.commit()


def _check_abort(con, task_id, batch_id, gen):
    row = con.execute(
        "SELECT cancel_requested FROM build_tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if row and row["cancel_requested"]:
        raise Cancelled()
    pkg = con.execute(
        "SELECT generation FROM packages WHERE batch_id=?", (batch_id,)
    ).fetchone()
    if pkg and pkg["generation"] > gen:
        raise Superseded()


def _step_delay():
    delay = config.build_step_delay()
    if delay > 0:
        time.sleep(delay)


def _zip_dir(src, zip_path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(src):
            for name in sorted(files):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, src).replace(os.sep, "/")
                info = zipfile.ZipInfo(rel, date_time=(2024, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                with open(full, "rb") as fh:
                    z.writestr(info, fh.read())


def _finish(con, task_id, batch_id, gen, status, error=None):
    now = db.now_iso()
    con.execute(
        "UPDATE build_tasks SET status=?, error=?, updated_at=? WHERE task_id=?",
        (status, error, now, task_id),
    )
    if status in ("cancelled", "failed"):
        # Only the still-current generation may move the package status.
        con.execute(
            "UPDATE packages SET status=?, updated_at=? WHERE batch_id=? AND generation=?",
            (status, now, batch_id, gen),
        )
    con.commit()


def run_build_task(task_id):
    con = db.connect()
    task = con.execute("SELECT * FROM build_tasks WHERE task_id=?", (task_id,)).fetchone()
    if task is None:
        con.close()
        return
    batch_id, gen = task["batch_id"], task["generation"]
    staging = _staging_dir(batch_id, gen)
    zip_path = os.path.join(
        config.package_dir(), "impact_package_%s__g%d.zip" % (batch_id, gen))
    try:
        batch = con.execute(
            "SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if batch is None or batch["status"] != "computed":
            raise BuildError("batch %s is not computed (status=%s)"
                             % (batch_id, batch["status"] if batch else "missing"))

        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(os.path.join(staging, "impact"))
        os.makedirs(os.path.join(staging, "evidence"))

        impact = core.load_impact(con, batch_id)
        report_ids = list(REPORTS)
        total = len(report_ids) + 2  # + evidence + finalize
        done = 0
        _progress(con, task_id, done, total)

        # --- step 1..N: one impact file per report (checkpointed) ----------
        for rid in report_ids:
            _check_abort(con, task_id, batch_id, gen)
            cp = con.execute(
                "SELECT payload FROM build_checkpoints WHERE batch_id=? AND report_id=?",
                (batch_id, rid),
            ).fetchone()
            if cp is not None:
                payload = cp["payload"]
                if isinstance(payload, str):
                    payload = payload.encode("utf-8")
            else:
                payload = core.canonical_json(core.impact_file_obj(batch, rid, impact))
                con.execute(
                    "INSERT OR REPLACE INTO build_checkpoints (batch_id, report_id, payload, sha256)"
                    " VALUES (?,?,?,?)",
                    (batch_id, rid, payload, hashlib.sha256(payload).hexdigest()),
                )
                con.commit()
            with open(os.path.join(staging, "impact", "%s.json" % rid), "wb") as fh:
                fh.write(payload)
            done += 1
            _progress(con, task_id, done, total)
            _step_delay()

        # --- step N+1: evidence (boundary snapshot + revision list) --------
        _check_abort(con, task_id, batch_id, gen)
        baseline, revisions = core.evidence_for_batch(con, batch_id)
        with open(os.path.join(staging, "evidence", "baseline_details.jsonl"), "w",
                  encoding="utf-8") as fh:
            for row in baseline:
                fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
        with open(os.path.join(staging, "evidence", "revisions.jsonl"), "w",
                  encoding="utf-8") as fh:
            for rev in revisions:
                fh.write(json.dumps(rev, sort_keys=True, ensure_ascii=False) + "\n")
        done += 1
        _progress(con, task_id, done, total)
        _step_delay()

        # --- step N+2: manifest, verifier, self-check, zip, publish --------
        _check_abort(con, task_id, batch_id, gen)
        files = {}
        for rid in report_ids:
            rel = "impact/%s.json" % rid
            files[rel] = _sha256_path(os.path.join(staging, rel))
        for rel in ("evidence/baseline_details.jsonl", "evidence/revisions.jsonl"):
            files[rel] = _sha256_path(os.path.join(staging, rel))

        totals = {}
        for row in con.execute(
            "SELECT * FROM report_diffs WHERE batch_id=?", (batch_id,)
        ).fetchall():
            totals[row["report_id"]] = {
                "old_amount": row["old_amount"], "old_qty": row["old_qty"],
                "new_amount": row["new_amount"], "new_qty": row["new_qty"],
            }
        generated_at = db.now_iso()
        manifest = {
            "package_type": "impact-explanation-package",
            "format_version": 1,
            "batch_id": batch_id,
            "boundary_version": batch["boundary_version"],
            "generated_at": generated_at,
            "generation": gen,
            "reports": {rid: {"dimensions": REPORTS[rid]["dimensions"],
                              "title": REPORTS[rid]["title"]} for rid in REPORTS},
            "evidence": {"baseline_rows": len(baseline), "revision_rows": len(revisions)},
            "totals": totals,
            "files": files,
        }
        with open(os.path.join(staging, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
        with open(VERIFY_SOURCE, "r", encoding="utf-8") as fh:
            verify_src = fh.read()
        with open(os.path.join(staging, "verify.py"), "w", encoding="utf-8") as fh:
            fh.write(verify_src)
        with open(os.path.join(staging, "README.txt"), "w", encoding="utf-8") as fh:
            fh.write(README_TEMPLATE.format(
                batch_id=batch_id, boundary_version=batch["boundary_version"],
                generated_at=generated_at, generation=gen))

        # Self-check: the embedded verifier must pass on the staged package.
        proc = subprocess.run(
            [sys.executable, "-I", "verify.py"],
            cwd=staging, capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            raise BuildError("package self-check failed:\n%s\n%s" % (proc.stdout, proc.stderr))

        _zip_dir(staging, zip_path)
        sha = _sha256_path(zip_path)

        # Publish: compare-and-swap on the generation. If a newer build was
        # requested while we worked, this build is stale and must not replace
        # the newer package.
        con.execute("BEGIN IMMEDIATE")
        cur = con.execute(
            "UPDATE packages SET status='done', artifact_path=?, artifact_sha256=?,"
            " published_generation=?, updated_at=?"
            " WHERE batch_id=? AND generation=?",
            (zip_path, sha, gen, db.now_iso(), batch_id, gen),
        )
        published = cur.rowcount == 1
        con.commit()

        if published:
            con.execute(
                "UPDATE build_tasks SET status='done', updated_at=? WHERE task_id=?",
                (db.now_iso(), task_id),
            )
            con.execute("DELETE FROM build_checkpoints WHERE batch_id=?", (batch_id,))
            con.commit()
        else:
            con.execute(
                "UPDATE build_tasks SET status='superseded', updated_at=? WHERE task_id=?",
                (db.now_iso(), task_id),
            )
            con.commit()
            if os.path.exists(zip_path):
                os.remove(zip_path)
    except Cancelled:
        _finish(con, task_id, batch_id, gen, "cancelled")
    except Superseded:
        _finish(con, task_id, batch_id, gen, "superseded")
    except Exception as e:  # noqa: BLE001 - record any build failure on the task
        _finish(con, task_id, batch_id, gen, "failed", "%s: %s" % (type(e).__name__, e))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        con.close()
