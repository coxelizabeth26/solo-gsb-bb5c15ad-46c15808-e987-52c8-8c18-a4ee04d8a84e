"""End-to-end API tests using FastAPI's TestClient against a temp SQLite DB."""
import os
import sqlite3
import tempfile
import threading
import unittest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["AGENDA_DB"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.client.__enter__()  # trigger lifespan -> init_db

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        os.unlink(_tmp.name)

    def setUp(self):
        # fresh meeting per test
        r = self.client.post("/api/meetings", json={
            "title": "测试会议",
            "start_time": "2026-09-21T09:00",
            "end_time": "2026-09-21T10:00",
        })
        self.assertEqual(r.status_code, 201)
        self.mid = r.json()["id"]

    def add_item(self, title, planned, minimum, priority=3, incompressible=False):
        r = self.client.post(f"/api/meetings/{self.mid}/items", json={
            "title": title, "planned_minutes": planned,
            "minimum_minutes": minimum, "priority": priority,
            "incompressible": incompressible,
        })
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["id"]

    def add_break(self, start, duration, title="茶歇"):
        r = self.client.post(f"/api/meetings/{self.mid}/breaks", json={
            "title": title, "fixed_start": start, "duration_minutes": duration,
        })
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["id"]

    # -- optimistic-concurrency helpers ------------------------------------
    def get_plan(self, mid=None):
        r = self.client.get(f"/api/meetings/{mid or self.mid}")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def revision(self, mid=None):
        return self.get_plan(mid)["revision"]

    def record_actual(self, item_id, actual_end, mid=None, revision=None):
        mid = mid or self.mid
        if revision is None:
            revision = self.revision(mid)
        return self.client.post(f"/api/meetings/{mid}/actuals", json={
            "item_id": item_id, "actual_end": actual_end,
            "revision": revision})

    def undo(self, mid=None, revision=None):
        mid = mid or self.mid
        if revision is None:
            revision = self.revision(mid)
        return self.client.post(f"/api/meetings/{mid}/undo",
                                json={"revision": revision})

    def reset(self, mid=None, revision=None):
        mid = mid or self.mid
        if revision is None:
            revision = self.revision(mid)
        return self.client.post(f"/api/meetings/{mid}/reset",
                                json={"revision": revision})

    def preview(self, mid=None):
        return self.client.post(
            f"/api/meetings/{mid or self.mid}/postponements/preview")

    def confirm(self, item_ids, revision, mid=None):
        return self.client.post(
            f"/api/meetings/{mid or self.mid}/postponements",
            json={"item_ids": item_ids, "revision": revision})

    def test_meeting_validation(self):
        r = self.client.post("/api/meetings", json={
            "title": "x", "start_time": "2026-09-21T10:00",
            "end_time": "2026-09-21T09:00",
        })
        self.assertEqual(r.status_code, 422)

    def test_item_validation(self):
        r = self.client.post(f"/api/meetings/{self.mid}/items", json={
            "title": "bad", "planned_minutes": 10, "minimum_minutes": 20})
        self.assertEqual(r.status_code, 422)

    def test_plan_endpoint_structure(self):
        self.add_item("开场", 10, 5, priority=5)
        self.add_item("讨论", 30, 10, priority=2)
        self.add_break("2026-09-21T09:30", 10)
        r = self.client.get(f"/api/meetings/{self.mid}")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertFalse(data["live"])
        self.assertEqual(data["revision"], 0)
        self.assertEqual(len(data["original"]["segments"]), 3)
        self.assertIn("current", data)

    def test_record_actual_triggers_compression_and_conflict(self):
        a = self.add_item("A", 20, 10, priority=5)
        b = self.add_item("B", 25, 5, priority=1)
        c = self.add_item("C", 20, 10, priority=5)
        # A finishes 35 minutes late at 09:35
        r = self.record_actual(a, "2026-09-21T09:35")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data["live"])
        self.assertEqual(data["revision"], 1)
        segs = {s["id"]: s for s in data["current"]["segments"] if s["kind"] == "item"}
        self.assertEqual(segs[b]["duration"], 5)
        self.assertEqual(segs[b]["status"], "compressed")
        self.assertEqual(segs[c]["duration"], 20)
        self.assertEqual(data["current"]["planned_end"], "2026-09-21T10:00")
        self.assertEqual(data["current"]["conflicts"], [])

    def test_overflow_conflict_reports_minutes(self):
        r = self.client.post("/api/meetings", json={
            "title": "短会", "start_time": "2026-09-21T09:00",
            "end_time": "2026-09-21T09:30"})
        mid = r.json()["id"]
        a = self.client.post(f"/api/meetings/{mid}/items", json={
            "title": "A", "planned_minutes": 10, "minimum_minutes": 5}).json()["id"]
        self.client.post(f"/api/meetings/{mid}/items", json={
            "title": "B", "planned_minutes": 20, "minimum_minutes": 20,
            "priority": 1, "incompressible": True})
        r = self.record_actual(a, "2026-09-21T09:20", mid=mid)
        data = r.json()
        conflicts = data["current"]["conflicts"]
        self.assertTrue(any(x["type"] == "overflow" and x["overflow_minutes"] == 10
                            for x in conflicts))

    def test_ordered_recording_required(self):
        a = self.add_item("A", 10, 5)
        b = self.add_item("B", 10, 5)
        r = self.record_actual(b, "2026-09-21T09:10")
        self.assertEqual(r.status_code, 409)
        # A first -> ok
        r = self.record_actual(a, "2026-09-21T09:10")
        self.assertEqual(r.status_code, 200)

    def test_undo_restores_plan(self):
        a = self.add_item("A", 20, 10)
        self.add_item("B", 20, 10)
        self.record_actual(a, "2026-09-21T09:25")
        r = self.undo()
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertFalse(data["live"])
        self.assertEqual(data["undo_count"], 0)
        # undo again -> 404
        r = self.undo()
        self.assertEqual(r.status_code, 404)

    def test_reset_is_undoable(self):
        a = self.add_item("A", 20, 10)
        self.record_actual(a, "2026-09-21T09:25")
        r = self.reset()
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["live"])
        r = self.undo()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["live"])

    def test_fixed_break_overlap_rejected(self):
        self.add_break("2026-09-21T09:30", 20)
        r = self.client.post(f"/api/meetings/{self.mid}/breaks", json={
            "title": "另一个", "fixed_start": "2026-09-21T09:40",
            "duration_minutes": 10})
        self.assertEqual(r.status_code, 422)

    def test_editing_blocked_while_live(self):
        a = self.add_item("A", 20, 10)
        self.record_actual(a, "2026-09-21T09:25")
        self.assertEqual(self.client.post(
            f"/api/meetings/{self.mid}/items",
            json={"title": "X", "planned_minutes": 5, "minimum_minutes": 5}
        ).status_code, 409)
        self.assertEqual(self.client.delete(f"/api/items/{a}").status_code, 409)

    def test_move_item(self):
        a = self.add_item("A", 10, 5)
        b = self.add_item("B", 10, 5)
        r = self.client.post(f"/api/items/{b}/move", json={"direction": "up"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([it["id"] for it in r.json()], [b, a])

    # ------------------------------------------------------------------ postponements
    def test_preview_requires_live_and_conflict(self):
        a = self.add_item("A", 20, 10)
        # not live -> 409
        self.assertEqual(
            self.client.post(f"/api/meetings/{self.mid}/postponements/preview").status_code,
            409)
        # live but everything fits -> 409
        r = self.record_actual(a, "2026-09-21T09:20")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            self.client.post(f"/api/meetings/{self.mid}/postponements/preview").status_code,
            409)

    def _make_overrun_meeting(self, end="2026-09-21T09:32"):
        r = self.client.post("/api/meetings", json={
            "title": "短会", "start_time": "2026-09-21T09:00", "end_time": end})
        mid = r.json()["id"]
        a = self.client.post(f"/api/meetings/{mid}/items", json={
            "title": "A", "planned_minutes": 10, "minimum_minutes": 5,
            "priority": 5}).json()["id"]
        lock = self.client.post(f"/api/meetings/{mid}/items", json={
            "title": "Lock", "planned_minutes": 10, "minimum_minutes": 10,
            "priority": 5, "incompressible": True}).json()["id"]
        lo = self.client.post(f"/api/meetings/{mid}/items", json={
            "title": "Lo", "planned_minutes": 10, "minimum_minutes": 10,
            "priority": 1}).json()["id"]
        hi = self.client.post(f"/api/meetings/{mid}/items", json={
            "title": "Hi", "planned_minutes": 10, "minimum_minutes": 2,
            "priority": 5}).json()["id"]
        self.record_actual(a, "2026-09-21T09:20", mid=mid)
        return mid, a, lock, lo, hi

    def test_postpone_preview_confirm_and_reschedule(self):
        # A done 09:20; 12 minutes remain for Lock(10)+Lo(10)+Hi(10/2);
        # removing Lo (P1) lets Lock(10) + Hi compressed (2) fill the gap
        mid, a, lock, lo, hi = self._make_overrun_meeting()
        r = self.preview(mid)
        self.assertEqual(r.status_code, 200, r.text)
        s = r.json()
        self.assertTrue(s["feasible"])
        self.assertEqual(s["item_ids"], [lo])
        # the preview stamps the revision it was computed at
        self.assertEqual(s["revision"], 1)
        # preview writes nothing
        before = self.client.get(f"/api/meetings/{mid}").json()
        self.assertFalse(any(it["id"] == lo and it["postponed"]
                             for it in before["items"]))
        # confirm: atomic save + immediate reschedule
        r = self.confirm([lo], s["revision"], mid=mid)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["revision"], 2)
        self.assertEqual(data["undo_count"], 2)  # actual + postpone
        self.assertTrue(any(it["id"] == lo and it["postponed"] for it in data["items"]))
        seg_ids = {x["id"] for x in data["current"]["segments"] if x["kind"] == "item"}
        self.assertNotIn(lo, seg_ids)
        self.assertIn(hi, seg_ids)
        self.assertIn(lock, seg_ids)
        hard = [c for c in data["current"]["conflicts"]
                if c["type"] in ("overflow", "item_before_break")]
        self.assertEqual(hard, [])
        # the postpone enters the undo chain
        r = self.undo(mid=mid)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertFalse(any(it["postponed"] for it in data["items"]))

    def test_confirm_rejected_when_overflow_remains(self):
        r = self.client.post("/api/meetings", json={
            "title": "短会", "start_time": "2026-09-21T09:00",
            "end_time": "2026-09-21T09:30"})
        mid = r.json()["id"]
        a = self.client.post(f"/api/meetings/{mid}/items", json={
            "title": "A", "planned_minutes": 10, "minimum_minutes": 5}).json()["id"]
        lock = self.client.post(f"/api/meetings/{mid}/items", json={
            "title": "Lock", "planned_minutes": 20, "minimum_minutes": 20,
            "incompressible": True}).json()["id"]
        self.record_actual(a, "2026-09-21T09:20", mid=mid)
        r = self.preview(mid)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["feasible"])
        self.assertEqual(r.json()["remaining_overflow_minutes"], 10)
        # the locked item cannot be submitted as a postponement
        r = self.confirm([lock], self.revision(mid), mid=mid)
        self.assertEqual(r.status_code, 422)

    def test_cannot_postpone_completed_or_incompressible(self):
        a = self.add_item("A", 10, 5)
        lock = self.add_item("Lock", 20, 20, incompressible=True)
        self.record_actual(a, "2026-09-21T09:50")
        rev = self.revision()
        self.assertEqual(self.confirm([a], rev).status_code, 422)
        self.assertEqual(self.confirm([lock], rev).status_code, 422)

    def test_postponed_items_skipped_in_ordered_recording(self):
        # 09:00-09:45 meeting: A done 09:20 (25 min left); B 30/30 (P1) +
        # C 10/10 = 40 -> overflow 15; B alone removed leaves 10 <= 25
        r = self.client.post("/api/meetings", json={
            "title": "短会", "start_time": "2026-09-21T09:00",
            "end_time": "2026-09-21T09:45"})
        mid = r.json()["id"]
        a = self.client.post(f"/api/meetings/{mid}/items", json={
            "title": "A", "planned_minutes": 10, "minimum_minutes": 5,
            "priority": 5}).json()["id"]
        b = self.client.post(f"/api/meetings/{mid}/items", json={
            "title": "B", "planned_minutes": 30, "minimum_minutes": 30,
            "priority": 1}).json()["id"]
        c = self.client.post(f"/api/meetings/{mid}/items", json={
            "title": "C", "planned_minutes": 10, "minimum_minutes": 10,
            "priority": 5}).json()["id"]
        self.record_actual(a, "2026-09-21T09:20", mid=mid)
        r = self.preview(mid)
        self.assertEqual(r.json()["item_ids"], [b])
        r = self.confirm([b], r.json()["revision"], mid=mid)
        self.assertEqual(r.status_code, 200, r.text)
        # recording C directly is allowed because B is deferred; recording B
        # itself is rejected
        r = self.record_actual(c, "2026-09-21T09:40", mid=mid)
        self.assertEqual(r.status_code, 200, r.text)
        r = self.record_actual(b, "2026-09-21T09:45", mid=mid)
        self.assertEqual(r.status_code, 409)

    def test_reset_clears_postponed_flags(self):
        # 09:00-09:25: A done 09:20 (5 min left); B 20/20 alone -> overflow 15
        r = self.client.post("/api/meetings", json={
            "title": "短会", "start_time": "2026-09-21T09:00",
            "end_time": "2026-09-21T09:25"})
        mid = r.json()["id"]
        a = self.client.post(f"/api/meetings/{mid}/items", json={
            "title": "A", "planned_minutes": 10, "minimum_minutes": 5}).json()["id"]
        b = self.client.post(f"/api/meetings/{mid}/items", json={
            "title": "B", "planned_minutes": 20, "minimum_minutes": 20,
            "priority": 1}).json()["id"]
        self.record_actual(a, "2026-09-21T09:20", mid=mid)
        r = self.preview(mid)
        self.assertEqual(r.json()["item_ids"], [b])
        r = self.confirm([b], r.json()["revision"], mid=mid)
        self.assertEqual(r.status_code, 200)
        r = self.reset(mid=mid)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(any(it["postponed"] for it in r.json()["items"]))
        # single undo restores both actuals and postponements
        r = self.undo(mid=mid)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["live"])
        self.assertTrue(any(it["id"] == b and it["postponed"] for it in data["items"]))

    # ------------------------------------------------------------------ revision / OCC
    def assert_revision_conflict(self, resp):
        self.assertEqual(resp.status_code, 409, resp.text)
        detail = resp.json()["detail"]
        self.assertIsInstance(detail, dict)
        self.assertEqual(detail["code"], "revision_conflict")

    def test_revision_monotonic_and_echoed(self):
        # every successful live mutation bumps the revision; undo of an empty
        # chain leaves it untouched
        self.assertEqual(self.revision(), 0)
        a = self.add_item("A", 20, 10)
        self.add_item("B", 20, 10)
        r = self.record_actual(a, "2026-09-21T09:25")
        self.assertEqual(r.json()["revision"], 1)
        r = self.undo()
        self.assertEqual(r.json()["revision"], 2)
        # nothing to undo -> 404 and revision unchanged
        r = self.undo()
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.revision(), 2)
        self.record_actual(a, "2026-09-21T09:25", revision=2)
        r = self.reset()
        self.assertEqual(r.json()["revision"], 4)

    def test_stale_record_actual_rejected_without_changes(self):
        a = self.add_item("A", 20, 10)
        b = self.add_item("B", 20, 10)
        stale = self.revision()  # 0
        self.record_actual(a, "2026-09-21T09:25")
        self.assertEqual(self.revision(), 1)
        # record the NEXT pending item with the old revision: it passes every
        # semantic check, so the rejection must come from the in-transaction
        # revision compare
        r = self.record_actual(b, "2026-09-21T09:50", revision=stale)
        self.assert_revision_conflict(r)
        # data untouched: B still pending, revision stays 1, no extra undo entry
        plan = self.get_plan()
        self.assertEqual(plan["revision"], 1)
        self.assertEqual(plan["undo_count"], 1)
        self.assertIsNone(next(it for it in plan["items"]
                               if it["id"] == b)["actual_end"])
        # a fresh revision works
        self.assertEqual(self.record_actual(b, "2026-09-21T09:50").status_code, 200)

    def test_revision_required_by_live_mutations(self):
        a = self.add_item("A", 20, 10)
        self.record_actual(a, "2026-09-21T09:25")
        # missing revision -> request body validation error
        self.assertEqual(self.client.post(
            f"/api/meetings/{self.mid}/actuals",
            json={"item_id": a, "actual_end": "2026-09-21T09:30"}).status_code, 422)
        self.assertEqual(self.client.post(
            f"/api/meetings/{self.mid}/undo").status_code, 422)
        self.assertEqual(self.client.post(
            f"/api/meetings/{self.mid}/reset").status_code, 422)
        self.assertEqual(self.client.post(
            f"/api/meetings/{self.mid}/postponements",
            json={"item_ids": [a]}).status_code, 422)

    def test_stale_undo_and_reset_rejected(self):
        a = self.add_item("A", 20, 10)
        self.record_actual(a, "2026-09-21T09:25")
        stale = self.revision()  # 1
        # an undo moves the timeline to revision 2 and clears live state
        self.assertEqual(self.undo().status_code, 200)
        # stale undo: the old entry is already gone -> revision check first,
        # data (nothing to undo) untouched
        r = self.client.post(f"/api/meetings/{self.mid}/undo",
                             json={"revision": stale})
        self.assert_revision_conflict(r)
        # make the meeting live again at revision 3, then reset with the
        # stale token captured before it
        self.assertEqual(
            self.record_actual(a, "2026-09-21T09:25", revision=2).status_code,
            200)
        r = self.client.post(f"/api/meetings/{self.mid}/reset",
                             json={"revision": stale})
        self.assert_revision_conflict(r)
        plan = self.get_plan()
        self.assertEqual(plan["revision"], 3)
        self.assertTrue(plan["live"])
        self.assertEqual(plan["undo_count"], 1)

    def test_stale_preview_cannot_confirm_against_new_timeline(self):
        # preview stamps revision 1; an intervening undo moves the meeting to
        # revision 2, so confirming the old preview must fail and change nothing
        mid, a, lock, lo, hi = self._make_overrun_meeting()
        s = self.preview(mid).json()
        self.assertEqual(s["revision"], 1)
        self.assertEqual(self.undo(mid=mid, revision=1).status_code, 200)
        # re-record A to keep the meeting live: revision moves on again
        self.assertEqual(self.record_actual(
            a, "2026-09-21T09:20", mid=mid, revision=2).status_code, 200)
        self.assertEqual(self.revision(mid), 3)

        r = self.confirm(s["item_ids"], s["revision"], mid=mid)
        self.assert_revision_conflict(r)
        # nothing postponed, revision/undo-chain untouched
        plan = self.get_plan(mid)
        self.assertEqual(plan["revision"], 3)
        self.assertEqual(plan["undo_count"], 1)
        self.assertFalse(any(it["postponed"] for it in plan["items"]))
        # a fresh preview confirms fine
        s2 = self.preview(mid).json()
        self.assertEqual(s2["revision"], 3)
        self.assertEqual(self.confirm(s2["item_ids"], 3, mid=mid).status_code, 200)

    def test_concurrent_actuals_only_one_wins(self):
        # Two clients race the record transaction itself. The leader reaches
        # the revision compare first and is held there; the follower enters
        # the write lock afterwards and must lose with RevisionConflict, so
        # exactly one actual (and one revision bump) is committed.
        a = self.add_item("A", 20, 10)

        entered = threading.Event()
        release = threading.Event()
        outcomes = []
        original_check = db._check_revision

        def gate_check(conn, meeting_id, expected_revision):
            if not entered.is_set():
                entered.set()
                release.wait(5)
            return original_check(conn, meeting_id, expected_revision)

        db._check_revision = gate_check

        def record(leader):
            try:
                if not leader:
                    entered.wait(5)
                    release.set()  # both are contending: let the pair run
                self.client.post(
                    f"/api/meetings/{self.mid}/actuals",
                    json={"item_id": a,
                          "actual_end": "2026-09-21T09:25", "revision": 0})
                outcomes.append("sent")
            finally:
                db._check_revision = original_check

        t1 = threading.Thread(target=record, args=(True,))
        t2 = threading.Thread(target=record, args=(False,))
        t1.start(); t2.start(); t1.join(); t2.join()
        release.set()

        plan = self.get_plan()
        self.assertEqual(plan["revision"], 1)
        self.assertEqual(plan["undo_count"], 1)
        done = [it for it in plan["items"] if it["actual_end"]]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["actual_end"], "2026-09-21T09:25")

    def test_concurrent_undo_only_one_wins(self):
        a = self.add_item("A", 20, 10)
        self.record_actual(a, "2026-09-21T09:25")
        rev = self.revision()  # 1
        barrier = threading.Barrier(2)
        results = []

        def undo():
            barrier.wait()
            results.append(self.client.post(
                f"/api/meetings/{self.mid}/undo", json={"revision": rev}))

        t1 = threading.Thread(target=undo)
        t2 = threading.Thread(target=undo)
        t1.start(); t2.start(); t1.join(); t2.join()

        statuses = {r.status_code for r in results}
        self.assertEqual(statuses, {200, 409})
        conflict = next(r for r in results if r.status_code == 409)
        self.assertEqual(conflict.json()["detail"]["code"],
                         "revision_conflict")
        plan = self.get_plan()
        self.assertEqual(plan["revision"], 2)
        self.assertEqual(plan["undo_count"], 0)
        self.assertFalse(plan["live"])

    def test_concurrent_confirm_only_one_wins(self):
        # Two clients race the confirm transaction itself. At the storage
        # layer the loser must get RevisionConflict and roll back fully:
        # exactly one postponement + one undo entry survive.
        mid, a, lock, lo, hi = self._make_overrun_meeting()
        s = self.preview(mid).json()
        self.assertEqual(s["revision"], 1)

        entered = threading.Event()
        release = threading.Event()
        outcomes = []

        original_check = db._check_revision

        def gate_check(conn, meeting_id, expected_revision):
            # let the first transaction reach its revision check, then hold it
            # until the second one is also queued behind the write lock
            if not entered.is_set():
                entered.set()
                release.wait(5)
            return original_check(conn, meeting_id, expected_revision)

        db._check_revision = gate_check

        def confirm(first):
            try:
                if not first:
                    entered.wait(5)
                    # both transactions are now contending: release the
                    # leader; BEGIN IMMEDIATE serialises the pair
                    release.set()
                db.postpone_items(mid, s["item_ids"], s["revision"])
                outcomes.append(("ok", first))
            except db.RevisionConflict:
                outcomes.append(("conflict", first))
            finally:
                db._check_revision = original_check

        t1 = threading.Thread(target=confirm, args=(True,))
        t2 = threading.Thread(target=confirm, args=(False,))
        t1.start(); t2.start(); t1.join(); t2.join()
        release.set()  # no-op if already set; never deadlock a stray waiter

        self.assertEqual(sorted(o[0] for o in outcomes),
                         ["conflict", "ok"], outcomes)
        plan = self.get_plan(mid)
        self.assertEqual(plan["revision"], 2)
        self.assertEqual(plan["undo_count"], 2)  # actual + single postpone
        self.assertEqual([it["id"] for it in plan["items"] if it["postponed"]],
                         [lo])


