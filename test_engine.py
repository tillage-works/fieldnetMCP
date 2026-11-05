"""Tests for the irrigation program engine (engine.py). No FieldNET or database: run with
python test_engine.py

Most tests drive the real Engine against a simulated pivot that moves at its commanded speed,
stops at its service stop like a real panel (status 'service-stop', the stop then cleared), only
reports its position every 15 minutes unless polled, and keeps track of the water it puts down.
So a whole program can be run start to finish and the field checked for evenness."""

from __future__ import annotations

import asyncio
import copy
import math
from typing import Any

import engine as E
import programs as P
from watcher import Event, Group, Member, WindowState, decide as watcher_decide

IN_PER_M = 39.3701
D_FULL = 0.142  # W-26: inches per pass at 100% speed
CIRCLE_H = 10.05  # W-26: hours per circle at 100%
W26 = [P.Wedge(61.4, 92.5, 3.50), P.Wedge(92.5, 136.6, 3.46), P.Wedge(136.6, 355.5, 2.99), P.Wedge(355.5, 61.4, 3.70)]


# ---------------------------------------------------------------------------
# Simulated pivot + FieldNET
# ---------------------------------------------------------------------------


class Sim:
    BINS = 3600  # 0.1°

    def __init__(self, angle=63.3, direction="forward", depth=0.19, water=True, wedges=W26):
        self.t = 1_800_000_000.0
        self.angle = angle
        self.direction = direction  # what it is doing: forward | reverse | stopped
        self.direction_key = direction if direction != "stopped" else "forward"  # last commanded
        self.water = water
        self.rate = D_FULL / depth
        self.service_stop: float | None = None
        self.plan_id = None
        self.status = f"{direction}-{'wet' if water else 'dry'}" if direction != "stopped" else "stopped"
        self.online = True
        self.status_at = self.t - 3600
        self.reported = (angle, self.t)
        self.pending: list[tuple[float, dict[str, Any]]] = []
        self.sent: list[dict[str, Any]] = []  # commands actually sent to the panel
        self.previews = 0
        self.ignore_service_stop = False
        self.verify_override = None  # (after dict) → after dict, to fake FieldNET quirks
        self.fail_sends = 0
        self.applied = [0.0] * self.BINS
        for w in wedges:
            span = P.cw(w.from_deg, w.to_deg) or 360.0
            for i in range(int(round(span * 10))):
                self.applied[int(round((P.norm(w.from_deg) * 10 + i))) % self.BINS] = w.depth_in

    # -- physics --
    def degph(self) -> float:
        return 360.0 / (CIRCLE_H / self.rate)

    def depth(self) -> float:
        return D_FULL / self.rate

    def set_status(self, s: str) -> None:
        if s != self.status:
            self.status = s
            self.status_at = self.t
            self.reported = (self.angle, self.t)

    def advance(self, dt: float) -> None:
        end = self.t + dt
        while self.t < end - 1e-9:
            step = min(30.0, end - self.t)
            for at, cmd in list(self.pending):
                if at <= self.t:
                    self.pending.remove((at, cmd))
                    self._apply(cmd)
            if self.direction in ("forward", "reverse"):
                d = self.degph() * step / 3600.0
                sign = 1 if self.direction == "forward" else -1
                new = self.angle + sign * d
                hit = False
                if self.service_stop is not None and not self.ignore_service_stop:
                    togo = P.degrees_to_go(self.angle, self.service_stop, self.direction == "forward")
                    if 1e-9 < togo <= d + 1e-9:
                        new, hit = self.angle + sign * togo, True
                if self.water:
                    self._wet(self.angle, new)
                self.angle = P.norm(new)
                if hit:
                    self.direction = "stopped"
                    self.service_stop = None
                    self.set_status("service-stop")
            self.t += step
            if self.t - self.reported[1] >= 15 * 60:
                self.reported = (self.angle, self.t)

    def _wet(self, a: float, b: float) -> None:
        lo, hi = sorted((a, b))
        i0, i1 = int(math.floor(lo * 10)), int(math.floor(hi * 10))
        for i in range(i0, i1 + 1):
            cover = min(hi, (i + 1) / 10) - max(lo, i / 10)
            if cover > 0:
                self.applied[i % self.BINS] += self.depth() * cover / 0.1

    def _apply(self, c: dict[str, Any]) -> None:
        self.direction_key = c["direction_key"]
        self.water = c["water_on"]
        self.rate = c["rate"]
        self.service_stop = c["service_stop_angle"]
        self.plan_id = c["plan_id"]
        if c["direction_key"] == "stopped":
            self.direction = "stopped"
            self.set_status("stopped")
        else:
            self.direction = c["direction_key"]
            self.set_status(f"{self.direction}-{'wet' if self.water else 'dry'}")

    # -- people --
    def manual_stop(self) -> None:
        self.direction = "stopped"
        self.set_status("stopped")

    def fault(self) -> None:
        self.direction = "stopped"
        self.set_status("low-pressure-shutdown")

    def change_depth(self, depth: float) -> None:
        self.rate = D_FULL / depth
        self.reported = (self.angle, self.t)

    # -- FieldNET as the engine sees it --
    def live(self) -> dict[str, Any]:
        return {"operational_status": self.status, "direction_key": self.direction_key, "water_on": self.water,
                "current_angle": round(self.reported[0], 1), "rate": round(self.rate, 4),
                "rate_conversion_factor": D_FULL / IN_PER_M, "full_run_time": CIRCLE_H * 3600,
                "service_stop": self.service_stop, "service_stop_repeat_on": False, "current_plan_id": self.plan_id,
                "rtu_online": self.online}

    def list_item(self) -> dict[str, Any]:
        from datetime import datetime, timezone

        return {"id": "w26", "name": "W-26", "operational_status": self.status,
                "communication_status": "online" if self.online else "offline",
                "direction": self.direction, "pivot_angle": round(self.reported[0], 1), "is_water_on": self.water,
                "last_updated": datetime.fromtimestamp(self.status_at, timezone.utc).isoformat()}

    async def pivots(self):
        return [self.list_item()]

    async def observe(self, pid: str, poll: bool):
        if poll and self.online:
            self.reported = (self.angle, self.t)
        return {**self.live(), "_fresh": poll and self.online}

    async def control(self, pid: str, cmd: dict[str, Any], send: bool, verify):
        rate = self.rate
        if cmd.get("depth_in") is not None:
            rate = max(0.01, min(1.0, round((D_FULL / IN_PER_M) / (cmd["depth_in"] / IN_PER_M), 4)))
        elif cmd.get("speed_pct") is not None:
            rate = cmd["speed_pct"] / 100
        payload = {"direction_key": cmd["direction"], "water_on": cmd["water_on"], "rate": rate,
                   "service_stop_angle": cmd.get("service_stop_deg", self.service_stop),
                   "plan_id": None if cmd["direction"] != "stopped" else self.plan_id}
        after = {"direction_key": payload["direction_key"], "water_on": payload["water_on"],
                 "speed_pct": round(rate * 100, 1), "depth_in": round(D_FULL / rate, 2),
                 "service_stop_deg": payload["service_stop_angle"], "plan_id": payload["plan_id"]}
        if self.verify_override:
            after = self.verify_override(after)
        if not self.online:
            return {"applied": False, "error": "RTU is offline; command would not be delivered."}
        why = verify(after)
        will = f"RUN {cmd['direction'].upper()}" if cmd["direction"] != "stopped" else "BE STOPPED"
        if why:
            return {"applied": False, "mismatch": why, "final_settings": after, "pivot_will": will}
        if not send:
            self.previews += 1
            return {"applied": False, "preview": True, "final_settings": after, "pivot_will": will}
        if self.fail_sends:
            self.fail_sends -= 1
            return {"applied": False, "sent": True, "panel_ack": "failure", "error": "The panel reported the apply FAILED."}
        self.sent.append({**cmd, "at": self.t, "angle": self.angle})
        self.pending.append((self.t + 45, payload))
        return {"applied": True, "panel_ack": "successful", "final_settings": after, "pivot_will": will}

    async def plan(self, pid: str, s: dict[str, Any]) -> dict[str, Any]:
        """The real planner, on the water this simulated field has had so far."""
        state = P.PivotState(angle=self.angle, direction=self.direction if self.direction != "stopped" else "stopped",
                             depth_in=self.depth() if self.direction != "stopped" else None,
                             water_on=self.water and self.direction != "stopped")
        return plan_for(state, self.wedge_depths(), s)

    def wedge_depths(self, wedges=W26) -> list[P.Wedge]:
        out = []
        for w in wedges:
            span = P.cw(w.from_deg, w.to_deg) or 360.0
            n = int(round(span * 10))
            vals = [self.applied[int(round(P.norm(w.from_deg) * 10 + i)) % self.BINS] for i in range(n)]
            out.append(P.Wedge(w.from_deg, w.to_deg, sum(vals) / len(vals)))
        return out

    def spread(self, skip_deg: float = 1.0, behind: tuple[float, float] | None = None) -> tuple[float, float]:
        """Lowest and highest water anywhere, ignoring bins within skip_deg of a wedge edge and the
        sliver `behind` the pivot's start (the planner leaves it short on purpose and says so)."""
        edges = [w.from_deg for w in W26]
        vals = [v for i, v in enumerate(self.applied) if min(E.circ_dist(i / 10, e) for e in edges) > skip_deg
                and not (behind and P.arc_contains(behind[0], behind[1], i / 10))]
        return min(vals), max(vals)


