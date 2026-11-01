"""Tests for the pump group watcher's decisions. Run: python test_watcher.py

Uses every operational status seen on the Hyer Farms FieldNET account plus
the fault statuses FieldNET documents. No network or database needed."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from watcher import Group, Member, WindowState, classify, decide, trigger_for, window_key

TZ = ZoneInfo("America/Los_Angeles")


def piv(pid: str, status: str, online: str = "online", direction: str | None = None, water: bool | None = None):
    if direction is None:
        direction = status.split("-")[0] if status.startswith(("forward", "reverse")) else "stopped"
    return {"id": pid, "name": pid, "operational_status": status, "communication_status": online,
            "direction": direction, "is_water_on": status.endswith("-wet") if water is None else water}


def test_classify_real_statuses():
    cases = {
        ("forward-wet", "online", None): "running-wet",
        ("reverse-wet", "online", None): "running-wet",
        ("forward", "online", None): "running-dry",
        ("reverse", "online", None): "running-dry",
        ("stopped", "online", None): "stopped",
        ("stopped", "unreliable", None): "stopped",
        ("stopped-wet", "online", None): "stopped",
        ("service-stop", "online", None): "finished",
        ("soft-barrier-stop", "online", None): "finished",
        ("auto-stop-at-barrier", "online", None): "finished",
        ("soft-barrier-stop", "offline", None): "offline",
        ("powered-off", "offline", None): "fault",
        ("not-connected", "offline", None): "offline",
        ("low-pressure-shutdown", "online", None): "low-pressure",
        ("high-pressure-shutdown", "online", None): "fault",
        ("safety-shutdown", "online", None): "fault",
        ("low-pressure-warning", "online", "forward"): "running-dry",
    }
    for (status, online, direction), want in cases.items():
        got = classify(piv("x", status, online, direction))
        assert got == want, f"{status}/{online}: {got} != {want}"
    assert classify(piv("x", "low-pressure-warning", "online", "forward", water=True)) == "running-wet"


def test_triggers():
    assert trigger_for("running-wet", "fault") == "fault"
    assert trigger_for("running-wet", "low-pressure") == "low-pressure"
    assert trigger_for("running-wet", "finished") == "finished"
    assert trigger_for("running-wet", "stopped") is None  # stopped by hand
    assert trigger_for("running-wet", "offline") is None  # lost contact: not chosen
    assert trigger_for("running-dry", "finished") is None  # wasn't using water
    assert trigger_for(None, "fault") is None  # first look in the window


def group(mode: str = "live") -> Group:
    return Group(1, "Niebar booster", mode, [(d, 20 * 60, (d + 1) % 7, 6 * 60) for d in range(7)], [
        Member("S1", "W-1", "scheduled"),
        Member("S2", "W-2", "scheduled"),
        Member("F1", "W-5", "fallback", 1, "forward", 50.0, None),
        Member("F2", "W-6", "fallback", 2, "reverse", None, 0.5),
    ])


def run(state, g, pivots):
    return decide(g, state, pivots, 0.0)


def test_fault_starts_first_ready_fallback_once():
    g, st = group(), WindowState("k")
    ev, starts = run(st, g, [piv("S1", "forward-wet"), piv("S2", "forward-wet"), piv("F1", "stopped"), piv("F2", "stopped")])
    assert not ev and not starts  # first look only records state
    ev, starts = run(st, g, [piv("S1", "high-pressure-shutdown"), piv("S2", "forward-wet"), piv("F1", "stopped"), piv("F2", "stopped")])
    assert [s.member.name for s in starts] == ["W-5"] and starts[0].reason == "fault"
    # Same stop seen again: nothing more.
    ev, starts = run(st, g, [piv("S1", "high-pressure-shutdown"), piv("S2", "forward-wet"), piv("F1", "forward-wet"), piv("F2", "stopped")])
    assert not starts
    # Second pivot finishes its run → next fallback, W-5 is used already.
    ev, starts = run(st, g, [piv("S1", "high-pressure-shutdown"), piv("S2", "service-stop"), piv("F1", "forward-wet"), piv("F2", "stopped")])
    assert [s.member.name for s in starts] == ["W-6"] and starts[0].reason == "finished"


def test_skips_offline_and_running_fallbacks_and_alerts_when_none():
    g, st = group(), WindowState("k")
    run(st, g, [piv("S1", "forward-wet"), piv("F1", "stopped", "offline"), piv("F2", "forward-wet")])
    ev, starts = run(st, g, [piv("S1", "service-stop"), piv("F1", "stopped", "offline"), piv("F2", "forward-wet")])
    assert not starts and [e.kind for e in ev] == ["no-fallback"] and ev[0].alert


def test_powered_off_fallback_not_started():
    g, st = group(), WindowState("k")
    run(st, g, [piv("S1", "forward-wet"), piv("F1", "powered-off", "online"), piv("F2", "stopped")])
    _, starts = run(st, g, [piv("S1", "service-stop"), piv("F1", "powered-off", "online"), piv("F2", "stopped")])
    assert [s.member.name for s in starts] == ["W-6"]


def test_low_pressure_retries_once_then_alerts():
    g, st = group(), WindowState("k")
    run(st, g, [piv("S1", "forward-wet"), piv("S2", "forward-wet"), piv("F1", "stopped"), piv("F2", "stopped")])
    _, starts = run(st, g, [piv("S1", "low-pressure-shutdown"), piv("S2", "forward-wet"), piv("F1", "stopped"), piv("F2", "stopped")])
    assert [s.member.name for s in starts] == ["W-5"]
    ev, starts = run(st, g, [piv("S1", "low-pressure-shutdown"), piv("S2", "low-pressure-shutdown"), piv("F1", "stopped"), piv("F2", "stopped")])
    assert not starts and [e.kind for e in ev] == ["gave-up"] and ev[0].alert


def test_started_fallback_is_watched_and_can_chain():
    g, st = group(), WindowState("k")
    run(st, g, [piv("S1", "forward-wet"), piv("F1", "stopped"), piv("F2", "stopped")])
    _, starts = run(st, g, [piv("S1", "service-stop"), piv("F1", "stopped"), piv("F2", "stopped")])
    st.watched.add(starts[0].member.pivot_id)  # the runner does this after a live start
    st.pending[starts[0].member.pivot_id] = 600.0
    ev, _ = run(st, g, [piv("S1", "service-stop"), piv("F1", "forward-wet"), piv("F2", "stopped")])
    assert [e.kind for e in ev] == ["confirmed"]
    _, starts = run(st, g, [piv("S1", "service-stop"), piv("F1", "service-stop"), piv("F2", "stopped")])
    assert [s.member.name for s in starts] == ["W-6"]


def test_no_response_alert():
    g, st = group(), WindowState("k")
    st.pending["F1"] = 100.0
    ev, _ = decide(g, st, [piv("F1", "stopped")], 101.0)
    assert [e.kind for e in ev] == ["no-response"] and ev[0].alert


def test_hand_stop_is_logged_once_not_triggered():
    g, st = group(), WindowState("k")
    run(st, g, [piv("S1", "forward-wet"), piv("F1", "stopped")])
    ev, starts = run(st, g, [piv("S1", "stopped"), piv("F1", "stopped")])
    assert not starts and [e.kind for e in ev] == ["no-trigger"] and not ev[0].alert


def test_windows():
    nightly = Group(1, "g", "live", [(d, 20 * 60, (d + 1) % 7, 6 * 60) for d in range(7)], [])
    weekend = Group(2, "g", "live", [(5, 18 * 60, 1, 6 * 60)], [])  # Fri 6 pm → Mon 6 am
    t = lambda s: datetime.fromisoformat(s).replace(tzinfo=TZ)  # noqa: E731
    assert window_key(nightly, t("2026-11-02T21:00")) == "1@2026-11-02T20:00"  # Mon 9 pm
    assert window_key(nightly, t("2026-11-03T05:59")) == "1@2026-11-02T20:00"  # same night
    assert window_key(nightly, t("2026-11-03T06:00")) is None
    assert window_key(nightly, t("2026-11-03T12:00")) is None
    assert window_key(weekend, t("2026-11-06T17:59")) is None  # Fri before
    assert window_key(weekend, t("2026-11-06T18:00")) == "2@2026-11-06T18:00"
    assert window_key(weekend, t("2026-11-08T12:00")) == "2@2026-11-06T18:00"  # Sunday
    assert window_key(weekend, t("2026-11-09T05:00")) == "2@2026-11-06T18:00"  # Mon early (wraps the week)
    assert window_key(weekend, t("2026-11-09T07:00")) is None


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok  {name}")
    print(f"{n} passed")
