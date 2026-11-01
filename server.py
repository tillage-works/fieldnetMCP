"""
FieldNET read-only MCP server
=============================

Wraps Lindsay FieldNET NextGen's internal REST API (https://v2.api.myfieldnet.com)
as a small set of read-only MCP tools, so Claude can answer questions like
"how much water has M-32 had since Oct 1" from any chat or Cowork session.

Deploy on Render (or anywhere) and add it to Claude as a custom connector
with "No sign in" + request header  X-API-Key: <MCP_AUTH_TOKEN>.

Environment variables
---------------------
FIELDNET_REFRESH_TOKEN   required  refresh token copied from the web app's localStorage
MCP_AUTH_TOKEN           required  any long random string; Claude sends it in the X-API-Key header
FIELDNET_ORG_ID          optional  organization GUID; auto-detected from the account if unset
FIELDNET_TZ              optional  IANA timezone for date ranges (default America/Los_Angeles)
FIELDNET_CLIENT_ID       optional  defaults to the NextGen web app's public client id
PORT                     optional  Render sets this automatically

Only GET endpoints are used, unless MCP_WRITE_TOKEN (control tools) or the pump
group watcher (WATCHER_ENABLED, see watcher.py) is switched on.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import json

import httpx
import websockets
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API = os.environ.get("FIELDNET_API_URL", "https://v2.api.myfieldnet.com")
CLIENT_ID = os.environ.get("FIELDNET_CLIENT_ID", "1f5ef800-1061-4146-95b1-42139f1f187a")
ORG_ID = os.environ.get("FIELDNET_ORG_ID", "")
REFRESH_TOKEN = os.environ.get("FIELDNET_REFRESH_TOKEN", "")
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
# Optional. When set, the control tools below are enabled, and only for requests
# that present THIS key (not the read-only key). Leave unset for a read-only server.
MCP_WRITE_TOKEN = os.environ.get("MCP_WRITE_TOKEN", "")
TZ = ZoneInfo(os.environ.get("FIELDNET_TZ", "America/Los_Angeles"))
WS_URL = os.environ.get("FIELDNET_WS_URL", "wss://v2.api.myfieldnet.com/websocket")

MM_PER_IN = 25.4
PSI_PER_BAR = 14.5038
M2_PER_AC = 4046.856

# ---------------------------------------------------------------------------
# Auth / token manager
# ---------------------------------------------------------------------------


class TokenManager:
    """Holds the refresh token and mints 10-minute access tokens on demand."""

    def __init__(self, refresh_token: str) -> None:
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def access_token(self, client: httpx.AsyncClient, force: bool = False) -> str:
        async with self._lock:
            if not force and self._access_token and time.time() < self._expires_at - 60:
                return self._access_token
            if not self._refresh_token:
                raise RuntimeError("FIELDNET_REFRESH_TOKEN is not set")
            r = await client.post(
                f"{API}/connect/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": CLIENT_ID,
                    "refresh_token": self._refresh_token,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=20,
            )
            if r.status_code != 200:
                raise RuntimeError(
                    f"FieldNET token refresh failed ({r.status_code}). "
                    "The refresh token may have expired; copy a fresh one from the web app."
                )
            body = r.json()
            self._access_token = body["access_token"]
            self._expires_at = time.time() + int(body.get("expires_in", 600))
            # Refresh tokens did not rotate in testing, but handle it if they start to.
            if body.get("refresh_token") and body["refresh_token"] != self._refresh_token:
                self._refresh_token = body["refresh_token"]
                print("NOTE: FieldNET issued a new refresh token; update FIELDNET_REFRESH_TOKEN "
                      "in the environment before the next restart.", flush=True)
            return self._access_token


tokens = TokenManager(REFRESH_TOKEN)
_http = httpx.AsyncClient(timeout=30)


async def api_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET from the FieldNET API with automatic token refresh on 401."""
    for attempt in (0, 1):
        tok = await tokens.access_token(_http, force=bool(attempt))
        r = await _http.get(f"{API}{path}", params=params, headers={"Authorization": f"Bearer {tok}"})
        if r.status_code == 401 and attempt == 0:
            continue
        r.raise_for_status()
        return r.json() if r.content else None
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_pivot_cache: dict[str, Any] = {"at": 0.0, "list": []}


async def org_id() -> str:
    """Return FIELDNET_ORG_ID, or discover it from the account's organizations."""
    global ORG_ID
    if ORG_ID:
        return ORG_ID
    orgs = await api_get("/organizations") or []
    if isinstance(orgs, dict):
        orgs = orgs.get("items", [])
    customers = [o for o in orgs if "customer" in (o.get("type") or [])] or orgs
    if not customers:
        raise RuntimeError("No FieldNET organization found for this account; set FIELDNET_ORG_ID.")
    ORG_ID = customers[0]["id"]
    names = ", ".join(f"{o.get('name')} ({o.get('id')})" for o in customers)
    print(f"Using FieldNET organization {customers[0].get('name')} ({ORG_ID}). "
          f"Available: {names}", flush=True)
    return ORG_ID


