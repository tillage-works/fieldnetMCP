"""Tests for the pivot log's comparison, wording and change watcher. Run: python test_pivotlog.py

No network or database: a memory store stands in for AgHub Ops DB. Pivot summaries look like
server.summarize_pivot (the same shape list_pivots returns), with real values from the farm."""

from __future__ import annotations

import asyncio
from typing import Any

from pivotlog import (ALERT_COOLDOWN_S, COMMAND_WINDOW_S, NOT_TAKEN_S, Command, PivotLog, command_text, compare,
                      expected_from, mark_alerts, outcome_for, snap, still_not_taken, worth_saving)

NOW = 1_762_538_400.0  # 2025-11-07 18:00 UTC


def piv(status: str = "stopped", *, pid: str = "w26", name: str = "W-26", water: bool = False, speed: float = 40.5,
        depth: float | None = 0.35, stop: float | None = None, plan: str = "Manual", online: str = "online",
        angle: float = 152.2, since: str | None = None) -> dict[str, Any]:
    return {"id": pid, "name": name, "status": status, "online": online,
            "direction": status.split("-")[0] if status.startswith(("forward", "reverse")) else "stopped",
            "angle_deg": angle, "speed_pct": speed, "depth_in": depth, "water_on": water, "endgun_1_on": False,
            "service_stop_deg": stop, "plan": plan, "status_since": since}


def cmp(prev: dict, cur: dict, commands: list[Command] | None = None, last: float | None = None):
    return compare(snap(prev), snap(cur), commands or [], NOW, last)


def cmd(cid: int, expected: dict[str, Any], ago_s: float = 60, source: str = "portal", who: str = "Sam") -> Command:
    return Command(cid, NOW - ago_s, source, who, expected)


# ── Snapshots ─────────────────────────────────────────────────────────


def test_snap_reads_a_list_pivots_summary():
    s = snap(piv("forward", water=True, speed=35, depth=0.45, stop=150.6))
    assert s["direction"] == "forward" and s["water_on"] is True and s["speed_pct"] == 35 and s["service_stop_deg"] == 150.6
    assert snap(piv("alignment-fault"))["direction"] == "stopped"
    assert snap(piv("reverse-wet"))["direction"] == "reverse"
    assert snap(piv("stopped", online="offline"))["offline"] is True
    assert snap(piv("stopped", online="unreliable"))["offline"] is False  # flaky links aren't logged


def test_angle_alone_is_not_a_change():
    a, b = snap(piv("forward", angle=100)), snap(piv("forward", angle=104))
    assert not worth_saving(a, b)
    assert cmp(piv("forward", angle=100), piv("forward", angle=104)) == ([], {})
    assert cmp(piv(speed=35.0), piv(speed=35.4)) == ([], {})  # rounding noise


# ── Outside the portal ────────────────────────────────────────────────


def test_start_outside_the_portal():
    entries, matches = cmp(piv(water=True, speed=35, depth=0.45), piv("forward", water=True, speed=35, depth=0.45))
    assert matches == {}
    assert len(entries) == 1 and entries[0].source == "outside"
    assert entries[0].message == "Started forward, wet · 0.45″ (35%)"
    assert entries[0].changes["direction"] == {"from": "stopped", "to": "forward"}


def test_stopped_by_hand_is_outside_but_a_service_stop_is_the_pivot():
    e, _ = cmp(piv("forward"), piv("stopped", angle=140.2))
    assert [(x.source, x.message) for x in e] == [("outside", "Stopped at 140°")]
    e, _ = cmp(piv("forward-wet", water=True, stop=152.2), piv("service-stop", water=True, stop=152.2, angle=152.2))
    assert [(x.source, x.message) for x in e] == [("pivot", "Service stop at 152°")]
    e, _ = cmp(piv("forward-wet", water=True), piv("low-pressure-shutdown", water=True, angle=88))
    assert [(x.source, x.message) for x in e] == [("pivot", "Low pressure shutdown at 88°")]


def test_settings_changed_outside_while_running():
    e, _ = cmp(piv("forward", water=True, speed=35, depth=0.45), piv("forward", water=True, speed=50, depth=0.32))
    assert [(x.source, x.message) for x in e] == [("outside", "Speed 35% → 50% (0.45″ → 0.32″)")]
    e, _ = cmp(piv("forward", water=True), piv("forward", water=False))
    assert e[0].message == "Wet → dry"
    e, _ = cmp(piv("forward", stop=None), piv("forward", stop=270))
    assert e[0].message == "Service stop off → 270.0°"
    e, _ = cmp(piv("forward"), piv("reverse"))
    assert e[0].message == "Turned round: now reverse" and e[0].source == "outside"


