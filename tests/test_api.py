"""End-to-end API tests using FastAPI's TestClient against a temp SQLite DB."""
import os
import tempfile
import unittest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["AGENDA_DB"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

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
        self.assertEqual(len(data["original"]["segments"]), 3)
        self.assertIn("current", data)

    def test_record_actual_triggers_compression_and_conflict(self):
        a = self.add_item("A", 20, 10, priority=5)
        b = self.add_item("B", 25, 5, priority=1)
        c = self.add_item("C", 20, 10, priority=5)
        # A finishes 35 minutes late at 09:35
        r = self.client.post(f"/api/meetings/{self.mid}/actuals", json={
            "item_id": a, "actual_end": "2026-09-21T09:35"})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data["live"])
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
        r = self.client.post(f"/api/meetings/{mid}/actuals", json={
            "item_id": a, "actual_end": "2026-09-21T09:20"})
        data = r.json()
        conflicts = data["current"]["conflicts"]
        self.assertTrue(any(x["type"] == "overflow" and x["overflow_minutes"] == 10
                            for x in conflicts))

    def test_ordered_recording_required(self):
        a = self.add_item("A", 10, 5)
        b = self.add_item("B", 10, 5)
        r = self.client.post(f"/api/meetings/{self.mid}/actuals", json={
            "item_id": b, "actual_end": "2026-09-21T09:10"})
        self.assertEqual(r.status_code, 409)
        # A first -> ok
        r = self.client.post(f"/api/meetings/{self.mid}/actuals", json={
            "item_id": a, "actual_end": "2026-09-21T09:10"})
        self.assertEqual(r.status_code, 200)

    def test_undo_restores_plan(self):
        a = self.add_item("A", 20, 10)
        self.add_item("B", 20, 10)
        self.client.post(f"/api/meetings/{self.mid}/actuals", json={
            "item_id": a, "actual_end": "2026-09-21T09:25"})
        r = self.client.post(f"/api/meetings/{self.mid}/undo")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertFalse(data["live"])
        self.assertEqual(data["undo_count"], 0)
        # undo again -> 404
        r = self.client.post(f"/api/meetings/{self.mid}/undo")
        self.assertEqual(r.status_code, 404)

    def test_reset_is_undoable(self):
        a = self.add_item("A", 20, 10)
        self.client.post(f"/api/meetings/{self.mid}/actuals", json={
            "item_id": a, "actual_end": "2026-09-21T09:25"})
        r = self.client.post(f"/api/meetings/{self.mid}/reset")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["live"])
        r = self.client.post(f"/api/meetings/{self.mid}/undo")
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
        self.client.post(f"/api/meetings/{self.mid}/actuals", json={
            "item_id": a, "actual_end": "2026-09-21T09:25"})
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
        r = self.client.post(f"/api/meetings/{self.mid}/actuals", json={
            "item_id": a, "actual_end": "2026-09-21T09:20"})
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
        self.client.post(f"/api/meetings/{mid}/actuals", json={
            "item_id": a, "actual_end": "2026-09-21T09:20"})
        return mid, a, lock, lo, hi

    def test_postpone_preview_confirm_and_reschedule(self):
        # A done 09:20; 12 minutes remain for Lock(10)+Lo(10)+Hi(10/2);
        # removing Lo (P1) lets Lock(10) + Hi compressed (2) fill the gap
        mid, a, lock, lo, hi = self._make_overrun_meeting()
        r = self.client.post(f"/api/meetings/{mid}/postponements/preview")
        self.assertEqual(r.status_code, 200, r.text)
        s = r.json()
        self.assertTrue(s["feasible"])
        self.assertEqual(s["item_ids"], [lo])
        # preview writes nothing
        before = self.client.get(f"/api/meetings/{mid}").json()
        self.assertFalse(any(it["id"] == lo and it["postponed"]
                             for it in before["items"]))
        # confirm: atomic save + immediate reschedule
        r = self.client.post(f"/api/meetings/{mid}/postponements", json={
            "item_ids": [lo]})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
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
        r = self.client.post(f"/api/meetings/{mid}/undo")
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
        self.client.post(f"/api/meetings/{mid}/actuals", json={
            "item_id": a, "actual_end": "2026-09-21T09:20"})
        r = self.client.post(f"/api/meetings/{mid}/postponements/preview")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["feasible"])
        self.assertEqual(r.json()["remaining_overflow_minutes"], 10)
        # the locked item cannot be submitted as a postponement
        r = self.client.post(f"/api/meetings/{mid}/postponements", json={
            "item_ids": [lock]})
        self.assertEqual(r.status_code, 422)

    def test_cannot_postpone_completed_or_incompressible(self):
        a = self.add_item("A", 10, 5)
        lock = self.add_item("Lock", 20, 20, incompressible=True)
        self.client.post(f"/api/meetings/{self.mid}/actuals", json={
            "item_id": a, "actual_end": "2026-09-21T09:50"})
        self.assertEqual(self.client.post(
            f"/api/meetings/{self.mid}/postponements",
            json={"item_ids": [a]}).status_code, 422)
        self.assertEqual(self.client.post(
            f"/api/meetings/{self.mid}/postponements",
            json={"item_ids": [lock]}).status_code, 422)

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
        self.client.post(f"/api/meetings/{mid}/actuals", json={
            "item_id": a, "actual_end": "2026-09-21T09:20"})
        r = self.client.post(f"/api/meetings/{mid}/postponements/preview")
        self.assertEqual(r.json()["item_ids"], [b])
        r = self.client.post(f"/api/meetings/{mid}/postponements",
                             json={"item_ids": [b]})
        self.assertEqual(r.status_code, 200, r.text)
        # recording C directly is allowed because B is deferred; recording B
        # itself is rejected
        r = self.client.post(f"/api/meetings/{mid}/actuals", json={
            "item_id": c, "actual_end": "2026-09-21T09:40"})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post(f"/api/meetings/{mid}/actuals", json={
            "item_id": b, "actual_end": "2026-09-21T09:45"})
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
        self.client.post(f"/api/meetings/{mid}/actuals", json={
            "item_id": a, "actual_end": "2026-09-21T09:20"})
        r = self.client.post(f"/api/meetings/{mid}/postponements/preview")
        self.assertEqual(r.json()["item_ids"], [b])
        r = self.client.post(f"/api/meetings/{mid}/postponements",
                             json={"item_ids": [b]})
        self.assertEqual(r.status_code, 200)
        r = self.client.post(f"/api/meetings/{mid}/reset")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(any(it["postponed"] for it in r.json()["items"]))
        # single undo restores both actuals and postponements
        r = self.client.post(f"/api/meetings/{mid}/undo")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["live"])
        self.assertTrue(any(it["id"] == b and it["postponed"] for it in data["items"]))


if __name__ == "__main__":
    unittest.main()