async def get_pivots(max_age_s: int = 60) -> list[dict[str, Any]]:
    if time.time() - _pivot_cache["at"] > max_age_s:
        data = await api_get("/irrigation-controllers", {"organization-id": await org_id(), "page-size": 1000})
        _pivot_cache["list"] = data if isinstance(data, list) else data.get("items", [])
        _pivot_cache["at"] = time.time()
    return _pivot_cache["list"]


async def resolve_pivot(name_or_id: str) -> dict[str, Any]:
    pivots = await get_pivots(max_age_s=600)
    key = name_or_id.strip().lower().replace(" ", "")
    for p in pivots:
        if p.get("id", "").lower() == key:
            return p
    for p in pivots:
        if (p.get("name") or "").lower().replace(" ", "") == key:
            return p
    names = ", ".join(sorted(p.get("name") or "?" for p in pivots))
    raise ValueError(f"No pivot named '{name_or_id}'. Known pivots: {names}")


def mm_to_in(v: float | None) -> float | None:
    return None if v is None else round(v / MM_PER_IN, 2)


def bar_to_psi(v: float | None) -> float | None:
    return None if v is None else round(v * PSI_PER_BAR, 0)


def pct(v: float | None) -> float | None:
    return None if v is None else round(v * 100, 1)


def _parse_day(s: str | None, default: date) -> date:
    return default if not s else date.fromisoformat(s)


def _range(from_date: str | None, to_date: str | None, default_days: int) -> tuple[date, date]:
    today = datetime.now(TZ).date()
    end = _parse_day(to_date, today)
    start = _parse_day(from_date, end - timedelta(days=default_days))
    return start, end


def _iso_bounds(start: date, end: date) -> tuple[str, str]:
    s = datetime.combine(start, datetime.min.time(), TZ)
    e = datetime.combine(end, datetime.max.time().replace(microsecond=999000), TZ)
    return s.isoformat(timespec="milliseconds"), e.isoformat(timespec="milliseconds")


def summarize_pivot(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": p.get("name"),
        "id": p.get("id"),
        "status": p.get("operational_status"),
        "online": p.get("communication_status"),
        "direction": p.get("direction"),
        "angle_deg": p.get("pivot_angle"),
        "speed_pct": pct(p.get("speed")),
        "depth_in": mm_to_in(p.get("application_depth")),
        "pressure_psi": bar_to_psi(p.get("pressure")),
        "water_on": p.get("is_water_on"),
        "endgun_1_on": p.get("is_endgun_1_on"),
        "service_stop_deg": p.get("service_stop"),
        "plan": p.get("plan"),
        "status_since": p.get("last_updated"),
    }


