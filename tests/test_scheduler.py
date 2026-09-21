"""Unit tests for the pure re-scheduling core (stdlib unittest)."""
import unittest
from datetime import datetime, timedelta

from app.scheduler import (
    build_original, compress_allocations, parse_dt, reschedule,
    suggest_postponements,
)


def dt(h, m=0, day=21):
    return datetime(2026, 9, day, h, m)


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M")


def item(id, title, planned, minimum, priority=3, incompressible=False,
         actual_end=None):
    return {
        "id": id, "title": title, "planned_minutes": planned,
        "minimum_minutes": minimum, "priority": priority,
        "incompressible": incompressible, "actual_end": actual_end,
    }


def brk(id, h, m, dur=10, title="休息"):
    return {"id": id, "title": title, "fixed_start": iso(dt(h, m)),
            "duration_minutes": dur}


def by_id(plan):
    return {str(s["id"]): s for s in plan["segments"] if s["kind"] == "item"}


class TestOriginal(unittest.TestCase):
    def test_items_sequential(self):
        plan = build_original(dt(9), dt(10), [
            item(1, "A", 20, 10), item(2, "B", 30, 15),
        ], [])
        segs = [s for s in plan["segments"] if s["kind"] == "item"]
        self.assertEqual(segs[0]["start_time"], "09:00")
        self.assertEqual(segs[0]["end_time"], "09:20")
        self.assertEqual(segs[1]["start_time"], "09:20")
        self.assertEqual(segs[1]["end_time"], "09:50")
        self.assertEqual(plan["planned_end"], iso(dt(9, 50)))
        self.assertEqual(plan["conflicts"], [])

    def test_break_pushes_item_and_leaves_slack(self):
        plan = build_original(dt(9), dt(11), [
            item(1, "A", 30, 10), item(2, "B", 20, 10),
        ], [brk(10, 9, 40, 10)])
        a, b = by_id(plan)["1"], by_id(plan)["2"]
        self.assertEqual(a["start_time"], "09:00")   # fits before break
        self.assertEqual(a["end_time"], "09:30")
        self.assertEqual(b["start_time"], "09:50")   # after break
        self.assertEqual(b["end_time"], "10:10")

    def test_plan_overflow_reported(self):
        plan = build_original(dt(9), dt(9, 40), [
            item(1, "A", 30, 10), item(2, "B", 30, 10),
        ], [])
        self.assertEqual(len(plan["conflicts"]), 1)
        self.assertEqual(plan["conflicts"][0]["type"], "plan_overflow")
        self.assertEqual(plan["conflicts"][0]["overflow_minutes"], 20)


class TestCompression(unittest.TestCase):
    def test_lowest_priority_compressed_first(self):
        items = [
            {"id": 1, "title": "hi", "planned": 30, "minimum": 10,
             "priority": 5, "incompressible": False, "sort_index": 0},
            {"id": 2, "title": "lo", "planned": 30, "minimum": 10,
             "priority": 1, "incompressible": False, "sort_index": 1},
        ]
        durations, overflow = compress_allocations(items, 45)
        self.assertEqual(overflow, 0)
        self.assertEqual(durations[1], 30)   # high priority untouched
        self.assertEqual(durations[2], 15)   # low priority shrinks

    def test_never_below_minimum(self):
        items = [
            {"id": 1, "title": "a", "planned": 30, "minimum": 20,
             "priority": 1, "incompressible": False, "sort_index": 0},
            {"id": 2, "title": "b", "planned": 30, "minimum": 20,
             "priority": 2, "incompressible": False, "sort_index": 1},
        ]
        durations, overflow = compress_allocations(items, 30)
        self.assertEqual(durations[1], 20)
        self.assertEqual(durations[2], 20)
        self.assertEqual(overflow, 10)

    def test_incompressible_never_shrinks(self):
        items = [
            {"id": 1, "title": "lock", "planned": 30, "minimum": 30,
             "priority": 1, "incompressible": True, "sort_index": 0},
            {"id": 2, "title": "soft", "planned": 30, "minimum": 10,
             "priority": 5, "incompressible": False, "sort_index": 1},
        ]
        durations, overflow = compress_allocations(items, 40)
        self.assertEqual(durations[1], 30)
        self.assertEqual(durations[2], 10)
        self.assertEqual(overflow, 0)


