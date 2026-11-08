"""Tests for the even-up planner. Run: python test_programs.py

W-26 is the real case from Nov 2026: both the numbers in the design brief and
the live FieldNET map read on Nov 5. No network or database needed."""

from __future__ import annotations

import random

from programs import FieldProfile, PivotState, Wedge, arc_contains, cw, degrees_to_go, has_passed, norm, plan_even_up

# W-26: 0.19"/pass at 75% → 0.1425" at 100%; 13.4 hr/circle at 75% → 10.05 hr at 100%.
W26 = FieldProfile(depth_full_in=0.1425, circle_hours_full=10.05, max_depth_in=0.35)


def steps_summary(plan):
    return [(s["direction"], s["water_on"], s["depth_in"], s["start_deg"], s["end_deg"], s["transition"]) for s in plan["steps"]]


# ── angles ──────────────────────────────────────────────────────────────────


def test_angles():
    assert norm(-5) == 355 and norm(360) == 0 and norm(725) == 5
    assert cw(355, 5) == 10 and cw(5, 355) == 350 and cw(42, 42) == 0
    assert arc_contains(355.5, 61.4, 10) and arc_contains(355.5, 61.4, 355.5)
    assert not arc_contains(355.5, 61.4, 61.4) and not arc_contains(355.5, 61.4, 100)
    assert arc_contains(0, 0, 123)  # a zero-length arc means the whole circle
    assert degrees_to_go(350, 10, True) == 20 and degrees_to_go(10, 350, False) == 20


def test_has_passed_through_zero():
    # Clockwise from 350° to 10°.
    assert not has_passed(350, 10, 355, True)
    assert not has_passed(350, 10, 5, True)
    assert has_passed(350, 10, 10, True) and has_passed(350, 10, 15, True)
    # Anticlockwise from 10° to 350°.
    assert not has_passed(10, 350, 5, False) and not has_passed(10, 350, 355, False)
    assert has_passed(10, 350, 350, False) and has_passed(10, 350, 345, False)
    assert has_passed(90, 90, 90, True)  # target = start: reached straight away


# ── W-26 ────────────────────────────────────────────────────────────────────


def test_w26_brief():
    """The brief: forward-wet at 60.6°, 0.19"/pass; intake limit 0.35". Expect exactly its three steps."""
    wedges = [Wedge(355.5, 58.4, 3.70), Wedge(58.4, 92.5, 3.50), Wedge(92.5, 136.6, 3.46), Wedge(136.6, 355.5, 2.99)]
    plan = plan_even_up(wedges, PivotState(60.6, "forward", 0.19, True), W26)
    assert steps_summary(plan) == [
        ("forward", True, 0.19, 60.6, 136.6, "flying"),  # keep going as it is: no speed change needed yet
        ("forward", True, 0.35, 136.6, 355.5, "stop"),
        ("reverse", True, 0.35, 355.5, 136.6, "end"),
    ], steps_summary(plan)
    assert plan["passes"] == 2 and plan["stops"] == 1
    assert abs(plan["steps"][0]["hours"] - 2.8) < 0.1  # ~8:25 PM from 5:36 PM
    assert abs(plan["steps"][1]["hours"] - 15.1) < 0.2 and abs(plan["steps"][2]["hours"] - 15.1) < 0.2
    assert 32.5 < plan["total_hours"] < 33.5  # "three commands across 33 hours"
    after = {w["from_deg"]: w["after_in"] for w in plan["wedges"]}
    assert after[355.5] == 3.70 and after[136.6] == 3.69 and after[92.5] == 3.65
    assert plan["range_after_in"] == [3.65, 3.70] and plan["spread_after_in"] == 0.05
    assert all(s["depth_in"] <= 0.35 for s in plan["steps"])


def test_w26_live_map_nov5():
    """The live map read on Nov 5 has its first boundary at 61.4° (not 58.4°): same three steps,
    and the 1.9° sliver right behind the pivot is reported, not chased round the circle."""
    wedges = [Wedge(61.4, 92.5, 3.50), Wedge(92.5, 136.6, 3.46), Wedge(136.6, 355.5, 2.99), Wedge(355.5, 61.4, 3.70)]
    profile = FieldProfile(depth_full_in=0.1417, circle_hours_full=10.05, max_depth_in=0.35)
    plan = plan_even_up(wedges, PivotState(63.3, "forward", 0.19, True), profile)
    assert [(d, w, dep, a, b, t) for d, w, dep, a, b, t in steps_summary(plan)] == [
        ("forward", True, 0.19, 63.3, 136.6, "flying"),
        ("forward", True, 0.35, 136.6, 355.5, "stop"),
        ("reverse", True, 0.35, 355.5, 136.6, "end"),
    ], steps_summary(plan)
    assert any("sliver" in n for n in plan["notes"])