def wedges_from_geojson(fc: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn FieldNET's applied-irrigation polygons into (start_deg, end_deg, depth) wedges."""
    feats = fc.get("features", [])
    pts: list[tuple[float, float]] = []

    def rings(g: dict[str, Any]):
        if g["type"] == "Polygon":
            yield from g["coordinates"]
        elif g["type"] == "MultiPolygon":
            for poly in g["coordinates"]:
                yield from poly

    for f in feats:
        for ring in rings(f["geometry"]):
            pts.extend((x, y) for x, y in ring)
    if not pts:
        return []
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    k = math.cos(math.radians(cy))
    R = max((max(xs) - min(xs)) / 2 * k, (max(ys) - min(ys)) / 2)

    out: list[dict[str, Any]] = []
    for f in feats:
        depth_mm = f.get("properties", {}).get("depth")
        g = f["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        for poly in polys:
            bearings: list[float] = []
            for ring in poly:
                for x, y in ring:
                    dx, dy = (x - cx) * k, y - cy
                    if math.hypot(dx, dy) > 0.5 * R:
                        bearings.append((math.degrees(math.atan2(dx, dy)) + 360) % 360)
            if not bearings:
                continue
            bearings.sort()
            gaps = [((bearings[(i + 1) % len(bearings)] - b) % 360) for i, b in enumerate(bearings)]
            gi = max(range(len(gaps)), key=gaps.__getitem__)
            start = bearings[(gi + 1) % len(bearings)]
            end = bearings[gi]
            span = (end - start) % 360 or 360.0
            out.append({
                "from_deg": round(start, 1),
                "to_deg": round(end, 1),
                "span_deg": round(span, 1),
                "share_pct": round(span / 3.6, 1),
                "depth_in": mm_to_in(depth_mm),
                "depth_mm": depth_mm,
            })
    out.sort(key=lambda w: w["from_deg"])
    return out


def wet_runs(samples: list[dict[str, Any]], wet_bar: float = 0.8) -> list[dict[str, Any]]:
    """Split a pressure/position series into runs where pressure indicates water on."""
    runs: list[dict[str, Any]] = []
    cur: list[dict[str, Any]] = []
    for s in samples:
        wet = (s.get("pressure") or 0) >= wet_bar
        if wet:
            cur.append(s)
        elif cur:
            runs.append(_run_summary(cur, ended=True))
            cur = []
    if cur:
        runs.append(_run_summary(cur, ended=False))
    return runs


def _run_summary(pts: list[dict[str, Any]], ended: bool) -> dict[str, Any]:
    travel = 0.0
    for a, b in zip(pts, pts[1:]):
        d = (b.get("position") or 0) - (a.get("position") or 0)
        if d > 180:
            d -= 360
        if d < -180:
            d += 360
        travel += d
    t0 = datetime.fromisoformat(pts[0]["date"])
    t1 = datetime.fromisoformat(pts[-1]["date"])
    return {
        "start": pts[0]["date"],
        "start_deg": pts[0].get("position"),
        "end": pts[-1]["date"],
        "end_deg": pts[-1].get("position"),
        "hours": round((t1 - t0).total_seconds() / 3600, 1),
        "net_travel_deg": round(travel),
        "direction": "forward" if travel > 0 else ("reverse" if travel < 0 else "none"),
        "ongoing": not ended,
    }


# ---------------------------------------------------------------------------
# MCP server + tools
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "FieldNET",
    instructions=(
        "Read-only access to Lindsay FieldNET pivot data for this farm. Pivots are named like "
        "M-31, W-2, R-12. Angles are degrees clockwise from north (0 = north). Depths are inches, "
        "pressure psi, speed percent of full. Dates are YYYY-MM-DD in America/Los_Angeles. "
        + ("Connectors holding the write key can control pivots with pivot_control (and stop_pivot). "
           "FieldNET applies the whole control panel at once, so put ALL requested changes for a pivot "
           "in ONE pivot_control call; never split them across calls. Each call previews first and "
           "only sends when repeated with confirm=true and the preview_token. Always show the user "
           "the preview (including 'pivot_will') and get a clear yes before confirming."
           if MCP_WRITE_TOKEN else "Nothing here can control equipment.")
    ),
    stateless_http=True,
    json_response=True,
    # The SDK's DNS-rebinding guard only trusts localhost Host headers by default,
    # which returns 421 to any public hostname. The API key gates access instead.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
async def list_pivots(status: str | None = None, watering_only: bool = False) -> list[dict[str, Any]]:
    """List every pivot with its current state (status, direction, angle, speed %, depth in, psi, water on).

    Args:
        status: optional filter, e.g. 'forward-wet', 'stopped', 'low-pressure-shutdown'.
        watering_only: if true, only pivots with water on.
    """
    pivots = await get_pivots(max_age_s=30)
    rows = [summarize_pivot(p) for p in pivots]
    if status:
        rows = [r for r in rows if (r["status"] or "") == status]
    if watering_only:
        rows = [r for r in rows if r["water_on"]]
    rows.sort(key=lambda r: r["name"] or "")
    return rows


@mcp.tool()
async def pivot_status(pivot: str, poll: bool = False) -> dict[str, Any]:
    """Current state of one pivot by name (e.g. 'W-2'): status, direction, angle, speed, depth, psi, service stop.

    FieldNET only refreshes a pivot's position and status when the panel reports
    in, so the cached reading can be minutes old. poll=true asks the panel for a
    fresh report first (like the Poll/refresh button in the app; takes ~5-45 s).
    Use it whenever the current angle or status matters."""
    p = await resolve_pivot(pivot)
    poll_result = None
    if poll:
        poll_result = await poll_pivot(p["id"])
    fresh = await get_pivots(max_age_s=0 if poll else 30)
    p = next((x for x in fresh if x["id"] == p["id"]), p)
    out = summarize_pivot(p)
    out["lat"] = p.get("latitude")
    out["lon"] = p.get("longitude")
    out["depth_at_full_speed_in"] = p.get("application_depth_at_full_speed")
    out["auto_reverse"] = p.get("is_auto_reverse_on")
    out["auto_restart"] = p.get("is_auto_restart_on")
    if poll_result is not None:
        out["poll"] = poll_result
    return out


@mcp.tool()
async def pivot_track(pivot: str, from_date: str | None = None, to_date: str | None = None) -> dict[str, Any]:
    """Position and pressure samples for a pivot over a date range, grouped into wet runs.

    Use this to see where laps started/stopped and which direction it ran.
    Defaults to the last 7 days. Dates are YYYY-MM-DD.
    """
    p = await resolve_pivot(pivot)
    start, end = _range(from_date, to_date, 7)
    s_iso, e_iso = _iso_bounds(start, end)
    samples = await api_get(f"/irrigation-systems/{p['id']}/pressure-position-report", {"from": s_iso, "to": e_iso})
    samples = [s for s in (samples or []) if s.get("position") is not None]
    for s in samples:
        s["pressure_psi"] = bar_to_psi(s.get("pressure"))
    runs = wet_runs(samples)
    return {
        "pivot": p.get("name"),
        "from": start.isoformat(),
        "to": end.isoformat(),
        "sample_count": len(samples),
        "wet_runs": [r for r in runs if r["hours"] >= 0.5 or r["ongoing"]],
        "latest": samples[-1] if samples else None,
    }


@mcp.tool()
async def applied_irrigation(pivot: str, from_date: str | None = None, to_date: str | None = None) -> dict[str, Any]:
    """Water applied per wedge of the circle over a date range (the 'Applied Irrigation' map as numbers).

    Returns wedges with from/to bearing, share of field, and depth in inches. Defaults to last 7 days.
    """
    p = await resolve_pivot(pivot)
    start, end = _range(from_date, to_date, 7)
    s_iso, e_iso = _iso_bounds(start, end)
    fc = await api_get(f"/irrigation-systems/{p['id']}/applied-irrigation", {"from": s_iso, "to": e_iso, "use-season": "false"})
    wedges = wedges_from_geojson(fc or {})
    covered = sum(w["span_deg"] for w in wedges)
    avg = (sum(w["span_deg"] * (w["depth_in"] or 0) for w in wedges) / covered) if covered else 0
    return {
        "pivot": p.get("name"),
        "from": start.isoformat(),
        "to": end.isoformat(),
        "covered_pct": round(covered / 3.6, 1),
        "avg_depth_in_on_covered": round(avg, 2),
        "min_depth_in": min((w["depth_in"] or 0 for w in wedges), default=0),
        "max_depth_in": max((w["depth_in"] or 0 for w in wedges), default=0),
        "wedges": wedges,
    }


@mcp.tool()
async def daily_report(pivot: str, from_date: str | None = None, to_date: str | None = None) -> dict[str, Any]:
    """Daily hours run and acres irrigated for a pivot. Defaults to last 14 days.

    Note: pivots set up as hour counters report volume/flow as 0 (no flow rate configured).
    """
    p = await resolve_pivot(pivot)
    start, end = _range(from_date, to_date, 14)
    s_iso, e_iso = _iso_bounds(start, end)
    rows = await api_get(
        f"/irrigation-systems/{p['id']}/daily-report",
        {"from": s_iso, "to": e_iso, "timezone": str(TZ), "from-date": start.isoformat(), "to-date": end.isoformat()},
    )
    days = []
    for r in rows or []:
        days.append({
            "date": (r.get("date") or "")[:10],
            "hours_run": round(r.get("hours_run") or 0, 1),
            "acres_irrigated": round((r.get("area_applied") or 0) / M2_PER_AC, 1),
            "avg_depth_in": mm_to_in(r.get("average_depth")) if r.get("average_depth") else 0,
            "volume_ac_ft": r.get("volume_applied"),
        })
    return {
        "pivot": p.get("name"),
        "from": start.isoformat(),
        "to": end.isoformat(),
        "total_hours": round(sum(d["hours_run"] for d in days), 1),
        "days": days,
    }


@mcp.tool()
async def irrigation_overview(from_date: str | None = None, to_date: str | None = None, pivots: list[str] | None = None) -> list[dict[str, Any]]:
    """Hours run, irrigated acres, volume and depth per pivot over a range (the Irrigation Overview report).

    Defaults to last 30 days and all pivots. Pass a list of names to limit it (faster).
    """
    start, end = _range(from_date, to_date, 30)
    s_iso, e_iso = _iso_bounds(start, end)
    org = await org_id()
    all_p = await get_pivots(max_age_s=600)
    targets = all_p if not pivots else [await resolve_pivot(n) for n in pivots]
    sem = asyncio.Semaphore(8)

    async def one(p: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            try:
                r = await api_get(
                    f"/irrigation-systems/{org}/overview-report/{p['id']}",
                    {"from": s_iso, "to": e_iso, "from-date": start.isoformat(), "to-date": end.isoformat()},
                )
            except Exception as e:  # noqa: BLE001
                return {"pivot": p.get("name"), "error": str(e)[:120]}
            r = r or {}
            return {
                "pivot": p.get("name"),
                "hours_run": r.get("hours_run"),
                "irrigated_acres": round((r.get("irrigated_area") or r.get("area_applied") or 0) / M2_PER_AC, 1)
                if (r.get("irrigated_area") or r.get("area_applied")) else r.get("irrigated_area"),
                "volume_ac_ft": r.get("volume_applied"),
                "avg_flow_gpm": r.get("average_flow"),
                "depth_in": mm_to_in(r.get("depth")) if r.get("depth") else r.get("depth"),
                "raw": r,
            }

    rows = await asyncio.gather(*(one(p) for p in targets))
    return sorted(rows, key=lambda r: r.get("pivot") or "")


@mcp.tool()
async def pivot_history(pivot: str, hours: int = 48) -> dict[str, Any]:
    """Raw controller telemetry rows (status, position, rate, pressure, faults) for the last N hours."""
    p = await resolve_pivot(pivot)
    rows = await api_get(f"/irrigation-controllers/{p['id']}/history") or []
    cutoff = datetime.now(TZ) - timedelta(hours=hours)
    keep = []
    for r in rows:
        try:
            t = datetime.fromisoformat(r["timestamp"])
        except Exception:  # noqa: BLE001
            continue
        if t >= cutoff:
            keep.append({
                "time": r["timestamp"],
                "status": r.get("status"),
                "direction": r.get("direction"),
                "position_deg": r.get("position"),
                "rate_pct": pct(r.get("rate")),
                "pressure_psi": bar_to_psi(r.get("pressure")),
                "irrigating": r.get("is_irrigating"),
                "moving": r.get("is_moving"),
                "fault": r.get("fault_result"),
                "note": r.get("note"),
            })
    keep.sort(key=lambda r: r["time"])
    return {"pivot": p.get("name"), "hours": hours, "rows": keep[-500:]}


# ---------------------------------------------------------------------------
# Control (write) tools — only registered when MCP_WRITE_TOKEN is set
# ---------------------------------------------------------------------------
#
# How the web app controls a pivot: it opens the WebSocket, authenticates,
# subscribes to the pivot's "pivot-status", copies the current state into a
# view-model, changes the fields the user touched, and sends
#   {"command_type": "pivot-control-dashboard-apply", "payload": {...}}
# with the full view-model. We do exactly the same.
#
# Speed math (from the app):  rate is a 0.01–1 fraction of full speed.
#   circle_time_hours = full_run_time_s / 3600 / rate
#   depth_m           = rate_conversion_factor / rate
#
# Guardrails: a separate key (MCP_WRITE_TOKEN); every tool previews the change
# and does nothing until called again with confirm=true; every applied command
# is logged.

DIRECTION_KEYS = ("forward", "reverse", "stopped")
IN_PER_M = 39.3701


def _presented_key(ctx: Context | None) -> str:
    try:
        req = ctx.request_context.request  # type: ignore[union-attr]
        return (req.headers.get("x-api-key")
                or req.headers.get("authorization", "").removeprefix("Bearer ").strip()
                or req.query_params.get("key", ""))
    except Exception:  # noqa: BLE001
        return ""


def _require_write(ctx: Context | None) -> None:
    if not MCP_WRITE_TOKEN:
        raise PermissionError("Control tools are disabled on this server (MCP_WRITE_TOKEN not set).")
    if _presented_key(ctx) != MCP_WRITE_TOKEN:
        raise PermissionError("This connector only has read access. Control requires the write key.")


async def ws_session(timeout: float = 15.0):
    """Open, authenticate and return a websocket connection to FieldNET."""
    tok = await tokens.access_token(_http)
    ws = await websockets.connect(WS_URL, open_timeout=timeout, close_timeout=5)
    await ws.send(json.dumps({"command_type": "auth-request", "token": tok, "organization_id": await org_id()}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time())))
        if msg.get("event_type") == "auth-result":
            if msg.get("payload") == "success":
                return ws
            await ws.close()
            raise RuntimeError(f"FieldNET websocket auth failed: {msg}")
    await ws.close()
    raise TimeoutError("FieldNET websocket auth timed out")


async def ws_wait(ws, predicate, timeout: float):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time()))
        except asyncio.TimeoutError:
            break
        try:
            msg = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        if predicate(msg):
            return msg
    return None