def plan_for(state: P.PivotState, wedges: list[P.Wedge], s: dict[str, Any]) -> dict[str, Any]:
    prof = P.FieldProfile(depth_full_in=D_FULL, circle_hours_full=CIRCLE_H, max_depth_in=s["maxDepthIn"])
    plan = P.plan_even_up(wedges, state, prof, target_in=s.get("targetIn"), tolerance_in=s.get("toleranceIn", 0.05))
    for st in plan["steps"]:
        st["service_stop_deg"] = st["end_deg"]
    return plan


class MemStore:
    """The engine's database, in memory. Runs are stored as copies, like rows."""

    def __init__(self):
        self.programs: dict[int, dict[str, Any]] = {}
        self.runs: dict[int, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.beats = 0

    def add(self, prog: E.Program) -> None:
        self.programs[prog.id] = {"p": prog, "state": prog.state, "reason": None}

    def _prog(self, pid: int) -> E.Program:
        d = self.programs[pid]
        return E.Program(**{**d["p"].__dict__, "state": d["state"]})

    async def armed(self):
        return [self._prog(i) for i, d in self.programs.items() if d["state"] == "armed"]

    async def open_runs(self):
        out = []
        for rid, r in self.runs.items():
            if r["ended"] is None:
                run = E.Run(id=rid, program=self._prog(r["program_id"]), practice=r["practice"], plan=copy.deepcopy(r["plan"]),
                            step=r["step"], memo=copy.deepcopy(r["memo"]), started_at=r["started_at"])
                out.append((run, self.programs[r["program_id"]]["state"]))
        return out

    async def program_state(self, pid):
        return self.programs[pid]["state"] if pid in self.programs else None

    async def begin_run(self, prog, practice, plan):
        d = self.programs[prog.id]
        if d["state"] != "armed":
            return None
        d["state"] = "running"
        rid = len(self.runs) + 1
        self.runs[rid] = {"program_id": prog.id, "practice": practice, "plan": copy.deepcopy(plan), "step": 0,
                          "memo": {}, "started_at": SIM.t, "ended": None, "outcome": None}
        return E.Run(id=rid, program=self._prog(prog.id), practice=practice, plan=plan, started_at=SIM.t)

    async def save_run(self, run):
        self.runs[run.id].update(step=run.step, memo=copy.deepcopy(run.memo))

    async def end_run(self, run, outcome, program_state, reason=None):
        r = self.runs[run.id]
        if r["ended"] is None:
            r.update(ended=SIM.t, outcome=outcome, step=run.step, memo=copy.deepcopy(run.memo))
        d = self.programs[run.program.id]
        if program_state and d["state"] == "running":
            d.update(state=program_state, reason=reason)

    async def set_program(self, pid, state, reason):
        d = self.programs[pid]
        if d["state"] in ("armed", "running"):
            d.update(state=state, reason=reason)

    async def log(self, prog, run, kind, message, practice, alert=False, angle=None, detail=None):
        self.events.append({"t": SIM.t, "kind": kind, "message": message, "practice": practice, "alert": alert,
                            "step": (run.step + 1) if run else None, "detail": detail})

    async def heartbeat(self, error, allow_live):
        self.beats += 1

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.events]


