"""
Irrigation program engine
=========================

Runs the irrigation programs armed on the Hyer Farms dashboard's Programs
page: position-based steps (direction, water, depth per pass, and the angle
each step ends at) that a field runs when it turns on. The first kind is the
even-up made by programs.py.

Rules (from the advanced irrigation programs brief, agreed with Andrew):
  * A program starts when its field starts watering after it was armed
    ("next start"), or as soon as it is watering ("take over now"). An even-up
    is worked out again at that moment from fresh applied-water data and the
    pivot's position. If the fresh plan is much bigger than the one the owner
    approved, the program pauses for a new approval instead of running.
  * Steps end at angles, never at times. Times are for display only.
  * Every step's command sets the panel's service stop to that step's end
    angle, so if this service goes down or misses a check, the pivot stops at
    the right place instead of running on. That is the most important rule.
  * One command per change, with every setting in it (direction, water,
    depth, service stop), because FieldNET applies the whole panel at once.
    The panel's plan is left as it is and must be manual (FieldNET's
    "manual-mode"); a saved plan pauses the program. Before sending, the command FieldNET would apply is checked
    against the step; if they differ, nothing is sent and the program pauses.
  * Changes happen on the move. A new depth or water ("flying") is sent just
    before its angle, and so is a turn-round: the reverse command is timed to
    land as the pivot reaches the end of the pass, so it turns without
    stopping (Andrew: the panel reverses fine on the move). Both are sent from
    a fresh panel position. If one is missed, the service stop holds the pivot
    at the angle and the next step goes out from there (a stop-and-go).
  * Manual stops win. The engine only restarts a pivot that its own service
    stop halted, at the angle it expected. Any other stop, a fault, settings
    changed by someone else, or a pivot that runs past its service stop pauses
    the program and raises an alert. A paused program never moves the pivot
    again; the owner arms it again (which works it out afresh) or cancels it.
  * If the panel stops reporting, no changes are sent; the service stop is the
    backstop and an alert is raised.
  * Each program is practice or live. Practice works everything out, checks
    every command with FieldNET, and logs what it would do, but sends nothing.
    PROGRAMS_ALLOW_LIVE=1 must also be set on the server for anything to be
    sent; without it every program runs as practice.

Shuttles (settings.kind == "shuttle", stored as kind 'custom'): back and forth
over a strip between two stops, for a number of passes or left on. Started
from a pivot's Irrigation page, a shuttle starts the pivot itself straight
away: the engine works it out from the pivot's position (programs.plan_shuttle)
and sends pass 1. If the pivot isn't reporting or is in a fault then, the
shuttle pauses rather than waiting (it must never start hours later on its
own). Every pass ends at a stop and turns round on the move there, with that
stop as its service stop; everything else above applies. Practice shuttles
check pass 1 with FieldNET and end, since they can't follow a pivot they
never start.

Storage: the `pumpgroups` schema in AgHub Ops DB, shared with the dashboard
(`npm run create-pumpgroups-db`): program, program_run and program_event.

The decisions (start_reason, decide_live, decide_practice, check_command,
split_long_steps, needs_reapproval) are pure so they can be tested without
FieldNET or a database (test_engine.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol
from zoneinfo import ZoneInfo

from programs import cw, norm
from watcher import classify

CONFIRM_S = 10 * 60  # a command must show up on the pivot within this
END_TOL_DEG = 2.0  # "stopped at the end of the step" means within this of its end angle
OVERRUN_DEG = 1.5  # this far past a step's end while still running = the service stop didn't hold
EARLY_DEG = 6.0  # this far BEFORE a step's start counts as "not started yet" (a flying change sent early)
MAX_ARC_DEG = 340.0  # longer steps are split, so a service stop is never set where the pivot already is
FIX_MAX_AGE_S = 30 * 60  # ask the panel for a fresh position at least this often while a program runs
SEND_TRIES = 2  # a command that FieldNET rejects is tried this many times before the program pauses


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass
class Program:
    id: int
    pivot_id: str
    pivot_name: str
    name: str
    mode: str  # 'practice' | 'live'
    state: str  # 'armed' | 'running' | …
    start_when: str | None  # 'next_start' | 'now'
    armed_at: float | None  # epoch s
    settings: dict[str, Any]  # maxDepthIn, fromDate, targetIn, toleranceIn
    approved: dict[str, Any]  # the plan the owner saw when arming


@dataclass
class Run:
    id: int
    program: Program
    practice: bool
    plan: dict[str, Any]  # worked out at start; its steps are what runs
    step: int = 0  # index of the step in progress
    memo: dict[str, Any] = field(default_factory=dict)  # sent, confirmed, fix, offline, tries
    started_at: float = 0.0

    @property
    def steps(self) -> list[dict[str, Any]]:
        return self.plan["steps"]


@dataclass
class Obs:
    """One look at the pivot (FieldNET's live status), in the engine's terms."""

    cls: str  # watcher.classify(): running-wet | running-dry | low-pressure | fault | finished | stopped | offline
    status: str
    direction: str  # what it's actually doing: forward | reverse | stopped
    angle: float | None
    water_on: bool
    depth_in: float | None
    speed_pct: float | None
    service_stop: float | None
    online: bool
    fresh: bool  # read straight after the panel answered a poll
    at: float


@dataclass
class Act:
    kind: str  # none | note | send | pause | finish | end
    message: str = ""
    event: str = ""
    step: int | None = None  # send: index of the step to send
    alert: bool = False
    poll_soon: bool = False  # a flying change is near: the next look must be a fresh panel position
    again_s: float | None = None  # look again this soon (seconds), e.g. waiting for a command to show


NONE = Act("none")


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------


def fmt_deg(a: float | None) -> str:
    return "?" if a is None else f"{norm(a):.1f}°"


def circ_dist(a: float, b: float) -> float:
    d = cw(a, b)
    return min(d, 360.0 - d)


def describe_step(st: dict[str, Any]) -> str:
    what = (f'{st["depth_in"]:.2f}" per pass' if st.get("water_on") and st.get("depth_in") is not None
            else "dry at 100% speed")
    return f'{st["direction"]}, {what}, to {fmt_deg(st["end_deg"])}'


def split_long_steps(steps: list[dict[str, Any]], max_arc: float = MAX_ARC_DEG) -> list[dict[str, Any]]:
    """Split any step longer than max_arc in two (a flying change halfway), so no step's service
    stop is set at or next to where the pivot already is."""
    out: list[dict[str, Any]] = []
    for st in steps:
        if st["arc_deg"] <= max_arc:
            out.append(dict(st))
            continue
        half = st["arc_deg"] / 2.0
        sign = 1.0 if st["direction"] == "forward" else -1.0
        mid = round(norm(st["start_deg"] + sign * half), 1)
        a = dict(st, end_deg=mid, arc_deg=round(half, 1), hours=round(st["hours"] / 2, 2), transition="flying",
                 service_stop_deg=mid, done_after_hours=round(st["done_after_hours"] - st["hours"] / 2, 2))
        b = dict(st, start_deg=mid, arc_deg=round(st["arc_deg"] - half, 1), hours=round(st["hours"] / 2, 2))
        out += [a, b]
    for i, st in enumerate(out):
        st["id"] = i + 1
        st["service_stop_deg"] = st["end_deg"]
    return out


def needs_reapproval(fresh: dict[str, Any], approved: dict[str, Any], max_depth_in: float) -> str | None:
    """Why a plan worked out at start can't run on the owner's earlier approval, or None."""
    deep = [s for s in fresh["steps"] if s.get("water_on") and (s.get("depth_in") or 0) > max_depth_in + 0.005]
    if deep:
        return f'step {deep[0]["id"]} would put on {deep[0]["depth_in"]:.2f}", over the {max_depth_in:.2f}" limit'
    if fresh.get("passes", 0) > approved.get("passes", 0) + 1:
        return f'it now needs {fresh["passes"]} passes; {approved.get("passes", 0)} were approved'
    budget = approved.get("total_hours", 0) * 1.5 + 4
    if fresh.get("total_hours", 0) > budget:
        return f'it now takes {fresh["total_hours"]:.0f} h; {approved.get("total_hours", 0):.0f} h were approved'
    return None


def command_for(st: dict[str, Any]) -> dict[str, Any]:
    """The one full-panel command for a step."""
    wet = bool(st.get("water_on"))
    return {
        "direction": st["direction"],
        "water_on": wet,
        "depth_in": st["depth_in"] if wet else None,
        "speed_pct": None if wet else 100.0,
        "service_stop_deg": round(norm(st["end_deg"]), 1),
    }


def check_command(after: dict[str, Any], cmd: dict[str, Any]) -> str | None:
    """Compare what FieldNET says the panel would be set to (server._describe) with the command.
    Returns what differs, or None if it matches."""
    bad = []
    if after.get("direction_key") != cmd["direction"]:
        bad.append(f'direction {after.get("direction_key")} (step: {cmd["direction"]})')
    if bool(after.get("water_on")) != cmd["water_on"]:
        bad.append(f'water {"on" if after.get("water_on") else "off"} (step: {"on" if cmd["water_on"] else "off"})')
    if cmd["depth_in"] is not None:
        d = after.get("depth_in")
        if d is None or abs(d - cmd["depth_in"]) > 0.011:
            bad.append(f'depth {d}" (step: {cmd["depth_in"]:.2f}")')
    elif (after.get("speed_pct") or 0) < 99.9:
        bad.append(f'speed {after.get("speed_pct")}% (step: 100%)')
    ss = after.get("service_stop_deg")
    if ss is None or circ_dist(ss, cmd["service_stop_deg"]) > 0.05:
        bad.append(f'service stop {fmt_deg(ss)} (step: {fmt_deg(cmd["service_stop_deg"])})')
    if not is_manual(after.get("plan_id")):
        bad.append(f'saved plan {after.get("plan_id")} is selected (programs need manual mode: set it to manual and arm again)')
    return "; ".join(bad) or None


def is_manual(plan_id: Any) -> bool:
    """FieldNET reports manual mode as plan id "manual-mode" (none at all on some panels)."""
    return not plan_id or "manual" in str(plan_id).lower()


def settings_differ(obs: Obs, st: dict[str, Any]) -> str | None:
    """How the pivot's settings differ from the step while it runs, or None."""
    bad = []
    if obs.direction != st["direction"]:
        bad.append(f'going {obs.direction} (step: {st["direction"]})')
    if obs.water_on != bool(st.get("water_on")):
        bad.append(f'water {"on" if obs.water_on else "off"} (step: {"on" if st.get("water_on") else "off"})')
    if st.get("water_on") and st.get("depth_in") is not None:
        if obs.depth_in is None or abs(obs.depth_in - st["depth_in"]) > max(0.015, 0.03 * st["depth_in"]):
            bad.append(f'{obs.depth_in if obs.depth_in is not None else "?"}" per pass (step: {st["depth_in"]:.2f}")')
    elif not st.get("water_on") and (obs.speed_pct or 0) < 98:
        bad.append(f"{obs.speed_pct}% speed (step: 100%)")
    if obs.service_stop is None:
        bad.append(f'no service stop set (step: {fmt_deg(st["end_deg"])})')
    elif circ_dist(obs.service_stop, st["end_deg"]) > 0.3:
        bad.append(f'service stop {fmt_deg(obs.service_stop)} (step: {fmt_deg(st["end_deg"])})')
    return "; ".join(bad) or None


def progress(st: dict[str, Any], angle: float) -> float:
    """Degrees travelled into the step from its start, in its direction. A pivot a little before
    the start (a flying change sent early) counts as 0. May exceed arc_deg (overrun)."""
    p = cw(st["start_deg"], angle) if st["direction"] == "forward" else cw(angle, st["start_deg"])
    return 0.0 if p > 360.0 - EARLY_DEG else p


def deg_per_s(st: dict[str, Any]) -> float:
    hours = st.get("hours") or 0
    return (st["arc_deg"] / hours / 3600.0) if hours > 0 else 0.0


def lead_deg(st: dict[str, Any]) -> float:
    """How far before a flying change's angle to send it: about two minutes of travel, 1° at least."""
    return max(1.0, deg_per_s(st) * 120.0)


def turn_lead_deg(st: dict[str, Any]) -> float:
    """How far before the end of a pass to send the reverse: about 90 seconds of travel (the
    command takes up to a minute to reach the panel), so it lands as the pivot gets there.
    Early leaves a sliver a pass short (0.4° at W-26's 0.35", 0.1% of the field); late just
    means a brief stop at the service stop. 0.3° at least."""
    return max(0.3, deg_per_s(st) * 90.0)


def turns_round(steps: list[dict[str, Any]], k: int) -> bool:
    """Does step k end by turning round into step k+1? (The planner marks those 'stop'.)"""
    return k + 1 < len(steps) and steps[k + 1]["direction"] != steps[k]["direction"]


def on_the_move(steps: list[dict[str, Any]], k: int) -> bool:
    """Is step k's change to the next sent while the pivot is still moving? Speed or water
    changes and turn-rounds both are; only the last step ends at its service stop."""
    return k + 1 < len(steps) and (steps[k]["transition"] == "flying" or turns_round(steps, k))


def near_deg(st: dict[str, Any], interval_s: float) -> float:
    """Within this of a flying change, every check asks the panel for a fresh position."""
    return lead_deg(st) + deg_per_s(st) * (interval_s + 60.0)


def estimate_to_go(run: Run, obs_angle: float | None, now: float) -> float | None:
    """Degrees left in the current step, from the newer of the last panel fix (moved on by the
    step's speed) and the reported angle. None without a position."""
    st = run.steps[run.step]
    best: float | None = None
    fix = run.memo.get("fix")
    if fix and fix.get("step") == run.step:
        best = progress(st, fix["angle"]) + deg_per_s(st) * max(0.0, now - fix["at"])
    if obs_angle is not None:
        p = progress(st, obs_angle)
        best = p if best is None else max(best, p)
    return None if best is None else st["arc_deg"] - best


def is_shuttle(prog: Program) -> bool:
    return (prog.settings or {}).get("kind") == "shuttle"


def shuttle_start(p: dict[str, Any] | None, name: str) -> tuple[bool, str]:
    """Can a shuttle start this pivot now? (True, why) or (False, why not). A shuttle never waits."""
    if p is None:
        return False, f"{name} isn't in FieldNET's list"
    cls = classify(p)
    if cls == "offline":
        return False, f"{name} isn't reporting"
    if cls in ("fault", "low-pressure"):
        return False, f"{name} is in {p.get('operational_status') or 'a fault'}"
    return True, "was started from the Irrigation page"


def start_reason(prog: Program, p: dict[str, Any] | None, seen_idle: bool) -> str | None:
    """Should an armed program start now? p = the pivot's FieldNET list entry."""
    if not p or classify(p) != "running-wet":
        return None
    if prog.start_when == "now":
        return "was watering, so the program took over"
    since = _epoch(p.get("last_updated"))
    if seen_idle or (since is not None and prog.armed_at is not None and since > prog.armed_at):
        return "started watering"
    return None


def decide_live(run: Run, obs: Obs, now: float, interval_s: float) -> list[Act]:
    """One check of a live run. Mutates run.memo (confirmed, offline flag); returns what to do."""
    k, steps, memo = run.step, run.steps, run.memo
    st = steps[k]
    name = run.program.pivot_name
    last = k == len(steps) - 1
    acts: list[Act] = []

    if not obs.online or obs.cls == "offline":
        if memo.get("offline"):
            return [NONE]
        memo["offline"] = True
        return [Act("note", f"{name} isn't reporting. Nothing will be sent until it does; the service stop at "
                            f"{fmt_deg(st['end_deg'])} is the backstop.", "lost-contact", alert=True)]
    if memo.pop("offline", False):
        acts.append(Act("note", f"{name} is reporting again ({obs.status} at {fmt_deg(obs.angle)}).", "back-online"))

    if obs.cls in ("fault", "low-pressure"):
        return acts + [Act("pause", f"{name} {obs.status} at {fmt_deg(obs.angle)} during step {k + 1}. "
                                    "The program is paused and won't restart it.", "fault", alert=True)]

    sent = memo.get("sent") or {}
    sent_at = sent.get("at", now)
    if obs.cls in ("running-wet", "running-dry"):
        why = settings_differ(obs, st)
        if not memo.get("confirmed"):
            if why is None:
                memo["confirmed"] = True
                acts.append(Act("note", f"{name} is on step {k + 1}: {describe_step(st)}.", "confirmed"))
            elif now - sent_at > CONFIRM_S:
                return acts + [Act("pause", f"{name} didn't take step {k + 1} within {CONFIRM_S // 60} minutes ({why}). "
                                            "The program is paused.", "not-taken", alert=True)]
            else:
                return acts + [Act("none", again_s=60)]
        elif why:
            return acts + [Act("pause", f"{name}'s settings were changed outside the program: {why}. "
                                        "The program is paused and won't change them back.", "override", alert=True)]
        if obs.angle is None:
            return acts + [NONE]
        to_go = st["arc_deg"] - progress(st, obs.angle)
        if to_go < -OVERRUN_DEG:
            return acts + [Act("pause", f"{name} ran past step {k + 1}'s service stop at {fmt_deg(st['end_deg'])} "
                                        f"(now at {fmt_deg(obs.angle)}). The program is paused; check the pivot.",
                               "overrun", alert=True)]
        moving_change = on_the_move(steps, k)
        turn = turns_round(steps, k)
        if moving_change and to_go <= (turn_lead_deg(st) if turn else lead_deg(st)):
            if not obs.fresh:
                return acts + [Act("none", poll_soon=True)]
            how = "turned round on the move" if turn else "changed on the move"
            return acts + [Act("send", f"Step {k + 1} done at {fmt_deg(obs.angle)}. Step {k + 2}: "
                                       f"{describe_step(steps[k + 1])} ({how}).", "step", step=k + 1)]
        est = estimate_to_go(run, obs.angle, now)
        soon = moving_change and est is not None and est <= near_deg(st, interval_s)
        return acts + [Act("none", poll_soon=soon)]

    # Not moving.
    at_end = obs.angle is not None and circ_dist(obs.angle, st["end_deg"]) <= END_TOL_DEG
    if obs.cls == "finished" and at_end and memo.get("confirmed"):
        if last:
            return acts + [Act("finish", f"{run.program.name} is done: {name} stopped at its last service stop, "
                                         f"{fmt_deg(obs.angle)}.", "finished", alert=True)]
        return acts + [Act("send", f"Step {k + 1} ended at its service stop ({fmt_deg(obs.angle)}). Step {k + 2}: "
                                   f"{describe_step(steps[k + 1])}.", "step", step=k + 1)]
    if not memo.get("confirmed") and now - sent_at <= CONFIRM_S:
        return acts + [Act("none", again_s=60)]  # just sent; give it time to get going
    prev_end = steps[k - 1]["end_deg"] if k > 0 else None
    if (not memo.get("confirmed") and obs.cls == "finished" and prev_end is not None
            and obs.angle is not None and circ_dist(obs.angle, prev_end) <= END_TOL_DEG
            and memo.get("resent", 0) < 1):
        memo["resent"] = memo.get("resent", 0) + 1
        return acts + [Act("send", f"{name} is still stopped at step {k}'s service stop; sending step {k + 1} again.",
                           "resend", step=k)]
    return acts + [Act("pause", f"{name} stopped at {fmt_deg(obs.angle)} ({obs.status}) during step {k + 1}, not at "
                                "the program's service stop. Manual stops win: the program is paused and won't "
                                "restart it.", "stopped", alert=True)]


def decide_practice(run: Run, obs: Obs, now: float, interval_s: float) -> list[Act]:
    """One check of a practice run. Nothing is sent, so the pivot keeps its own settings; the run
    follows it for as long as it travels the way the program would."""
    k, steps = run.step, run.steps
    st = steps[k]
    name = run.program.pivot_name
    last = k == len(steps) - 1
    if not obs.online or obs.cls == "offline":
        if run.memo.get("offline"):
            return [NONE]
        run.memo["offline"] = True
        return [Act("note", f"{name} isn't reporting. In live, nothing would be sent until it does.", "lost-contact")]
    run.memo.pop("offline", None)
    if obs.cls in ("fault", "low-pressure"):
        return [Act("end", f"{name} {obs.status} at {fmt_deg(obs.angle)}. In live, the program would pause here.",
                    "practice-done", alert=True)]
    if not obs.cls.startswith("running"):
        at_end = obs.angle is not None and circ_dist(obs.angle, st["end_deg"]) <= END_TOL_DEG
        live = ("carry on with the next step" if obs.cls == "finished" and at_end and not last
                else "be done" if obs.cls == "finished" and at_end else "pause (manual stops win)")
        return [Act("end", f"{name} stopped at {fmt_deg(obs.angle)} ({obs.status}) during step {k + 1}. "
                           f"In live, the program would {live}.", "practice-done", alert=True)]
    if obs.direction != st["direction"]:
        return [Act("end", f"{name} is going {obs.direction}; step {k + 1} goes {st['direction']}. Practice can't follow "
                           "a pivot the program hasn't set, so this practice run ends here.", "practice-done", alert=True)]
    if obs.angle is None:
        return [NONE]
    to_go = st["arc_deg"] - progress(st, obs.angle)
    if to_go > lead_deg(st):
        est = estimate_to_go(run, obs.angle, now)
        return [Act("none", poll_soon=est is not None and est <= near_deg(st, interval_s))]
    if not obs.fresh and to_go > -OVERRUN_DEG:
        return [Act("none", poll_soon=True)]
    if last:
        return [Act("end", f"{name} reached the last step's end, {fmt_deg(st['end_deg'])}. In live, its service stop "
                           "would stop it there and the program would be done.", "practice-done", alert=True)]
    nxt = steps[k + 1]
    if st["transition"] == "flying":
        return [Act("send", f"Step {k + 1} done at {fmt_deg(obs.angle)}. Would change on the move to step {k + 2}: "
                            f"{describe_step(nxt)}.", "would-step", step=k + 1)]
    if nxt["direction"] == st["direction"]:
        return [Act("send", f"Step {k + 1} done at {fmt_deg(obs.angle)}. Would stop at the service stop and start "
                            f"step {k + 2}: {describe_step(nxt)}.", "would-step", step=k + 1)]
    return [Act("end", f"Step {k + 1} done at {fmt_deg(obs.angle)}. In live, the service stop would stop {name} here "
                       f"and the program would turn it round for step {k + 2}: {describe_step(nxt)}. Practice can't "
                       "follow a turn-round, so this practice run ends here.", "practice-done", alert=True)]


def wake_in(run: Run, obs: Obs | None, now: float, interval_s: float) -> float:
    """Seconds until this run next needs a look: sooner as a flying change gets near. (In practice
    every change is followed by position, so every step counts as flying.)"""
    st = run.steps[run.step]
    rate = deg_per_s(st)
    est = estimate_to_go(run, obs.angle if obs else None, now)
    if est is None or rate <= 0:
        return interval_s
    if on_the_move(run.steps, run.step) or run.practice:
        return max(20.0, min(interval_s, (est - near_deg(st, interval_s)) / rate))
    return max(60.0, min(interval_s, est / rate))  # waiting for the service stop: the panel reports it


def _epoch(v: Any) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, datetime):
        return v.timestamp()
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