async def live_status(ws, equipment_id: str, timeout: float = 10.0) -> dict[str, Any]:
    await ws.send(json.dumps({"command_type": "subscribe", "id": equipment_id, "event_type": "pivot-status"}))
    msg = await ws_wait(ws, lambda m: m.get("event_type") == "pivot-status"
                        and (m.get("payload") or {}).get("id") == equipment_id, timeout)
    if not msg:
        raise TimeoutError("No pivot-status received for this pivot (is the RTU online?)")
    return msg["payload"]


POLL_TOPIC = "pivot-control-dashboard-poll"
APPLY_TOPIC = "pivot-control-dashboard-apply"
FINAL_CMD_STATES = ("successful", "failure", "waiting-for-online-status")


async def ws_command_status(ws, equipment_id: str, kind: str, topic: str, timeout: float) -> dict[str, Any] | None:
    """Wait for the final status of a poll/apply command.

    FieldNET reports progress as {event_type: 'poll-event'|'apply-event',
    payload: {equipment_id, event_type: <topic>, status, progress}} with status
    in-progress -> successful / failure / waiting-for-online-status."""
    last: dict[str, Any] | None = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = await ws_wait(ws, lambda m: m.get("event_type") == kind
                            and (m.get("payload") or {}).get("equipment_id") == equipment_id
                            and (m.get("payload") or {}).get("event_type") == topic,
                            max(0.1, deadline - time.time()))
        if not msg:
            break
        last = msg["payload"]
        if (last.get("status") or "") in FINAL_CMD_STATES:
            return last
    return last


