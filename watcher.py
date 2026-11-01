"""
Pump group fallback watcher
===========================

Fields fed by the same water are grouped into PUMP GROUPS (set up on the Hyer
Farms dashboard's Pump groups page). During a group's schedule windows (e.g.
every night 8 pm-6 am, or Fri 6 pm-Mon 6 am) this watcher checks the group's
pivots every few minutes. When a scheduled pivot that was watering stops
because it faulted or finished its run, the watcher starts the group's next
fallback pivot with the settings saved for it, so the water keeps going to a
field.

Rules (agreed with Andrew):
  * Triggers: a pivot that was WATERING (moving with water on) and is now
      - shut down on a fault (shutdown / fault / alarm / safety / powered off), or
      - finished normally (service stop, barrier stop).
    A plain "stopped" (someone stopped it by hand) and losing contact do NOT
    trigger anything.
  * Low pressure: the first low-pressure shutdown in a window starts one
    fallback; a second one in the same window starts nothing and alerts,
    because the water source itself is probably the problem.
  * A fallback is started at most once per window, only if it's online and
    stopped, and only with the direction and speed/depth saved for it.
    Fallbacks the watcher starts are watched too, so they can chain.
  * After a live start, the pivot must be seen moving within CONFIRM_MINUTES
    or an alert is raised.
  * Each group is 'off', 'practice' (log what would happen, start nothing) or
    'live'. WATCHER_ALLOW_LIVE must also be '1' on the server for any pivot to
    be started; otherwise live groups behave like practice.
  * Outside its windows a group is not checked at all (no FieldNET calls).
    Fallbacks the watcher started keep running when the window ends.

Storage: the `pumpgroups` schema in AgHub Ops DB (created by the dashboard's
`npm run create-pumpgroups-db`). The watcher reads groups and writes events;
the dashboard sends push notifications for alert events.

The decision logic (classify, trigger_for, pick_fallback, window_key, decide)
is pure so it can be tested without FieldNET or a database.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

CONFIRM_MINUTES = 10
WEEK = 7 * 1440

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass
class Member:
    pivot_id: str
    name: str
    role: str  # 'scheduled' | 'fallback'
    order: int | None = None
    direction: str | None = None  # 'forward' | 'reverse' (fallbacks)
    speed_pct: float | None = None
    depth_in: float | None = None


@dataclass
class Group:
    id: int
    name: str
    mode: str  # 'off' | 'practice' | 'live'
    windows: list[tuple[int, int, int, int]]  # (start_dow, start_min, end_dow, end_min); dow 0 = Sunday
    members: list[Member]

    def fallbacks(self) -> list[Member]:
        return sorted((m for m in self.members if m.role == "fallback"), key=lambda m: (m.order or 0, m.name))


@dataclass
class WindowState:
    key: str
    last: dict[str, str] = field(default_factory=dict)  # pivot id -> last classification
    watched: set[str] = field(default_factory=set)  # pivot ids whose stops trigger fallbacks
    used: set[str] = field(default_factory=set)  # fallback ids already started (or would-have-started)
    low_pressure_retries: int = 0
    pending: dict[str, float] = field(default_factory=dict)  # fallback id -> confirm deadline (epoch s)
    noted_stop: set[str] = field(default_factory=set)  # pivots we've logged a no-trigger stop for


@dataclass
class Event:
    kind: str
    message: str
    pivot: str | None = None
    fallback: str | None = None
    alert: bool = False


@dataclass
class Start:
    member: Member
    trigger: str  # pivot name that stopped
    reason: str  # 'fault' | 'low-pressure' | 'finished'


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------

FAULT = re.compile(r"shutdown|fault|alarm|error|safety|powered-off|power-loss")
FINISHED = re.compile(r"service-stop|barrier|end-of-plan|plan-complete")


def classify(p: dict[str, Any]) -> str:
    """One FieldNET pivot (raw /irrigation-controllers item) → what the watcher cares about.

    running-wet | running-dry | low-pressure | fault | finished | stopped | offline"""
    status = str(p.get("operational_status") or "").lower()
    online = str(p.get("communication_status") or "").lower()
    direction = str(p.get("direction") or "").lower()
    moving = direction in ("forward", "reverse") and status.startswith(("forward", "reverse"))
    if "low-pressure" in status and "shutdown" in status:
        return "low-pressure"
    if FAULT.search(status):
        return "fault"
    if status == "not-connected" or online == "offline":
        return "offline"
    if moving or (direction in ("forward", "reverse") and "warning" in status):
        wet = status.endswith("-wet") or p.get("is_water_on") is True
        return "running-wet" if wet else "running-dry"
    if FINISHED.search(status):
        return "finished"
    return "stopped"


def trigger_for(prev: str | None, cur: str) -> str | None:
    """Reason to start a fallback, or None. Only a pivot that was watering counts."""
    if prev != "running-wet":
        return None
    return cur if cur in ("fault", "low-pressure", "finished") else None


def can_start(p: dict[str, Any] | None) -> bool:
    """A fallback can be started if it's online and not already moving."""
    if not p:
        return False
    c = classify(p)
    status = str(p.get("operational_status") or "").lower()
    if c in ("offline", "running-wet", "running-dry"):
        return False
    if "powered-off" in status:
        return False
    return str(p.get("communication_status") or "").lower() == "online"