def test_w26_with_a_stricter_intake_limit_takes_more_passes():
    wedges = [Wedge(355.5, 58.4, 3.70), Wedge(58.4, 92.5, 3.50), Wedge(92.5, 136.6, 3.46), Wedge(136.6, 355.5, 2.99)]
    tight = FieldProfile(depth_full_in=0.1425, circle_hours_full=10.05, max_depth_in=0.25)
    plan = plan_even_up(wedges, PivotState(60.6, "forward", 0.19, True), tight)
    assert plan["passes"] == 3  # 0.71" at ≤0.25" per pass
    assert all(s["depth_in"] is None or s["depth_in"] <= 0.25 for s in plan["steps"])
    assert plan["spread_after_in"] <= 0.06


# ── edge cases from the brief ───────────────────────────────────────────────

FLAT = FieldProfile(depth_full_in=0.14, circle_hours_full=10.0, max_depth_in=0.35)


def test_dry_region_wraps_through_zero_carrying_on():
    wedges = [Wedge(300, 60, 2.0), Wedge(60, 300, 3.0)]
    plan = plan_even_up(wedges, PivotState(250, "forward", 0.2, True), FLAT)
    s = steps_summary(plan)
    # Dry-run 50° on to 300°, then 3 passes of ~0.33" over 300→60, through 0°.
    assert s[0][:2] == ("forward", False) and s[0][3:5] == (250.0, 300.0)
    assert s[1][:2] == ("forward", True) and s[1][3:5] == (300.0, 60.0) and s[1][5] == "stop"
    assert s[2][0] == "reverse" and s[2][3:5] == (60.0, 300.0)
    assert s[3][0] == "forward" and s[3][3:5] == (300.0, 60.0) and s[3][5] == "end"
    assert plan["passes"] == 3 and plan["spread_after_in"] <= 0.05


def test_dry_region_wraps_through_zero_turning_round():
    """From 100°, the dry arc is 40° back but 200° on: turning round wins despite the restart."""
    wedges = [Wedge(300, 60, 2.0), Wedge(60, 300, 3.0)]
    plan = plan_even_up(wedges, PivotState(100, "forward", 0.2, True), FLAT)
    s = steps_summary(plan)
    assert s[0][:2] == ("reverse", False) and s[0][3:5] == (100.0, 60.0)
    assert s[1][:2] == ("reverse", True) and s[1][3:5] == (60.0, 300.0)  # through 0°
    assert plan["passes"] == 3 and plan["spread_after_in"] <= 0.05
    assert plan["total_hours"] < 26  # vs ~29.7 carrying on round


def test_pivot_starting_inside_the_dry_region():
    wedges = [Wedge(355.5, 58.4, 3.70), Wedge(58.4, 92.5, 3.50), Wedge(92.5, 136.6, 3.46), Wedge(136.6, 355.5, 2.99)]
    plan = plan_even_up(wedges, PivotState(200, "forward", 0.19, True), W26)
    assert plan["steps"] and plan["steps"][0]["start_deg"] == 200.0
    assert_invariants(plan, W26)


def test_turns_round_when_the_work_is_just_behind():
    wedges = [Wedge(60, 95, 2.7), Wedge(95, 60, 3.0)]
    plan = plan_even_up(wedges, PivotState(100, "forward", 0.2, True), FLAT)
    assert plan["steps"][0]["direction"] == "reverse"
    assert any("Turning round first" in n for n in plan["notes"])
    assert plan["total_hours"] < 3  # not a trip right round the circle


def test_two_separate_dry_regions_in_one_sweep():
    wedges = [Wedge(0, 90, 2.6), Wedge(90, 180, 3.0), Wedge(180, 270, 2.6), Wedge(270, 0, 3.0)]
    plan = plan_even_up(wedges, PivotState(0, "forward", None, False), FLAT)
    assert steps_summary(plan) == [
        ("forward", True, 0.35, 0.0, 90.0, "flying"),
        ("forward", False, None, 90.0, 180.0, "flying"),  # dry-run at 100% over the wet quarter
        ("forward", True, 0.35, 180.0, 270.0, "end"),
    ], steps_summary(plan)