async def ws_poll(ws, equipment_id: str, timeout: float = 45.0) -> dict[str, Any]:
    """Ask the panel for a fresh report (the app's Poll button)."""
    await ws.send(json.dumps({"command_type": "subscribe", "id": equipment_id, "event_type": POLL_TOPIC}))
    await ws.send(json.dumps({"command_type": POLL_TOPIC, "payload": {"equipment_id": equipment_id}}))
    st = await ws_command_status(ws, equipment_id, "poll-event", POLL_TOPIC, timeout)
    status = (st or {}).get("status") or "no-response"
    return {"status": status, "fresh": status == "successful",
            "at": datetime.now(TZ).isoformat(timespec="seconds")}


async def poll_pivot(equipment_id: str, timeout: float = 45.0) -> dict[str, Any]:
    ws = await ws_session()
    try:
        return await ws_poll(ws, equipment_id, timeout)
    finally:
        await ws.close()


def _rate_from(status: dict[str, Any], speed_pct: float | None, depth_in: float | None, hours: float | None) -> float | None:
    given = [v is not None for v in (speed_pct, depth_in, hours)]
    if sum(given) == 0:
        return None
    if sum(given) > 1:
        raise ValueError("Give only one of speed_pct, depth_in or hours.")
    if speed_pct is not None:
        rate = speed_pct / 100.0
    elif depth_in is not None:
        rcf = status.get("rate_conversion_factor")
        if not rcf:
            raise ValueError("This pivot has no depth calibration (rate_conversion_factor); use speed_pct.")
        rate = rcf / (depth_in / IN_PER_M)
    else:
        frt = status.get("full_run_time")
        if not frt:
            raise ValueError("This pivot has no full-circle time; use speed_pct.")
        rate = (frt / 3600.0) / hours  # type: ignore[operator]
    return max(0.01, min(1.0, round(rate, 4)))


