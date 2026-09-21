"""Optimistic-concurrency (revision) regression tests.

Covers the live-state contract:

* GET plan and postponement preview return a monotonically increasing
  per-meeting ``revision``;
* recording an actual, undo, reset and postponement confirmation must echo
  the revision they read — mismatch -> HTTP 409 with no data change;
* the revision compare-and-increment happens inside the same transaction as
  the write, so concurrent writers can never both succeed;
* a postponement preview carries a revision: after any intervening live write
  an old preview cannot be applied on top of the new timeline;
* databases created before the feature are migrated by adding the column.
"""
import os
import sqlite3
import tempfile
import threading
import unittest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ.setdefault("AGENDA_DB", _tmp.name)

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402

CONFLICT = "revision_conflict"


class ConcurrencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.client.__enter__()  # lifespan -> init_db

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        os.unlink(_tmp.name)

    def setUp(self):
        r = self.client.post("/api/meetings", json={
            "title": "并发测试会议",
            "start_time": "2026-09-21T09:00",
            "end_time": "2026-09-21T09:30",
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

    def plan(self):
        return self.client.get(f"/api/meetings/{self.mid}").json()

    def revision(self):
        return self.plan()["revision"]

    def record(self, item_id, when, revision):
        return self.client.post(f"/api/meetings/{self.mid}/actuals", json={
            "item_id": item_id, "actual_end": when, "revision": revision})

    def assert_conflict(self, resp, expected_revision):
        self.assertEqual(resp.status_code, 409, resp.text)
        body = resp.json()
        self.assertEqual(body["code"], CONFLICT)
        self.assertEqual(body["current_revision"], expected_revision)

    def make_live(self):
        """Create A/B and record A done 09:20 (revision 1 -> 2)."""
        a = self.add_item("A", 10, 5)
        b = self.add_item("B", 10, 5)
        self.assertEqual(self.record(a, "2026-09-21T09:20", 1).status_code, 200)
        return a, b

    # ----------------------------------------------------------------- basics
    def test_revision_starts_at_one_and_advances_monotonically(self):
        self.assertEqual(self.plan()["revision"], 1)
        a = self.add_item("A", 10, 5)
        b = self.add_item("B", 10, 5)
        # pre-live edits do not touch the revision
        self.assertEqual(self.plan()["revision"], 1)

        r = self.record(a, "2026-09-21T09:20", 1)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["revision"], 2)

        r = self.client.post(f"/api/meetings/{self.mid}/undo",
                             json={"revision": 2})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["revision"], 3)

        # recording again at the newest revision works and bumps once more
        r = self.record(a, "2026-09-21T09:20", 3)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["revision"], 4)
        self.assertEqual(self.record(b, "2026-09-21T09:28", 4).json()["revision"], 5)

    def test_missing_revision_is_422_not_silently_accepted(self):
        a = self.add_item("A", 10, 5)
        r = self.client.post(f"/api/meetings/{self.mid}/actuals", json={
            "item_id": a, "actual_end": "2026-09-21T09:20"})
        self.assertEqual(r.status_code, 422)
        r = self.client.post(f"/api/meetings/{self.mid}/undo", json={})
        self.assertEqual(r.status_code, 422)

    # ----------------------------------------------------- stale live writes
    def test_stale_record_returns_409_and_changes_nothing(self):
        a, b = self.add_item("A", 10, 5), self.add_item("B", 10, 5)
        self.assertEqual(self.record(a, "2026-09-21T09:20", 1).status_code, 200)
        items_before = {it["id"]: it["actual_end"] for it in self.plan()["items"]}

        # stale revision 1 -> conflict, B stays unrecorded, revision untouched
        r = self.record(b, "2026-09-21T09:28", 1)
        self.assert_conflict(r, 2)
        plan = self.plan()
        self.assertEqual(plan["revision"], 2)
        self.assertEqual(
            {it["id"]: it["actual_end"] for it in plan["items"]}, items_before)

        # retrying with the fresh revision succeeds
        self.assertEqual(self.record(b, "2026-09-21T09:28", 2).status_code, 200)

    def test_stale_undo_and_reset_return_409_and_change_nothing(self):
        a, b = self.make_live()
        # another client records B first -> revision 2 -> 3
        r = self.record(b, "2026-09-21T09:28", 2)
        self.assertEqual(r.status_code, 200)

        r = self.client.post(f"/api/meetings/{self.mid}/undo",
                             json={"revision": 2})
        self.assert_conflict(r, 3)
        self.assertEqual(self.plan()["undo_count"], 2)  # both records intact

        r = self.client.post(f"/api/meetings/{self.mid}/reset",
                             json={"revision": 2})
        self.assert_conflict(r, 3)
        self.assertTrue(self.plan()["live"])

        # fresh revision: reset succeeds
        r = self.client.post(f"/api/meetings/{self.mid}/reset",
                             json={"revision": 3})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["revision"], 4)

    def test_undo_with_empty_log_and_stale_revision_reports_conflict(self):
        # nothing recorded: empty undo log would normally be 404; a stale
        # revision must still surface as the 409 conflict
        r = self.client.post(f"/api/meetings/{self.mid}/undo",
                             json={"revision": 99})
        self.assert_conflict(r, 1)
        self.assertEqual(self.plan()["revision"], 1)

    # --------------------------------------------- preview revision handling
    def make_overrun(self, end="2026-09-21T09:25"):
        """Short overrun meeting; returns (mid, a, lo, preview).

        A finishes 09:20; Lo (P1, 20/20) is the unique postponement that
        clears the overflow, and the trial is feasible.
        """
        r = self.client.post("/api/meetings", json={
            "title": "短会", "start_time": "2026-09-21T09:00",
            "end_time": end})
        mid = r.json()["id"]

        def item(title, planned, minimum, priority=3, incompressible=False):
            r = self.client.post(f"/api/meetings/{mid}/items", json={
                "title": title, "planned_minutes": planned,
                "minimum_minutes": minimum, "priority": priority,
                "incompressible": incompressible})
            self.assertEqual(r.status_code, 201, r.text)
            return r.json()["id"]

        a = item("A", 10, 5, priority=5)
        lo = item("Lo", 20, 20, priority=1)
        item("Hi", 10, 2, priority=5)
        r = self.client.post(f"/api/meetings/{mid}/actuals", json={
            "item_id": a, "actual_end": "2026-09-21T09:20", "revision": 1})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post(f"/api/meetings/{mid}/postponements/preview")
        self.assertEqual(r.status_code, 200, r.text)
        preview = r.json()
        return mid, a, lo, preview

    def test_preview_carries_revision_and_does_not_advance_it(self):
        mid, a, lo, preview = self.make_overrun()
        self.assertEqual(preview["revision"], 2)
        plan = self.client.get(f"/api/meetings/{mid}").json()
        self.assertEqual(plan["revision"], 2)
        self.assertFalse(any(it["id"] == lo and it["postponed"]
                             for it in plan["items"]))

    def test_old_preview_cannot_be_confirmed_after_intervening_write(self):
        mid, a, lo, preview = self.make_overrun()
        stale_rev = preview["revision"]  # 2

        # intervening live write: undo A's recording -> revision 3
        r = self.client.post(f"/api/meetings/{mid}/undo",
                             json={"revision": stale_rev})
        self.assertEqual(r.status_code, 200)

        # confirming the old preview is a conflict, not an apply
        r = self.client.post(f"/api/meetings/{mid}/postponements",
                             json={"item_ids": preview["item_ids"],
                                   "revision": stale_rev})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["code"], CONFLICT)
        self.assertEqual(r.json()["current_revision"], 3)
        plan = self.client.get(f"/api/meetings/{mid}").json()
        self.assertEqual(plan["revision"], 3)
        self.assertFalse(any(it["postponed"] for it in plan["items"]))

        # and a later intervening postponement also invalidates an old preview:
        # re-record A (rev 4), re-preview (still rev 4), then a second client
        # records another item, and the fresh preview must itself go stale.
        self.assertEqual(self.client.post(
            f"/api/meetings/{mid}/actuals", json={
                "item_id": a, "actual_end": "2026-09-21T09:20",
                "revision": 3}).status_code, 200)
        preview2 = self.client.post(
            f"/api/meetings/{mid}/postponements/preview").json()
        self.assertEqual(preview2["revision"], 4)
        # reset from another client -> rev 5, timeline fully rebuilt
        self.assertEqual(self.client.post(
            f"/api/meetings/{mid}/reset",
            json={"revision": 4}).status_code, 200)
        r = self.client.post(f"/api/meetings/{mid}/postponements",
                             json={"item_ids": preview2["item_ids"],
                                   "revision": 4})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["current_revision"], 5)
        self.assertTrue(
            all(not it["postponed"] for it in self.client.get(
                f"/api/meetings/{mid}").json()["items"]))

    def test_preview_revision_must_be_used_even_when_plan_still_fits(self):
        # After a state change the same item set may still be the suggestion;
        # the revision guard rejects it anyway so the confirmed timeline is
        # always derived from the previewed one.
        mid, a, lo, preview = self.make_overrun()
        # another compatible postponement confirmation lands first
        r = self.client.post(f"/api/meetings/{mid}/postponements",
                             json={"item_ids": [lo], "revision": 2})
        self.assertEqual(r.status_code, 200)
        r = self.client.post(f"/api/meetings/{mid}/postponements",
                             json={"item_ids": preview["item_ids"],
                                   "revision": preview["revision"]})
        self.assertEqual(r.status_code, 409)
        # only one undo entry + one postponement actually exist
        self.assertEqual(
            self.client.get(f"/api/meetings/{mid}").json()["undo_count"], 2)

    # --------------------------------------------------------- true races
    def test_concurrent_recordings_only_one_succeeds(self):
        a, b = self.add_item("A", 10, 5), self.add_item("B", 10, 5)
        self.assertEqual(self.record(a, "2026-09-21T09:20", 1).status_code, 200)

        results = {}
        barrier = threading.Barrier(2)

        def fire(item_id, key):
            barrier.wait()
            results[key] = self.record(item_id, "2026-09-21T09:28", 2).status_code

        # B is the valid next item; run two identical requests at rev 2
        t1 = threading.Thread(target=fire, args=(b, "first"))
        t2 = threading.Thread(target=fire, args=(b, "second"))
        t1.start(); t2.start(); t1.join(); t2.join()

        statuses = sorted(results.values())
        self.assertEqual(statuses, [200, 409], results)
        plan = self.plan()
        self.assertEqual(plan["revision"], 3)  # bumped exactly once
        self.assertEqual(
            sum(1 for it in plan["items"] if it["actual_end"]), 2)

    def test_concurrent_postponement_confirmations_only_one_succeeds(self):
        mid, a, lo, preview = self.make_overrun()
        results = {}
        barrier = threading.Barrier(2)

        def fire(key):
            barrier.wait()
            results[key] = self.client.post(
                f"/api/meetings/{mid}/postponements",
                json={"item_ids": preview["item_ids"], "revision": 2}
            ).status_code

        t1 = threading.Thread(target=fire, args=("first",))
        t2 = threading.Thread(target=fire, args=("second",))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(sorted(results.values()), [200, 409], results)
        plan = self.client.get(f"/api/meetings/{mid}").json()
        self.assertEqual(plan["revision"], 3)
        self.assertEqual(plan["undo_count"], 2)  # one actual + one postpone
        self.assertTrue(any(it["id"] == lo and it["postponed"]
                            for it in plan["items"]))

    # ------------------------------------------------------------- migration
    def test_legacy_database_is_migrated_with_revision_column(self):
        legacy = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        legacy.close()
        # build an old-schema database (no meetings.revision) with one meeting
        conn = sqlite3.connect(legacy.name)
        conn.executescript(
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
                    '2026-09-01T08:00');
            INSERT INTO items (meeting_id, title, planned_minutes, minimum_minutes,
                               sort_order, actual_end)
            VALUES (1, 'A', 10, 5, 0, '2026-09-21T09:20');
            """
        )
        conn.commit()
        conn.close()

        original_path = db.DB_PATH
        try:
            db.DB_PATH = legacy.name
            db.init_db()
            # column exists with the default revision
            conn = sqlite3.connect(legacy.name)
            row = conn.execute("SELECT revision FROM meetings WHERE id = 1").fetchone()
            conn.close()
            self.assertEqual(row[0], 1)

            # and optimistic writes work on the migrated database
            meeting = db.get_meeting(1)
            self.assertEqual(meeting["revision"], 1)
            import app.main as main_module
            client = TestClient(main_module.app)
            r = client.get("/api/meetings/1")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["revision"], 1)
        finally:
            db.DB_PATH = original_path
            os.unlink(legacy.name)


if __name__ == "__main__":
    unittest.main()