def pick_fallback(group: Group, state: WindowState, by_id: dict[str, dict[str, Any]]) -> Member | None:
    for m in group.fallbacks():
        if m.pivot_id in state.used:
            continue
        if can_start(by_id.get(m.pivot_id)):
            return m
    return None


def minute_of_week(now: datetime) -> int:
    dow = (now.weekday() + 1) % 7  # Python Monday=0 → Sunday=0
    return dow * 1440 + now.hour * 60 + now.minute


def window_key(group: Group, now: datetime) -> str | None:
    """Which occurrence of a schedule window `now` (farm time) is in, or None.

    A window ending at or before its start wraps (e.g. 20:00 → 06:00 next day,
    Fri 18:00 → Mon 06:00). The key names the occurrence by its start, so state
    resets each new night or weekend."""
    m = minute_of_week(now)
    for sd, sm, ed, em in group.windows:
        s = sd * 1440 + sm
        e = ed * 1440 + em
        if e <= s:
            e += WEEK
        for mm in (m, m + WEEK):
            if s <= mm < e:
                start = now - timedelta(minutes=mm - s)
                return f"{group.id}@{start.strftime('%Y-%m-%dT%H:%M')}"
    return None


def describe_settings(m: Member) -> str:
    speed = f"{m.speed_pct:g}% speed" if m.speed_pct is not None else f'{m.depth_in:g}" depth'
    return f"{m.direction}, water on, {speed}"


REASON_TEXT = {"fault": "shut down", "low-pressure": "shut down on low pressure", "finished": "finished its run"}


def decide(group: Group, state: WindowState, pivots: list[dict[str, Any]], now_s: float) -> tuple[list[Event], list[Start]]:
    """One check of one group inside an active window. Mutates `state`; returns events and starts."""
    events: list[Event] = []
    starts: list[Start] = []
    by_id = {str(p.get("id")): p for p in pivots}
    members = {m.pivot_id: m for m in group.members}
    state.watched |= {m.pivot_id for m in group.members if m.role == "scheduled"}

    # Confirm fallbacks started earlier.
    for pid, deadline in list(state.pending.items()):
        p = by_id.get(pid)
        name = members[pid].name if pid in members else pid
        if p and classify(p).startswith("running"):
            del state.pending[pid]
            events.append(Event("confirmed", f"{name} is running.", fallback=name))
        elif now_s > deadline:
            del state.pending[pid]
            events.append(Event("no-response", f"{name} was started but isn't moving after {CONFIRM_MINUTES} minutes. Check it.",
                                fallback=name, alert=True))

    for pid in sorted(state.watched, key=lambda i: members[i].name if i in members else i):
        p = by_id.get(pid)
        if not p or pid not in members:
            continue
        name = members[pid].name
        cur = classify(p)
        prev = state.last.get(pid)
        state.last[pid] = cur
        if prev is None or prev == cur:
            continue
        reason = trigger_for(prev, cur)
        if not reason:
            if prev == "running-wet" and cur in ("stopped", "offline") and pid not in state.noted_stop:
                state.noted_stop.add(pid)
                why = "lost contact" if cur == "offline" else "stopped without a fault or end of run (stopped by hand?)"
                events.append(Event("no-trigger", f"{name} {why}; no fallback started.", pivot=name))
            continue
        status = str(p.get("operational_status") or cur)
        if reason == "low-pressure":
            if state.low_pressure_retries >= 1:
                events.append(Event("gave-up", f"{name} shut down on low pressure again. Not starting another fallback; "
                                    f"check the water source for {group.name}.", pivot=name, alert=True))
                continue
            state.low_pressure_retries += 1
        fb = pick_fallback(group, state, by_id)
        if not fb:
            events.append(Event("no-fallback", f"{name} {REASON_TEXT[reason]} ({status}), but no fallback pivot in "
                                f"{group.name} is ready (all used, running, or offline).", pivot=name, alert=True))
            continue
        state.used.add(fb.pivot_id)
        starts.append(Start(fb, name, reason))
    return events, starts