def _describe(status: dict[str, Any], rate: float | None = None) -> dict[str, Any]:
    r = status.get("rate") if rate is None else rate
    rcf, frt = status.get("rate_conversion_factor"), status.get("full_run_time")
    return {
        "direction_key": status.get("direction_key"),
        "status": status.get("operational_status"),
        "angle_deg": status.get("current_angle"),
        "water_on": status.get("water_on"),
        "speed_pct": pct(r),
        "depth_in": round(rcf / r * IN_PER_M, 2) if (rcf and r) else None,
        "circle_hours": round(frt / 3600.0 / r, 1) if (frt and r) else None,
        "service_stop_deg": status.get("service_stop"),
        "service_stop_repeat": status.get("service_stop_repeat_on"),
        "plan_id": status.get("current_plan_id"),
        "auto_reverse": status.get("auto_reverse_on"),
        "auto_restart": status.get("auto_restart_on"),
        "accessory_1": status.get("accessory_1_on"),
        "rtu_online": status.get("rtu_online"),
    }


async def _plan_id_for(equipment_id: str, plan: str | None) -> str | None:
    """Resolve a plan name (or 'manual') to its id."""
    if plan is None:
        return None
    if plan.strip().lower() in ("manual", "manual mode", "none", ""):
        return None
    plans = await api_get(f"/irrigation-controllers/{equipment_id}/plans") or []
    if isinstance(plans, dict):
        plans = plans.get("items", [])
    for p in plans:
        if (p.get("name") or "").strip().lower() == plan.strip().lower() or p.get("id") == plan:
            return p["id"]
    names = ", ".join(p.get("name") or "?" for p in plans)
    raise ValueError(f"No plan named '{plan}' on this pivot. Plans: {names or '(none)'}")


MOVING_PREFIXES = ("forward", "reverse")


def _effective_direction(status: dict[str, Any]) -> str:
    """The direction the pivot is actually doing right now.

    FieldNET keeps reporting the last commanded direction_key after a fault or
    shutdown (e.g. 'forward' while in low-pressure-shutdown). Re-sending that
    stale value restarts the pivot, so anything not visibly moving counts as
    'stopped'."""
    op = (status.get("operational_status") or "").lower()
    d = (status.get("direction_key") or "").lower()
    if op.startswith(MOVING_PREFIXES) and d in MOVING_PREFIXES:
        return d
    return "stopped"


def _payload_token(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:10]