def test_cleared_service_stop_at_the_stop_is_fieldnet_not_a_person():
    e, _ = cmp(piv("service-stop", stop=180, angle=180), piv("service-stop", stop=None, angle=180))
    assert [x.source for x in e] == ["pivot"]
    e, _ = cmp(piv("stopped", stop=180, angle=90), piv("stopped", stop=None, angle=90))
    assert [x.source for x in e] == ["outside"]


def test_a_saved_plan_changing_speed_is_the_pivot():
    e, _ = cmp(piv("forward", plan="Corners", speed=40), piv("forward", plan="Corners", speed=60))
    assert [x.source for x in e] == ["pivot"]


def test_status_change_while_stopped_and_contact():
    e, _ = cmp(piv("service-stop"), piv("stopped"))
    assert [(x.source, x.message) for x in e] == [("pivot", "Service stop → Stopped")]
    e, _ = cmp(piv("service-stop"), piv("stopped", speed=60, depth=0.24))
    assert len(e) == 1 and e[0].source == "outside" and set(e[0].changes) == {"speed_pct", "status"}
    e, _ = cmp(piv("forward"), piv("forward", online="offline"))
    assert [(x.source, x.message) for x in e] == [("pivot", "Lost contact")]
    e, _ = cmp(piv("stopped", online="offline"), piv("stopped"))
    assert e[0].message == "Back in contact"


def test_time_comes_from_fieldnet_status_when_its_since_the_last_look():
    since = "2025-11-07T17:58:00+00:00"
    e, _ = cmp(piv("forward"), piv("service-stop", since=since), last=NOW - 150)
    assert e[0].at == NOW - 120
    e, _ = cmp(piv("forward"), piv("service-stop", since="2025-11-01T00:00:00+00:00"), last=NOW - 150)
    assert e[0].at == NOW  # stale status time: use the time we saw it


# ── Commands ──────────────────────────────────────────────────────────


def test_a_matching_command_explains_the_change():
    c = cmd(7, {"direction": "forward", "water_on": True, "speed_pct": 35.0})
    e, m = cmp(piv(speed=40.5), piv("forward", water=True, speed=35.0), [c])
    assert e == [] and sorted(m[7]) == ["direction", "speed_pct", "water_on"]


def test_part_explained_part_outside():
    c = cmd(7, {"speed_pct": 50.0})
    e, m = cmp(piv("forward", speed=35, stop=None), piv("forward", speed=50, stop=200), [c])
    assert m == {7: ["speed_pct"]}
    assert [(x.source, list(x.changes)) for x in e] == [("outside", ["service_stop_deg"])]


def test_old_commands_and_seen_fields_dont_match_again():
    old = cmd(1, {"direction": "forward"}, ago_s=COMMAND_WINDOW_S + 60)
    e, m = cmp(piv(), piv("forward"), [old])
    assert m == {} and e[0].source == "outside"
    used = cmd(2, {"direction": "forward"})
    used.seen_fields.add("direction")
    e, m = cmp(piv(), piv("forward"), [used])
    assert m == {} and e[0].source == "outside"
    # A command whose value differs from what showed up doesn't explain it either.
    e, m = cmp(piv("forward", speed=35), piv("forward", speed=60), [cmd(3, {"speed_pct": 50.0})])
    assert m == {} and e[0].source == "outside"


def test_newest_command_wins():
    a, b = cmd(1, {"speed_pct": 50.0}, ago_s=600), cmd(2, {"speed_pct": 50.0}, ago_s=60)
    _, m = cmp(piv("forward", speed=35), piv("forward", speed=50), [a, b])
    assert m == {2: ["speed_pct"]}


def test_stop_command_takes_the_status_change_with_it():
    e, m = cmp(piv("forward", water=True), piv("stopped", water=False), [cmd(4, {"direction": "stopped", "water_on": False})])
    assert e == [] and sorted(m[4]) == ["direction", "water_on"]