def _r(v: float | None, digits: int) -> float | None:
    """The database stores speed/depth as REAL; round away float noise (0.6000000238 → 0.6)."""
    return None if v is None else round(float(v), digits)


# ---------------------------------------------------------------------------
# Runner (database + FieldNET)
# ---------------------------------------------------------------------------

StartFn = Callable[[Member], Awaitable[dict[str, Any]]]


class Watcher:
    def __init__(self, db_url: str, get_pivots: Callable[[], Awaitable[list[dict[str, Any]]]], start_pivot: StartFn,
                 tz: ZoneInfo, interval_s: int = 180, allow_live: bool = False,
                 notify_url: str = "", notify_secret: str = "") -> None:
        self.db_url = db_url
        self.get_pivots = get_pivots
        self.start_pivot = start_pivot
        self.tz = tz
        self.interval_s = interval_s
        self.allow_live = allow_live
        self.notify_url = notify_url
        self.notify_secret = notify_secret
        self.states: dict[int, WindowState] = {}
        self._pool = None

    async def pool(self):
        if self._pool is None:
            import asyncpg  # imported here so the read-only server doesn't need it

            self._pool = await asyncpg.create_pool(self.db_url, min_size=1, max_size=2, command_timeout=20)
        return self._pool

    async def load_groups(self) -> list[Group]:
        db = await self.pool()
        groups = await db.fetch("SELECT id, name, mode FROM pumpgroups.pump_group WHERE mode <> 'off' ORDER BY id")
        if not groups:
            return []
        ids = [g["id"] for g in groups]
        wins = await db.fetch("SELECT group_id, start_dow, start_min, end_dow, end_min FROM pumpgroups.pump_group_window "
                              "WHERE group_id = ANY($1::int[])", ids)
        mems = await db.fetch("SELECT group_id, pivot_id, pivot_name, role, fallback_order, direction, speed_pct, depth_in "
                              "FROM pumpgroups.pump_group_pivot WHERE group_id = ANY($1::int[])", ids)
        out = []
        for g in groups:
            out.append(Group(
                id=g["id"], name=g["name"], mode=g["mode"],
                windows=[(w["start_dow"], w["start_min"], w["end_dow"], w["end_min"]) for w in wins if w["group_id"] == g["id"]],
                members=[Member(m["pivot_id"], m["pivot_name"], m["role"], m["fallback_order"], m["direction"],
                                _r(m["speed_pct"], 1), _r(m["depth_in"], 2)) for m in mems if m["group_id"] == g["id"]],
            ))
        return out

    async def used_in_window(self, group: Group, key: str) -> set[str]:
        """Fallbacks already started this window (survives a restart)."""
        start = datetime.strptime(key.split("@", 1)[1], "%Y-%m-%dT%H:%M").replace(tzinfo=self.tz)
        db = await self.pool()
        rows = await db.fetch("SELECT DISTINCT fallback FROM pumpgroups.event WHERE group_id = $1 AND at >= $2 "
                              "AND kind IN ('started', 'would-start', 'start-failed') AND fallback IS NOT NULL",
                              group.id, start)
        names = {r["fallback"] for r in rows}
        return {m.pivot_id for m in group.members if m.name in names}

    async def log(self, group: Group | None, ev: Event, practice: bool) -> None:
        db = await self.pool()
        await db.execute("INSERT INTO pumpgroups.event (group_id, group_name, kind, practice, pivot, fallback, message, alert) "
                         "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                         group.id if group else None, group.name if group else None, ev.kind, practice,
                         ev.pivot, ev.fallback, ev.message, ev.alert)
        print(f"PUMPGROUP {datetime.now(self.tz).isoformat(timespec='seconds')} "
              f"{'[practice] ' if practice else ''}{group.name if group else '-'}: {ev.kind}: {ev.message}", flush=True)

    async def heartbeat(self, error: str | None, active: list[str]) -> None:
        db = await self.pool()
        await db.execute("INSERT INTO pumpgroups.watcher (id, last_check_at, last_error, active_groups, allow_live) "
                         "VALUES (1, now(), $1, $2, $3) ON CONFLICT (id) DO UPDATE SET last_check_at = now(), "
                         "last_error = $1, active_groups = $2, allow_live = $3", error, active, self.allow_live)

    async def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(self.tz)
        groups = await self.load_groups()
        active: list[tuple[Group, WindowState]] = []
        for g in groups:
            key = window_key(g, now)
            if not key:
                self.states.pop(g.id, None)
                continue
            st = self.states.get(g.id)
            if not st or st.key != key:
                st = WindowState(key=key, used=await self.used_in_window(g, key))
                self.states[g.id] = st
            active.append((g, st))
        for gid in list(self.states):
            if gid not in {g.id for g in groups}:
                del self.states[gid]
        if not active:
            await self.heartbeat(None, [])
            return

        pivots = await self.get_pivots()
        alerts = False
        for g, st in active:
            practice = g.mode != "live" or not self.allow_live
            events, starts = decide(g, st, pivots, time.time())
            for ev in events:
                await self.log(g, ev, practice)
                alerts |= ev.alert
            for s in starts:
                alerts = True
                what = f"{s.trigger} {REASON_TEXT[s.reason]}"
                retry = " (low-pressure retry: one only)" if s.reason == "low-pressure" else ""
                if practice:
                    note = "" if g.mode != "live" else " Live control is switched off on the server (WATCHER_ALLOW_LIVE)."
                    await self.log(g, Event("would-start", f"{what}. Would start {s.member.name} ({describe_settings(s.member)})"
                                            f"{retry}.{note}", pivot=s.trigger, fallback=s.member.name, alert=True), True)
                    continue
                try:
                    res = await self.start_pivot(s.member)
                except Exception as e:  # noqa: BLE001
                    res = {"applied": False, "error": str(e)}
                if res.get("applied") or res.get("panel_ack") == "waiting-for-online-status":
                    st.pending[s.member.pivot_id] = time.time() + CONFIRM_MINUTES * 60
                    await self.log(g, Event("started", f"{what}. Started {s.member.name} ({describe_settings(s.member)}){retry}.",
                                            pivot=s.trigger, fallback=s.member.name, alert=True), False)
                    st.watched.add(s.member.pivot_id)
                else:
                    err = res.get("error") or res.get("note") or res.get("panel_ack") or "no acknowledgement"
                    await self.log(g, Event("start-failed", f"{what}. Tried to start {s.member.name} but it failed: {err}.",
                                            pivot=s.trigger, fallback=s.member.name, alert=True), False)
        await self.heartbeat(None, [g.name for g, _ in active])
        if alerts:
            await self.notify()

    async def notify(self) -> None:
        """Nudge the dashboard to push unsent alerts. Best effort: it may be asleep (it wakes on this request)."""
        if not self.notify_url:
            return
        import httpx

        try:
            async with httpx.AsyncClient(timeout=90) as c:
                await c.post(self.notify_url, headers={"X-Pumpgroups-Secret": self.notify_secret})
        except Exception as e:  # noqa: BLE001
            print(f"PUMPGROUP notify failed: {e}", flush=True)

    async def run_forever(self) -> None:
        print(f"Pump group watcher running every {self.interval_s}s; live control "
              f"{'ALLOWED' if self.allow_live else 'OFF (practice only)'}.", flush=True)
        unsent_check = 0
        while True:
            try:
                await self.tick()
                # Retry pushes the dashboard didn't take (e.g. it was deploying).
                unsent_check += 1
                if unsent_check % 5 == 0:
                    db = await self.pool()
                    if await db.fetchval("SELECT count(*) FROM pumpgroups.event WHERE alert AND notified_at IS NULL "
                                         "AND at > now() - interval '1 day'"):
                        await self.notify()
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                try:
                    await self.heartbeat(f"{type(e).__name__}: {e}"[:500], [])
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(self.interval_s)


def from_env(get_pivots, start_pivot, tz: ZoneInfo) -> Watcher | None:
    """Build the watcher from environment variables, or None if it isn't switched on."""
    if os.environ.get("WATCHER_ENABLED") != "1":
        return None
    db_url = os.environ.get("PUMPGROUPS_DB_URL", "")
    if not db_url:
        print("WATCHER_ENABLED=1 but PUMPGROUPS_DB_URL is not set; watcher not started.", flush=True)
        return None
    return Watcher(
        db_url=db_url,
        get_pivots=get_pivots,
        start_pivot=start_pivot,
        tz=tz,
        interval_s=max(60, int(os.environ.get("WATCHER_INTERVAL_S", "180"))),
        allow_live=os.environ.get("WATCHER_ALLOW_LIVE") == "1",
        notify_url=os.environ.get("PUMPGROUPS_NOTIFY_URL", ""),
        notify_secret=os.environ.get("PUMPGROUPS_NOTIFY_SECRET", ""),
    )


__all__ = ["Watcher", "from_env", "classify", "trigger_for", "decide", "window_key", "Group", "Member", "WindowState"]