def test_pivot_on_a_wedge_boundary():
    wedges = [Wedge(0, 180, 2.8), Wedge(180, 0, 3.0)]
    plan = plan_even_up(wedges, PivotState(180, "reverse", 0.2, True), FLAT)
    assert plan["steps"][0]["start_deg"] == 180.0
    assert_invariants(plan, FLAT)


def test_already_even():
    plan = plan_even_up([Wedge(0, 180, 3.0), Wedge(180, 0, 3.02)], PivotState(10, "forward", 0.2, True), FLAT)
    assert plan["steps"] == [] and "Nothing to do" in plan["notes"][0]


def test_shortfall_smaller_than_half_a_pass_cant_be_helped():
    plan = plan_even_up([Wedge(0, 180, 3.0), Wedge(180, 0, 2.94)], PivotState(10, "forward", 0.2, True), FLAT)
    assert plan["steps"] == []
    assert any("less than half a pass" in n for n in plan["notes"])


def test_refuses_impossible_or_unsupported_inputs():
    for wedges, profile, fragment in [
        ([Wedge(0, 180, 3.0)], FLAT, "covers only 50%"),
        ([Wedge(0, 180, 3.0), Wedge(180, 0, 2.0)], FieldProfile(0.2, 10, 0.15), "lighter than a pass at 100%"),
    ]:
        try:
            plan_even_up(wedges, PivotState(0, "stopped"), profile)
        except ValueError as e:
            assert fragment in str(e), e
        else:
            raise AssertionError(f"expected refusal: {fragment}")


def test_stopped_pivot_has_no_turn_round_penalty():
    wedges = [Wedge(60, 95, 2.7), Wedge(95, 60, 3.0)]
    plan = plan_even_up(wedges, PivotState(100, "stopped"), FLAT)
    assert not any("Turning round" in n for n in plan["notes"]) and plan["total_hours"] < 3


def test_reverse_is_anticlockwise():
    """Forward is clockwise on every pivot, so going round anticlockwise is reverse."""
    wedges = [Wedge(60, 95, 2.7), Wedge(95, 60, 3.0)]
    plan = plan_even_up(wedges, PivotState(100, "reverse", 0.2, True), FLAT)
    assert steps_summary(plan)[0][:2] == ("reverse", False) and plan["steps"][0]["end_deg"] == 95.0
    assert not any("Turning round" in n for n in plan["notes"])  # already going the quick way
    assert_invariants(plan, FLAT)


def test_intake_limit_defaults_to_0_3():
    profile = FieldProfile(depth_full_in=0.14, circle_hours_full=10.0)
    assert profile.max_depth_in == 0.3
    plan = plan_even_up([Wedge(0, 180, 2.5), Wedge(180, 0, 3.0)], PivotState(0, "forward", 0.2, True), profile)
    assert max(s["depth_in"] or 0 for s in plan["steps"]) <= 0.3


# ── invariants over random fields ───────────────────────────────────────────


def assert_invariants(plan, profile):
    steps = plan["steps"]
    for a, b in zip(steps, steps[1:]):
        assert abs(cw(a["end_deg"], b["start_deg"])) < 0.15 or abs(cw(b["start_deg"], a["end_deg"])) < 0.15, (a, b)
        if a["transition"] == "flying":
            assert a["direction"] == b["direction"], (a, b)
        if a["transition"] == "stop":
            assert a["direction"] != b["direction"], (a, b)
    for s in steps:
        if s["water_on"]:
            assert profile.depth_full_in - 1e-9 <= s["depth_in"] <= profile.max_depth_in + 1e-9, s
        else:
            assert s["depth_in"] is None and s["speed_pct"] == 100.0
    if steps:
        assert steps[-1]["transition"] == "end"
    assert plan["spread_after_in"] <= plan["spread_before_in"] + 1e-9