def test_command_wording():
    diff = {"direction_key": {"from": "stopped", "to": "forward"}, "service_stop_deg": {"from": None, "to": 150.6}}
    after = {"direction_key": "forward", "water_on": True, "speed_pct": 35.0, "depth_in": 0.45, "service_stop_deg": 150.6}
    assert command_text(diff, after) == "Start forward, wet · 0.45″ (35%), service stop 150.6°"
    assert command_text({"direction_key": {"from": "forward", "to": "stopped"}}, {"direction_key": "stopped"}) == "Stop"
    assert command_text({"direction_key": {"from": "forward", "to": "reverse"}},
                        {"direction_key": "reverse", "water_on": False, "speed_pct": 100.0}) == "Turn round: reverse"
    assert command_text({"speed_pct": {"from": 35, "to": 50}, "depth_in": {"from": 0.45, "to": 0.32}},
                        {"direction_key": "stopped", "speed_pct": 50.0, "depth_in": 0.32}) == "Speed 50% (0.32″)"
    assert command_text({"accessory_1": {"from": False, "to": True}, "auto_reverse": {"from": True, "to": False}},
                        {"accessory_1": True, "auto_reverse": False}) == "Auto reverse off, chemigation (aux 1) on"
    assert command_text({"service_stop_deg": {"from": 136.6, "to": None}}, {"service_stop_deg": None}) == "Service stop off"
    assert command_text({"speed_pct": {"from": 40.5, "to": 60.0}},
                        {"direction_key": "forward", "water_on": True, "speed_pct": 60.0, "depth_in": 0.24}) == "Speed 60% (0.24″)"
    assert command_text({"water_on": {"from": True, "to": False}},
                        {"direction_key": "forward", "water_on": False, "speed_pct": 100.0}) == "Dry · 100%"
    assert command_text({"plan_id": {"from": "manual-mode", "to": "x1"}}, {"plan_id": "x1"}, "Corners") == "Plan Corners"


def test_expected_values():
    diff = {"direction_key": {"from": "stopped", "to": "forward"}, "depth_in": {"from": 0.3, "to": 0.45},
            "speed_pct": {"from": 40.0, "to": 35.0}, "plan_id": {"from": "x", "to": None}}
    after = {"direction_key": "forward", "water_on": True, "speed_pct": 35.04, "depth_in": 0.45, "plan_id": None}
    assert expected_from(diff, after) == {"direction": "forward", "speed_pct": 35.0, "plan": "Manual"}
    assert outcome_for("successful") == "taken" and outcome_for("failure") == "refused"
    assert outcome_for("waiting-for-online-status") == "queued" and outcome_for(None) == "no-answer"


# ── The change watcher end to end, with a memory store ────────────────


class MemStore:
    def __init__(self) -> None:
        self.snaps: dict[str, dict[str, Any]] = {}
        self.rows: list[dict[str, Any]] = []
        self.saves = 0
        self.beats = 0
        self.programs: dict[str, str] = {}

    async def seen(self):
        return {k: dict(v) for k, v in self.snaps.items()}

    async def save_seen(self, snaps):
        self.saves += len(snaps)
        self.snaps.update(snaps)

    async def open_commands(self, since_s):
        out: dict[str, list[Command]] = {}
        for r in self.rows:
            if r["kind"] == "command" and r["at"] >= since_s and r["outcome"] in ("taken", "queued", "no-answer"):
                out.setdefault(r["pivot_id"], []).append(Command(r["id"], r["at"], r["source"], r["who"], r["expected"],
                                                                 set(r["seen_fields"])))
        return out

    async def add_change(self, pivot_id, pivot_name, e, angle):
        self.rows.append({"id": len(self.rows) + 1, "kind": e.kind, "pivot_id": pivot_id, "source": e.source,
                          "message": e.message, "at": e.at, "alert": e.alert})

    async def mark_seen(self, command_id, fields):
        for r in self.rows:
            if r["id"] == command_id:
                r["seen_fields"] = sorted(set(r["seen_fields"]) | set(fields))
                r["seen"] = True

    async def add_command(self, pivot_id, pivot_name, source, who, message, changes, expected, outcome, angle):
        self.rows.append({"id": len(self.rows) + 1, "kind": "command", "pivot_id": pivot_id, "source": source, "who": who,
                          "message": message, "expected": expected, "outcome": outcome, "seen_fields": [],
                          "seen": False, "at": clock[0], "alert": None})
        return len(self.rows)

    async def unseen_commands(self, sent_before_s, sent_after_s):
        return [Command(r["id"], r["at"], r["source"], r["who"], r["expected"], set(), r["pivot_id"], r["message"])
                for r in self.rows
                if r["kind"] == "command" and r["source"] in ("portal", "claude") and r["outcome"] in ("taken", "no-answer")
                and not r["seen"] and not r["alert"] and r["expected"] and sent_after_s <= r["at"] <= sent_before_s]

    async def flag_alert(self, entry_id, kind):
        r = self.rows[entry_id - 1]
        if not r["alert"]:
            r["alert"] = kind

    async def unsent_alerts(self):
        return sum(1 for r in self.rows if r.get("alert"))

    async def program_name(self, pivot_id):
        return self.programs.get(pivot_id)

    async def heartbeat(self, error):
        self.beats += 1