SIM: Sim = None  # type: ignore[assignment]


def setup(sim: Sim, mode="live", start_when="now", allow_live=True, max_depth=0.35, approved=None, armed_at=None):
    global SIM
    SIM = sim
    store = MemStore()
    settings = {"maxDepthIn": max_depth, "fromDate": "2026-09-24", "targetIn": None, "toleranceIn": 0.05}
    if approved is None:
        state = P.PivotState(angle=sim.angle, direction=sim.direction, depth_in=sim.depth(), water_on=sim.water)
        approved = plan_for(state, sim.wedge_depths(), settings)
    store.add(E.Program(id=1, pivot_id="w26", pivot_name="W-26", name="Even up", mode=mode, state="armed",
                        start_when=start_when, armed_at=armed_at if armed_at is not None else sim.t,
                        settings=settings, approved=approved))
    eng = E.Engine(store, sim.pivots, sim.observe, sim.plan, sim.control, interval_s=120, allow_live=allow_live,
                   clock=lambda: SIM.t)
    return store, eng


async def run_for(sim: Sim, eng: E.Engine, hours: float, events: dict[float, Any] | None = None, until=None) -> None:
    """Tick the engine, moving the pivot between ticks, for `hours` (or until `until()`)."""
    end = sim.t + hours * 3600
    todo = sorted((events or {}).items())
    while sim.t < end:
        wait = await eng.tick()
        assert 20 <= wait <= 120 or wait == 120, wait
        target = sim.t + wait
        while todo and todo[0][0] * 3600 + START <= target:
            at, fn = todo.pop(0)
            sim.advance(max(0.0, at * 3600 + START - sim.t))
            fn()
        sim.advance(max(0.0, target - sim.t))
        if until and until():
            return