class RevisionMigrationTest(unittest.TestCase):
    """A database file created before the revision column must auto-migrate."""

    def test_legacy_db_gains_revision(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        legacy = sqlite3.connect(tmp.name)
        legacy.executescript(
            """
            CREATE TABLE meetings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                planned_minutes INTEGER NOT NULL,
                minimum_minutes INTEGER NOT NULL,
                priority INTEGER NOT NULL DEFAULT 3,
                incompressible INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0,
                actual_end TEXT,
                postponed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE breaks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '休息',
                fixed_start TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL
            );
            CREATE TABLE undo_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                snapshot TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO meetings (title, start_time, end_time, created_at)
            VALUES ('旧库会议', '2026-09-21T09:00', '2026-09-21T10:00',
                    '2026-09-21T08:00');
            INSERT INTO items (id, meeting_id, title, planned_minutes,
                               minimum_minutes, priority, incompressible,
                               sort_order, actual_end, postponed)
            VALUES (1, 1, 'A', 10, 5, 3, 0, 0, NULL, 0);
            """)
        legacy.commit()
        legacy.close()

        try:
            old_path = db.DB_PATH
            db.DB_PATH = tmp.name
            try:
                db.init_db()
                meeting = db.get_meeting(1)
                self.assertEqual(meeting["revision"], 0)
                # a stale revision is rejected inside one transaction
                with self.assertRaises(db.RevisionConflict):
                    db.record_actual(1, 1, "2026-09-21T09:10", 99)
                # the correct migrated revision (0) is accepted and bumps it
                db.record_actual(1, 1, "2026-09-21T09:10", 0)
                self.assertEqual(db.get_meeting(1)["revision"], 1)
            finally:
                db.DB_PATH = old_path
        finally:
            os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main()