clock = [NOW]


def test_change_watcher_end_to_end():
    store = MemStore()
    board = [piv(water=True, speed=35, depth=0.45), piv(pid="k15", name="K-15", status="forward", angle=100)]

    async def pivots():
        return [dict(p) for p in board]

    log = PivotLog(store, pivots, clock=lambda: clock[0])

    async def go():
        assert await log.tick() == 0  # first look: remembers, logs nothing
        assert store.saves == 2 and store.beats == 1
        # A program starts W-26 through _apply; the panel shows it at the next look.
        store.programs["w26"] = "W-26 even up"
        cid = await log.record_command(
            pivot_id="w26", pivot_name="W-26", source="program", who=None,
            diff={"direction_key": {"from": "stopped", "to": "forward"}},
            after={"direction_key": "forward", "water_on": True, "speed_pct": 35.0, "depth_in": 0.45}, ack="successful")
        row = store.rows[cid - 1]
        assert row["who"] == "W-26 even up" and row["message"] == "Start forward, wet · 0.45″ (35%)" and row["outcome"] == "taken"
        clock[0] += 150
        board[0] = piv("forward", water=True, speed=35, depth=0.45)
        board[1] = piv(pid="k15", name="K-15", status="forward", angle=104)  # only moved
        saves = store.saves
        assert await log.tick() == 0  # explained by the command: nothing new to log
        assert store.rows[cid - 1]["seen"] and store.rows[cid - 1]["seen_fields"] == ["direction"]
        assert store.saves == saves + 1  # W-26 changed; K-15 only moved
        # Someone slows it down in the FieldNET app, then it reaches its service stop.
        clock[0] += 150
        board[0] = piv("forward", water=True, speed=25, depth=0.63)
        assert await log.tick() == 1
        assert store.rows[-1]["source"] == "outside" and store.rows[-1]["message"] == "Speed 35% → 25% (0.45″ → 0.63″)"
        clock[0] += 150
        board[0] = piv("service-stop", water=True, speed=25, depth=0.63, angle=152.2)
        board[1] = piv(pid="k15", name="K-15", status="forward", angle=108, online="offline")
        assert await log.tick() == 2
        assert {(r["pivot_id"], r["source"], r["message"]) for r in store.rows[-2:]} == {
            ("w26", "pivot", "Service stop at 152°"), ("k15", "pivot", "Lost contact")}

    asyncio.run(go())


def test_recording_never_raises():
    class Broken(MemStore):
        async def add_command(self, *a, **k):
            raise RuntimeError("database down")

    async def pivots():
        return []

    log = PivotLog(Broken(), pivots)
    out = asyncio.run(log.record_command(pivot_id="w26", pivot_name="W-26", source="claude", who="Claude",
                                         diff={"direction_key": {"from": "forward", "to": "stopped"}},
                                         after={"direction_key": "stopped"}, ack="successful"))
    assert out is None


# ── Alerts ────────────────────────────────────────────────────────────


def alerts(prev, cur, commands=None):
    p, c = snap(prev), snap(cur)
    entries, _ = compare(p, c, commands or [], NOW)
    mark_alerts(p, c, entries)
    return [(e.source, e.alert) for e in entries]


def test_what_alerts_and_what_doesnt():
    assert alerts(piv("forward-wet", water=True), piv("low-pressure-shutdown", water=True)) == [("pivot", "fault")]
    assert alerts(piv("forward"), piv("alignment-fault")) == [("pivot", "fault")]
    assert alerts(piv("forward-wet", water=True), piv("forward-wet", water=True, online="offline")) == [("pivot", "contact")]
    assert alerts(piv("forward"), piv("stopped")) == [("outside", "stop")]
    # Normal ends and things that happen while it's stopped don't alert.
    assert alerts(piv("forward", stop=152.2), piv("service-stop", stop=152.2)) == [("pivot", None)]
    assert alerts(piv("forward"), piv("soft-barrier-stop")) == [("pivot", None)]
    assert alerts(piv("stopped"), piv("alignment-fault")) == [("pivot", None)]
    assert alerts(piv("stopped"), piv("stopped", online="offline")) == [("pivot", None)]
    assert alerts(piv(), piv("forward")) == [("outside", None)]
    # A stop someone sent through the portal is explained, so no alert.
    assert alerts(piv("forward"), piv("stopped"), [cmd(1, {"direction": "stopped"})]) == []


