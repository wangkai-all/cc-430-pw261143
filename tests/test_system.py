"""System tests: boundary consistency, impact math, package verification,
immutability, cancel/resume, generation fencing, and end-to-end services."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from impact import core, db, fiction, package_builder  # noqa: E402


def wait_until(fn, timeout=10.0, interval=0.05):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn():
            return True
        time.sleep(interval)
    return False


class ImpactTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="impact-test-")
        os.environ["IMPACT_HOME"] = self.tmp
        os.environ["IMPACT_BUILD_STEP_DELAY"] = "0"
        os.environ["IMPACT_POLL_INTERVAL"] = "0.05"
        db.init_db()
        con = db.connect()
        fiction.seed_database(con, n=120, seed=7)
        con.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -------------------------------------------------------------

    def current(self, con, detail_id):
        v = int(con.execute("SELECT v FROM meta WHERE k='version'").fetchone()["v"])
        state = {r["detail_id"]: r for r in core.state_at_version(con, v)}
        return state.get(detail_id)

    def accept_and_compute(self, con, changes):
        r = core.accept_batch(con, changes)
        core.compute_impact(con, r["batch_id"])
        con.execute("UPDATE batches SET status='computed' WHERE batch_id=?",
                    (r["batch_id"],))
        con.commit()
        return r["batch_id"]

    def independent_impact(self, con, batch_id):
        """Test-side recomputation of the expected impact, written independently
        of core.compute_impact (baseline replay + apply revisions)."""
        batch = con.execute("SELECT * FROM batches WHERE batch_id=?",
                            (batch_id,)).fetchone()
        boundary = batch["boundary_version"]
        base = {r["detail_id"]: r for r in core.state_at_version(con, boundary)}
        changes = con.execute(
            "SELECT * FROM detail_versions WHERE batch_id=? ORDER BY version",
            (batch_id,)).fetchall()
        state = dict(base)
        for ch in changes:
            state.pop(ch["detail_id"], None)
            if not ch["deleted"]:
                state[ch["detail_id"]] = {f: ch[f] for f in core.FIELDS}
        from impact.reports import REPORTS
        expected = {}
        for rid, spec in REPORTS.items():
            dims = spec["dimensions"]

            def sums(rows):
                acc = {}
                for r in rows:
                    k = tuple(r[d] for d in dims)
                    a, q = acc.get(k, (0, 0))
                    acc[k] = (a + r["amount"], q + r["qty"])
                return acc

            bs, ns = sums(base.values()), sums(state.values())
            rows = {}
            for k in set(bs) | set(ns):
                o, n = bs.get(k, (0, 0)), ns.get(k, (0, 0))
                if o != n:
                    rows[k] = (o, n)
            expected[rid] = {
                "rows": rows,
                "old_total": (sum(v[0] for v in bs.values()), sum(v[1] for v in bs.values())),
                "new_total": (sum(v[0] for v in ns.values()), sum(v[1] for v in ns.values())),
            }
        return expected

    def assert_impact_matches(self, con, batch_id):
        from impact.reports import REPORTS
        expected = self.independent_impact(con, batch_id)
        got = core.load_impact(con, batch_id)
        for rid, exp in expected.items():
            got_rows = {}
            for row in got[rid]["rows"]:
                k = tuple(row["key"][d] for d in REPORTS[rid]["dimensions"])
                got_rows[k] = ((row["old"]["amount"], row["old"]["qty"]),
                               (row["new"]["amount"], row["new"]["qty"]))
            self.assertEqual(got_rows, exp["rows"], "%s rows mismatch" % rid)
            self.assertEqual((got[rid]["total"]["old"]["amount"],
                              got[rid]["total"]["old"]["qty"]), exp["old_total"])
            self.assertEqual((got[rid]["total"]["new"]["amount"],
                              got[rid]["total"]["new"]["qty"]), exp["new_total"])

    def build_and_extract(self, con, batch_id):
        task, created = package_builder.request_build(con, batch_id)
        self.assertTrue(created)
        package_builder.run_build_task(task["task_id"])
        st = package_builder.package_status(con, batch_id)
        self.assertEqual(st["package"]["status"], "done", st)
        xdir = tempfile.mkdtemp(prefix="impact-pkg-")
        self.addCleanup(shutil.rmtree, xdir, True)
        with zipfile.ZipFile(st["package"]["artifact_path"]) as z:
            z.extractall(xdir)
        return st, xdir

    def run_verifier(self, xdir):
        return subprocess.run([sys.executable, "-I", "verify.py"],
                              cwd=xdir, capture_output=True, text=True, timeout=120)


class TestBoundaryAndMath(ImpactTestCase):
    def test_boundary_isolation(self):
        """A batch accepted later must not leak into an earlier batch's impact,
        even if the earlier batch is recomputed afterwards."""
        con = db.connect()
        d1 = self.current(con, "D000001")
        d2 = self.current(con, "D000002")
        b1 = core.accept_batch(con, [{"detail_id": "D000001",
                                      "amount": d1["amount"] + 1000}])
        # B2 is accepted BEFORE B1 is recomputed, and touches the same detail.
        b2 = core.accept_batch(con, [{"detail_id": "D000001",
                                      "amount": d1["amount"] + 9999},
                                     {"detail_id": "D000002",
                                      "amount": d2["amount"] + 500}])
        self.assertLess(b1["boundary_version"], b2["boundary_version"])

        core.compute_impact(con, b1["batch_id"])   # runs AFTER b2 was accepted
        imp1 = core.load_impact(con, b1["batch_id"])
        row = [r for r in imp1["regional_sales"]["rows"]
               if r["key"]["region"] == d1["region"]][0]
        self.assertEqual(row["delta"]["amount"], 1000)          # not 9999
        contrib = row["contributions"][0]
        self.assertEqual(contrib["old"]["amount"], d1["amount"])
        self.assertEqual(contrib["new"]["amount"], d1["amount"] + 1000)

        core.compute_impact(con, b2["batch_id"])
        imp2 = core.load_impact(con, b2["batch_id"])
        row2 = [r for r in imp2["regional_sales"]["rows"]
                if r["key"]["region"] == d1["region"]][0]
        # B2's baseline already contains B1's +1000; D000002's +500 lands in
        # the same region row only if the two details share a region.
        expected = (9999 - 1000) + (500 if d2["region"] == d1["region"] else 0)
        self.assertEqual(row2["delta"]["amount"], expected)
        con.close()

    def test_impact_math_crosscheck(self):
        con = db.connect()
        v = int(con.execute("SELECT v FROM meta WHERE k='version'").fetchone()["v"])
        state = core.state_at_version(con, v)
        # pick three details with distinct products so effects cannot overlap
        seen, picked = set(), []
        for r in state:
            if r["product"] not in seen and r["detail_id"] <= "D000060":
                seen.add(r["product"])
                picked.append(r)
            if len(picked) == 3:
                break
        a, b, c = picked
        other_region = "East" if b["region"] != "East" else "West"
        bid = self.accept_and_compute(con, [
            {"detail_id": a["detail_id"], "amount": a["amount"] + 12345,
             "qty": a["qty"] + 3},                                        # update
            {"detail_id": b["detail_id"], "region": other_region},         # dimension move
            {"detail_id": c["detail_id"], "delete": True},                 # delete
            {"detail_id": "DX00001", "region": "North", "product": "P-99",
             "channel": "online", "amount": 50000, "qty": 7},              # add
        ])
        self.assert_impact_matches(con, bid)
        imp = core.load_impact(con, bid)
        # dimension move shows up as two rows in region_product: old key loses, new key gains
        rp_keys = {(r["key"]["region"], r["key"]["product"]) for r in imp["region_product"]["rows"]}
        self.assertIn((b["region"], b["product"]), rp_keys)
        self.assertIn((other_region, b["product"]), rp_keys)
        # ...but a pure region move must NOT create a zero-delta row in product_sales
        ps_keys = {r["key"]["product"] for r in imp["product_sales"]["rows"]}
        self.assertNotIn(b["product"], ps_keys)
        self.assertEqual(ps_keys, {a["product"], c["product"], "P-99"})
        con.close()

    def test_compute_idempotent(self):
        con = db.connect()
        d = self.current(con, "D000005")
        r = core.accept_batch(con, [{"detail_id": "D000005", "amount": d["amount"] + 42}])
        core.compute_impact(con, r["batch_id"])
        first = core.load_impact(con, r["batch_id"])
        core.compute_impact(con, r["batch_id"])  # re-run
        second = core.load_impact(con, r["batch_id"])
        self.assertEqual(first, second)
        con.close()

    def test_validation(self):
        con = db.connect()
        d = self.current(con, "D000001")
        bad = [
            [],                                                        # empty
            [{"detail_id": "D000001"}],                                # no-op
            [{"detail_id": "D000001", "amount": d["amount"]}],         # no-op
            [{"detail_id": "NOPE", "delete": True}],                   # delete missing
            [{"detail_id": "D000001", "amount": -5}],                  # negative
            [{"detail_id": "D000001", "amount": "x"}],                 # wrong type
            [{"detail_id": "D000001", "amount": 1},
             {"detail_id": "D000001", "qty": 2}],                      # duplicate id
            [{"detail_id": "NEW1", "amount": 100}],                    # incomplete add
        ]
        for changes in bad:
            with self.assertRaises(core.Validation, msg=str(changes)):
                core.accept_batch(con, changes)
        con.close()


class TestPackage(ImpactTestCase):
    def make_batch(self, con):
        a, b = self.current(con, "D000011"), self.current(con, "D000012")
        return self.accept_and_compute(con, [
            {"detail_id": "D000011", "amount": a["amount"] + 30000, "qty": a["qty"] + 4},
            {"detail_id": "D000012", "delete": True},
            {"detail_id": "DX10001", "region": "South", "product": "P-05",
             "channel": "retail", "amount": 66000, "qty": 11},
        ])

    def test_build_and_independent_verification(self):
        con = db.connect()
        bid = self.make_batch(con)
        st, xdir = self.build_and_extract(con, bid)
        proc = self.run_verifier(xdir)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("VERIFICATION PASSED", proc.stdout)
        # package contains the promised parts
        names = set()
        with zipfile.ZipFile(st["package"]["artifact_path"]) as z:
            names = set(z.namelist())
        for want in ("manifest.json", "verify.py", "README.txt",
                     "evidence/baseline_details.jsonl", "evidence/revisions.jsonl",
                     "impact/regional_sales.json", "impact/region_product.json"):
            self.assertIn(want, names)
        con.close()

    def test_package_immutable_under_source_updates(self):
        con = db.connect()
        bid = self.make_batch(con)
        st, xdir = self.build_and_extract(con, bid)
        sha_before = st["package"]["artifact_sha256"]
        impact_before = core.load_impact(con, bid)

        # the source keeps updating afterwards
        d = self.current(con, "D000040")
        self.accept_and_compute(con, [{"detail_id": "D000040",
                                       "amount": d["amount"] + 777}])
        self.accept_and_compute(con, [{"detail_id": "D000041", "delete": True}])

        st2 = package_builder.package_status(con, bid)
        with open(st2["package"]["artifact_path"], "rb") as fh:
            import hashlib
            sha_after = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(sha_before, sha_after)
        self.assertEqual(core.load_impact(con, bid), impact_before)
        proc = self.run_verifier(xdir)   # still verifies, offline
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        con.close()

    def test_tamper_detection(self):
        con = db.connect()
        bid = self.make_batch(con)
        _st, xdir = self.build_and_extract(con, bid)

        # tamper with an impact number
        p = os.path.join(xdir, "impact", "regional_sales.json")
        with open(p) as fh:
            obj = json.load(fh)
        obj["affected_rows"][0]["new"]["amount"] += 1
        with open(p, "w") as fh:
            json.dump(obj, fh)
        proc = self.run_verifier(xdir)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("VERIFICATION FAILED", proc.stdout)
        con.close()

    def test_tamper_detection_evidence(self):
        con = db.connect()
        bid = self.make_batch(con)
        _st, xdir = self.build_and_extract(con, bid)
        p = os.path.join(xdir, "evidence", "baseline_details.jsonl")
        with open(p) as fh:
            lines = fh.read().splitlines()
        row = json.loads(lines[0])
        row["amount"] += 1
        lines[0] = json.dumps(row, sort_keys=True)
        with open(p, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        proc = self.run_verifier(xdir)
        self.assertEqual(proc.returncode, 1)
        con.close()

    def test_cancel_then_resume_uses_checkpoints(self):
        os.environ["IMPACT_BUILD_STEP_DELAY"] = "0.15"
        con = db.connect()
        bid = self.make_batch(con)
        task, _ = package_builder.request_build(con, bid)

        t = threading.Thread(target=package_builder.run_build_task,
                             args=(task["task_id"],))
        t.start()
        ok = wait_until(
            lambda: package_builder.package_status(con, bid)["checkpoints"] >= 1,
            timeout=10)
        self.assertTrue(ok, "builder never wrote a checkpoint")
        package_builder.request_cancel(con, bid)
        t.join(10)
        self.assertFalse(t.is_alive())

        st = package_builder.package_status(con, bid)
        self.assertEqual(st["latest_task"]["status"], "cancelled")
        self.assertEqual(st["package"]["status"], "cancelled")
        self.assertIsNone(st["package"]["artifact_path"])
        self.assertGreaterEqual(st["checkpoints"], 1)

        # resume: new generation, reuses checkpoints, publishes
        os.environ["IMPACT_BUILD_STEP_DELAY"] = "0"
        task2, created = package_builder.request_build(con, bid)
        self.assertTrue(created)
        self.assertEqual(task2["generation"], task["generation"] + 1)
        package_builder.run_build_task(task2["task_id"])

        st = package_builder.package_status(con, bid)
        self.assertEqual(st["package"]["status"], "done")
        self.assertEqual(st["package"]["published_generation"], task2["generation"])
        self.assertEqual(st["checkpoints"], 0)  # cleared after publish
        _st2, xdir = self.build_xdir_only(st)
        proc = self.run_verifier(xdir)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        con.close()

    def build_xdir_only(self, st):
        xdir = tempfile.mkdtemp(prefix="impact-pkg-")
        self.addCleanup(shutil.rmtree, xdir, True)
        with zipfile.ZipFile(st["package"]["artifact_path"]) as z:
            z.extractall(xdir)
        return st, xdir

    def test_stale_build_cannot_replace_newer_package(self):
        """An old generation finishing late must not publish (CAS fencing)."""
        con = db.connect()
        bid = self.make_batch(con)
        task, _ = package_builder.request_build(con, bid)   # generation 1
        # a newer build gets registered while gen-1 is still working
        con.execute("UPDATE packages SET generation=2 WHERE batch_id=?", (bid,))
        con.commit()
        # force the old build to run all the way to the publish step
        orig = package_builder._check_abort
        package_builder._check_abort = lambda *a, **k: None
        try:
            package_builder.run_build_task(task["task_id"])
        finally:
            package_builder._check_abort = orig
        st = package_builder.package_status(con, bid)
        self.assertEqual(st["latest_task"]["status"], "superseded")
        self.assertIsNone(st["package"]["published_generation"])
        self.assertIsNone(st["package"]["artifact_path"])
        leftovers = [f for f in os.listdir(os.path.join(self.tmp, "packages"))
                     if f.endswith(".zip")]
        self.assertEqual(leftovers, [])
        con.close()

    def test_build_requires_computed_batch(self):
        con = db.connect()
        d = self.current(con, "D000050")
        r = core.accept_batch(con, [{"detail_id": "D000050",
                                     "amount": d["amount"] + 1}])  # not computed
        with self.assertRaises(package_builder.NotReady):
            package_builder.request_build(con, r["batch_id"])
        con.close()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestEndToEnd(unittest.TestCase):
    """All three services as real subprocesses; drive the full flow over HTTP."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="impact-e2e-")
        os.environ["IMPACT_HOME"] = self.tmp
        db.init_db()
        con = db.connect()
        fiction.seed_database(con, n=150, seed=11)
        con.close()

        self.ports = {"ingest": free_port(), "worker": free_port(), "pkg": free_port()}
        env = dict(os.environ)
        env.update({
            "IMPACT_HOME": self.tmp,
            "IMPACT_INGEST_PORT": str(self.ports["ingest"]),
            "IMPACT_WORKER_PORT": str(self.ports["worker"]),
            "IMPACT_PACKAGE_PORT": str(self.ports["pkg"]),
            "IMPACT_INGEST_BASE": "http://127.0.0.1:%d" % self.ports["ingest"],
            "IMPACT_POLL_INTERVAL": "0.05",
            "IMPACT_BUILD_STEP_DELAY": "0.01",
            "PYTHONPATH": REPO,
        })
        self.procs = []
        for mod in ("impact.ingestd", "impact.recomputed", "impact.packagesvc"):
            self.procs.append(subprocess.Popen(
                [sys.executable, "-m", mod], cwd=REPO, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))
        for port in self.ports.values():
            self.assertTrue(wait_until(lambda p=port: self._healthy(p), timeout=20),
                            "service on %s did not start" % port)

    def tearDown(self):
        for p in self.procs:
            p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
            if p.stdout:
                p.stdout.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _healthy(self, port):
        try:
            self.http("GET", "http://127.0.0.1:%d/healthz" % port)
            return True
        except Exception:
            return False

    def http(self, method, url, payload=None, raw=False):
        req = urllib.request.Request(
            url, method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()
            if raw:
                return resp.status, dict(resp.headers), body
            return json.loads(body.decode() or "{}")

    def pkg(self, path, **kw):
        return self.http("GET", "http://127.0.0.1:%d%s" % (self.ports["pkg"], path), **kw)

    def test_full_flow(self):
        base = "http://127.0.0.1:%d" % self.ports["pkg"]
        det = self.pkg("/api/sample-details?limit=4")["details"]
        a, b = det[0], det[1]
        r = self.http("POST", base + "/api/submit", {"changes": [
            {"detail_id": a["detail_id"], "amount": a["amount"] + 20000, "qty": a["qty"] + 2},
            {"detail_id": b["detail_id"], "delete": True},
            {"detail_id": "DXE2E01", "region": "West", "product": "P-09",
             "channel": "wholesale", "amount": 91000, "qty": 13},
        ]})
        bid = r["batch_id"]
        self.assertEqual(r["n_changes"], 3)

        self.assertTrue(wait_until(
            lambda: self.pkg("/api/batches/" + bid)["batch"]["status"] == "computed",
            timeout=20), "batch never computed")

        imp = self.pkg("/api/batches/%s/impact" % bid)["impact"]
        self.assertEqual(imp["regional_sales"]["total"]["delta"]["amount"],
                         20000 - b["amount"] + 91000)
        rows = imp["regional_sales"]["rows"]
        self.assertTrue(any(row["key"]["region"] == a["region"] for row in rows))

        r = self.http("POST", base + "/api/packages/%s/build" % bid)
        self.assertTrue(r["created"])
        self.assertTrue(wait_until(
            lambda: (self.pkg("/api/packages/%s/status" % bid)["package"] or {})
                    .get("status") == "done",
            timeout=30), "package never published")

        status, headers, data = self.http(
            "GET", base + "/api/packages/%s/download" % bid, raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Sha256") or headers.get("x-sha256"),
                         __import__("hashlib").sha256(data).hexdigest())

        xdir = tempfile.mkdtemp(prefix="impact-e2e-pkg-")
        self.addCleanup(shutil.rmtree, xdir, True)
        zpath = os.path.join(xdir, "pkg.zip")
        with open(zpath, "wb") as fh:
            fh.write(data)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(xdir)
        proc = subprocess.run([sys.executable, "-I", "verify.py"],
                              cwd=xdir, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        # source keeps updating; the published package must not change
        c = det[2]
        self.http("POST", base + "/api/submit",
                  {"changes": [{"detail_id": c["detail_id"], "amount": c["amount"] + 5}]})
        _s, _h, data2 = self.http("GET", base + "/api/packages/%s/download" % bid, raw=True)
        self.assertEqual(data, data2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
