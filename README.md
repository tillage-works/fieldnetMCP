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
| `pivot_status` | One pivot's current state |
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
| `start_pivot` | Run forward or reverse, water on/off |
| `stop_pivot` | Stop |
| `set_water` | Water on/off without changing direction |
| `set_speed` | By percent, inches per pass, or hours per circle (one of the three) |
| `set_service_stop` | Set or clear the service-stop angle, optional repeat |
| `select_plan` | Pick a saved plan by name, or `manual` |
| `set_toggles` | Auto-reverse, auto-restart, accessory 1 |
| `list_plans` | Names of the saved plans on a pivot |

Safety: every control tool returns a preview of the current state and the
exact change, and sends nothing until called again with `confirm=true`. A
connector holding only the read key gets "read access only" from these tools.
Every applied command is logged in Render's Logs as `CONTROL <time> <pivot> <diff>`.
Commands are sent exactly the way the web app's dashboard sends them
(`pivot-control-dashboard-apply` over the WebSocket) and the server waits for
the acknowledgement. Endgun enable/disable is not included (it lives in the
endgun tables, a different flow).

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