IN_PER_M = 39.3701


def obs_from_live(status: dict[str, Any], fresh: bool, now: float) -> Obs:
    """FieldNET's live pivot-status (server.live_status) → Obs."""
    op = str(status.get("operational_status") or "").lower()
    d = str(status.get("direction_key") or "").lower()
    # Moving: forward-wet, reverse-dry…, or a warning (e.g. low-pressure-warning) while going one way.
    moving = d in ("forward", "reverse") and (op.startswith(("forward", "reverse")) or "warning" in op)
    online = status.get("rtu_online") is not False
    as_list = {"operational_status": op, "communication_status": "online" if online else "offline",
               "direction": d if moving else "stopped", "is_water_on": status.get("water_on")}
    rate, rcf = status.get("rate"), status.get("rate_conversion_factor")
    angle = status.get("current_angle")
    return Obs(
        cls=classify(as_list),
        status=op or "unknown",
        direction=d if moving else "stopped",
        angle=None if angle is None else float(angle),
        water_on=bool(status.get("water_on")),
        depth_in=round(rcf / rate * IN_PER_M, 3) if (rate and rcf) else None,
        speed_pct=None if rate is None else round(float(rate) * 100, 1),
        service_stop=None if status.get("service_stop") is None else float(status["service_stop"]),
        online=online,
        fresh=fresh,
        at=now,
    )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class Store(Protocol):
    async def armed(self) -> list[Program]: ...
    async def open_runs(self) -> list[tuple[Run, str]]: ...  # (run, its program's state now)
    async def program_state(self, program_id: int) -> str | None: ...
    async def begin_run(self, prog: Program, practice: bool, plan: dict[str, Any]) -> Run | None: ...
    async def save_run(self, run: Run) -> None: ...
    async def end_run(self, run: Run, outcome: str, program_state: str | None, reason: str | None = None) -> None: ...
    async def set_program(self, program_id: int, state: str, reason: str | None) -> None: ...
    async def log(self, prog: Program, run: Run | None, kind: str, message: str, practice: bool,
                  alert: bool = False, angle: float | None = None, detail: dict[str, Any] | None = None) -> None: ...
    async def heartbeat(self, error: str | None, allow_live: bool) -> None: ...