START = 0.0


def go(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------


def test_split_long_steps():
    steps = [{"id": 1, "direction": "forward", "water_on": True, "depth_in": 0.2, "start_deg": 10, "end_deg": 9,
              "arc_deg": 359, "hours": 10, "transition": "end", "done_after_hours": 10, "service_stop_deg": 9}]
    out = E.split_long_steps(steps)
    assert [(s["start_deg"], s["end_deg"], s["transition"]) for s in out] == [(10, 189.5, "flying"), (189.5, 9, "end")]
    assert [s["id"] for s in out] == [1, 2] and all(s["service_stop_deg"] == s["end_deg"] for s in out)
    rev = E.split_long_steps([dict(steps[0], direction="reverse", start_deg=10, end_deg=11)])
    assert rev[0]["end_deg"] == 190.5 and rev[0]["arc_deg"] == 179.5
    short = E.split_long_steps([dict(steps[0], arc_deg=200)])
    assert len(short) == 1


def test_check_command():
    cmd = E.command_for({"direction": "forward", "water_on": True, "depth_in": 0.35, "end_deg": 355.5})
    good = {"direction_key": "forward", "water_on": True, "depth_in": 0.35, "speed_pct": 40.6,
            "service_stop_deg": 355.5, "plan_id": None}
    assert E.check_command(good, cmd) is None
    assert "depth" in E.check_command(dict(good, depth_in=0.14), cmd)  # FieldNET clamped the speed
    assert "service stop" in E.check_command(dict(good, service_stop_deg=None), cmd)
    assert "direction" in E.check_command(dict(good, direction_key="reverse"), cmd)
    assert "saved plan" in E.check_command(dict(good, plan_id="abc"), cmd)
    dry = E.command_for({"direction": "reverse", "water_on": False, "depth_in": None, "end_deg": 10})
    assert dry["speed_pct"] == 100 and E.check_command({"direction_key": "reverse", "water_on": False, "speed_pct": 100,
                                                         "service_stop_deg": 10.0, "plan_id": None}, dry) is None


def test_needs_reapproval():
    approved = {"passes": 2, "total_hours": 33}
    ok = {"passes": 2, "total_hours": 30, "steps": [{"id": 1, "water_on": True, "depth_in": 0.35}]}
    assert E.needs_reapproval(ok, approved, 0.35) is None
    assert "passes" in E.needs_reapproval(dict(ok, passes=4), approved, 0.35)
    assert "takes" in E.needs_reapproval(dict(ok, total_hours=60), approved, 0.35)
    assert "over the" in E.needs_reapproval(dict(ok, steps=[{"id": 2, "water_on": True, "depth_in": 0.4}]), approved, 0.35)


def test_obs_from_live():
    base = {"operational_status": "low-pressure-warning", "direction_key": "forward", "water_on": True,
            "current_angle": 100, "rate": 0.5, "rate_conversion_factor": D_FULL / IN_PER_M, "service_stop": 120,
            "rtu_online": True}
    o = E.obs_from_live(base, True, 0)
    assert o.cls == "running-wet" and o.direction == "forward" and abs(o.depth_in - 0.284) < 0.001
    # FieldNET keeps the last commanded direction after a stop: that's not moving.
    o = E.obs_from_live(dict(base, operational_status="service-stop"), False, 0)
    assert o.cls == "finished" and o.direction == "stopped"
    o = E.obs_from_live(dict(base, operational_status="low-pressure-shutdown"), False, 0)
    assert o.cls == "low-pressure"
    assert E.obs_from_live(dict(base, rtu_online=False), False, 0).cls == "offline"


def test_progress_and_early_flying():
    st = {"direction": "forward", "start_deg": 136.6, "end_deg": 355.5, "arc_deg": 218.9, "hours": 15}
    assert E.progress(st, 136.6) == 0 and abs(E.progress(st, 200) - 63.4) < 1e-6
    assert E.progress(st, 135.0) == 0  # sent a little early: not started yet, not an overrun
    rev = dict(st, direction="reverse", start_deg=355.5, end_deg=136.6)
    assert abs(E.progress(rev, 300) - 55.5) < 1e-6 and E.progress(rev, 357) == 0
    assert 1.0 <= E.lead_deg(st) <= 2.0


def test_start_reason():
    prog = E.Program(1, "w26", "W-26", "x", "live", "armed", "next_start", 1000.0, {}, {})
    running = {"operational_status": "forward-wet", "communication_status": "online", "direction": "forward",
               "is_water_on": True, "last_updated": "1970-01-01T00:10:00+00:00"}  # 600 s: before arming
    assert E.start_reason(prog, running, seen_idle=False) is None
    assert E.start_reason(prog, running, seen_idle=True)
    assert E.start_reason(prog, dict(running, last_updated="1970-01-01T00:20:00+00:00"), False)  # started after arming
    assert E.start_reason(E.Program(**{**prog.__dict__, "start_when": "now"}), running, False)
    assert E.start_reason(prog, dict(running, operational_status="forward-dry", is_water_on=False), True) is None


def test_watcher_skips_mid_program_stops():
    g = Group(1, "Booster", "live", [(0, 0, 0, 0)], [Member("w26", "W-26", "scheduled"),
                                                       Member("k1", "K-1", "fallback", 1, "forward", 50)])
    st = WindowState("k", last={"w26": "running-wet"}, watched={"w26"})
    pivots = [{"id": "w26", "name": "W-26", "operational_status": "service-stop", "communication_status": "online",
               "direction": "stopped"},
              {"id": "k1", "name": "K-1", "operational_status": "stopped", "communication_status": "online",
               "direction": "stopped"}]
    events, starts = watcher_decide(g, copy.deepcopy(st), pivots, 0, {"w26"})
    assert not starts and events[0].kind == "no-trigger" and "program" in events[0].message
    events, starts = watcher_decide(g, copy.deepcopy(st), pivots, 0, set())
    assert [s.member.name for s in starts] == ["K-1"]  # a real end of run still starts the fallback


# ---------------------------------------------------------------------------
# Whole runs on the simulated W-26
# ---------------------------------------------------------------------------


def test_w26_live_runs_the_program_and_evens_the_field():
    sim = Sim()
    store, eng = setup(sim)
    go(run_for(sim, eng, 40, until=lambda: store.programs[1]["state"] != "running" and sim.t > START + 3600))
    d = store.programs[1]
    assert d["state"] == "finished", (d, store.kinds())
    # Three step commands and the water off at the end; each step's service stop at its end.
    cmds = [(c["direction"], c.get("depth_in"), c.get("service_stop_deg")) for c in sim.sent]
    assert cmds == [("forward", 0.19, 136.6), ("forward", 0.35, 355.5), ("reverse", 0.35, 136.6), ("stopped", None, None)], cmds
    # The flying change went out before 136.6°, within the lead; the turn-round after the stop at 355.5°.
    assert 134.0 < sim.sent[1]["angle"] < 136.6, sim.sent[1]
    assert abs(sim.sent[2]["angle"] - 355.5) < 0.05
    assert sim.direction == "stopped" and abs(sim.angle - 136.6) < 0.05 and not sim.water
    assert any("slivers" in n for n in store.runs[1]["plan"]["notes"])
    lo, hi = sim.spread(behind=(61.4, 63.6))
    assert hi - lo <= 0.08 and lo >= 3.63, (lo, hi)  # was 2.99–3.70
    hours = (sim.t - START) / 3600
    assert 32 <= hours <= 36, hours
    k = store.kinds()
    assert k[0] == "started" and k.count("confirmed") == 3 and k[-1] == "finished", k
    assert not any(e["kind"] in ("override", "stopped", "fault", "overrun", "mismatch") for e in store.events)


def test_w26_practice_sends_nothing_and_follows_until_the_turn():
    sim = Sim()
    store, eng = setup(sim, mode="practice")
    go(run_for(sim, eng, 20, until=lambda: store.programs[1]["state"] == "finished"))
    assert not sim.sent and sim.previews == 2, (sim.sent, sim.previews)  # step 1 and the flying change, checked only
    k = store.kinds()
    assert k[0] == "practice-start" and k.count("would-step") == 2 and k[-1] == "practice-done", k
    assert "turn it round" in store.events[-1]["message"] and all(e["practice"] for e in store.events)
    assert store.programs[1]["state"] == "finished"


def test_live_needs_the_server_switch():
    sim = Sim()
    store, eng = setup(sim, mode="live", allow_live=False)
    go(run_for(sim, eng, 4))
    assert not sim.sent and store.kinds()[0] == "practice-start"


def test_manual_stop_wins():
    sim = Sim()
    store, eng = setup(sim)
    go(run_for(sim, eng, 8, {6.0: sim.manual_stop}))
    assert store.programs[1]["state"] == "paused" and "Manual stops win" in store.programs[1]["reason"]
    n = len(sim.sent)
    go(run_for(sim, eng, 6))
    assert len(sim.sent) == n and sim.direction == "stopped"  # never restarted
    assert store.kinds()[-1] == "stopped" and store.events[-1]["alert"]


def test_settings_changed_by_someone_else_pauses():
    sim = Sim()
    store, eng = setup(sim)
    go(run_for(sim, eng, 8, {5.0: lambda: sim.change_depth(0.5)}))
    assert store.programs[1]["state"] == "paused" and store.kinds()[-1] == "override"
    assert "0.5" in store.events[-1]["message"] and "won't change them back" in store.events[-1]["message"]


def test_fault_pauses():
    sim = Sim()
    store, eng = setup(sim)
    go(run_for(sim, eng, 10, {9.0: sim.fault}))
    assert store.programs[1]["state"] == "paused" and store.kinds()[-1] == "fault"


def test_offline_through_a_flying_change_falls_back_to_the_service_stop():
    sim = Sim()
    store, eng = setup(sim)

    def off():
        sim.online = False

    def on():
        sim.online = True

    # Step 1 (to 136.6°) ends ~2.7 h in. Lose contact from 2 h to 4 h.
    go(run_for(sim, eng, 6, {2.0: off, 4.0: on}))
    k = store.kinds()
    assert "lost-contact" in k and "back-online" in k, k
    # The flying change couldn't be sent: the service stop held it at 136.6°, then step 2 went out as a stop-and-go.
    assert abs(sim.sent[1]["angle"] - 136.6) < 0.05 and sim.sent[1]["depth_in"] == 0.35, sim.sent
    assert store.programs[1]["state"] == "running"
    go(run_for(sim, eng, 36, until=lambda: store.programs[1]["state"] != "running"))
    assert store.programs[1]["state"] == "finished"


def test_waits_for_the_next_start():
    sim = Sim()
    store, eng = setup(sim, start_when="next_start")
    go(run_for(sim, eng, 1))
    assert store.programs[1]["state"] == "armed" and not sim.sent  # running when armed: not a new start
    sim.manual_stop()
    go(run_for(sim, eng, 1))
    assert store.programs[1]["state"] == "armed"

    def restart():
        sim._apply({"direction_key": "forward", "water_on": True, "rate": sim.rate, "service_stop_angle": None, "plan_id": None})

    go(run_for(sim, eng, 1, {(sim.t - START) / 3600 + 0.1: restart}))
    assert store.programs[1]["state"] == "running" and store.kinds()[0] == "started"


def test_bigger_plan_needs_a_new_approval():
    sim = Sim()
    small = {"passes": 1, "total_hours": 10, "steps": []}
    store, eng = setup(sim, approved=small)
    go(run_for(sim, eng, 1))
    assert store.programs[1]["state"] == "paused" and "new approval" in store.programs[1]["reason"]
    assert not sim.sent and store.kinds() == ["needs-approval"]


def test_command_that_doesnt_match_is_not_sent():
    sim = Sim()
    sim.verify_override = lambda after: dict(after, depth_in=0.14)  # e.g. FieldNET clamps the speed at 100%
    store, eng = setup(sim)
    go(run_for(sim, eng, 1))
    assert not sim.sent and store.programs[1]["state"] == "paused" and "mismatch" in store.kinds()


def test_paused_on_the_dashboard():
    sim = Sim()
    store, eng = setup(sim)

    def pause():
        store.programs[1]["state"] = "paused"

    go(run_for(sim, eng, 20, {4.0: pause}))
    assert len(sim.sent) == 2  # step 1, the flying change; nothing after the pause
    assert sim.direction == "stopped" and abs(sim.angle - 355.5) < 0.05  # stopped by step 2's service stop
    assert "paused" in store.kinds() and store.runs[1]["outcome"] == "paused"


def test_failed_sends_retry_then_pause():
    sim = Sim()
    sim.fail_sends = 1
    store, eng = setup(sim)
    go(run_for(sim, eng, 1))
    assert store.programs[1]["state"] == "running" and len(sim.sent) == 1  # second try worked
    sim2 = Sim()
    sim2.fail_sends = 5
    store2, eng2 = setup(sim2)
    go(run_for(sim2, eng2, 1))
    assert store2.programs[1]["state"] == "paused" and store2.kinds()[-1] == "send-failed"


def test_overrun_pauses():
    sim = Sim()
    sim.ignore_service_stop = True  # the panel's stop doesn't hold
    store, eng = setup(sim)
    go(run_for(sim, eng, 20))
    assert store.programs[1]["state"] == "paused" and store.kinds()[-1] == "overrun", store.kinds()


def test_survives_a_restart():
    sim = Sim()
    store, eng = setup(sim)
    go(run_for(sim, eng, 10))
    eng2 = E.Engine(store, sim.pivots, sim.observe, sim.plan, sim.control, interval_s=120, allow_live=True,
                    clock=lambda: SIM.t)
    go(run_for(sim, eng2, 30, until=lambda: store.programs[1]["state"] != "running"))
    assert store.programs[1]["state"] == "finished" and len(sim.sent) == 4


def test_polls_only_near_changes():
    sim = Sim()
    store, eng = setup(sim)
    polls = []
    real = sim.observe

    async def counting(pid, poll):
        polls.append((sim.t, poll))
        return await real(pid, poll)

    eng.observe = counting
    go(run_for(sim, eng, 40, until=lambda: store.programs[1]["state"] != "running" and sim.t > START + 3600))
    n_polls = sum(1 for _, p in polls if p)
    hours = (sim.t - START) / 3600
    assert n_polls <= hours * 2 + 20, (n_polls, hours)  # ~every 30 min, plus a few near each change
    assert len(polls) <= hours * 30 + 40, len(polls)


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            START = Sim().t
            fn()
            n += 1
            print(f"ok  {name}")
    print(f"{n} passed")
