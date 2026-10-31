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
FIELDNET_ORG_ID          required  organization GUID (Hyer Farms 2 = 937981d9-...)
MCP_AUTH_TOKEN           required  any long random string; Claude sends it as a bearer token
FIELDNET_CLIENT_ID       optional  defaults to the NextGen web app's public client id
PORT                     optional  Render sets this automatically

Only GET endpoints are used. Nothing here can start, stop, or change a pivot.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API = "https://v2.api.myfieldnet.com"
CLIENT_ID = os.environ.get("FIELDNET_CLIENT_ID", "1f5ef800-1061-4146-95b1-42139f1f187a")
ORG_ID = os.environ.get("FIELDNET_ORG_ID", "")
REFRESH_TOKEN = os.environ.get("FIELDNET_REFRESH_TOKEN", "")
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
TZ = ZoneInfo(os.environ.get("FIELDNET_TZ", "America/Los_Angeles"))

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


async def get_pivots(max_age_s: int = 60) -> list[dict[str, Any]]:
    if time.time() - _pivot_cache["at"] > max_age_s:
        data = await api_get("/irrigation-controllers", {"organization-id": ORG_ID, "page-size": 1000})
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
        "Nothing here can control equipment."
    ),
    stateless_http=True,
    json_response=True,
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
async def pivot_status(pivot: str) -> dict[str, Any]:
    """Current state of one pivot by name (e.g. 'W-2'): status, direction, angle, speed, depth, psi, service stop."""
    p = await resolve_pivot(pivot)
    fresh = await get_pivots(max_age_s=30)
    p = next((x for x in fresh if x["id"] == p["id"]), p)
    out = summarize_pivot(p)
    out["lat"] = p.get("latitude")
    out["lon"] = p.get("longitude")
    out["depth_at_full_speed_in"] = p.get("application_depth_at_full_speed")
    out["auto_reverse"] = p.get("is_auto_reverse_on")
    out["auto_restart"] = p.get("is_auto_restart_on")
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
    all_p = await get_pivots(max_age_s=600)
    targets = all_p if not pivots else [await resolve_pivot(n) for n in pivots]
    sem = asyncio.Semaphore(8)

    async def one(p: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            try:
                r = await api_get(
                    f"/irrigation-systems/{ORG_ID}/overview-report/{p['id']}",
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
        if not MCP_AUTH_TOKEN or presented != MCP_AUTH_TOKEN:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


async def health(_: Request):
    return PlainTextResponse("fieldnet-mcp ok")


app = mcp.streamable_http_app()
app.add_route("/", health, methods=["GET"])
app.add_route("/health", health, methods=["GET"])
app.add_middleware(BearerAuth)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
