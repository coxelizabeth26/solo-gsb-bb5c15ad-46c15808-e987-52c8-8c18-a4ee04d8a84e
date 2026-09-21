"""Meeting agenda re-scheduling core.

Pure, dependency-free functions (SQLite rows are passed in as plain dicts)
so the whole algorithm can be exercised directly by the test-suite.

Model
-----
* A meeting has a fixed ``start`` / ``end``.
* Agenda items carry planned / minimum durations, a priority (higher = more
  important) and an ``incompressible`` flag.
* Breaks are *fixed* segments pinned to a start time; they never move.
* While the meeting is live the actual end time of the last finished item is
  recorded; everything after that point is re-planned: fixed segments stay in
  place, compressible items shrink (lowest priority first, never below their
  minimum) and whatever still cannot fit is reported as an explicit conflict
  with the number of overflowing minutes.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def parse_dt(value: Any) -> Optional[datetime]:
    """Parse a naive ``YYYY-MM-DDTHH:MM`` / ISO string into a datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    value = str(value).strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1]
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def fmt_time(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _prep(items: list[dict]) -> list[dict]:
    """Return agenda items in order with parsed integers and sort_index."""
    out = []
    for i, raw in enumerate(items):
        it = dict(raw)
        it["sort_index"] = i
        it["planned"] = int(raw.get("planned_minutes") or 0)
        it["minimum"] = int(raw.get("minimum_minutes") or 0)
        it["priority"] = int(raw.get("priority") or 0)
        it["incompressible"] = bool(raw.get("incompressible"))
        it["postponed"] = bool(raw.get("postponed"))
        out.append(it)
    return out


def _prep_breaks(breaks: list[dict]) -> list[dict]:
    out = []
    for b in breaks:
        bb = dict(b)
        bb["start_dt"] = parse_dt(b.get("fixed_start"))
        bb["duration"] = int(b.get("duration_minutes") or 0)
        bb["end_dt"] = bb["start_dt"] + timedelta(minutes=bb["duration"])
        out.append(bb)
    return sorted(out, key=lambda x: x["start_dt"])


def _item_seg(item: dict, start: datetime, duration: int, status: str,
              source: str) -> dict:
    end = start + timedelta(minutes=duration)
    return {
        "kind": "item",
        "id": item["id"],
        "title": item.get("title", ""),
        "start": fmt_dt(start),
        "end": fmt_dt(end),
        "start_time": fmt_time(start),
        "end_time": fmt_time(end),
        "duration": duration,
        "planned_duration": item["planned"],
        "minimum_duration": item["minimum"],
        "priority": item["priority"],
        "incompressible": item["incompressible"],
        "status": status,          # planned | completed | compressed | conflict
        "source": source,          # original | current
    }


def _break_seg(brk: dict, status: str, source: str) -> dict:
    return {
        "kind": "break",
        "id": brk["id"],
        "title": brk.get("title") or "休息",
        "start": fmt_dt(brk["start_dt"]),
        "end": fmt_dt(brk["end_dt"]),
        "start_time": fmt_time(brk["start_dt"]),
        "end_time": fmt_time(brk["end_dt"]),
        "duration": brk["duration"],
        "status": status,          # planned | completed | anchor_overlap
        "source": source,
    }


def _seg_end(seg: dict) -> datetime:
    return parse_dt(seg["end"])


def _item_compressible(item: dict) -> bool:
    return (not item["incompressible"]) and item["planned"] > item["minimum"]


def _item_floor(item: dict) -> int:
    """Minimum footprint when packing an item into a gap."""
    if item["incompressible"]:
        return item["planned"]
    return item["minimum"]


# ---------------------------------------------------------------------------
# compression
# ---------------------------------------------------------------------------

def compress_allocations(items: list[dict], capacity: int):
    """Allocate durations to ``items`` fitting ``capacity`` minutes.

    Returns ``({item_id: duration}, remaining_overflow)``. Compression order
    is lowest priority first; ties favour the item with the most slack (then
    original order). Incompressible items never shrink.
    """
    durations = {it["id"]: it["planned"] for it in items}
    overflow = sum(durations.values()) - capacity
    if overflow <= 0:
        return durations, 0

    order = sorted(
        (it for it in items if _item_compressible(it)),
        key=lambda it: (it["priority"], -(it["planned"] - it["minimum"]), it["sort_index"]),
    )
    for it in order:
        slack = durations[it["id"]] - it["minimum"]
        take = min(slack, overflow)
        durations[it["id"]] -= take
        overflow -= take
        if overflow <= 0:
            break
    return durations, max(overflow, 0)


# ---------------------------------------------------------------------------
# original (as-planned) timeline
# ---------------------------------------------------------------------------

def build_original(start: datetime, end: Optional[datetime],
                   items: list[dict], breaks: list[dict]) -> dict:
    """Lay items out from ``start``; fixed breaks pin the timeline.

    An item that would overlap a coming break is pushed *after* it (leaving
    empty slack before the break). Nothing is compressed in the original plan
    — if the meeting end is exceeded that is reported as ``plan_overflow``.
    """
    items = _prep(items)
    breaks = _prep_breaks(breaks)

    segments: list[dict] = []
    conflicts: list[dict] = []
    cursor = start
    bi = 0

    for item in items:
        # a break whose start has already been reached happens now
        while bi < len(breaks) and breaks[bi]["start_dt"] <= cursor:
            brk = breaks[bi]
            segments.append(_break_seg(brk, "planned", "original"))
            cursor = max(cursor, brk["end_dt"])
            bi += 1

        # wait for the break if this item would overlap it
        while bi < len(breaks) and cursor + timedelta(minutes=item["planned"]) > breaks[bi]["start_dt"]:
            brk = breaks[bi]
            segments.append(_break_seg(brk, "planned", "original"))
            cursor = max(cursor, brk["end_dt"])
            bi += 1

        seg = _item_seg(item, cursor, item["planned"], "planned", "original")
        segments.append(seg)
        cursor = _seg_end(seg)

    # breaks scheduled beyond the last item
    while bi < len(breaks):
        segments.append(_break_seg(breaks[bi], "planned", "original"))
        bi += 1

    segments.sort(key=lambda s: (parse_dt(s["start"]), 0 if s["kind"] == "break" else 1))

    planned_end = max((_seg_end(s) for s in segments), default=start)
    view_end = max([end, start, planned_end])
    if end is not None and planned_end > end:
        over = int((planned_end - end).total_seconds() // 60)
        conflicts.append({
            "type": "plan_overflow",
            "title": "原计划超出会议结束时间",
            "message": f"原计划将于 {fmt_time(planned_end)} 结束，超出会议结束时间 {over} 分钟。",
            "overflow_minutes": over,
            "item_ids": [it["id"] for it in items],
        })

    return {
        "segments": segments,
        "conflicts": conflicts,
        "planned_end": fmt_dt(planned_end),
        "view_start": fmt_dt(start),
        "view_end": fmt_dt(view_end),
    }


# ---------------------------------------------------------------------------
# live (re-arranged) timeline
# ---------------------------------------------------------------------------

def _schedule_block(block_items, block_start, boundary, final_end,
                    segments, conflicts) -> datetime:
    """Schedule items into one gap with priority-based compression.

    ``boundary`` - next fixed break start (hard wall), or ``None`` for the
    final gap. ``final_end`` - meeting end used for overflow reporting.
    """
    if not block_items:
        return block_start

    if boundary is not None:
        capacity = int((boundary - block_start).total_seconds() // 60)
    elif final_end is not None:
        capacity = int((final_end - block_start).total_seconds() // 60)
    else:
        capacity = sum(it["planned"] for it in block_items)

    durations, overflow = compress_allocations(block_items, capacity)
    cursor = block_start
    for it in block_items:
        dur = durations[it["id"]]
        status = "compressed" if dur < it["planned"] else "planned"
        segments.append(_item_seg(it, cursor, dur, status, "current"))
        cursor += timedelta(minutes=dur)

    if boundary is None and final_end is not None and cursor > final_end:
        over_min = int((cursor - final_end).total_seconds() // 60)
        compressed_ids = [it["id"] for it in block_items if durations[it["id"]] < it["planned"]]
        incomp = [it for it in block_items if it["incompressible"]]
        conflicts.append({
            "type": "overflow",
            "title": "议程无法在会议结束前容纳",
            "message": (f"即使将 {len(compressed_ids)} 个可压缩议程项压到最短时长，"
                        f"仍超出 {over_min} 分钟。"
                        + (f"其中 {len(incomp)} 项不可压缩。" if incomp else "")),
            "overflow_minutes": over_min,
            "item_ids": [it["id"] for it in block_items],
            "boundary": fmt_dt(final_end),
        })
        for seg in segments:
            if (seg["source"] == "current" and seg["kind"] == "item"
                    and parse_dt(seg["end"]) > final_end):
                seg["status"] = "conflict"
    return cursor


def reschedule(start: datetime, end: Optional[datetime],
               items: list[dict], breaks: list[dict],
               anchor_raw: Any, exclude_ids: Optional[set] = None) -> dict:
    """Re-plan every pending agenda item from the recorded anchor time.

    * Items with an ``actual_end`` are completed and never move.
    * Pending items flagged ``postponed`` (or whose id is in ``exclude_ids``)
      are dropped from this meeting's live plan entirely.
    * Fixed breaks before the anchor are marked completed; a break that
      contains the anchor is flagged ``anchor_overlap`` and the effective
      anchor moves to its end.
    * Pending items are packed gap-by-gap between fixed breaks. They only move
      past a break when they cannot fit even at their minimum duration (which
      is reported as a conflict). The final gap is capped by the meeting end
      and any residual overflow is reported.
    """
    items = _prep(items)
    breaks = _prep_breaks(breaks)
    recorded_anchor = parse_dt(anchor_raw)
    excluded = set(exclude_ids or ())

    segments: list[dict] = []
    conflicts: list[dict] = []

    completed = [it for it in items if parse_dt(it.get("actual_end")) is not None]
    pending = [it for it in items
               if parse_dt(it.get("actual_end")) is None
               and not it.get("postponed") and it["id"] not in excluded]

    # --- completed items: frozen, laid out from meeting start --------------
    cursor = start
    for it in completed:
        actual_end = parse_dt(it["actual_end"])
        duration = max(0, int((actual_end - cursor).total_seconds() // 60))
        seg = _item_seg(it, cursor, duration, "completed", "current")
        seg["actual_end"] = it["actual_end"]
        segments.append(seg)
        cursor = actual_end

    # --- fixed breaks relative to the anchor -------------------------------
    eff_anchor = cursor
    future_breaks: list[dict] = []
    for brk in breaks:
        if brk["end_dt"] <= eff_anchor:
            segments.append(_break_seg(brk, "completed", "current"))
        elif brk["start_dt"] <= eff_anchor < brk["end_dt"]:
            overlap = int((brk["end_dt"] - eff_anchor).total_seconds() // 60)
            seg = _break_seg(brk, "anchor_overlap", "current")
            seg["message"] = (f"录入的结束时间落入固定休息段，重排从休息结束 "
                              f"{fmt_time(brk['end_dt'])} 开始（休息被占用 {overlap} 分钟）。")
            segments.append(seg)
            conflicts.append({
                "type": "anchor_overlap",
                "title": f"实际结束时间落入固定休息「{brk.get('title') or '休息'}」",
                "message": seg["message"],
                "overflow_minutes": 0,
                "item_ids": [],
            })
            eff_anchor = brk["end_dt"]
        else:
            future_breaks.append(brk)

    # --- pending items packed gap-by-gap -----------------------------------
    block_start = eff_anchor
    idx = 0
    flagged: set = set()  # items already reported as unable to fit a gap
    for brk in future_breaks:
        boundary = brk["start_dt"]
        capacity = int((boundary - block_start).total_seconds() // 60)

        block_items: list[dict] = []
        used = 0
        while idx < len(pending):
            it = pending[idx]
            floor = _item_floor(it)
            if block_items and used + floor > capacity:
                break
            if not block_items and floor > capacity:
                break  # not even one item fits at minimum -> conflict below
            block_items.append(it)
            used += floor
            idx += 1

        _schedule_block(block_items, block_start, boundary, None,
                        segments, conflicts)

        if not block_items and idx < len(pending) and pending[idx]["id"] not in flagged:
            it = pending[idx]
            floor = _item_floor(it)
            over = floor - max(capacity, 0)
            flagged.add(it["id"])
            conflicts.append({
                "type": "item_before_break",
                "title": f"「{it['title']}」无法在固定休息前安排",
                "message": (f"固定休息 {fmt_time(boundary)} 前仅剩 {max(capacity, 0)} 分钟，"
                            f"该议程最少需要 {floor} 分钟"
                            + ("（不可压缩）" if it["incompressible"] else "")
                            + f"，冲突 {max(over, 0)} 分钟。该议程已顺延到休息之后。"),
                "overflow_minutes": max(over, 0),
                "item_ids": [it["id"]],
                "boundary": fmt_dt(boundary),
            })

        # fixed break stays pinned
        segments.append(_break_seg(brk, "planned", "current"))
        block_start = brk["end_dt"]

    # remaining items -> final gap capped by meeting end
    _schedule_block(pending[idx:], block_start, None, end, segments, conflicts)

    segments.sort(key=lambda s: (parse_dt(s["start"]), 0 if s["kind"] == "break" else 1))

    live_end = max((_seg_end(s) for s in segments), default=eff_anchor)
    view_end = max([e for e in (end, start, live_end) if e is not None])

    return {
        "segments": segments,
        "conflicts": conflicts,
        "anchor": fmt_dt(eff_anchor),
        "recorded_anchor": fmt_dt(recorded_anchor) if recorded_anchor else fmt_dt(eff_anchor),
        "planned_end": fmt_dt(live_end),
        "view_start": fmt_dt(start),
        "view_end": fmt_dt(view_end),
    }


# ---------------------------------------------------------------------------
# postponement suggestions (trial scheduling, writes nothing)
# ---------------------------------------------------------------------------

# Conflicts that postponing an agenda item can resolve. ``anchor_overlap`` is
# only an informational warning and never blocks a postponement proposal.
HARD_CONFLICT_TYPES = ("overflow", "item_before_break")


def _hard_conflicts(conflicts: list[dict]) -> list[dict]:
    return [c for c in conflicts if c.get("type") in HARD_CONFLICT_TYPES]


def _remaining_overflow(conflicts: list[dict]) -> int:
    return sum(int(c.get("overflow_minutes") or 0) for c in _hard_conflicts(conflicts))


def _conflict_boundary(c: dict) -> datetime:
    """Sort key: the time wall the conflict is measured against."""
    parsed = parse_dt(c.get("boundary")) if c.get("boundary") else None
    return parsed or datetime.max


def _item_brief(it: dict) -> dict:
    return {
        "id": it["id"],
        "title": it.get("title", ""),
        "planned_minutes": it["planned"],
        "minimum_minutes": it["minimum"],
        "priority": it["priority"],
        "incompressible": it["incompressible"],
    }


def suggest_postponements(start: datetime, end: Optional[datetime],
                          items: list[dict], breaks: list[dict],
                          anchor: Any) -> dict:
    """Repeatedly trial-schedule, moving one candidate out of this meeting.

    Only unfinished, non-postponed, *compressible* agenda items are
    candidates; fixed breaks, completed items and incompressible items are
    never removed. Starting from the earliest hard conflict, the candidate
    chosen at each round is the lowest priority first, then the one releasing
    the most planned minutes, then the one appearing earliest in the original
    order. The loop stops as soon as all hard conflicts are cleared or no
    candidate remains.

    The function never mutates its inputs and writes nothing; the returned
    trial plan shows exactly what the live plan would look like after the
    proposed postponements.
    """
    prepped = _prep(items)
    by_id = {it["id"]: it for it in prepped}
    anchor_dt = parse_dt(anchor)
    done_ids = {it["id"] for it in prepped if parse_dt(it.get("actual_end")) is not None}

    def postponable(i):
        return (i["id"] not in done_ids and not i.get("postponed")
                and not i["incompressible"])

    def trial(excluded):
        return reschedule(start, end, prepped, breaks, anchor_dt, excluded)

    excluded: set = set()
    steps: list[dict] = []
    plan = trial(excluded)
    initial = _hard_conflicts(plan["conflicts"])

    while True:
        hard = _hard_conflicts(plan["conflicts"])
        if not hard:
            break
        earliest = min(hard, key=_conflict_boundary)

        # Candidates that actually take part in this conflict. For an
        # item-before-break conflict that is just the single item unable to
        # fit the gap; for an end-overflow conflict it is every remaining
        # (already packed) item in the final block.
        involved = {i for i in earliest.get("item_ids", []) if i in by_id}
        candidates = [by_id[i] for i in involved if postponable(by_id[i])]
        if not candidates:
            break

        candidates.sort(key=lambda it: (it["priority"], -it["planned"],
                                        it["sort_index"]))
        pick = candidates[0]
        excluded.add(pick["id"])
        steps.append({
            "item": _item_brief(pick),
            "reason_type": earliest["type"],
            "reason": earliest["title"],
            "releases_minutes": pick["planned"],
        })
        plan = trial(excluded)

    remaining = _hard_conflicts(plan["conflicts"])
    return {
        "feasible": not remaining,
        "item_ids": [s["item"]["id"] for s in steps],
        "steps": steps,
        "remaining_conflicts": remaining,
        "remaining_overflow_minutes": _remaining_overflow(remaining),
        "initial_hard_conflict_count": len(initial),
        "anchor": plan["anchor"],
        "trial_plan": plan,
    }
