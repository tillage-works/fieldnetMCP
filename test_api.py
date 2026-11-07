"""Tests for the dashboard's plan endpoint (POST /api/plan-even-up).

Needs the requirements installed (pip install -r requirements.txt). Run: python test_api.py
FieldNET is replaced by W-26's real readings from Nov 5, so no network is used."""

from __future__ import annotations

import os

os.environ.setdefault("FIELDNET_REFRESH_TOKEN", "test-refresh-token")
os.environ["MCP_AUTH_TOKEN"] = "read-key"
os.environ.pop("WATCHER_ENABLED", None)
os.environ.pop("PUMPGROUPS_DB_URL", None)  # no background jobs or database in tests

from starlette.testclient import TestClient  # noqa: E402

import server  # noqa: E402

W26 = {"id": "c6f28e12", "name": "W-26", "operational_status": "forward-wet", "communication_status": "online",
       "direction": "forward", "pivot_angle": 63.3, "speed": 0.75, "application_depth": 0.19 * 25.4,
       "is_water_on": True, "last_updated": "2026-11-05T15:02:00+00:00"}
WEDGES = [{"from_deg": 61.4, "to_deg": 92.5, "span_deg": 31.1, "depth_in": 3.5, "depth_mm": 89},
          {"from_deg": 92.5, "to_deg": 136.6, "span_deg": 44.1, "depth_in": 3.46, "depth_mm": 88},
          {"from_deg": 136.6, "to_deg": 355.5, "span_deg": 218.9, "depth_in": 2.99, "depth_mm": 76},
          {"from_deg": 355.5, "to_deg": 61.4, "span_deg": 65.9, "depth_in": 3.7, "depth_mm": 94}]


class _WS:
    async def close(self):
        pass


def fake_fieldnet(wedges=WEDGES, fail: Exception | None = None):
    async def resolve(name):
        if fail:
            raise fail
        if name not in ("W-26", W26["id"]):
            raise ValueError(f"No pivot named '{name}'.")
        return dict(W26)

    async def applied(pid, start, end):
        return wedges

    async def pivots(max_age_s=60):
        return [dict(W26)]

    async def ws():
        return _WS()

    async def live(ws, eid, timeout=10.0):
        return {"rate_conversion_factor": 0.1417 / server.IN_PER_M, "full_run_time": 10.05 * 3600}

    server.resolve_pivot, server._applied_wedges, server.get_pivots = resolve, applied, pivots
    server.ws_session, server.live_status = ws, live


client = TestClient(server.app)
KEY = {"X-API-Key": "read-key"}


def test_needs_the_key():
    fake_fieldnet()
    assert client.post("/api/plan-even-up", json={"pivot": "W-26"}).status_code == 401
    assert client.post("/api/plan-even-up", json={"pivot": "W-26"}, headers={"X-API-Key": "wrong"}).status_code == 401


def test_w26_plan():
    fake_fieldnet()
    r = client.post("/api/plan-even-up", headers=KEY,
                    json={"pivot": "W-26", "max_depth_per_pass_in": 0.35, "from_date": "2026-09-24", "to_date": "2026-11-04"})
    assert r.status_code == 200, r.text
    plan = r.json()
    assert [(s["direction"], s["depth_in"], s["end_deg"], s["transition"]) for s in plan["steps"]] == [
        ("forward", 0.19, 136.6, "flying"), ("forward", 0.35, 355.5, "stop"), ("reverse", 0.35, 136.6, "end")]
    assert plan["sent_nothing"] is True and plan["applied_from"] == "2026-09-24"
    assert all(s["service_stop_deg"] == s["end_deg"] for s in plan["steps"])


def test_default_intake_limit_is_0_3():
    fake_fieldnet()
    plan = client.post("/api/plan-even-up", headers=KEY, json={"pivot": "W-26"}).json()
    assert plan["max_depth_per_pass_in"] == 0.3
    assert all((s["depth_in"] or 0) <= 0.3 for s in plan["steps"])


def test_bad_input():
    fake_fieldnet()
    for body, fragment in [
        ({}, "pivot"),
        ({"pivot": "W-26", "max_depth_per_pass_in": "lots"}, "max_depth_per_pass_in"),
        ({"pivot": "W-26", "max_depth_per_pass_in": 5}, "max_depth_per_pass_in"),
        ({"pivot": "W-26", "tolerance_in": True}, "tolerance_in"),
        ({"pivot": "W-26", "from_date": "last tuesday"}, "from_date must be a date"),
        ({"pivot": "W-26", "to_date": 20261104}, "to_date must be a date"),
    ]:
        r = client.post("/api/plan-even-up", headers=KEY, json=body)
        assert r.status_code == 400 and fragment in r.json()["error"], (body, r.text)
    r = client.post("/api/plan-even-up", headers={**KEY, "Content-Type": "application/json"}, content=b"not json")
    assert r.status_code == 400


def test_planner_refusals_are_422():
    fake_fieldnet(wedges=WEDGES[:2])  # only 21% of the circle
    r = client.post("/api/plan-even-up", headers=KEY, json={"pivot": "W-26"})
    assert r.status_code == 422 and "covers only" in r.json()["error"]
    fake_fieldnet()
    r = client.post("/api/plan-even-up", headers=KEY, json={"pivot": "W-99"})
    assert r.status_code == 422 and "No pivot named" in r.json()["error"]


def test_fieldnet_trouble_is_502():
    fake_fieldnet(fail=TimeoutError("FieldNET slow"))
    r = client.post("/api/plan-even-up", headers=KEY, json={"pivot": "W-26"})
    assert r.status_code == 502 and "FieldNET" in r.json()["error"]