class PgStore:
    """Store on the pumpgroups schema (asyncpg)."""

    def __init__(self, db_url: str, tz: ZoneInfo) -> None:
        self.db_url = db_url
        self.tz = tz
        self._pool = None

    async def pool(self):
        if self._pool is None:
            import asyncpg

            async def init(conn):
                await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

            self._pool = await asyncpg.create_pool(self.db_url, min_size=1, max_size=2, command_timeout=20, init=init)
        return self._pool

    @staticmethod
    def _program(r) -> Program:
        return Program(id=r["id"], pivot_id=r["pivot_id"], pivot_name=r["pivot_name"], name=r["name"],
                       mode=r["mode"], state=r["state"], start_when=r["start_when"],
                       armed_at=r["armed_at"].timestamp() if r["armed_at"] else None,
                       settings=r["settings"], approved=r["plan"])

    _COLS = "p.id, p.pivot_id, p.pivot_name, p.name, p.mode, p.state, p.start_when, p.armed_at, p.settings, p.plan"

    async def armed(self) -> list[Program]:
        db = await self.pool()
        rows = await db.fetch(f"SELECT {self._COLS} FROM pumpgroups.program p WHERE p.state = 'armed' ORDER BY p.id")
        return [self._program(r) for r in rows]

    async def open_runs(self) -> list[tuple[Run, str]]:
        db = await self.pool()
        rows = await db.fetch(f"SELECT {self._COLS}, r.id AS run_id, r.practice, r.plan AS run_plan, r.step, r.memo, "
                              "r.started_at FROM pumpgroups.program_run r JOIN pumpgroups.program p ON p.id = r.program_id "
                              "WHERE r.ended_at IS NULL ORDER BY r.id")
        return [(Run(id=r["run_id"], program=self._program(r), practice=r["practice"], plan=r["run_plan"],
                     step=r["step"], memo=r["memo"] or {}, started_at=r["started_at"].timestamp()), r["state"])
                for r in rows]

    async def program_state(self, program_id: int) -> str | None:
        db = await self.pool()
        return await db.fetchval("SELECT state FROM pumpgroups.program WHERE id = $1", program_id)

    async def begin_run(self, prog: Program, practice: bool, plan: dict[str, Any]) -> Run | None:
        db = await self.pool()
        async with db.acquire() as c, c.transaction():
            ok = await c.fetchval("UPDATE pumpgroups.program SET state = 'running', paused_reason = NULL, updated_at = now(), "
                                  "updated_by = 'program engine' WHERE id = $1 AND state = 'armed' RETURNING id", prog.id)
            if not ok:
                return None  # disarmed or deleted in the meantime
            row = await c.fetchrow("INSERT INTO pumpgroups.program_run (program_id, pivot_id, practice, plan, step, memo) "
                                   "VALUES ($1, $2, $3, $4, 0, '{}'::jsonb) RETURNING id, started_at",
                                   prog.id, prog.pivot_id, practice, plan)
        prog.state = "running"
        return Run(id=row["id"], program=prog, practice=practice, plan=plan, started_at=row["started_at"].timestamp())

    async def save_run(self, run: Run) -> None:
        db = await self.pool()
        await db.execute("UPDATE pumpgroups.program_run SET step = $2, memo = $3 WHERE id = $1", run.id, run.step, run.memo)

    async def end_run(self, run: Run, outcome: str, program_state: str | None, reason: str | None = None) -> None:
        db = await self.pool()
        async with db.acquire() as c, c.transaction():
            await c.execute("UPDATE pumpgroups.program_run SET ended_at = now(), outcome = $2, step = $3, memo = $4 "
                            "WHERE id = $1 AND ended_at IS NULL", run.id, outcome, run.step, run.memo)
            if program_state:
                # Only a program the engine is running; one the owner already paused or cancelled keeps that.
                await c.execute("UPDATE pumpgroups.program SET state = $2, paused_reason = $3, updated_at = now(), "
                                "updated_by = 'program engine' WHERE id = $1 AND state = 'running'",
                                run.program.id, program_state, reason)

    async def set_program(self, program_id: int, state: str, reason: str | None) -> None:
        db = await self.pool()
        await db.execute("UPDATE pumpgroups.program SET state = $2, paused_reason = $3, updated_at = now(), "
                         "updated_by = 'program engine' WHERE id = $1 AND state IN ('armed', 'running')",
                         program_id, state, reason)

    async def log(self, prog: Program, run: Run | None, kind: str, message: str, practice: bool,
                  alert: bool = False, angle: float | None = None, detail: dict[str, Any] | None = None) -> None:
        db = await self.pool()
        await db.execute("INSERT INTO pumpgroups.program_event (program_id, run_id, program_name, pivot, step, kind, practice, "
                         "angle, message, detail, alert) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
                         prog.id, run.id if run else None, prog.name, prog.pivot_name,
                         (run.step + 1) if run else None, kind, practice,
                         None if angle is None else round(float(angle), 1), message, detail, alert)
        print(f"PROGRAM {datetime.now(self.tz).isoformat(timespec='seconds')} {'[practice] ' if practice else ''}"
              f"{prog.pivot_name} '{prog.name}': {kind}: {message}", flush=True)

    async def heartbeat(self, error: str | None, allow_live: bool) -> None:
        db = await self.pool()
        await db.execute("INSERT INTO pumpgroups.watcher (id, programs_check_at, programs_error, programs_allow_live) "
                         "VALUES (1, now(), $1, $2) ON CONFLICT (id) DO UPDATE SET programs_check_at = now(), "
                         "programs_error = $1, programs_allow_live = $2", error, allow_live)

    async def active_pivots_mid_program(self) -> set[str]:
        """Pivots whose live program is between steps (its service stops aren't the end of a run)."""
        db = await self.pool()
        rows = await db.fetch("SELECT r.pivot_id FROM pumpgroups.program_run r JOIN pumpgroups.program p ON p.id = r.program_id "
                              "WHERE r.ended_at IS NULL AND NOT r.practice AND p.state = 'running' "
                              "AND r.step < jsonb_array_length(r.plan -> 'steps') - 1")
        return {r["pivot_id"] for r in rows}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