async def _apply(ctx: Context | None, pivot: str, changes: dict[str, Any], confirm: bool,
                 preview_token: str | None = None, poll: bool = True,
                 speed_pct: float | None = None, depth_in: float | None = None, hours: float | None = None,
                 plan: str | None = None, internal: bool = False) -> dict[str, Any]:
    """Build ONE complete control-panel payload and (on confirm) send it once.

    FieldNET applies the whole panel at once, so every field is sent every
    time. Fields not given are carried over from the live state, except
    direction, which defaults to what the pivot is actually doing (stopped
    unless it is visibly moving).

    internal=True is ONLY for the pump group watcher (watcher.py): it skips the
    connector key check and the human preview step, because a person already
    approved these exact settings when they set the group live."""
    if not internal:
        _require_write(ctx)
    p = await resolve_pivot(pivot)
    eid = p["id"]
    ws = await ws_session()
    try:
        poll_result = await ws_poll(ws, eid) if poll else None
        status = await live_status(ws, eid)
        rate = _rate_from(status, speed_pct, depth_in, hours)
        if plan is not None:
            changes["plan_id"] = await _plan_id_for(eid, plan)
        if rate is not None:
            changes["rate"] = rate
        eff_dir = _effective_direction(status)
        payload = {
            "equipment_id": eid,
            "direction_key": eff_dir,
            "plan_id": status.get("current_plan_id"),
            "aux_1_on": status.get("accessory_1_on"),
            "chem_stop": status.get("chem_stop"),
            "auto_restart_on": status.get("auto_restart_on"),
            "auto_reverse_on": status.get("auto_reverse_on"),
            "service_stop_angle": status.get("service_stop"),
            "service_stop_repeat_on": status.get("service_stop_repeat_on"),
            "water_on": status.get("water_on"),
            "rate": status.get("rate"),
        }
        before = _describe(dict(status, direction_key=eff_dir))
        payload.update(changes)
        after_status = dict(status, **{
            "direction_key": payload["direction_key"], "water_on": payload["water_on"], "rate": payload["rate"],
            "service_stop": payload["service_stop_angle"], "service_stop_repeat_on": payload["service_stop_repeat_on"],
            "current_plan_id": payload["plan_id"], "auto_reverse_on": payload["auto_reverse_on"],
            "auto_restart_on": payload["auto_restart_on"], "accessory_1_on": payload["aux_1_on"],
        })
        after = _describe(after_status)
        diff = {k: {"from": before[k], "to": after[k]} for k in after if before.get(k) != after.get(k)
                and k not in ("status", "angle_deg", "rtu_online")}
        token = _payload_token(payload)
        will_move = payload["direction_key"] in MOVING_PREFIXES
        summary = {
            "poll": poll_result,
            "pivot_will": (f"RUN {payload['direction_key'].upper()}, water {'ON' if payload['water_on'] else 'OFF'}"
                           if will_move else "BE STOPPED"),
            "final_settings": after,
        }
        if not status.get("rtu_online"):
            return {"pivot": p["name"], "applied": False, "error": "RTU is offline; command would not be delivered.", "current": before}
        if not internal and (not confirm or preview_token != token):
            out = {"pivot": p["name"], "applied": False, "preview": True, "current": before,
                   "would_change": diff, **summary, "preview_token": token,
                   "next_step": "Show this to the user. If they approve, call again with the same arguments, "
                                "confirm=true and this preview_token. Everything is sent in one apply."}
            if confirm and preview_token != token:
                out["note"] = ("Not sent: no preview_token, or the pivot's state changed since the preview. "
                               "This is a fresh preview; confirm again with its token.")
            return out
        if not diff:
            return {"pivot": p["name"], "applied": False, "note": "No change from current state.", "current": before}
        print(f"CONTROL {datetime.now(TZ).isoformat(timespec='seconds')} {p['name']} "
              f"{'[watcher] ' if internal else ''}{json.dumps(diff)}", flush=True)
        await ws.send(json.dumps({"command_type": "subscribe", "id": eid, "event_type": APPLY_TOPIC}))
        await ws.send(json.dumps({"command_type": APPLY_TOPIC, "payload": payload}))
        ack = await ws_command_status(ws, eid, "apply-event", APPLY_TOPIC, 45.0)
        ack_status = (ack or {}).get("status") or "no-response"
        result: dict[str, Any] = {"pivot": p["name"], "applied": ack_status == "successful",
                                  "sent": True, "panel_ack": ack_status, "changed": diff, **summary}
        if ack_status == "failure":
            result["error"] = "The panel reported the apply FAILED."
        elif ack_status == "waiting-for-online-status":
            result["note"] = "Queued: the panel is offline; FieldNET will deliver it when it reconnects."
        elif ack_status != "successful":
            result["note"] = "Sent, but no final acknowledgement from the panel within 45 s; check status with poll=true."
        try:
            fresh = await live_status(ws, eid, timeout=8.0)
            result["now"] = _describe(fresh)
        except Exception:  # noqa: BLE001
            pass
        return result
    finally:
        await ws.close()