def test_not_taken_compares_with_the_panel_now():
    c = cmd(1, {"direction": "forward", "speed_pct": 50.0})
    assert still_not_taken(c, snap(piv())) is True
    assert still_not_taken(c, snap(piv("forward", speed=50))) is False
    assert still_not_taken(cmd(2, {}), snap(piv())) is False  # nothing it can be checked against


def test_alerts_end_to_end():
    store = MemStore()
    board = [piv("forward-wet", water=True), piv("forward", pid="k15", name="K-15", water=True)]
    nudges = []
    aux = {"w26": True, "k15": False}

    async def pivots():
        return [dict(p) for p in board]

    async def aux_on(pid):
        return aux[pid]

    async def notify():
        nudges.append(clock[0])

    clock[0] = NOW
    log = PivotLog(store, pivots, clock=lambda: clock[0], aux_on=aux_on, notify=notify)

    async def go():
        await log.tick()
        # W-26 shuts down on low pressure with the chemical pumps still on; K-15 is stopped at the panel.
        clock[0] += 150
        board[0] = piv("low-pressure-shutdown", water=True)
        board[1] = piv("stopped", pid="k15", name="K-15", water=True)
        await log.tick()
        got = {(r["pivot_id"], r["kind"], r["alert"]) for r in store.rows}
        assert got == {("w26", "change", "fault"), ("w26", "alert", "chem"), ("k15", "change", "stop")}, got
        assert [r["message"] for r in store.rows if r["alert"] == "chem"] == ["Chemigation (aux 1) is still on and W-26 is stopped"]
        assert len(nudges) == 1
        # Restarted and down again within half an hour: logged, but no second notification.
        for status in ("forward-wet", "low-pressure-shutdown"):
            clock[0] += 150
            board[0] = piv(status, water=True)
            await log.tick()
        last = [r for r in store.rows if r["pivot_id"] == "w26"][-2:]  # the shutdown and the chemigation check
        assert [(r["kind"], r["alert"]) for r in last] == [("change", None), ("alert", None)], last
        assert len(nudges) == 1
        clock[0] += ALERT_COOLDOWN_S
        board[0] = piv("forward-wet", water=True)
        await log.tick()
        clock[0] += 150
        board[0] = piv("low-pressure-shutdown", water=True)
        await log.tick()
        last = [r for r in store.rows if r["pivot_id"] == "w26"][-2:]
        assert [r["alert"] for r in last] == ["fault", "chem"] and len(nudges) == 2

    asyncio.run(go())


def test_change_not_taken_alerts_once():
    store = MemStore()
    board = [piv()]
    nudges = []

    async def pivots():
        return [dict(p) for p in board]

    async def notify():
        nudges.append(1)

    clock[0] = NOW
    log = PivotLog(store, pivots, clock=lambda: clock[0], notify=notify)

    async def go():
        await log.tick()
        start = {"direction_key": {"from": "stopped", "to": "forward"}}
        after = {"direction_key": "forward", "water_on": False, "speed_pct": 40.5}
        a = await log.record_command(pivot_id="w26", pivot_name="W-26", source="portal", who="Sam Crew", diff=start,
                                     after=after, ack="successful")
        p = await log.record_command(pivot_id="w26", pivot_name="W-26", source="program", who="Even up", diff=start,
                                     after=after, ack="successful")
        clock[0] += NOT_TAKEN_S - 60
        await log.tick()
        assert store.rows[a - 1]["alert"] is None  # not yet
        clock[0] += 120
        await log.tick()
        assert store.rows[a - 1]["alert"] == "not-taken" and store.rows[p - 1]["alert"] is None  # programs alert themselves
        assert len(nudges) == 1
        clock[0] += 150
        await log.tick()
        assert len(nudges) == 1  # once
        # One that did reach the panel, but between looks, is marked seen instead.
        b = await log.record_command(pivot_id="w26", pivot_name="W-26", source="claude", who="Claude",
                                     diff={"speed_pct": {"from": 40.5, "to": 60.0}},
                                     after={"direction_key": "stopped", "speed_pct": 60.0}, ack="successful")
        board[0] = piv(speed=60)
        store.snaps["w26"]["speed_pct"] = 60.0  # the change was already in the stored look
        clock[0] += NOT_TAKEN_S + 60
        await log.tick()
        assert store.rows[b - 1]["seen"] and store.rows[b - 1]["alert"] is None

    asyncio.run(go())


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok  {name}")
    print(f"{n} passed")
