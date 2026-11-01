# fieldnet-mcp

Read-only MCP server for Lindsay FieldNET NextGen, so Claude can look up pivot
status, water applied, laps and daily hours from any chat, Cowork session or
the mobile app.

Only GET endpoints are used. Nothing here can start, stop or change a pivot.
Each farm runs its own copy with its own FieldNET login; nothing is shared.

## Deploy your own (about 10 minutes)

You need: a FieldNET NextGen login, a free [Render](https://render.com) account,
and a Claude plan that allows custom connectors.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/tillage-works/fieldnetMCP)

1. **Get your FieldNET refresh token.** In Chrome, sign in at
   nextgen.myfieldnet.com, press F12 → **Application** → **Local Storage** →
   `https://nextgen.myfieldnet.com`, and copy the value of `refresh_token`
   (a 66-character string). Treat it like your FieldNET password.
2. **Click the Deploy to Render button above.** Render asks for one value:
   `FIELDNET_REFRESH_TOKEN`. Paste the token. Everything else is preset
   (`MCP_AUTH_TOKEN` is generated for you). Click Apply.
3. **Wait for the deploy** (about a minute). Your server URL will be
   `https://fieldnet-mcp-XXXX.onrender.com`; open `/health` on it and you
   should see `fieldnet-mcp ok`.
4. **Copy your connector key.** In the Render service → **Environment**, click
   the eye icon on `MCP_AUTH_TOKEN` and copy it.
5. **Add it to Claude.** Settings → Connectors → **Add** → **Custom** (Team/
   Enterprise: the account Owner does this under Organization settings):

   | Field | Value |
   | --- | --- |
   | Name | FieldNET |
   | Remote MCP server URL | `https://<your-service>.onrender.com/mcp` |
   | Authentication | No sign in |
   | Request header | name `X-API-Key`, value = the key from step 4 |

   Then click **Connect** on it. Start a new chat and ask "what's pivot
   &lt;name&gt; doing right now?".

The organization is detected from your account automatically. If your login
belongs to more than one organization, set `FIELDNET_ORG_ID` in Render's
Environment tab to the one you want (the GUID in the FieldNET URL after
`/organization/`). Set `FIELDNET_TZ` if you're not in Pacific time.

## Tools

| Tool | What it answers |
| --- | --- |
| `list_pivots` | Every pivot with status, direction, angle, speed %, depth in, psi, water on |
| `pivot_status` | One pivot's current state; `poll=true` asks the panel for a fresh report first (the app's Poll button) |
| `pivot_track` | Position/pressure samples over a range, grouped into wet runs (where laps started/stopped) |
| `applied_irrigation` | Depth applied per wedge of the circle (the Applied Irrigation map as numbers) |
| `daily_report` | Hours run and acres per day |
| `irrigation_overview` | Hours / acres / volume / depth per pivot over a range |
| `pivot_history` | Raw controller telemetry for the last N hours |

## Optional: control tools (read & write)

Off by default. To enable, add a second environment variable in Render,
`MCP_WRITE_TOKEN` (use Render's Generate button), and redeploy. Then add a
**second** connector in Claude, e.g. "FieldNET Control", with the URL
`https://<your-service>.onrender.com/control/mcp` (Claude won't accept two
connectors with the same URL; both paths serve the same server) and
`X-API-Key` = the write key. Keep the read-only connector for everyone else.

| Tool | Does |
| --- | --- |
| `pivot_control_preview` | Current control settings for a pivot |
| `pivot_control` | Everything in one command: direction (forward/reverse/stopped), water on/off, speed (percent, inches per pass, or hours per circle), service stop (set/clear, repeat), plan, auto-reverse, auto-restart, accessory 1 |
| `stop_pivot` | Stop, leaving the other settings alone |
| `list_plans` | Names of the saved plans on a pivot |

FieldNET's Apply sends the whole control panel at once, so all the changes for
a pivot go in a single `pivot_control` call and are applied together. Settings
you don't mention are kept, except direction: if you don't give one, the pivot
keeps doing what it's actually doing, so a stopped or faulted pivot stays
stopped. (FieldNET keeps reporting the last commanded direction after a fault;
re-sending that stale value would restart the pivot.)

Safety: every control call returns a preview (current state, exact change, and
what the pivot will do) plus a `preview_token`, and sends nothing until called
again with `confirm=true` and that token. If the pivot's state changed in
between, the token no longer matches and you get a fresh preview instead. A
connector holding only the read key gets "read access only" from these tools.
Every applied command is logged in Render's Logs as `CONTROL <time> <pivot> <diff>`.
Commands are sent exactly the way the web app's dashboard sends them
(`pivot-control-dashboard-apply` over the WebSocket) and the server waits for
the acknowledgement. Endgun enable/disable is not included (it lives in the
endgun tables, a different flow).

## Fresh readings (polling)

FieldNET only updates a pivot's position and status when the panel reports in,
so a plain status check can be minutes old. `pivot_status(poll=true)` sends
`pivot-control-dashboard-poll` (the same thing the app's Poll button does) and
waits for the panel's reply before reading. The control tools always poll
before previewing and before sending, and wait for the panel's own
acknowledgement of an apply (`panel_ack`: successful, failure, or
waiting-for-online-status when the panel is offline).

## Notes

- FieldNET access tokens last 10 minutes; the server refreshes them
  automatically. The refresh token has not been seen to rotate. If FieldNET
  ever starts rotating it, the server logs a notice and you re-copy the token.
- Free Render instances sleep after 15 minutes idle; the first call after
  that takes 30–60 s. Upgrade to Starter ($7/mo) if that gets annoying.
- The API is metric under the hood; tools convert to inches, psi and percent.
- This uses FieldNET's internal web-app API, which is undocumented and could
  change with any NextGen release. Keep usage light and read-only.
- The refresh token carries full control permissions even though this server
  only reads. Keep it in Render's environment and nowhere else.

## Running locally

```
pip install -r requirements.txt
FIELDNET_REFRESH_TOKEN=... MCP_AUTH_TOKEN=anything python server.py
curl -H "X-API-Key: anything" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' localhost:8000/mcp
```
