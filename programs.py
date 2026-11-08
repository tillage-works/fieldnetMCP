"""
Irrigation programs: the even-up planner
========================================

Plans how to even up the water applied around a circle: from the Applied
Irrigation wedges, the pivot's position and a field's limits, it proposes the
position-based steps (direction, water, depth per pass, stop angle), how long
each takes, and the depth every wedge ends up with.

Pure logic, no FieldNET or database, so it can be tested on its own
(test_programs.py) and used the same way by the plan_even_up tool, the
dashboard's preview and, later, the program engine. Nothing here sends a
command to a pivot.

Why it works the way it does (the W-26 case, Nov 2026):
  * At a constant speed every pass puts the same depth everywhere, so where
    you shut off can't even anything up. Uneven ground needs a different depth
    per pass by sector (slower over dry ground, dry-running over wet ground)
    or extra passes over only the dry arc by reversing.
  * One pass can't put on more than the ground takes in (the field's intake
    limit, e.g. 0.35" in the fall). A bigger shortfall needs more passes.
  * The lightest wet pass is the depth at 100% speed. A shortfall smaller than
    half of that can't be improved without overwatering more than it fixes.

The plan, from where the pivot is now:
  1. Carry on (or set off) one way and keep going until every wedge that needs
     water has been crossed once, watering each at its own depth per pass.
  2. Then shuttle back and forth over only the stretch that still needs more,
     each pass shorter than the last. Reversals are stops.
  Both starting directions are tried; the quicker plan wins, and turning
  round straight away pays a stop-and-restart allowance.

Angles are degrees clockwise from north. Forward is clockwise (increasing
angle) on every Hyer Farms pivot; reverse is anticlockwise. Speed and water
can always change on the fly; only a reversal needs a stop.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

EPS = 1e-6

# The most one pass may put on when nobody says otherwise. The real limit
# changes with soil, season and conditions, so it's asked for every plan.
DEFAULT_MAX_DEPTH_IN = 0.30

# ---------------------------------------------------------------------------
# Angles. Every arc and crossing check goes through these, so wrap-around
# (355° → 5° is 10° clockwise) is handled in one tested place.
# ---------------------------------------------------------------------------


def norm(a: float) -> float:
    """An angle in [0, 360)."""
    v = a % 360.0
    return 0.0 if abs(v - 360.0) < EPS or abs(v) < EPS else v


def cw(a: float, b: float) -> float:
    """Degrees travelled going clockwise from a to b, in [0, 360)."""
    return norm(b - a)


def arc_contains(start: float, end: float, x: float) -> bool:
    """Is x on the clockwise arc [start, end)? start == end is the whole circle."""
    span = cw(start, end)
    return span < EPS or cw(start, x) < span - EPS


def degrees_to_go(now: float, target: float, clockwise: bool) -> float:
    """How far the pivot still has to travel to reach target, moving one way."""
    return cw(now, target) if clockwise else cw(target, now)


def has_passed(start: float, target: float, now: float, clockwise: bool) -> bool:
    """Moving one way from start (less than a full turn), has the pivot reached target?

    Compares distance travelled with distance needed, so it works through 0°.
    A target equal to start counts as reached straight away."""
    travelled = cw(start, now) if clockwise else cw(now, start)
    needed = cw(start, target) if clockwise else cw(target, start)
    return travelled >= needed - EPS


# ---------------------------------------------------------------------------
# Inputs and outputs
# ---------------------------------------------------------------------------


@dataclass
class Wedge:
    """One wedge of the Applied Irrigation map: the clockwise arc from_deg → to_deg."""

    from_deg: float
    to_deg: float
    depth_in: float


@dataclass
class FieldProfile:
    depth_full_in: float  # inches per pass at 100% speed: the lightest wet pass there is
    circle_hours_full: float  # hours per circle at 100% speed
    max_depth_in: float = DEFAULT_MAX_DEPTH_IN  # intake limit: the most one pass may put on
    restart_minutes: float = 5.0  # allowance for each stop and restart (a reversal); restarts aren't costly


@dataclass
class PivotState:
    angle: float
    direction: str  # 'forward' | 'reverse' | 'stopped'
    depth_in: float | None = None  # current depth per pass, if it's running
    water_on: bool = False


@dataclass
class _Seg:
    start: float  # clockwise start angle
    span: float
    applied: float
    wedge: int  # index into the wedge list
    need: float = 0.0  # inches short of target
    passes: int = 0  # wet passes it gets
    per_pass: float = 0.0  # depth for each of them
    lo: float = 0.0  # each wet pass must fall in [lo, hi], so that all its passes together
    hi: float = 0.0  # land within the tolerance of what it needs
    share: float = 0.0  # percent of the field
    added: float = 0.0  # what the plan puts on


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


def _segments(wedges: list[Wedge], at: float) -> list[_Seg]:
    """The circle cut at every wedge boundary and at the pivot, clockwise from the pivot."""
    cuts = {round(norm(at), 6)}
    for w in wedges:
        cuts.add(round(norm(w.from_deg), 6))
        cuts.add(round(norm(w.to_deg), 6))
    order = sorted(cuts, key=lambda a: cw(at, a))
    segs: list[_Seg] = []
    for i, a in enumerate(order):
        b = order[(i + 1) % len(order)]
        span = cw(a, b) if len(order) > 1 else 360.0
        if span < EPS:
            continue
        mid = norm(a + span / 2)
        idx = next((j for j, w in enumerate(wedges) if arc_contains(w.from_deg, w.to_deg, mid)), None)
        if idx is None:
            raise ValueError(f"No applied-water wedge covers {mid:.1f}°.")
        segs.append(_Seg(start=a, span=span, applied=wedges[idx].depth_in, wedge=idx, share=span / 3.6))
    return segs


def _crossings(segs: list[_Seg], at: float, clockwise: bool, work: list[int]) -> list[tuple[int, list[int]]]:
    """The passes: [(+1 or -1, segment indices in travel order)].

    Pass 1 runs from the pivot one way until every segment that needs water has
    been crossed. Each later pass turns round and stops at the last segment
    that still needs another pass, so the shuttles get shorter."""
    # Position of each segment along the path, measured from the pivot.
    pos = []
    for s in segs:
        enter = cw(at, s.start) if clockwise else cw(norm(s.start + s.span), at)
        pos.append((enter, enter + s.span))
    order = sorted(range(len(segs)), key=lambda i: pos[i][0])

    def covering(lo: float, hi: float) -> list[int]:
        return [i for i in order if pos[i][0] >= lo - EPS and pos[i][1] <= hi + EPS]

    out: list[tuple[int, list[int]]] = []
    hi = max(pos[i][1] for i in work)
    out.append((+1, covering(0.0, hi)))
    lo, k = 0.0, 2
    while True:
        need_k = [i for i in work if segs[i].passes >= k]
        if not need_k:
            break
        if k % 2 == 0:
            lo = min(pos[i][0] for i in need_k)
            out.append((-1, list(reversed(covering(lo, hi)))))
        else:
            hi = max(pos[i][1] for i in need_k)
            out.append((+1, covering(lo, hi)))
        k += 1
    return out


def _pick_depth(lo: float, hi: float, members: list[tuple[float, float]], prefer: float | None) -> float:
    """A depth inside [lo, hi]: the one already running if it fits, else the span-weighted target."""
    if prefer is not None and lo - EPS <= prefer <= hi + EPS:
        return prefer
    total = sum(sp for _, sp in members)
    mean = sum(r * sp for r, sp in members) / total if total else (lo + hi) / 2
    return min(max(mean, lo), hi)


def _round_depth(d: float, profile: FieldProfile) -> float:
    """Two decimals, never lighter than 100% speed allows nor over the intake limit."""
    r = round(d, 2)
    if r < profile.depth_full_in:
        return profile.depth_full_in
    return min(r, math.floor(profile.max_depth_in * 100 + EPS) / 100)


def _build(segs: list[_Seg], at: float, clockwise: bool, work: list[int], profile: FieldProfile,
           state: PivotState, min_step_deg: float) -> dict[str, Any]:
    """Turn one starting direction into steps, times and what each segment gets."""
    for s in segs:
        s.added = 0.0
    crossed = [0] * len(segs)
    steps: list[dict[str, Any]] = []
    moving_dir = state.direction if state.direction in ("forward", "reverse") else None
    cur_cw = None if moving_dir is None else moving_dir == "forward"  # forward = clockwise

    passes = _crossings(segs, at, clockwise, work)
    for pi, (sign, idxs) in enumerate(passes):
        goes_cw = clockwise if sign > 0 else not clockwise
        direction = "forward" if goes_cw else "reverse"
        groups: list[dict[str, Any]] = []  # consecutive segments sharing one command
        for i in idxs:
            s = segs[i]
            crossed[i] += 1
            wet = crossed[i] <= s.passes
            g = groups[-1] if groups else None
            tiny = s.span < min_step_deg
            if g and (tiny or (g["wet"] == wet and (not wet or max(g["lo"], s.lo) <= min(g["hi"], s.hi) + EPS))):
                g["segs"].append(i)
                if wet and not tiny and g["wet"]:
                    g["lo"] = max(g["lo"], s.lo)
                    g["hi"] = min(g["hi"], s.hi)
                    g["members"].append((s.per_pass, s.span))
                continue
            groups.append({"wet": wet, "segs": [i], "lo": s.lo if wet else 0.0, "hi": s.hi if wet else 0.0,
                           "members": [(s.per_pass, s.span)] if wet else []})
        # A group spanning less than min_step_deg is too short for its own speed
        # change: fold it into the next group (or the previous one at the end).
        merged: list[dict[str, Any]] = []
        carry: list[int] = []
        for g in groups:
            arc = sum(segs[i].span for i in carry + g["segs"])
            if arc < min_step_deg and g is not groups[-1]:
                carry += g["segs"]
                continue
            g["segs"] = carry + g["segs"]
            carry = []
            merged.append(g)
        if carry and merged:
            merged[-1]["segs"] += carry
        elif carry:
            merged.append({"wet": False, "segs": carry, "lo": 0.0, "hi": 0.0, "members": []})
        groups = merged
        for gi, g in enumerate(groups):
            prefer = None
            if pi == 0 and gi == 0 and state.water_on and cur_cw == goes_cw and state.depth_in:
                prefer = state.depth_in
            depth = _round_depth(_pick_depth(g["lo"], g["hi"], g["members"], prefer), profile) if g["wet"] and g["members"] else None
            if g["wet"] and depth is None:  # a group made only of tiny wet slivers
                depth = _round_depth(segs[g["segs"][0]].per_pass, profile)
            for i in g["segs"]:
                if depth is not None:
                    segs[i].added += depth
            first, last = segs[g["segs"][0]], segs[g["segs"][-1]]
            if goes_cw:
                start, end = first.start, norm(last.start + last.span)
            else:
                start, end = norm(first.start + first.span), last.start
            arc = sum(segs[i].span for i in g["segs"])
            hours = profile.circle_hours_full * arc / 360.0 * ((depth / profile.depth_full_in) if depth else 1.0)
            steps.append({
                "direction": direction,
                "water_on": depth is not None,
                "depth_in": depth,
                "speed_pct": round(100.0 * profile.depth_full_in / depth, 1) if depth else 100.0,
                "start_deg": round(start, 1),
                "end_deg": round(end, 1),
                "arc_deg": round(arc, 1),
                "hours": round(hours, 2),
                "transition": "flying" if gi < len(groups) - 1 else ("stop" if pi < len(passes) - 1 else "end"),
            })
    stops = sum(1 for st in steps if st["transition"] == "stop")
    turn_now = cur_cw is not None and cur_cw != clockwise
    restarts = stops + (1 if turn_now else 0)
    run_hours = sum(st["hours"] for st in steps)
    return {"steps": steps, "passes": len(passes), "stops": restarts, "turns_round_first": turn_now,
            "run_hours": run_hours, "total_hours": run_hours + restarts * profile.restart_minutes / 60.0,
            "added": [s.added for s in segs]}


def plan_even_up(wedges: list[Wedge], state: PivotState, profile: FieldProfile, target_in: float | None = None,
                 tolerance_in: float = 0.05, min_share_pct: float = 1.0, min_step_deg: float = 3.0) -> dict[str, Any]:
    """Plan an even-up. Returns the steps, timings, projected depths and notes (see the module doc)."""
    if not wedges:
        raise ValueError("No applied-water wedges for that range.")
    covered = sum(cw(w.from_deg, w.to_deg) or 360.0 for w in wedges)
    if covered < 359.0:
        raise ValueError(f"The applied-water map covers only {covered / 3.6:.0f}% of the circle. "
                         "Partial circles and barricades aren't supported yet.")
    if profile.max_depth_in < profile.depth_full_in - EPS:
        raise ValueError(f'The intake limit ({profile.max_depth_in:g}") is lighter than a pass at 100% speed '
                         f'({profile.depth_full_in:.3f}"), so no wet pass fits.')
    if profile.depth_full_in <= 0 or profile.circle_hours_full <= 0:
        raise ValueError("The field profile needs depth and circle time at 100% speed.")

    target = max(w.depth_in for w in wedges) if target_in is None else target_in
    at = norm(state.angle)
    segs = _segments(wedges, at)
    notes: list[str] = []
    d_full, d_max = profile.depth_full_in, profile.max_depth_in
    for s in segs:
        s.need = max(0.0, target - s.applied)
        if s.need < tolerance_in - EPS or s.need < d_full / 2:
            s.passes, s.per_pass = 0, 0.0
        else:
            s.passes = max(1, math.ceil((s.need - tolerance_in) / d_max - EPS))
            s.per_pass = min(max(s.need / s.passes, d_full), d_max)
            # The window is on the TOTAL: passes × depth within need ± tolerance. When
            # even the lightest pass overshoots, the window is just that pass.
            s.lo = max(d_full, (s.need - tolerance_in) / s.passes)
            s.hi = min(d_max, (s.need + tolerance_in) / s.passes)
            if s.lo > s.hi:
                s.lo = s.hi = s.per_pass

    unhelpable = [s for s in segs if s.need >= tolerance_in - EPS and s.passes == 0]
    if unhelpable:
        worst = max(unhelpable, key=lambda s: s.need)
        notes.append(f'Some ground is short by less than half a pass at 100% speed ({worst.need:.2f}" at most, '
                     f'vs {d_full:.2f}" per pass), so watering it would overshoot by more than it fixes.')

    # Work that decides how far the pivot travels; tiny slivers don't extend a run.
    work = [i for i, s in enumerate(segs) if s.passes >= 1 and s.share >= min_share_pct]
    slivers = [s for s in segs if s.passes >= 1 and s.share < min_share_pct]

    def wedge_table(added: list[float]) -> list[dict[str, Any]]:
        out = []
        for j, w in enumerate(wedges):
            mine = [(s, added[i]) for i, s in enumerate(segs) if s.wedge == j]
            span = sum(s.span for s, _ in mine)
            after = sum((s.applied + a) * s.span for s, a in mine) / span if span else w.depth_in
            out.append({"from_deg": w.from_deg, "to_deg": w.to_deg, "share_pct": round(span / 3.6, 1),
                        "before_in": round(w.depth_in, 2), "after_in": round(after, 2)})
        return out

    def spread(values: list[tuple[float, _Seg]]) -> tuple[float, float]:
        big = [v for v, s in values if s.share >= min_share_pct] or [v for v, _ in values]
        return min(big), max(big)

    lo0, hi0 = spread([(s.applied, s) for s in segs])
    base = {"target_in": round(target, 2), "tolerance_in": tolerance_in, "max_depth_per_pass_in": d_max,
            "depth_at_full_speed_in": round(d_full, 3), "start": {"angle_deg": round(at, 1), "direction": state.direction},
            "range_before_in": [round(lo0, 2), round(hi0, 2)], "spread_before_in": round(hi0 - lo0, 2)}

    if not work:
        notes.insert(0, "Nothing to do: every wedge is already within tolerance of the target, or too close to it to help.")
        return {**base, "feasible": True, "steps": [], "passes": 0, "stops": 0, "total_hours": 0.0,
                "range_after_in": base["range_before_in"], "spread_after_in": base["spread_before_in"],
                "wedges": wedge_table([0.0] * len(segs)), "notes": notes}

    # Try both starting directions; turning round straight away costs a restart.
    cur = state.direction if state.direction in ("forward", "reverse") else None
    cur_cw = None if cur is None else cur == "forward"  # forward = clockwise
    options = [cur_cw] if cur_cw is not None else [True]
    options.append(not options[0])
    best = None
    for clockwise in options:
        plan = _build(segs, at, clockwise, work, profile, state, min_step_deg)
        if best is None or plan["total_hours"] < best["total_hours"] * 0.99 - EPS:
            best = plan
    assert best is not None
    added = best["added"]
    for i, s in enumerate(segs):
        s.added = added[i]
    for st in best["steps"]:  # the intake limit is never exceeded, whatever the rounding did
        if st["depth_in"] is not None and st["depth_in"] > d_max + EPS:
            raise AssertionError(f"step depth {st['depth_in']} over the intake limit {d_max}")

    lo1, hi1 = spread([(s.applied + s.added, s) for s in segs])
    left_short = [s for s in slivers if s.added < s.need - tolerance_in]
    if left_short:
        total = sum(s.span for s in left_short)
        notes.append(f"{total:.1f}° of slivers ({total / 3.6:.1f}% of the field, e.g. right behind the pivot) stay short: "
                     "not worth extending the run for.")
    if best["turns_round_first"]:
        notes.append("Turning round first is quicker than carrying on, so step 1 starts with a reversal.")
    elapsed = profile.restart_minutes / 60.0 if best["turns_round_first"] else 0.0
    for n, st in enumerate(best["steps"], start=1):
        st["id"] = n
        elapsed += st["hours"]
        st["done_after_hours"] = round(elapsed, 2)
        if st["transition"] == "stop":
            elapsed += profile.restart_minutes / 60.0
    return {**base, "feasible": True, "steps": best["steps"], "passes": best["passes"], "stops": best["stops"],
            "total_hours": round(best["total_hours"], 2), "range_after_in": [round(lo1, 2), round(hi1, 2)],
            "spread_after_in": round(hi1 - lo1, 2), "wedges": wedge_table(added), "notes": notes}


# ---------------------------------------------------------------------------
# Shuttle: back and forth over a strip between two stops.
# ---------------------------------------------------------------------------

SHUTTLE_END_TOL_DEG = 2.0  # this close to a stop counts as at it
SHUTTLE_HORIZON_H = 30 * 24.0  # "leave on" plans this far ahead (it stops at the last pass's service stop)
SHUTTLE_MAX_PASSES = 1000


def plan_shuttle(from_deg: float, to_deg: float, angle_deg: float, passes: int | None, water_on: bool,
                 depth_in: float | None, depth_full_in: float, circle_hours_full: float,
                 first: str | None = None) -> dict[str, Any]:
    """Back and forth over the strip that runs FORWARD (clockwise) from from_deg to to_deg.

    One pass = across the strip once: passes=1 goes to the other stop and stops there; 2 goes over
    and back; None = "leave on" (keeps going, planned SHUTTLE_HORIZON_H ahead). The pivot must be on
    the strip. The first pass heads for the farther stop unless `first` says which way; from the
    middle of the strip it's a part pass. Every pass ends at a stop and turns round on the move
    there, with the stop as its service stop (the engine's rules); the last one stops at it.
    Wet passes run at depth_in (no less than the pivot puts on at full speed); dry ones at 100%."""
    width = cw(from_deg, to_deg)
    if width < 1.0:
        raise ValueError("The two stops are the same angle: pick two different stops.")
    if passes is not None and not (1 <= passes <= SHUTTLE_MAX_PASSES):
        raise ValueError(f"Passes must be 1 to {SHUTTLE_MAX_PASSES}, or leave it on.")
    if water_on:
        if depth_in is None or depth_in <= 0:
            raise ValueError("Say how much to put on per pass.")
        if depth_in < depth_full_in - 0.005:
            raise ValueError(f'{depth_in:.2f}" is less than this pivot puts on at full speed ({depth_full_in:.2f}").')
    if circle_hours_full <= 0:
        raise ValueError("This pivot has no full-circle time, so a shuttle can't be timed.")
    if first not in (None, "forward", "reverse"):
        raise ValueError("first must be forward or reverse.")
    pos = cw(from_deg, angle_deg)  # how far along the strip, from the first stop going forward
    if pos > 360.0 - SHUTTLE_END_TOL_DEG:
        pos = 0.0
    if pos > width + SHUTTLE_END_TOL_DEG:
        raise ValueError(f"The pivot is at {norm(angle_deg):.1f}°, off the strip {norm(from_deg):.1f}° → {norm(to_deg):.1f}°. "
                         "Park it at one of the stops first.")
    pos = min(pos, width)
    direction = first or ("forward" if width - pos >= pos else "reverse")
    if (direction == "forward" and width - pos < 0.5) or (direction == "reverse" and pos < 0.5):
        direction = "reverse" if direction == "forward" else "forward"  # already at that stop: the other way first
    speed = 1.0 if not water_on else min(1.0, depth_full_in / float(depth_in))
    hours_per_deg = circle_hours_full / 360.0 / speed
    limit = passes or SHUTTLE_MAX_PASSES
    steps: list[dict[str, Any]] = []
    elapsed = 0.0
    while len(steps) < limit:
        target = width if direction == "forward" else 0.0
        arc = abs(target - pos)
        hours = arc * hours_per_deg
        elapsed += hours
        end = norm(from_deg + target)
        steps.append({
            "id": len(steps) + 1, "pass_no": len(steps) + 1, "direction": direction, "water_on": bool(water_on),
            "depth_in": round(float(depth_in), 2) if water_on else None, "speed_pct": round(100.0 * speed, 1),
            "start_deg": round(norm(from_deg + pos), 1), "end_deg": round(end, 1), "arc_deg": round(arc, 1),
            "hours": round(hours, 2), "transition": "stop", "done_after_hours": round(elapsed, 2),
            "service_stop_deg": round(end, 1),
        })
        pos, direction = target, ("reverse" if direction == "forward" else "forward")
        if passes is None and elapsed >= SHUTTLE_HORIZON_H:
            break
    steps[-1]["transition"] = "end"
    what = f'wet at {float(depth_in):.2f}" per pass' if water_on else "dry at 100%"
    notes = [f"Back and forth over {width:.1f}° between {norm(from_deg):.1f}° and {norm(to_deg):.1f}°, {what}."]
    if steps[0]["arc_deg"] < width - 0.5:
        notes.append(f"It starts {norm(angle_deg):.1f}° along the strip, so pass 1 is a part pass.")
    if passes is None:
        notes.append(f"Left on: it keeps going (planned {len(steps)} passes, about {elapsed / 24:.0f} days ahead); "
                     "stop it any time with Stop or by pausing the program.")
    return {
        "kind": "shuttle", "feasible": True, "pivot": "", "position_as_of": None,
        "start": {"angle_deg": round(norm(angle_deg), 1), "direction": steps[0]["direction"]},
        "shuttle": {"from_deg": round(norm(from_deg), 1), "to_deg": round(norm(to_deg), 1), "width_deg": round(width, 1),
                    "passes": passes, "leave_on": passes is None, "water_on": bool(water_on),
                    "depth_in": round(float(depth_in), 2) if water_on else None, "pass_hours": round(width * hours_per_deg, 2)},
        "steps": steps, "passes": len(steps), "stops": 0, "total_hours": round(elapsed, 2),
        "depth_at_full_speed_in": round(depth_full_in, 3), "max_depth_per_pass_in": round(float(depth_in), 2) if water_on else 0,
        "target_in": 0, "tolerance_in": 0, "range_before_in": [0, 0], "spread_before_in": 0, "range_after_in": [0, 0],
        "spread_after_in": 0, "wedges": [], "notes": notes,
    }


__all__ = ["Wedge", "FieldProfile", "PivotState", "plan_even_up", "plan_shuttle", "DEFAULT_MAX_DEPTH_IN", "norm", "cw",
           "arc_contains", "degrees_to_go", "has_passed"]