class TestReschedule(unittest.TestCase):
    def test_overrun_compresses_later_items_by_priority(self):
        # meeting 09:00-10:00; item A ran 35 min (recorded end 09:35);
        # remaining: B 25min/P1, C 20min/P5 -> 45 planned into 25 min slot
        plan = reschedule(dt(9), dt(10), [
            item(1, "A", 20, 10, actual_end=iso(dt(9, 35))),
            item(2, "B", 25, 5, priority=1),
            item(3, "C", 20, 10, priority=5),
        ], [], iso(dt(9, 35)))
        segs = by_id(plan)
        self.assertEqual(segs["1"]["status"], "completed")
        self.assertEqual(segs["1"]["end_time"], "09:35")
        self.assertEqual(segs["2"]["start_time"], "09:35")
        # B (P1) compressed first: needs 20 reduction overall; B gives 20 -> 5
        self.assertEqual(segs["2"]["duration"], 5)
        self.assertEqual(segs["2"]["status"], "compressed")
        self.assertEqual(segs["3"]["duration"], 20)
        self.assertEqual(segs["3"]["status"], "planned")
        self.assertEqual(plan["planned_end"], iso(dt(10)))
        self.assertEqual(plan["conflicts"], [])

    def test_impossible_overflow_listed_with_minutes(self):
        # 09:00-09:30, A done at 09:20, B is locked at 20 but only 10 remain
        plan = reschedule(dt(9), dt(9, 30), [
            item(1, "A", 10, 5, actual_end=iso(dt(9, 20))),
            item(2, "B", 20, 20, priority=1, incompressible=True),
        ], [], iso(dt(9, 20)))
        overflow = [c for c in plan["conflicts"] if c["type"] == "overflow"]
        self.assertEqual(len(overflow), 1)
        self.assertEqual(overflow[0]["overflow_minutes"], 10)
        self.assertIn(2, overflow[0]["item_ids"])
        self.assertEqual(by_id(plan)["2"]["status"], "conflict")

    def test_fixed_break_never_moves_and_past_break_completed(self):
        # A recorded end 09:35. Break at 09:40 stays pinned 09:40-09:50.
        # B (10 min) fits 09:35-09:45 -> cannot fit before break at min?
        # B min=5, planned=10: compress to 5, fits 09:35-09:40.
        plan = reschedule(dt(9), dt(11), [
            item(1, "A", 20, 10, actual_end=iso(dt(9, 35))),
            item(2, "B", 10, 5),
        ], [brk(10, 9, 40, 10)], iso(dt(9, 35)))
        break_seg = next(s for s in plan["segments"] if s["kind"] == "break")
        self.assertEqual(break_seg["start_time"], "09:40")
        self.assertEqual(break_seg["end_time"], "09:50")
        self.assertEqual(break_seg["status"], "planned")
        b = by_id(plan)["2"]
        self.assertEqual(b["start_time"], "09:35")
        self.assertEqual(b["duration"], 5)
        self.assertEqual(b["status"], "compressed")

    def test_break_before_anchor_marked_completed(self):
        plan = reschedule(dt(9), dt(11), [
            item(1, "A", 60, 30, actual_end=iso(dt(10, 5))),
        ], [brk(10, 9, 30, 10)], iso(dt(10, 5)))
        break_seg = next(s for s in plan["segments"] if s["kind"] == "break")
        self.assertEqual(break_seg["status"], "completed")

    def test_anchor_inside_break_flagged_and_shifted(self):
        plan = reschedule(dt(9), dt(11), [
            item(1, "A", 45, 20, actual_end=iso(dt(9, 45))),
            item(2, "B", 20, 10),
        ], [brk(10, 9, 40, 20)], iso(dt(9, 45)))
        overlap = [c for c in plan["conflicts"] if c["type"] == "anchor_overlap"]
        self.assertEqual(len(overlap), 1)
        self.assertEqual(plan["anchor"], iso(dt(10)))      # effective anchor
        b = by_id(plan)["2"]
        self.assertEqual(b["start_time"], "10:00")

    def test_item_that_cannot_fit_before_break_moves_after_with_conflict(self):
        # anchor 09:35, break 09:40 (5-min gap), B min 10 -> moves after break
        plan = reschedule(dt(9), dt(11), [
            item(1, "A", 20, 10, actual_end=iso(dt(9, 35))),
            item(2, "B", 10, 10),
        ], [brk(10, 9, 40, 10)], iso(dt(9, 35)))
        c = next(x for x in plan["conflicts"] if x["type"] == "item_before_break")
        self.assertEqual(c["overflow_minutes"], 5)
        self.assertIn(2, c["item_ids"])
        b = by_id(plan)["2"]
        self.assertEqual(b["start_time"], "09:50")  # after the break

    def test_incompressible_protected_in_compression_chain(self):
        # 60 planned in a 40-minute final gap:
        # locked (30, P1) untouched; soft1 (20,P2) ->10; soft2 (10,P5)->0min?
        plan = reschedule(dt(9), dt(9, 40), [
            item(1, "A", 10, 5, actual_end=iso(dt(9, 0))),
            item(2, "Locked", 30, 30, priority=1, incompressible=True),
            item(3, "Soft1", 20, 10, priority=2),
            item(4, "Soft2", 10, 1, priority=5),
        ], [], iso(dt(9)))
        segs = by_id(plan)
        self.assertEqual(segs["2"]["duration"], 30)
        self.assertEqual(segs["2"]["status"], "planned")
        self.assertEqual(segs["3"]["duration"], 10)
        self.assertEqual(segs["4"]["duration"], 1)
        overflow = [c for c in plan["conflicts"] if c["type"] == "overflow"]
        self.assertEqual(len(overflow), 1)
        self.assertEqual(overflow[0]["overflow_minutes"], 1)


    def test_incompressible_with_bad_minimum_uses_planned_as_floor(self):
        # defensive: even if an incompressible item row has minimum=0 the
        # packer must treat its footprint as planned duration
        bad = item(2, "Locked", 20, 0, priority=1, incompressible=True)
        plan = reschedule(dt(9), dt(11), [
            item(1, "A", 20, 10, actual_end=iso(dt(9, 35))),
            bad,
        ], [brk(10, 9, 40, 10)], iso(dt(9, 35)))
        conflicts = [c for c in plan["conflicts"] if c["type"] == "item_before_break"]
        self.assertEqual(len(conflicts), 1)
        self.assertIn(2, conflicts[0]["item_ids"])
        self.assertEqual(by_id(plan)["2"]["start_time"], "09:50")