def test_random_fields_keep_the_safety_rules():
    rng = random.Random(42)
    for _ in range(400):
        n = rng.randint(2, 7)
        cuts = sorted(round(rng.uniform(0, 360), 1) for _ in range(n))
        if len(set(cuts)) < n:
            continue
        wedges = [Wedge(cuts[i], cuts[(i + 1) % n], round(rng.uniform(2.0, 4.0), 2)) for i in range(n)]
        profile = FieldProfile(depth_full_in=rng.uniform(0.08, 0.2), circle_hours_full=rng.uniform(8, 30),
                               max_depth_in=rng.uniform(0.25, 0.8))
        state = PivotState(round(rng.uniform(0, 360), 1), rng.choice(["forward", "reverse", "stopped"]),
                           round(rng.uniform(0.15, 0.4), 2), rng.random() < 0.5)
        tol = rng.choice([0.03, 0.05, 0.1])
        plan = plan_even_up(wedges, state, profile, tolerance_in=tol)
        assert_invariants(plan, profile)
        # Accuracy: every wedge ends within what physics allows of the target: off by at most the
        # tolerance, or half the lightest pass (a shortfall smaller than that can't be helped, and
        # the lightest pass can overshoot by that much). 0.02" covers rounding depths to 0.01".
        if not any("sliver" in n for n in plan["notes"]):
            slack = max(tol, profile.depth_full_in / 2) + 0.02
            for w in plan["wedges"]:
                if w["share_pct"] >= 1.0:
                    assert abs(w["after_in"] - plan["target_in"]) <= slack, (w, plan)


# ── Shuttle ───────────────────────────────────────────────────────────


def _shuttle(angle, passes, **kw):
    from programs import plan_shuttle
    return plan_shuttle(kw.pop("a", 100.0), kw.pop("b", 200.0), angle, passes, kw.pop("wet", True), kw.pop("depth", 0.5),
                        kw.pop("full", 0.14), kw.pop("hours", 10.0), **kw)


def test_shuttle_passes_bounce_between_the_stops():
    p = _shuttle(100.0, 3)
    assert [(s["direction"], s["start_deg"], s["end_deg"], s["transition"]) for s in p["steps"]] == [
        ("forward", 100.0, 200.0, "stop"), ("reverse", 200.0, 100.0, "stop"), ("forward", 100.0, 200.0, "end")]
    assert all(s["service_stop_deg"] == s["end_deg"] for s in p["steps"])
    # 100° at 0.5" (28% speed) on a 10 h circle: 100/360 * 10 / 0.28 = 9.92 h a pass
    assert abs(p["steps"][0]["hours"] - 9.92) < 0.02 and p["steps"][0]["speed_pct"] == 28.0
    assert p["kind"] == "shuttle" and p["passes"] == 3 and p["shuttle"]["leave_on"] is False


def test_one_pass_goes_across_and_stops():
    p = _shuttle(200.0, 1)  # at the second stop: across to the first
    assert [(s["direction"], s["end_deg"], s["transition"]) for s in p["steps"]] == [("reverse", 100.0, "end")]


def test_shuttle_from_the_middle_and_over_north():
    p = _shuttle(130.0, 2)  # 30° in: the farther stop is 200°
    assert [(s["direction"], s["arc_deg"]) for s in p["steps"]] == [("forward", 70.0), ("reverse", 100.0)]
    assert any("part pass" in n for n in p["notes"])
    q = _shuttle(15.0, 2, a=350.0, b=20.0)  # the strip crosses north; 350° is the farther stop
    assert [(s["direction"], s["start_deg"], s["end_deg"], s["arc_deg"]) for s in q["steps"]] == [
        ("reverse", 15.0, 350.0, 25.0), ("forward", 350.0, 20.0, 30.0)]
    assert _shuttle(5.0, 1, a=350.0, b=20.0)["steps"][0]["direction"] == "forward"  # halfway: forward
    r = _shuttle(130.0, 1, first="reverse")
    assert r["steps"][0]["direction"] == "reverse" and r["steps"][0]["end_deg"] == 100.0


def test_shuttle_left_on_plans_weeks_ahead():
    p = _shuttle(100.0, None, wet=False, depth=None)
    assert p["shuttle"]["leave_on"] and p["total_hours"] >= 30 * 24 and p["steps"][-1]["transition"] == "end"
    assert all(s["speed_pct"] == 100.0 and s["depth_in"] is None for s in p["steps"])
    assert {s["direction"] for s in p["steps"][:2]} == {"forward", "reverse"}


def test_shuttle_refuses_what_it_cant_do():
    for kw, angle, passes in (({"a": 100.0, "b": 100.0}, 100.0, 2), ({}, 300.0, 2), ({}, 100.0, 0),
                              ({"depth": 0.1}, 100.0, 2), ({"hours": 0}, 100.0, 2), ({"first": "sideways"}, 100.0, 1)):
        try:
            _shuttle(angle, passes, **kw)
        except ValueError:
            continue
        raise AssertionError(f"should have refused {kw} at {angle} x{passes}")
    assert _shuttle(201.5, 1)["steps"][0]["end_deg"] == 100.0  # within 2° of a stop counts as at it


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok  {name}")
    print(f"{n} passed")
