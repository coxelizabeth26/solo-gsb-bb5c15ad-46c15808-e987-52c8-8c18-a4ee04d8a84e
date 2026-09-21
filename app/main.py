"""FastAPI application: meeting agenda live re-arrangement."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .scheduler import (
    build_original, parse_dt, reschedule, suggest_postponements,
)
from .schemas import (
    ActualIn, BreakCreate, ItemCreate, MeetingCreate, MoveIn, PostponeIn,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="会议议程实时重排", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# plan assembly
# ---------------------------------------------------------------------------

def _build_plan(meeting: dict) -> dict:
    items = db.list_items(meeting["id"])
    breaks = db.list_breaks(meeting["id"])
    start = parse_dt(meeting["start_time"])
    end = parse_dt(meeting["end_time"])

    original = build_original(start, end, items, breaks)

    actual_items = [it for it in items if it.get("actual_end")]
    live = bool(actual_items)
    if live:
        last_actual = max(parse_dt(it["actual_end"]) for it in actual_items)
        current = reschedule(start, end, items, breaks, last_actual)
    else:
        # nothing recorded yet: live plan equals the original plan
        current = {
            "segments": [dict(s) for s in original["segments"]],
            "conflicts": [],
            "anchor": meeting["start_time"],
            "recorded_anchor": meeting["start_time"],
            "planned_end": original["planned_end"],
            "view_start": original["view_start"],
            "view_end": original["view_end"],
        }

    return {
        "meeting": meeting,
        "items": items,
        "breaks": breaks,
        "live": live,
        "anchor": current["anchor"],
        "undo_count": db.undo_count(meeting["id"]),
        "original": original,
        "current": current,
    }


def _get_meeting_or_404(meeting_id: int) -> dict:
    meeting = db.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(404, "会议不存在")
    return meeting


def _is_live(meeting_id: int) -> bool:
    return any(it.get("actual_end") for it in db.list_items(meeting_id))


def _live_anchor(items: list[dict]):
    actual_items = [it for it in items if it.get("actual_end")]
    return max(parse_dt(it["actual_end"]) for it in actual_items)


def _build_suggestion(meeting: dict) -> dict:
    """Trial postponement proposal for the current live state (no writes)."""
    items = db.list_items(meeting["id"])
    breaks = db.list_breaks(meeting["id"])
    start = parse_dt(meeting["start_time"])
    end = parse_dt(meeting["end_time"])
    return suggest_postponements(start, end, items, breaks, _live_anchor(items))


# ---------------------------------------------------------------------------
# meetings
# ---------------------------------------------------------------------------

@app.get("/api/meetings")
def api_list_meetings():
    return db.list_meetings()


@app.post("/api/meetings", status_code=201)
def api_create_meeting(body: MeetingCreate):
    start = parse_dt(body.start_time)
    end = parse_dt(body.end_time)
    if end <= start:
        raise HTTPException(422, "会议结束时间必须晚于开始时间")
    return db.create_meeting(body.title.strip(), body.start_time, body.end_time)


@app.get("/api/meetings/{meeting_id}")
def api_get_meeting(meeting_id: int):
    meeting = _get_meeting_or_404(meeting_id)
    return _build_plan(meeting)


# ---------------------------------------------------------------------------
# agenda items
# ---------------------------------------------------------------------------

@app.post("/api/meetings/{meeting_id}/items", status_code=201)
def api_create_item(meeting_id: int, body: ItemCreate):
    _get_meeting_or_404(meeting_id)
    if _is_live(meeting_id):
        raise HTTPException(409, "会议已开始录入，请先撤销或重置后再修改议程")
    if body.minimum_minutes > body.planned_minutes:
        raise HTTPException(422, "最短时长不能大于计划时长")
    if body.incompressible and body.minimum_minutes != body.planned_minutes:
        raise HTTPException(422, "不可压缩议程的最短时长必须等于计划时长")
    return db.create_item(meeting_id, body.title.strip(), body.planned_minutes,
                          body.minimum_minutes, body.priority, body.incompressible)


@app.delete("/api/items/{item_id}", status_code=204)
def api_delete_item(item_id: int):
    item = db.get_item(item_id)
    if item is None:
        raise HTTPException(404, "议程项不存在")
    if _is_live(item["meeting_id"]):
        raise HTTPException(409, "会议已开始录入，请先撤销或重置后再删除议程")
    db.delete_item(item_id)


@app.post("/api/items/{item_id}/move")
def api_move_item(item_id: int, body: MoveIn):
    item = db.get_item(item_id)
    if item is None:
        raise HTTPException(404, "议程项不存在")
    if _is_live(item["meeting_id"]):
        raise HTTPException(409, "会议已开始录入，不能调整议程顺序")
    items = db.move_item(item["meeting_id"], item_id, body.direction)
    return items


# ---------------------------------------------------------------------------
# breaks
# ---------------------------------------------------------------------------

@app.post("/api/meetings/{meeting_id}/breaks", status_code=201)
def api_create_break(meeting_id: int, body: BreakCreate):
    meeting = _get_meeting_or_404(meeting_id)
    if _is_live(meeting_id):
        raise HTTPException(409, "会议已开始录入，不能新增固定休息段")
    start = parse_dt(body.fixed_start)
    m_start, m_end = parse_dt(meeting["start_time"]), parse_dt(meeting["end_time"])
    if start < m_start or start >= m_end:
        raise HTTPException(422, "固定休息开始时间必须在会议时间范围内")
    br_end = start + timedelta(minutes=body.duration_minutes)
    if br_end > m_end:
        raise HTTPException(422, "固定休息结束时间超出会议结束时间")
    for ex in db.list_breaks(meeting_id):
        ex_start, ex_end = parse_dt(ex["fixed_start"]), parse_dt(ex["fixed_start"]) + timedelta(minutes=ex["duration_minutes"])
        if start < ex_end and ex_start < br_end:
            raise HTTPException(422, f"与已有固定休息「{ex['title']}」时间重叠")
    return db.create_break(meeting_id, body.title.strip() or "休息",
                           body.fixed_start, body.duration_minutes)


@app.delete("/api/breaks/{break_id}", status_code=204)
def api_delete_break(break_id: int):
    brk = db.get_break(break_id)
    if brk is None:
        raise HTTPException(404, "休息段不存在")
    if _is_live(brk["meeting_id"]):
        raise HTTPException(409, "会议已开始录入，请先撤销或重置后再删除休息段")
    db.delete_break(break_id)


# ---------------------------------------------------------------------------
# live recording / undo
# ---------------------------------------------------------------------------

@app.post("/api/meetings/{meeting_id}/actuals")
def api_record_actual(meeting_id: int, body: ActualIn):
    meeting = _get_meeting_or_404(meeting_id)
    item = db.get_item(body.item_id)
    if item is None or item["meeting_id"] != meeting_id:
        raise HTTPException(404, "议程项不存在")
    if item.get("actual_end"):
        raise HTTPException(409, "该议程项已录入实际结束时间")
    if item.get("postponed"):
        raise HTTPException(409, "该议程项已延期移出本次会议，请先撤销延期")

    items = db.list_items(meeting_id)
    ordered = sorted(items, key=lambda it: (it["sort_order"], it["id"]))
    target_pos = next(i for i, it in enumerate(ordered) if it["id"] == body.item_id)
    # postponed items are deferred out of this meeting and skipped when
    # enforcing in-order live recording
    prev_active = [it for it in ordered[:target_pos]
                   if it.get("actual_end") or not it.get("postponed")]
    prev_done = [it for it in prev_active if it.get("actual_end")]
    if len(prev_done) != len(prev_active):
        raise HTTPException(409, "请按议程顺序录入：前面尚有未完成的议程项")

    actual = parse_dt(body.actual_end)
    if actual < parse_dt(meeting["start_time"]):
        raise HTTPException(422, "实际结束时间不能早于会议开始时间")
    if prev_done:
        anchor = max(parse_dt(it["actual_end"]) for it in prev_done)
        if actual < anchor:
            raise HTTPException(422, f"实际结束时间不能早于上一议程结束时间 {anchor.strftime('%H:%M')}")

    db.record_actual(meeting_id, body.item_id, body.actual_end)
    return _build_plan(db.get_meeting(meeting_id))


@app.post("/api/meetings/{meeting_id}/undo")
def api_undo(meeting_id: int):
    _get_meeting_or_404(meeting_id)
    if not db.undo_last(meeting_id):
        raise HTTPException(404, "没有可撤销的实况录入")
    return _build_plan(db.get_meeting(meeting_id))


@app.post("/api/meetings/{meeting_id}/reset")
def api_reset(meeting_id: int):
    _get_meeting_or_404(meeting_id)
    if not _is_live(meeting_id):
        raise HTTPException(409, "当前没有任何实况录入")
    db.reset_actuals(meeting_id)
    return _build_plan(db.get_meeting(meeting_id))


# ---------------------------------------------------------------------------
# postponement suggestions (延期建议)
# ---------------------------------------------------------------------------

@app.post("/api/meetings/{meeting_id}/postponements/preview")
def api_preview_postponements(meeting_id: int):
    """Trial-schedule a postponement proposal; persists nothing."""
    meeting = _get_meeting_or_404(meeting_id)
    if not _is_live(meeting_id):
        raise HTTPException(409, "会议尚未开始录入，没有需要延期消解的冲突")
    suggestion = _build_suggestion(meeting)
    if suggestion["initial_hard_conflict_count"] == 0:
        raise HTTPException(409, "当前没有冲突，无需延期")
    return suggestion


@app.post("/api/meetings/{meeting_id}/postponements")
def api_confirm_postponements(meeting_id: int, body: PostponeIn):
    """Atomically persist the postponement and immediately re-schedule."""
    meeting = _get_meeting_or_404(meeting_id)
    if not _is_live(meeting_id):
        raise HTTPException(409, "会议尚未开始录入，不能延期议程")

    items = db.list_items(meeting_id)
    by_id = {it["id"]: it for it in items}
    ids = list(dict.fromkeys(body.item_ids))  # de-dup, keep order
    for iid in ids:
        it = by_id.get(iid)
        if it is None or it["meeting_id"] != meeting_id:
            raise HTTPException(404, f"议程项 {iid} 不存在")
        if it.get("actual_end"):
            raise HTTPException(422, f"「{it['title']}」已完成，不能延期")
        if it.get("incompressible"):
            raise HTTPException(422, f"「{it['title']}」不可压缩，不能移出本次会议")
        if it.get("postponed"):
            raise HTTPException(422, f"「{it['title']}」已处于延期状态")

    # Re-verify with the exact submitted set: confirmation is only allowed
    # when the trial run clears every hard conflict.
    start = parse_dt(meeting["start_time"])
    end = parse_dt(meeting["end_time"])
    breaks = db.list_breaks(meeting_id)
    trial = suggest_postponements(start, end, items, breaks, _live_anchor(items))
    proposed = set(trial["item_ids"])
    if set(ids) != proposed:
        raise HTTPException(422, "提交的延期项目与系统建议不一致，请重新生成建议")
    if not trial["feasible"]:
        raise HTTPException(
            409,
            f"延期后仍超出 {trial['remaining_overflow_minutes']} 分钟，无法确认延期",
        )

    db.postpone_items(meeting_id, ids)
    return _build_plan(db.get_meeting(meeting_id))


# ---------------------------------------------------------------------------
# frontend
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