if MCP_WRITE_TOKEN:

    @mcp.tool()
    async def pivot_control_preview(pivot: str, ctx: Context, poll: bool = True) -> dict[str, Any]:
        """Show a pivot's current control settings (direction, water, speed %, depth, circle hours, service stop, plan, toggles).
        Polls the panel first for a fresh reading unless poll=false."""
        _require_write(ctx)
        p = await resolve_pivot(pivot)
        ws = await ws_session()
        try:
            poll_result = await ws_poll(ws, p["id"]) if poll else None
            st = await live_status(ws, p["id"])
        finally:
            await ws.close()
        cur = _describe(dict(st, direction_key=_effective_direction(st)))
        cur["last_commanded_direction"] = st.get("direction_key")
        return {"pivot": p["name"], "poll": poll_result, "current": cur}

    @mcp.tool()
    async def pivot_control(pivot: str,
                            direction: str | None = None,
                            water_on: bool | None = None,
                            speed_pct: float | None = None,
                            depth_in: float | None = None,
                            hours: float | None = None,
                            service_stop_deg: float | None = None,
                            clear_service_stop: bool = False,
                            service_stop_repeat: bool | None = None,
                            plan: str | None = None,
                            auto_reverse: bool | None = None,
                            auto_restart: bool | None = None,
                            accessory_1: bool | None = None,
                            confirm: bool = False,
                            preview_token: str | None = None,
                            ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Set ALL desired pivot controls in ONE command, like the FieldNET control panel's Apply.

        Put every change in a single call (e.g. direction='reverse', water_on=false,
        speed_pct=90, service_stop_deg=260). Never split changes across calls:
        FieldNET applies the whole panel each time.
        - direction: 'forward', 'reverse' or 'stopped'. If omitted, the pivot keeps
          doing what it is doing now (a stopped or faulted pivot stays stopped).
        - speed: give ONE of speed_pct (1-100), depth_in (inches per pass) or hours (per circle).
        - service_stop_deg sets the stop angle; clear_service_stop=true removes it.
        - plan: saved plan name, or 'manual'.
        Unspecified settings are kept. First call returns a preview and a
        preview_token; after the user approves, call again with the same
        arguments plus confirm=true and that preview_token."""
        ch: dict[str, Any] = {}
        if direction is not None:
            d = direction.strip().lower()
            if d not in DIRECTION_KEYS:
                raise ValueError("direction must be 'forward', 'reverse' or 'stopped'")
            ch["direction_key"] = d
        if water_on is not None:
            ch["water_on"] = bool(water_on)
        if clear_service_stop and service_stop_deg is not None:
            raise ValueError("Give service_stop_deg or clear_service_stop, not both.")
        if clear_service_stop:
            ch["service_stop_angle"] = None
        elif service_stop_deg is not None:
            ch["service_stop_angle"] = float(service_stop_deg) % 360
        if service_stop_repeat is not None:
            ch["service_stop_repeat_on"] = bool(service_stop_repeat)
        if auto_reverse is not None:
            ch["auto_reverse_on"] = bool(auto_reverse)
        if auto_restart is not None:
            ch["auto_restart_on"] = bool(auto_restart)
        if accessory_1 is not None:
            ch["aux_1_on"] = bool(accessory_1)
        if not ch and plan is None and all(v is None for v in (speed_pct, depth_in, hours)):
            raise ValueError("No changes given.")
        return await _apply(ctx, pivot, ch, confirm, preview_token,
                            speed_pct=speed_pct, depth_in=depth_in, hours=hours, plan=plan)

    @mcp.tool()
    async def stop_pivot(pivot: str, confirm: bool = False, preview_token: str | None = None,
                         ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Stop a pivot (direction 'stopped'), leaving other settings as they are. Previews first;
        confirm=true with the preview_token to send."""
        return await _apply(ctx, pivot, {"direction_key": "stopped"}, confirm, preview_token)

    @mcp.tool()
    async def list_plans(pivot: str, ctx: Context) -> list[dict[str, Any]]:
        """List the saved plans on a pivot (names you can pass to pivot_control's plan)."""
        _require_write(ctx)
        p = await resolve_pivot(pivot)
        plans = await api_get(f"/irrigation-controllers/{p['id']}/plans") or []
        if isinstance(plans, dict):
            plans = plans.get("items", [])
        return [{"name": x.get("name"), "type": x.get("plan_type"), "id": x.get("id")} for x in plans]


# ---------------------------------------------------------------------------
# Pump group fallback watcher (off unless WATCHER_ENABLED=1; see watcher.py)
# ---------------------------------------------------------------------------

import contextlib  # noqa: E402

import watcher as _watcher  # noqa: E402


async def _watcher_pivots() -> list[dict[str, Any]]:
    return await get_pivots(max_age_s=30)


async def _watcher_start(m: "_watcher.Member") -> dict[str, Any]:
    """Start a fallback pivot with the settings saved for it: direction, water on, speed or depth."""
    return await _apply(None, m.pivot_id, {"direction_key": m.direction, "water_on": True}, confirm=True,
                        speed_pct=m.speed_pct, depth_in=m.depth_in, internal=True)


WATCHER = _watcher.from_env(_watcher_pivots, _watcher_start, TZ)


# ---------------------------------------------------------------------------
# HTTP app with bearer-token gate
# ---------------------------------------------------------------------------


class BearerAuth(BaseHTTPMiddleware):
    """Accepts the shared key as  X-API-Key: <key>  (preferred for Claude custom
    connectors, which may not forward a custom Authorization header), or as
    Authorization: Bearer <key>, or as ?key=<key> for quick manual tests."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        path = request.url.path
        if path in ("/", "/health"):
            return await call_next(request)
        if path.startswith("/.well-known/"):
            return JSONResponse({"error": "not found"}, status_code=404)
        presented = (
            request.headers.get("x-api-key")
            or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            or request.query_params.get("key", "")
        )
        valid = {k for k in (MCP_AUTH_TOKEN, MCP_WRITE_TOKEN) if k}
        if not valid or presented not in valid:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


async def health(_: Request):
    return PlainTextResponse("fieldnet-mcp ok")


app = mcp.streamable_http_app()
# Second URL for the same MCP endpoint, so a separate "FieldNET Control"
# connector can be added in Claude (it won't accept two connectors with the
# same URL). Access is still decided by which key the connector sends.
from starlette.routing import Route as _Route  # noqa: E402
_mcp_route = next(r for r in app.routes if getattr(r, "path", None) == mcp.settings.streamable_http_path)
app.router.routes.append(_Route("/control/mcp", endpoint=_mcp_route.endpoint, methods=list(_mcp_route.methods or [])))
app.add_route("/", health, methods=["GET"])
app.add_route("/health", health, methods=["GET"])
app.add_middleware(BearerAuth)

if WATCHER:
    _mcp_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def _lifespan(a):  # type: ignore[no-untyped-def]
        async with _mcp_lifespan(a) as state:
            task = asyncio.create_task(WATCHER.run_forever())
            try:
                yield state
            finally:
                task.cancel()

    app.router.lifespan_context = _lifespan


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
