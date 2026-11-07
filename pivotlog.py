"""
Pivot log
=========

A record of every change to every pivot and who made it, for the Hyer Farms
dashboard's Pivots pages. Two halves:

  * Commands. Every command this server sends goes through server._apply,
    which writes it here as it's sent: who sent it (a person in the dashboard,
    a program, a pump group, or Claude through the FieldNET Control
    connector), what it changed, and what the panel answered.

  * Changes. Every CHECK_S the change watcher reads all pivots in one FieldNET
    call and compares each with what it saw last (kept in the database, so a
    restart or deploy doesn't lose it). What it compares: running or stopped,
    direction, wet or dry, speed (and depth), service stop, plan, status and
    contact. Not the angle (it moves) and not the end gun, whose list value is
    what it's doing at that angle, not a setting.

    A change that matches a recent command (same field, same new value, sent
    within COMMAND_WINDOW_S) marks that command "seen on the panel". Any other
    change is logged as either
      - 'outside': made outside the portal (at the panel or in the FieldNET
        app; a start or direction change can also be the panel's own auto
        restart or auto reverse), or
      - 'pivot': the pivot's own report: a service stop or barrier reached, a
        fault or shutdown, lost or regained contact, a speed or water change
        while a saved plan is in charge, or a one-time service stop that
        FieldNET cleared on reaching it.

Storage: pumpgroups.pivot_log and pumpgroups.pivot_seen in AgHub Ops DB
(created by the dashboard's `npm run create-pumpgroups-db`). Read only towards
FieldNET: nothing here sends anything to a pivot.

The comparison (snap, compare) and the wording are pure, so they're tested
without FieldNET or a database (test_pivotlog.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol

CHECK_S = 150
COMMAND_WINDOW_S = 20 * 60
SPEED_TOL = 0.6  # percent: FieldNET reports the rate rounded
STOP_TOL = 0.15  # degrees
AT_STOP_DEG = 1.5  # a cleared service stop this close to the pivot was FieldNET clearing a one-time stop
RETRY_MISSING_S = 600  # tables not created yet: look again this often

MOVING = ("forward", "reverse")
FIELDS = ("direction", "water_on", "speed_pct", "service_stop_deg", "plan")
SOURCES = ("portal", "program", "pump-group", "claude", "outside", "pivot")
OUTCOMES = {"successful": "taken", "failure": "refused", "waiting-for-online-status": "queued"}


# ---------------------------------------------------------------------------
# Snapshots and comparison (pure)
# ---------------------------------------------------------------------------


def _num(v: Any, digits: int) -> float | None:
    try:
        return None if v is None else round(float(v), digits)
    except (TypeError, ValueError):
        return None


def norm_plan(v: Any) -> str:
    s = str(v or "").strip().lower()
    return "manual" if (not s or s.startswith("manual")) else s


def snap(p: dict[str, Any]) -> dict[str, Any]:
    """What the change watcher keeps of a pivot, from a list_pivots summary (server.summarize_pivot)."""
    status = str(p.get("status") or "unknown").lower()
    moving = status.startswith(MOVING)
    return {
        "name": p.get("name") or p.get("id"),
        "status": status,
        "direction": status.split("-")[0] if moving else "stopped",
        "water_on": bool(p.get("water_on")),
        "speed_pct": _num(p.get("speed_pct"), 1),
        "depth_in": _num(p.get("depth_in"), 2),
        "service_stop_deg": _num(p.get("service_stop_deg"), 1),
        "plan": p.get("plan") or None,
        "offline": str(p.get("online") or "").lower() == "offline",
        "angle_deg": _num(p.get("angle_deg"), 1),
        "status_since": p.get("status_since"),
    }


def _gap(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return min(d, 360 - d)


def same(f: str, a: Any, b: Any) -> bool:
    if f in ("speed_pct", "service_stop_deg"):
        if a is None or b is None:
            return a is None and b is None
        return (abs(a - b) <= SPEED_TOL) if f == "speed_pct" else (_gap(a, b) <= STOP_TOL)
    if f == "plan":
        return norm_plan(a) == norm_plan(b)
    return a == b


def worth_saving(prev: dict[str, Any] | None, cur: dict[str, Any]) -> bool:
    """Whether the stored snapshot needs updating (anything compared changed; not just the angle)."""
    if prev is None:
        return True
    return any(not same(f, prev.get(f), cur.get(f)) for f in FIELDS) or prev.get("status") != cur.get("status") \
        or prev.get("offline") != cur.get("offline")


@dataclass
class Command:
    id: int
    at: float  # epoch s
    source: str
    who: str | None
    expected: dict[str, Any]  # snapshot field -> the value it should show once the panel has it
    seen_fields: set[str] = field(default_factory=set)


@dataclass
class Entry:
    source: str  # 'outside' | 'pivot'
    message: str
    changes: dict[str, dict[str, Any]]
    at: float  # epoch s


def _label(status: str) -> str:
    s = (status or "unknown").replace("-", " ")
    return s[:1].upper() + s[1:]


def _pct(v: float | None) -> str:
    return "–" if v is None else f"{v:g}%"


def _deg(v: float | None) -> str:
    return "off" if v is None else f"{v:.1f}°"


def _rate(s: dict[str, Any]) -> str:
    if s.get("water_on") and s.get("depth_in") is not None:
        return f"{s['depth_in']:.2f}″ ({_pct(s.get('speed_pct'))})"
    return _pct(s.get("speed_pct"))


def change_text(prev: dict[str, Any], cur: dict[str, Any], fields: list[str]) -> str:
    """'Started forward, wet · 0.45″ (35%)', 'Service stop at 152°', 'Speed 35% → 50% (0.45″ → 0.32″)'…"""
    fs = set(fields)
    parts: list[str] = []
    if "direction" in fs:
        if cur["direction"] in MOVING and prev.get("direction") not in MOVING:
            parts.append(f"Started {cur['direction']}, {'wet' if cur.get('water_on') else 'dry'} · {_rate(cur)}")
            fs -= {"water_on", "speed_pct"}
        elif cur["direction"] in MOVING:
            parts.append(f"Turned round: now {cur['direction']}")
        else:
            at = f" at {cur['angle_deg']:.0f}°" if cur.get("angle_deg") is not None else ""
            parts.append(f"{_label(cur.get('status', 'stopped'))}{at}")
    elif "status" in fs:
        parts.append(f"{_label(prev.get('status', ''))} → {_label(cur.get('status', ''))}")
    if "water_on" in fs:
        parts.append("Wet → dry" if prev.get("water_on") else "Dry → wet")
    if "speed_pct" in fs:
        depth = ""
        if prev.get("depth_in") is not None and cur.get("depth_in") is not None:
            depth = f" ({prev['depth_in']:.2f}″ → {cur['depth_in']:.2f}″)"
        parts.append(f"Speed {_pct(prev.get('speed_pct'))} → {_pct(cur.get('speed_pct'))}{depth}")
    if "service_stop_deg" in fs:
        parts.append(f"Service stop {_deg(prev.get('service_stop_deg'))} → {_deg(cur.get('service_stop_deg'))}")
    if "plan" in fs:
        parts.append(f"Plan {prev.get('plan') or 'none'} → {cur.get('plan') or 'none'}")
    if "offline" in fs:
        parts.append("Lost contact" if cur.get("offline") else "Back in contact")
    return " · ".join(parts)


def _since(status_since: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(status_since).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def compare(prev: dict[str, Any], cur: dict[str, Any], commands: list[Command], now_s: float,
            last_check_s: float | None = None) -> tuple[list[Entry], dict[int, list[str]]]:
    """What changed on one pivot since the last look, and who it's down to.

    Returns (entries to log, {command id: fields now seen on the panel}). Changes a command explains
    only mark that command seen; the rest become an 'outside' entry and/or a 'pivot' entry."""
    changed = [f for f in FIELDS if not same(f, prev.get(f), cur.get(f))]
    matches: dict[int, list[str]] = {}
    left: list[str] = []
    recent = sorted((c for c in commands if now_s - c.at <= COMMAND_WINDOW_S), key=lambda c: -c.at)
    for f in changed:
        c = next((c for c in recent if f in c.expected and f not in c.seen_fields and same(f, c.expected[f], cur.get(f))),
                 None)
        if c:
            matches.setdefault(c.id, []).append(f)
        else:
            left.append(f)

    manual = norm_plan(cur.get("plan")) == "manual"
    own: list[str] = []  # the pivot's own doing
    outside: list[str] = []
    for f in left:
        if f == "direction" and cur["direction"] == "stopped" and cur.get("status") != "stopped":
            own.append(f)  # reached a service stop or barrier, or faulted
        elif (f == "service_stop_deg" and cur.get(f) is None and prev.get(f) is not None
              and cur.get("angle_deg") is not None and _gap(cur["angle_deg"], prev[f]) <= AT_STOP_DEG):
            own.append(f)  # FieldNET cleared a one-time stop on reaching it
        elif f in ("speed_pct", "water_on") and not manual:
            own.append(f)  # a saved plan changes these by itself
        else:
            outside.append(f)

    # A status change goes with whatever explains it; on its own it's the pivot's report.
    moving_both = prev.get("direction") in MOVING and cur.get("direction") in MOVING
    if prev.get("status") != cur.get("status") and not moving_both:
        matched = {f for fs in matches.values() for f in fs}
        if "direction" in matched or ("direction" not in left and matches):
            pass  # the command's own row covers it
        elif "direction" in outside or ("direction" not in own and outside):
            outside.append("status")
        else:
            own.append("status")
    if prev.get("offline") != cur.get("offline"):
        own.append("offline")

    # When it happened: FieldNET's status time if it's since the last look, otherwise now.
    at = now_s
    since = _since(cur.get("status_since"))
    if since is not None and since <= now_s and (last_check_s is None or since > last_check_s - 60):
        at = since if ("status" in own + outside or "direction" in own + outside) else now_s

    entries = []
    for source, fs in (("outside", outside), ("pivot", own)):
        if fs:
            changes = {f: {"from": prev.get(f), "to": cur.get(f)} for f in fs}
            entries.append(Entry(source, change_text(prev, cur, fs), changes, at))
    return entries, matches


# ---------------------------------------------------------------------------
# Commands (pure wording; written by server._apply)
# ---------------------------------------------------------------------------


def outcome_for(ack: str | None) -> str:
    return OUTCOMES.get(ack or "", "no-answer")


def command_text(diff: dict[str, dict[str, Any]], after: dict[str, Any], plan_name: str | None = None) -> str:
    """What a command asked for, e.g. 'Start forward, wet · 0.45″ (35%), service stop 150.6°'."""
    parts: list[str] = []
    d = after.get("direction_key")
    moving = d in MOVING
    head = ""
    if "direction_key" in diff:
        was = diff["direction_key"].get("from")
        head = "Stop" if not moving else (f"Turn round: {d}" if was in MOVING else f"Start {d}")
    rate_changed = "speed_pct" in diff or "depth_in" in diff
    if moving and ("direction_key" in diff and diff["direction_key"].get("from") not in MOVING or "water_on" in diff
                   or rate_changed):
        s = {"water_on": after.get("water_on"), "depth_in": after.get("depth_in"), "speed_pct": after.get("speed_pct")}
        parts.append(f"{'wet' if after.get('water_on') else 'dry'} · {_rate(s)}")
    elif "water_on" in diff:
        parts.append("wet" if after.get("water_on") else "dry")
    elif rate_changed:
        parts.append(f"speed {_pct(after.get('speed_pct'))}"
                     + (f" ({after['depth_in']:.2f}″)" if after.get("depth_in") is not None else ""))
    if "service_stop_deg" in diff:
        parts.append(f"service stop {_deg(after.get('service_stop_deg'))}")
    if "service_stop_repeat" in diff:
        parts.append("stop every pass" if after.get("service_stop_repeat") else "stop once")
    if "plan_id" in diff:
        parts.append(f"plan {plan_name or ('manual' if norm_plan(after.get('plan_id')) == 'manual' else after.get('plan_id'))}")
    for key, name in (("auto_reverse", "auto reverse"), ("auto_restart", "auto restart"),
                      ("accessory_1", "chemigation (aux 1)")):
        if key in diff:
            parts.append(f"{name} {'on' if after.get(key) else 'off'}")
    text = ", ".join(([head] if head else []) + parts)
    return (text[:1].upper() + text[1:]) if text else "No change"


def expected_from(diff: dict[str, dict[str, Any]], after: dict[str, Any], plan_name: str | None = None) -> dict[str, Any]:
    """The snapshot values a command should lead to, for the fields it changed."""
    e: dict[str, Any] = {}
    if "direction_key" in diff:
        e["direction"] = after.get("direction_key") if after.get("direction_key") in MOVING else "stopped"
    if "water_on" in diff:
        e["water_on"] = bool(after.get("water_on"))
    if "speed_pct" in diff or "depth_in" in diff:
        e["speed_pct"] = _num(after.get("speed_pct"), 1)
    if "service_stop_deg" in diff:
        e["service_stop_deg"] = _num(after.get("service_stop_deg"), 1)
    if "plan_id" in diff:
        e["plan"] = plan_name or ("Manual" if norm_plan(after.get("plan_id")) == "manual" else after.get("plan_id"))
    return e


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class Store(Protocol):
    async def seen(self) -> dict[str, dict[str, Any]]: ...
    async def save_seen(self, snaps: dict[str, dict[str, Any]]) -> None: ...
    async def open_commands(self, since_s: float) -> dict[str, list[Command]]: ...
    async def add_change(self, pivot_id: str, pivot_name: str, e: Entry, angle: float | None) -> None: ...
    async def mark_seen(self, command_id: int, fields: list[str]) -> None: ...
    async def add_command(self, pivot_id: str, pivot_name: str, source: str, who: str | None, message: str,
                          changes: dict[str, Any], expected: dict[str, Any], outcome: str, angle: float | None) -> int: ...
    async def program_name(self, pivot_id: str) -> str | None: ...
    async def heartbeat(self, error: str | None) -> None: ...


class PgStore:
    def __init__(self, db_url: str) -> None:
        self.db_url = db_url
        self._pool = None

    async def pool(self):
        if self._pool is None:
            import asyncpg  # imported here so the read-only server doesn't need it

            self._pool = await asyncpg.create_pool(self.db_url, min_size=1, max_size=2, command_timeout=20)
        return self._pool

    async def seen(self) -> dict[str, dict[str, Any]]:
        db = await self.pool()
        rows = await db.fetch("SELECT pivot_id, snap FROM pumpgroups.pivot_seen")
        return {r["pivot_id"]: json.loads(r["snap"]) if isinstance(r["snap"], str) else dict(r["snap"]) for r in rows}

    async def save_seen(self, snaps: dict[str, dict[str, Any]]) -> None:
        if not snaps:
            return
        db = await self.pool()
        await db.executemany("INSERT INTO pumpgroups.pivot_seen (pivot_id, snap, seen_at) VALUES ($1, $2::jsonb, now()) "
                             "ON CONFLICT (pivot_id) DO UPDATE SET snap = EXCLUDED.snap, seen_at = now()",
                             [(k, json.dumps(v)) for k, v in snaps.items()])

    async def open_commands(self, since_s: float) -> dict[str, list[Command]]:
        db = await self.pool()
        rows = await db.fetch("SELECT id, extract(epoch FROM at) AS at, pivot_id, source, who, expected, seen_fields "
                              "FROM pumpgroups.pivot_log WHERE kind = 'command' AND at >= to_timestamp($1) "
                              "AND expected IS NOT NULL AND outcome IN ('taken', 'queued', 'no-answer')", since_s)
        out: dict[str, list[Command]] = {}
        for r in rows:
            exp = r["expected"]
            exp = json.loads(exp) if isinstance(exp, str) else dict(exp or {})
            out.setdefault(r["pivot_id"], []).append(Command(r["id"], float(r["at"]), r["source"], r["who"], exp,
                                                             set(r["seen_fields"] or [])))
        return out

    async def add_change(self, pivot_id: str, pivot_name: str, e: Entry, angle: float | None) -> None:
        db = await self.pool()
        await db.execute("INSERT INTO pumpgroups.pivot_log (at, pivot_id, pivot_name, kind, source, message, changes, angle_deg) "
                         "VALUES (to_timestamp($1), $2, $3, 'change', $4, $5, $6::jsonb, $7)",
                         e.at, pivot_id, pivot_name, e.source, e.message, json.dumps(e.changes, default=str), angle)

    async def mark_seen(self, command_id: int, fields: list[str]) -> None:
        db = await self.pool()
        await db.execute("UPDATE pumpgroups.pivot_log SET seen_at = coalesce(seen_at, now()), "
                         "seen_fields = (SELECT array_agg(DISTINCT f) FROM unnest(seen_fields || $2::text[]) f) "
                         "WHERE id = $1", command_id, fields)

    async def add_command(self, pivot_id: str, pivot_name: str, source: str, who: str | None, message: str,
                          changes: dict[str, Any], expected: dict[str, Any], outcome: str, angle: float | None) -> int:
        db = await self.pool()
        return await db.fetchval(
            "INSERT INTO pumpgroups.pivot_log (pivot_id, pivot_name, kind, source, who, message, changes, expected, outcome, angle_deg) "
            "VALUES ($1, $2, 'command', $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9) RETURNING id",
            pivot_id, pivot_name, source, who, message, json.dumps(changes, default=str),
            json.dumps(expected, default=str), outcome, angle)

    async def program_name(self, pivot_id: str) -> str | None:
        db = await self.pool()
        try:
            return await db.fetchval("SELECT p.name FROM pumpgroups.program_run r JOIN pumpgroups.program p ON p.id = r.program_id "
                                     "WHERE r.pivot_id = $1 AND r.ended_at IS NULL ORDER BY r.id DESC LIMIT 1", pivot_id)
        except Exception as e:  # noqa: BLE001 - programs tables not created yet
            if "does not exist" not in str(e):
                raise
            return None

    async def heartbeat(self, error: str | None) -> None:
        db = await self.pool()
        await db.execute("INSERT INTO pumpgroups.watcher (id, log_check_at, log_error) VALUES (1, now(), $1) "
                         "ON CONFLICT (id) DO UPDATE SET log_check_at = now(), log_error = $1", error)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

PivotsFn = Callable[[], Awaitable[list[dict[str, Any]]]]


class PivotLog:
    def __init__(self, store: Store, pivots: PivotsFn, interval_s: int = CHECK_S,
                 clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.pivots = pivots
        self.interval_s = interval_s
        self.clock = clock
        self.last_check_s: float | None = None

    async def tick(self) -> int:
        """One look at every pivot. Returns how many log rows it wrote."""
        now = self.clock()
        pivots = await self.pivots()
        before = await self.store.seen()
        commands = await self.store.open_commands(now - COMMAND_WINDOW_S)
        save: dict[str, dict[str, Any]] = {}
        wrote = 0
        for p in pivots:
            pid = p.get("id")
            if not pid:
                continue
            cur = snap(p)
            prev = before.get(pid)
            if worth_saving(prev, cur):
                save[pid] = cur
            if prev is None:
                continue  # first look: nothing to compare with
            entries, matches = compare(prev, cur, commands.get(pid, []), now, self.last_check_s)
            for cid, fields in matches.items():
                await self.store.mark_seen(cid, fields)
                for c in commands.get(pid, []):
                    if c.id == cid:
                        c.seen_fields.update(fields)
            for e in entries:
                await self.store.add_change(pid, cur["name"], e, cur.get("angle_deg"))
                print(f"PIVOTLOG {cur['name']} [{e.source}] {e.message}", flush=True)
                wrote += 1
        await self.store.save_seen(save)
        await self.store.heartbeat(None)
        self.last_check_s = now
        return wrote

    async def record_command(self, *, pivot_id: str, pivot_name: str, source: str, who: str | None,
                             diff: dict[str, dict[str, Any]], after: dict[str, Any], ack: str | None,
                             plan_name: str | None = None, angle: float | None = None) -> int | None:
        """Write a command just sent (server._apply). Never raises: the log must not get in the way of control."""
        try:
            if source == "program" and not who:
                who = await self.store.program_name(pivot_id)
            return await self.store.add_command(pivot_id, pivot_name, source, who, command_text(diff, after, plan_name),
                                                diff, expected_from(diff, after, plan_name), outcome_for(ack), angle)
        except Exception as e:  # noqa: BLE001
            print(f"PIVOTLOG couldn't record a command for {pivot_name}: {type(e).__name__}: {e}", flush=True)
            return None

    async def run_forever(self) -> None:
        print(f"Pivot log watching every pivot every {self.interval_s}s.", flush=True)
        missing_said = False
        while True:
            wait = self.interval_s
            try:
                await self.tick()
                missing_said = False
            except Exception as e:  # noqa: BLE001
                if "does not exist" in str(e):
                    if not missing_said:
                        print("PIVOTLOG: the pivot log tables aren't there yet; re-run the dashboard's "
                              "`npm run create-pumpgroups-db`. Looking again every 10 minutes.", flush=True)
                        missing_said = True
                    wait = RETRY_MISSING_S
                else:
                    traceback.print_exc()
                    try:
                        await self.store.heartbeat(f"{type(e).__name__}: {e}"[:500])
                    except Exception:  # noqa: BLE001
                        pass
            await asyncio.sleep(wait)


def _off(v: str | None) -> bool:
    return (v or "").strip().lower() in ("0", "false", "no", "off")


def from_env(db_url: str, pivots: PivotsFn) -> PivotLog | None:
    """On whenever the dashboard's database is set (PUMPGROUPS_DB_URL); PIVOT_LOG_ENABLED=0 switches it off."""
    if not db_url or _off(os.environ.get("PIVOT_LOG_ENABLED")):
        return None
    interval = max(60, int(os.environ.get("PIVOT_LOG_INTERVAL_S", str(CHECK_S))))
    return PivotLog(PgStore(db_url), pivots, interval)


__all__ = ["PivotLog", "PgStore", "Command", "Entry", "snap", "compare", "command_text", "expected_from",
           "outcome_for", "from_env", "SOURCES"]