def test_program_commands_leave_the_plan_alone():
    """W-26, Nov 4 2026: a command that cleared the panel's "manual-mode" plan was acknowledged but
    never taken. Program step commands must not touch the plan."""
    import asyncio

    seen = {}

    async def fake_apply(ctx, pivot, changes, confirm, **kw):
        seen.update(changes=changes, kw=kw)
        return {"applied": True}

    real = server._apply
    server._apply = fake_apply
    try:
        cmd = {"direction": "forward", "water_on": True, "depth_in": 0.35, "speed_pct": None, "service_stop_deg": 355.5}
        asyncio.run(server._engine_control("W-26", cmd, True, lambda after: None))
    finally:
        server._apply = real
    assert "plan_id" not in seen["changes"], seen
    assert seen["changes"]["service_stop_angle"] == 355.5 and seen["changes"]["service_stop_repeat_on"] is False
    assert seen["kw"]["depth_in"] == 0.35 and seen["kw"]["source"] == "program" and seen["kw"]["internal"] is True


def test_apply_writes_what_it_sends_to_the_pivot_log():
    """Every command sent goes in the pivot log with its source, so the dashboard can say who did what."""
    import asyncio

    import pivotlog

    fake_fieldnet()
    recorded = []
    sent = []

    class Store:
        async def add_command(self, *a):
            recorded.append(a)
            return 1

        async def program_name(self, pivot_id):
            return "W-26 even up"

    class WS(_WS):
        async def send(self, m):
            sent.append(m)

    async def ws():
        return WS()

    async def live(ws, eid, timeout=10.0):
        return {"operational_status": "stopped", "direction_key": "forward", "water_on": True, "current_angle": 152.2,
                "rate": 0.4, "rate_conversion_factor": 0.1417 / server.IN_PER_M, "full_run_time": 10.05 * 3600,
                "service_stop": None, "service_stop_repeat_on": False, "current_plan_id": "manual-mode",
                "auto_reverse_on": False, "auto_restart_on": False, "accessory_1_on": False, "rtu_online": True}

    async def ack(ws, eid, kind, topic, timeout):
        return {"status": "successful"}

    saved = server.ws_session, server.live_status, server.ws_command_status, server.PIVOT_LOG
    server.ws_session, server.live_status, server.ws_command_status = ws, live, ack
    server.PIVOT_LOG = pivotlog.PivotLog(Store(), server._pivot_summaries)
    try:
        res = asyncio.run(server._apply(None, "W-26", {"direction_key": "forward", "service_stop_angle": 150.6},
                                        confirm=True, poll=False, internal=True, source="program"))
        assert res["applied"] and len(sent) == 2
        pid, name, source, who, message, changes, expected, outcome, angle = recorded[-1]
        assert (pid, name, source, who, outcome, angle) == ("c6f28e12", "W-26", "program", "W-26 even up", "taken", 152.2)
        assert message == "Start forward, wet · 0.35″ (40%), service stop 150.6°", message
        assert expected == {"direction": "forward", "service_stop_deg": 150.6}
        asyncio.run(server._apply(None, "W-26", {"direction_key": "forward"}, confirm=True, poll=False, internal=True,
                                  source="pump-group", who="North wells"))
        assert recorded[-1][2:4] == ("pump-group", "North wells")
        # Practice sends nothing and logs nothing.
        n = len(recorded)
        asyncio.run(server._apply(None, "W-26", {"direction_key": "forward"}, confirm=True, poll=False, internal=True,
                                  send=False, source="program"))
        assert len(recorded) == n
    finally:
        server.ws_session, server.live_status, server.ws_command_status, server.PIVOT_LOG = saved


def test_pivot_live_and_applied():
    fake_fieldnet()
    polled = []

    async def poll(ws, eid, timeout=45.0):
        polled.append(eid)
        return {"status": "successful", "fresh": True}

    async def live(ws, eid, timeout=10.0):
        return {"operational_status": "forward-wet", "direction_key": "forward", "water_on": True, "current_angle": 63.3,
                "rate": 0.746, "rate_conversion_factor": 0.1417 / server.IN_PER_M, "full_run_time": 10.05 * 3600,
                "service_stop": 136.6, "service_stop_repeat_on": False, "current_plan_id": "manual-mode",
                "auto_reverse_on": False, "auto_restart_on": False, "accessory_1_on": True, "rtu_online": True}

    real_poll, server.ws_poll, server.live_status = server.ws_poll, poll, live
    try:
        r = client.get("/api/pivots/W-26/live", headers=KEY)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["fresh"] is False and not polled and d["settings"]["service_stop_deg"] == 136.6
        assert d["settings"]["accessory_1"] is True and d["settings"]["depth_in"] == 0.19 and d["panel"]["rtu_online"]
        assert client.get("/api/pivots/W-26/live?poll=1", headers=KEY).json()["fresh"] is True and polled
        assert client.get("/api/pivots/W-99/live", headers=KEY).status_code == 404
        assert client.get("/api/pivots/W-26/live").status_code == 401
    finally:
        server.ws_poll = real_poll
    r = client.get("/api/pivots/W-26/applied?from=2026-09-24&to=2026-11-04", headers=KEY)
    assert r.status_code == 200 and len(r.json()["wedges"]) == 4 and r.json()["covered_pct"] == 100.0, r.text
    assert client.get("/api/pivots/W-26/applied?from=yesterday", headers=KEY).status_code == 400


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok  {name}")
    print(f"{n} passed")