class TestSuggestPostponements(unittest.TestCase):
    def test_low_priority_moved_first_clears_overflow(self):
        # A done 09:20, meeting ends 09:32 (12 min left):
        # Lock 10 (incompressible, P5) + Lo 10/10 (P1) + Hi 10/2 (P5).
        # Removing Lo lets Lock(10)+Hi compressed(2) fill the 12 minutes.
        s = suggest_postponements(dt(9), dt(9, 32), [
            item(1, "A", 10, 5, actual_end=iso(dt(9, 20))),
            item(2, "Lock", 10, 10, priority=5, incompressible=True),
            item(3, "Lo", 10, 10, priority=1),
            item(4, "Hi", 10, 2, priority=5),
        ], [], iso(dt(9, 20)))
        self.assertTrue(s["feasible"])
        self.assertEqual(s["item_ids"], [3])
        self.assertEqual(s["steps"][0]["releases_minutes"], 10)
        self.assertEqual(s["remaining_overflow_minutes"], 0)
        segs = {x["id"]: x for x in s["trial_plan"]["segments"] if x["kind"] == "item"}
        self.assertNotIn(3, segs)          # moved out
        self.assertIn(2, segs)             # incompressible stays
        self.assertIn(4, segs)

    def test_releasable_planned_minutes_breaks_priority_tie(self):
        # 30 min gap: Small 10 (no slack) + Big 20 (no slack) + Lock 10 fixed
        # = 40 -> overflow 10; both soft items are P1, Big (releases 20) wins
        s = suggest_postponements(dt(9), dt(9, 40), [
            item(1, "A", 10, 5, actual_end=iso(dt(9, 10))),
            item(2, "Small", 10, 10, priority=1),
            item(3, "Big", 20, 20, priority=1),
            item(4, "Lock", 10, 10, priority=1, incompressible=True),
        ], [], iso(dt(9, 10)))
        self.assertTrue(s["feasible"])
        self.assertEqual(s["item_ids"], [3])

    def test_original_order_breaks_final_tie(self):
        # 25 min gap: X 10 + Y 10 (identical P2, no slack) + Lock 15 fixed
        s = suggest_postponements(dt(9), dt(9, 35), [
            item(1, "A", 10, 5, actual_end=iso(dt(9, 10))),
            item(2, "X", 10, 10, priority=2),
            item(3, "Y", 10, 10, priority=2),
            item(4, "Lock", 15, 15, priority=1, incompressible=True),
        ], [], iso(dt(9, 10)))
        self.assertTrue(s["feasible"])
        self.assertEqual(s["item_ids"], [2])

    def test_incompressible_overflow_is_infeasible_with_remaining(self):
        # only a locked item overflows the meeting end
        s = suggest_postponements(dt(9), dt(9, 30), [
            item(1, "A", 10, 5, actual_end=iso(dt(9, 20))),
            item(2, "Lock", 20, 20, priority=1, incompressible=True),
        ], [], iso(dt(9, 20)))
        self.assertFalse(s["feasible"])
        self.assertEqual(s["item_ids"], [])
        self.assertEqual(s["remaining_overflow_minutes"], 10)
        self.assertEqual(len(s["remaining_conflicts"]), 1)

    def test_completed_items_never_candidates(self):
        # A itself overran (done 09:25); only a locked item remains, overflow 5
        s = suggest_postponements(dt(9), dt(9, 30), [
            item(1, "A", 15, 5, actual_end=iso(dt(9, 25))),
            item(2, "Lock", 10, 10, priority=5, incompressible=True),
        ], [], iso(dt(9, 25)))
        self.assertFalse(s["feasible"])
        self.assertEqual(s["item_ids"], [])

    def test_item_before_break_resolved_by_moving_item(self):
        # anchor 09:35, break 09:40-09:50. B (10/10) cannot fit the 5-min gap
        # even at minimum -> conflict; moving B out lets C (5/5) fill the gap.
        s = suggest_postponements(dt(9), dt(11), [
            item(1, "A", 20, 10, actual_end=iso(dt(9, 35))),
            item(2, "B", 10, 10, priority=1),
            item(3, "C", 5, 5, priority=3),
        ], [brk(10, 9, 40, 10)], iso(dt(9, 35)))
        self.assertTrue(s["feasible"])
        self.assertEqual(s["item_ids"], [2])
        self.assertEqual(s["steps"][0]["reason_type"], "item_before_break")
        segs = {x["id"]: x for x in s["trial_plan"]["segments"] if x["kind"] == "item"}
        self.assertNotIn(2, segs)
        self.assertEqual(segs[3]["start_time"], "09:35")
        self.assertEqual(segs[3]["end_time"], "09:40")

    def test_already_postponed_items_absent_from_trial_plan(self):
        plan = reschedule(dt(9), dt(10), [
            item(1, "A", 10, 5, actual_end=iso(dt(9, 10))),
            dict(item(2, "Gone", 20, 10), postponed=True),
            item(3, "Keep", 20, 10),
        ], [], iso(dt(9, 10)))
        segs = {x["id"]: x for x in plan["segments"] if x["kind"] == "item"}
        self.assertNotIn(2, segs)
        self.assertEqual(segs[3]["start_time"], "09:10")
        self.assertEqual(plan["conflicts"], [])


if __name__ == "__main__":
    unittest.main()