PivotsFn = Callable[[], Awaitable[list[dict[str, Any]]]]
ObserveFn = Callable[[str, bool], Awaitable[dict[str, Any]]]  # (pivot id, poll first) → live status
PlanFn = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]  # (pivot id, settings) → plan
# (pivot id, command, send, verify) → {applied, panel_ack, final_settings, pivot_will, error?, mismatch?}
ControlFn = Callable[[str, dict[str, Any], bool, Callable[[dict[str, Any]], str | None]], Awaitable[dict[str, Any]]]


class Engine:
    def __init__(self, store: Store, pivots: PivotsFn, observe: ObserveFn, plan: PlanFn, control: ControlFn,
                 interval_s: int = 180, allow_live: bool = False, notify: Callable[[], Awaitable[None]] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.pivots = pivots
        self.observe = observe
        self.plan = plan
        self.control = control
        self.interval_s = interval_s
        self.allow_live = allow_live
        self.notify = notify
        self.clock = clock
        self.seen_idle: set[int] = set()  # armed programs whose pivot was seen not watering since
        self.start_failures: dict[int, int] = {}
        self._alerts = False

    async def _log(self, run: Run | Program, kind: str, message: str, alert: bool = False,
                   angle: float | None = None, detail: dict[str, Any] | None = None) -> None:
        prog, r = (run.program, run) if isinstance(run, Run) else (run, None)
        practice = r.practice if r else (prog.mode != "live" or not self.allow_live)
        await self.store.log(prog, r, kind, message, practice, alert, angle, detail)
        self._alerts |= alert

    async def tick(self) -> float:
        """One pass over every armed and running program. Returns seconds until the next is needed."""
        now = self.clock()
        self._alerts = False
        wake = float(self.interval_s)
        # Runs paused or cancelled on the dashboard: close them first (re-arming starts a new run).
        runs = []
        for run, state in await self.store.open_runs():
            if state == "running":
                runs.append(run)
                continue
            outcome = "cancelled" if state == "cancelled" else "paused"
            await self.store.end_run(run, outcome, None)
            st = run.steps[run.step]
            await self._log(run, outcome, f"{run.program.name} was {outcome} on the dashboard during step {run.step + 1}. "
                            f"The pivot carries on to this step's service stop, {fmt_deg(st['end_deg'])}, and "
                            "nothing more is sent.")
        armed = await self.store.armed()
        self.seen_idle &= {p.id for p in armed}
        if armed:
            by_id = {str(p.get("id")): p for p in await self.pivots()}
            for prog in armed:
                p = by_id.get(prog.pivot_id)
                if is_shuttle(prog):
                    ok, why = shuttle_start(p, prog.pivot_name)
                    if not ok:
                        await self.store.set_program(prog.id, "paused", f"couldn't start: {why}")
                        await self._log(prog, "start-failed", f"{prog.name} couldn't start: {why}. The shuttle is paused; "
                                        "start it again when the pivot is ready.", alert=True)
                        continue
                    if await self.start(prog, why, p):
                        wake = min(wake, 60.0)
                    continue
                if p is not None and classify(p) != "running-wet":
                    self.seen_idle.add(prog.id)
                why = start_reason(prog, p, prog.id in self.seen_idle)
                if why and await self.start(prog, why, p):
                    wake = min(wake, 60.0)  # first look at the new run (has it taken step 1?) next pass
        for run in runs:
            try:
                wake = min(wake, await self.check(run))
            except Exception as e:  # noqa: BLE001 - one bad run mustn't stop the others
                traceback.print_exc()
                await self._log(run, "error", f"Couldn't check {run.program.pivot_name}: {type(e).__name__}: {e}. "
                                "Trying again next check.")
        await self.store.heartbeat(None, self.allow_live)
        if self._alerts and self.notify:
            await self.notify()
        return wake

    async def start(self, prog: Program, why: str, p: dict[str, Any] | None) -> Run | None:
        practice = prog.mode != "live" or not self.allow_live
        try:
            plan = await self.plan(prog.pivot_id, prog.settings)
        except Exception as e:  # noqa: BLE001
            n = self.start_failures.get(prog.id, 0) + 1
            self.start_failures[prog.id] = n
            if is_shuttle(prog):  # e.g. the pivot is off the strip: retrying won't help, and it must not start later
                self.start_failures.pop(prog.id, None)
                await self.store.set_program(prog.id, "paused", f"couldn't start: {e}")
                await self._log(prog, "start-failed", f"{prog.name} couldn't start: {e} The shuttle is paused.", alert=True)
                return None
            if n >= 3:
                await self.store.set_program(prog.id, "paused", f"couldn't work out the plan: {e}")
                await self._log(prog, "start-failed", f"{prog.pivot_name} {why}, but the plan couldn't be worked out "
                                f"three times ({e}). The program is paused.", alert=True)
            else:
                await self._log(prog, "start-failed", f"{prog.pivot_name} {why}, but the plan couldn't be worked out "
                                f"({e}). Trying again next check.")
            return None
        self.start_failures.pop(prog.id, None)
        plan = dict(plan, steps=split_long_steps(plan.get("steps") or []))
        if not plan["steps"]:
            await self.store.set_program(prog.id, "finished", "nothing to even up")
            await self._log(prog, "nothing-to-do", f"{prog.pivot_name} {why}, but the field is already even "
                            "(nothing to do). The program is finished.", alert=True)
            return None
        why_not = None if is_shuttle(prog) else needs_reapproval(plan, prog.approved, float(prog.settings.get("maxDepthIn", 0.3)))
        if why_not:
            await self.store.set_program(prog.id, "paused", f"needs a new approval: {why_not}")
            await self._log(prog, "needs-approval", f"{prog.pivot_name} {why}, but the plan worked out now is bigger than "
                            f"the one approved: {why_not}. The program is paused; preview it again and arm it to "
                            "approve the new plan.", alert=True, detail={"plan": _plan_summary(plan)})
            return None
        run = await self.store.begin_run(prog, practice, plan)
        if not run:
            return None
        self.seen_idle.discard(prog.id)
        if is_shuttle(prog):
            sh = plan.get("shuttle") or {}
            how_many = "left on" if sh.get("leave_on") else f"{len(plan['steps'])} pass{'' if len(plan['steps']) == 1 else 'es'}"
            summary = (f"back and forth between {fmt_deg(sh.get('from_deg'))} and {fmt_deg(sh.get('to_deg'))}, {how_many}"
                       + ("" if sh.get("leave_on") else f", about {plan.get('total_hours', 0):.0f} h"))
        else:
            summary = (f"{len(plan['steps'])} step{'' if len(plan['steps']) == 1 else 's'}, about {plan.get('total_hours', 0):.0f} h, "
                       f"spread {plan.get('spread_before_in', 0):.2f}\" → {plan.get('spread_after_in', 0):.2f}\"")
        await self._log(run, "started" if not practice else "practice-start",
                        f"{prog.pivot_name} {why}. {'Practice run of' if practice else 'Running'} {prog.name}: {summary}.",
                        alert=True, angle=(p or {}).get("pivot_angle"), detail={"plan": _plan_summary(plan)})
        ok = await self.send(run, 0, f"Step 1: {describe_step(plan['steps'][0])}.", "would-step" if practice else "step",
                             (p or {}).get("pivot_angle"))
        if practice and is_shuttle(prog) and ok:
            # Practice can't follow a pivot it never starts: pass 1 was checked with FieldNET; that's the practice.
            await self.store.end_run(run, "practice", "finished", "practice run ended")
            await self._log(run, "practice-done", f"Practice: {prog.name} was worked out and pass 1 checked with FieldNET; "
                            "nothing was sent. Start it live to run it.", alert=True)
        return run

    async def send(self, run: Run, k: int, message: str, event: str, angle: float | None = None) -> bool:
        """Send (or, in practice, check without sending) the command for step k."""
        st = run.steps[k]
        cmd = command_for(st)
        if not run.practice and await self.store.program_state(run.program.id) != "running":
            return False  # paused or cancelled on the dashboard a moment ago
        try:
            res = await self.control(run.program.pivot_id, cmd, not run.practice, lambda after: check_command(after, cmd))
        except Exception as e:  # noqa: BLE001
            res = {"applied": False, "error": f"{type(e).__name__}: {e}"}
        detail = {"command": cmd, "pivot_will": res.get("pivot_will"), "final_settings": res.get("final_settings"),
                  "panel_ack": res.get("panel_ack")}
        now = self.clock()
        if res.get("mismatch"):
            await self._pause(run, "mismatch", f"Didn't send step {k + 1}: what FieldNET would apply doesn't match it "
                              f"({res['mismatch']}). The program is paused.", detail)
            return False
        if run.practice:
            ok = not res.get("error")
            run.step = k
            run.memo.update(sent={"step": k, "at": now, **cmd}, confirmed=True)
            await self.store.save_run(run)
            await self._log(run, event, message + ("" if ok else f" (FieldNET couldn't check it: {res.get('error')})")
                            + (f" Would send: {res.get('pivot_will')}." if res.get("pivot_will") else ""),
                            angle=angle, detail=detail)
            return ok
        queued = res.get("panel_ack") == "waiting-for-online-status"
        if res.get("applied") or queued or res.get("no_change"):
            run.step = k
            run.memo.pop("want", None)
            run.memo.update(sent={"step": k, "at": now, **cmd}, confirmed=False, tries=0)
            if event != "resend":
                run.memo.pop("resent", None)
            await self.store.save_run(run)
            await self._log(run, event, message + (" Queued: the panel is offline." if queued else ""), angle=angle,
                            detail=detail)
            return True
        tries = run.memo.get("tries", 0) + 1
        run.memo["tries"] = tries
        run.memo["want"] = {"step": k, "message": message, "event": event}  # try again next check
        await self.store.save_run(run)
        err = res.get("error") or res.get("note") or res.get("panel_ack") or "no acknowledgement"
        if tries >= SEND_TRIES:
            await self._pause(run, "send-failed", f"Couldn't send step {k + 1} ({err}), {tries} tries. The program is "
                              f"paused; the pivot stops at the service stop already set, {fmt_deg(run.steps[run.step]['end_deg'])}.",
                              detail)
        else:
            await self._log(run, "send-failed", f"Couldn't send step {k + 1} ({err}). Trying again next check.", detail=detail)
        return False

    async def _pause(self, run: Run, kind: str, message: str, detail: dict[str, Any] | None = None) -> None:
        await self.store.end_run(run, "paused", "paused", message)
        await self._log(run, kind, message, alert=True, detail=detail)

    async def look(self, run: Run, poll: bool) -> Obs:
        now = self.clock()
        status = dict(await self.observe(run.program.pivot_id, poll))
        fresh = bool(status.pop("_fresh", False))
        obs = obs_from_live(status, fresh, now)
        if fresh and obs.angle is not None:
            run.memo["fix"] = {"angle": obs.angle, "at": now, "step": run.step}
        return obs

    async def check(self, run: Run) -> float:
        now = self.clock()
        st = run.steps[run.step]
        fix = run.memo.get("fix") or {}
        est = estimate_to_go(run, None, now)
        follow = on_the_move(run.steps, run.step) or run.practice
        poll = (fix.get("step") != run.step or now - fix.get("at", 0) > FIX_MAX_AGE_S
                or (follow and est is not None and est <= near_deg(st, self.interval_s) * 1.5))
        obs = await self.look(run, poll)
        want = run.memo.get("want")
        if want and obs.online and obs.cls not in ("fault", "low-pressure", "offline"):
            await self.send(run, want["step"], want["message"], want["event"], obs.angle)  # the last try failed
            await self.store.save_run(run)
            return 60.0
        # A run that takes far longer than planned is stuck somewhere: hand it back to a person.
        limit = run.started_at + (run.plan.get("total_hours", 0) * 2 + 6) * 3600
        if not run.practice and now > limit:
            await self._pause(run, "overdue", f"{run.program.name} has taken more than twice its planned time. "
                              "The program is paused; check the pivot.")
            return self.interval_s
        decide = decide_practice if run.practice else decide_live
        acts = decide(run, obs, now, self.interval_s)
        if not obs.fresh and any(a.poll_soon for a in acts) and all(a.kind in ("none", "note") for a in acts):
            # Near a change on a position that may be old: note what was seen, then ask the panel.
            for a in acts:
                if a.kind == "note":
                    await self._log(run, a.event, a.message, a.alert, obs.angle)
            obs = await self.look(run, True)
            acts = decide(run, obs, self.clock(), self.interval_s)
        again: float | None = None
        for a in acts:
            if a.kind == "note":
                await self._log(run, a.event, a.message, a.alert, obs.angle)
            elif a.kind == "send":
                await self.send(run, a.step if a.step is not None else run.step, a.message, a.event, obs.angle)
                again = 60.0  # see that it took
            elif a.kind == "pause":
                await self._pause(run, a.event, a.message, {"status": obs.status, "angle": obs.angle})
                return self.interval_s
            elif a.kind == "end":
                await self.store.end_run(run, "practice", "finished", "practice run ended")
                await self._log(run, a.event, a.message, a.alert, obs.angle)
                return self.interval_s
            elif a.kind == "finish":
                await self.finish(run, obs, a)
                return self.interval_s
            if a.again_s is not None:
                again = a.again_s if again is None else min(again, a.again_s)
        await self.store.save_run(run)
        wake = wake_in(run, obs, self.clock(), self.interval_s)
        return min(wake, again) if again is not None else wake

    async def finish(self, run: Run, obs: Obs, a: Act) -> None:
        """Last step done. on_complete = stop, water off: turn the panel's water off if it's still on."""
        msg = a.message
        if obs.water_on:
            cmd = {"direction": "stopped", "water_on": False}
            try:
                res = await self.control(run.program.pivot_id, cmd, True,
                                         lambda after: None if (after.get("direction_key") == "stopped"
                                                                and not after.get("water_on")) else "not stopped with water off")
                msg += " Water turned off." if (res.get("applied") or res.get("no_change")) else \
                    f" Couldn't turn the water off ({res.get('error') or res.get('panel_ack')}); check it."
            except Exception as e:  # noqa: BLE001
                msg += f" Couldn't turn the water off ({e}); check it."
        await self.store.end_run(run, "finished", "finished", None)
        await self._log(run, a.event, msg, a.alert, obs.angle)

    async def run_forever(self) -> None:
        print(f"Program engine running (every {self.interval_s}s, sooner near a change); live control "
              f"{'ALLOWED' if self.allow_live else 'OFF (practice only)'}.", flush=True)
        while True:
            try:
                wait = await self.tick()
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                wait = self.interval_s
                try:
                    await self.store.heartbeat(f"{type(e).__name__}: {e}"[:500], self.allow_live)
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(max(20.0, min(float(self.interval_s), wait)))


def _plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
    keys = ("target_in", "max_depth_per_pass_in", "start", "range_before_in", "range_after_in", "spread_before_in",
            "spread_after_in", "passes", "stops", "total_hours", "applied_from", "applied_to")
    return {k: plan.get(k) for k in keys} | {"steps": [
        {k: s.get(k) for k in ("id", "direction", "water_on", "depth_in", "start_deg", "end_deg", "hours", "transition")}
        for s in plan.get("steps", [])]}


def from_env(store_url: str, tz: ZoneInfo, pivots: PivotsFn, observe: ObserveFn, plan: PlanFn, control: ControlFn,
             notify: Callable[[], Awaitable[None]] | None) -> Engine | None:
    """Build the engine from environment variables, or None if it isn't switched on."""
    if not _on("PROGRAMS_ENABLED"):
        return None
    if not store_url:
        print("PROGRAMS_ENABLED=1 but PUMPGROUPS_DB_URL is not set; program engine not started.", flush=True)
        return None
    return Engine(PgStore(store_url, tz), pivots, observe, plan, control,
                  interval_s=max(60, int(os.environ.get("PROGRAMS_INTERVAL_S", "180"))),
                  allow_live=_on("PROGRAMS_ALLOW_LIVE"), notify=notify)


def _on(name: str) -> bool:
    """A switch typed by hand into Render: 1, true, yes or on, with or without quotes or spaces."""
    return os.environ.get(name, "").strip().strip("'\"").strip().lower() in ("1", "true", "yes", "on")


__all__ = ["Engine", "PgStore", "from_env", "decide_live", "decide_practice", "check_command", "split_long_steps",
           "needs_reapproval", "start_reason", "obs_from_live", "Program", "Run", "Obs", "Act"]
