# fieldnet-mcp

Read-only MCP server for Lindsay FieldNET NextGen, so Claude can look up pivot
status, water applied, laps and daily hours from any chat, Cowork session or
the mobile app.

Only GET endpoints are used. Nothing here can start, stop or change a pivot.

## Tools

| Tool | What it answers |
| --- | --- |
| `list_pivots` | Every pivot with status, direction, angle, speed %, depth in, psi, water on |
| `pivot_status` | One pivot's current state |
| `pivot_track` | Position/pressure samples over a range, grouped into wet runs (where laps started/stopped) |
| `applied_irrigation` | Depth applied per wedge of the circle (the Applied Irrigation map as numbers) |
| `daily_report` | Hours run and acres per day |
| `irrigation_overview` | Hours / acres / volume / depth per pivot over a range |
| `pivot_history` | Raw controller telemetry for the last N hours |

## Deploy on Render

1. Push this folder to a GitHub repo.
2. Render → New → Web Service → pick the repo. Render reads `render.yaml`
   (Python, free plan, `python server.py`).
3. Set the two secret environment variables in the Render dashboard:
   - `FIELDNET_REFRESH_TOKEN` — in Chrome, signed in to nextgen.myfieldnet.com:
     DevTools (F12) → Application → Local Storage → `https://nextgen.myfieldnet.com`
     → copy the value of `refresh_token`.
   - `MCP_AUTH_TOKEN` — any long random string (e.g. run `openssl rand -hex 32`).
     Claude will send this as a bearer token.
4. Deploy. `https://<service>.onrender.com/health` should return `fieldnet-mcp ok`.

## Add to Claude as a custom connector

Team plan: the account Owner goes to **Organization settings → Connectors → Add → Custom → Web**.

| Field | Value |
| --- | --- |
| Name | FieldNET |
| Remote MCP server URL | `https://<service>.onrender.com/mcp` |
| Authentication | No sign in |
| Request header | name `X-API-Key`, value `<MCP_AUTH_TOKEN>` (just the key, no "Bearer") |

Then each member opens Customize → Connectors and clicks Connect on FieldNET.

## Notes

- Access tokens last 10 minutes; the server refreshes them automatically.
  The refresh token did not rotate in testing. If FieldNET ever starts
  rotating it, the server logs a notice and you re-copy the token.
- Free Render instances sleep after 15 minutes idle; the first call after
  that takes 30–60 s. Upgrade to Starter if that gets annoying.
- API is metric under the hood; tools convert to inches, psi and percent.
- Treat the refresh token like your FieldNET password: it carries full
  control permissions even though this server only reads.
