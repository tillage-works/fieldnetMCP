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
| `plan_even_up` | **Plan only, sends nothing.** How to even up the applied water: position-based steps (direction, water, depth per pass, stop angle) within the field's intake limit, with times and the depth each wedge ends up with. See *Even-up planner* below |
| `daily_report` | Hours run and acres per day |
| `irrigation_overview` | Hours / acres / volume / depth per pivot over a range |
| `pivot_history` | Raw controller telemetry for the last N hours |
| `pivot_raw` | Everything FieldNET says about one pivot, unprocessed: controller record, live panel status, a few related paths (its end gun tables included), and the places in FieldNET's own web app code that mention end guns. Read only. For working out settings this server doesn't handle yet, like the end gun |

## Even-up planner

`plan_even_up` answers "this circle got uneven water: what do I run to even it up?" from
the Applied Irrigation map, the pivot's position, FieldNET's own depth and circle-time
calibration, and the most the ground takes in one pass right now (`max_depth_per_pass_in`,
which the planner never exceeds). The intake limit changes with soil, season and
conditions, so it's asked for every plan, with **0.3"** as the default.

- **Why a shutoff point can't fix it:** at a constant speed every pass puts the same depth
  everywhere. Evening up needs a different depth by sector, or extra passes over only the dry
  arc by reversing.
- **The plan:** carry on (or set off) one way until every wedge that needs water has been
  crossed once, each at its own depth per pass, dry-running at 100% over ground that needs
  none. Then shuttle back and forth over only what still needs more, each pass shorter than
  the last. Both starting directions are tried; turning round straight away pays a
  stop-and-restart allowance (`restart_minutes`, default 5: restarts aren't costly).
- **Directions:** forward is clockwise (increasing angle) on every pivot; reverse is
  anticlockwise. Speed and water change on the fly; only a reversal stops the pivot.
- **Per step:** direction, water, depth (and the speed that means), the angle it ends at (use
  it as the service stop), hours, and the clock time it should finish. `flying` steps change
  speed without stopping; `stop` steps end at a reversal.
- **Limits it reports instead of overwatering:** a shortfall under half a pass at 100% speed
  can't be helped; slivers under 1% of the field right behind the pivot aren't worth a trip
  round; FieldNET's applied depths are whole millimetres (±0.02").
- **Not supported yet:** partial circles and barricades (it refuses), park zones.

The Hyer Farms dashboard gets the same plans over HTTP: `POST /api/plan-even-up` with the
read key (`X-API-Key`) and a JSON body of the same parameters (`pivot` = name or id). It
returns the same result, 400 for bad input, 422 when the planner refuses (with the reason),
and 502 if FieldNET can't be reached.

The dashboard's Irrigation pages read two more, also with the read key, both read only:

- `GET /api/pivots/{id}/live` — everything the panel is set to (direction, water, speed and
  depth, service stop, aux 1, auto reverse, plan) plus the raw live status. Add `?poll=1` to
  ask the panel for a fresh report first (5–45 s).
- `GET /api/pivots/{id}/applied?from=YYYY-MM-DD&to=YYYY-MM-DD` — water applied around the
  circle as wedges (the Applied Irrigation map); default the last 7 days.

Both take a name or an id, and answer 404 for an unknown pivot and 502 for FieldNET trouble.

**Dashboard controls** (the Irrigation pages' change sheet): `POST /api/pivots/{id}/control`.
- **Keys:** it needs the read key *and* `X-Control-Key` set to `PORTAL_CONTROL_KEY`. That's a
  separate key, so the dashboard's control can be switched off or rotated without touching the
  Claude connector. Without `PORTAL_CONTROL_KEY` set, the endpoint answers 503.
- **Body:** `{who, changes: {direction, water_on, speed_pct | depth_in | hours, service_stop_deg |
  clear_service_stop, service_stop_repeat, auto_reverse, accessory_1}, confirm, preview_token}`.
- **Two steps, like the connector's control tools:**
  - Without `confirm`, it polls the panel and returns a preview and its token, and sends nothing.
  - With `confirm=true` and the token, it sends the one full-panel command. If the panel's
    settings changed in between, it sends nothing and returns a fresh preview.
- **Logged** in the pivot log as the person (`source = portal`, `who`).

`POST /api/pivots/{id}/endgun` (same two keys) switches one end gun:
`{who, endgun: 1-4, enabled, confirm, preview_token}`. It previews, then sends on confirm, and is
logged like `/control`. See *End guns* below.

The logic is pure and lives in `programs.py` (no FieldNET, no database); `python test_programs.py`
runs its tests, including the real W-26 case from Nov 2026 and the safety rules over 400
random fields; `python test_api.py` (with the requirements installed) tests the endpoint.

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
| `endgun_control` | Turn end gun 1 or 2 on (it sprays in the areas of its end gun table) or off |
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
the acknowledgement.

**End guns** are switched with their own command, not the control panel's apply. It was found
in FieldNET's own web app (`nextgen.myfieldnet.com`, the end gun page) with `pivot_raw`:
- It reads the pivot's `endgun-status`.
- It copies that into an apply form: `equipment_id`, `endgun_1..4_active_table` (1 where the
  status has none, as the app does) and `endgun_1..4_enabled`.
- It sends the form as `{command_type: "endgun-apply", payload}`, and the result comes back
  on the `endgun-apply` topic.

`endgun_control` (and the dashboard's `POST /api/pivots/{id}/endgun`) does exactly that:
- It changes only the one gun's on/off. Active tables and the other guns go back as FieldNET
  has them.
- It previews first and sends on confirm, then reads the status back.
- Each gun's spray areas are its tables (`/irrigation-controllers/{id}/endguns/{n}/tables/{t}`),
  which are set up in FieldNET.
- Only pivot-control panels; Series 700 panels use a different command and are refused.

**Chemigation (accessory 1).** On this farm aux 1 runs the chemical pumps, so it only runs
with the pivot running wet:
- asking for it on a stopped or dry pivot is refused;
- any command that leaves the pivot stopped or dry, programs' included, turns it off, and the
  preview says so.

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

## Optional: pump group watcher (automatic fallback pivots)

Off by default. For farms using the Hyer Farms dashboard's **Pump groups** page:
pivots fed by the same water are grouped, and during a group's schedule (e.g.
overnight, or a weekend) this server checks them every 3 minutes. When a
*scheduled* pivot that was watering shuts down on a fault or finishes its run,
it starts the group's next *fallback* pivot with the direction and speed/depth
saved for it. The rules are at the top of [`watcher.py`](watcher.py); the
decisions are tested in `test_watcher.py` (`python test_watcher.py`).

| Variable | |
| --- | --- |
| `WATCHER_ENABLED` | `1` to run the watcher |
| `PUMPGROUPS_DB_URL` | the `pumpgroups_rw` URL from the dashboard's `npm run create-pumpgroups-db` |
| `WATCHER_ALLOW_LIVE` | `1` lets groups set to *Live* start pivots. Unset: every group is practice (logs only) |
| `PUMPGROUPS_NOTIFY_URL` | `https://<dashboard>/api/v1/pump-groups/notify`, so alerts become push notifications |
| `PUMPGROUPS_NOTIFY_SECRET` | same value as on the dashboard |
| `WATCHER_INTERVAL_S` | seconds between checks (default 180, minimum 60) |

- It needs an **always-on** Render plan: free services sleep after 15 minutes
  without requests, and a sleeping service watches nothing.
- Outside every group's schedule it makes no FieldNET calls.
- It starts pivots without the preview/confirm step the Claude control tools
  use, because a person already chose the exact settings when they set the
  group live. Every command is logged (`CONTROL … [pump-group: <group>]` and the pivot
  log), and every decision goes to the dashboard's activity log.
- It never stops a pivot, and doesn't need `MCP_WRITE_TOKEN`.

## Optional: irrigation program engine

Off by default. Runs the programs armed on the Hyer Farms dashboard's **Programs**
page: position-based steps (direction, water, depth per pass, and the angle each
step ends at) that a field runs when it turns on. The first kind is the even-up
from the planner above. The rules are at the top of [`engine.py`](engine.py); in
short:

- **When it starts.** A program starts when its field starts watering after it
  was armed, or straight away if it was armed to *take over now*. An even-up is
  worked out again at that moment from fresh applied-water data and the pivot's
  position. If the fresh plan is much bigger than the one approved (more passes,
  much longer, or a pass over the intake limit), it pauses for a new approval
  instead of running.
- **Service stop on every step.** Every step's command sets the panel's service
  stop to that step's end angle. If this server goes down or misses a check, the
  pivot stops there instead of running on.
- **One command per change.** Each command carries every setting (direction,
  water, depth or 100% for a dry step, service stop). The panel's plan is left as
  it is and must be manual; a saved plan pauses the program before anything is
  sent. Before it is sent, what FieldNET says it would apply is checked against
  the step; if they differ, nothing is sent and the program pauses.
- **Changes on the move, turn-rounds included.** A speed or water change is sent
  just before its angle, and so is a reverse at the end of a pass: it's timed to
  reach the panel as the pivot gets there (about 90 seconds of travel early), so
  the pivot turns round without stopping. Both go out from a fresh panel
  position. Missing one is safe: the service stop holds the pivot at the angle
  and the next step goes out from there. Only the last step ends at its service
  stop.
- **Manual stops win.** The engine only restarts a pivot that its own service
  stop halted, at the angle it expected. Anything else pauses the program and
  raises an alert, and a paused program never moves the pivot again:
  - a stop by hand;
  - a fault;
  - settings changed by someone else;
  - a pivot that runs past its service stop;
  - a command that doesn't show up within 10 minutes;
  - a run taking twice its planned time.
- **Lost contact.** If the panel stops reporting, nothing is sent; the service
  stop is the backstop.
- **Pump groups.** Mid-program service stops don't count as "finished its run",
  so they never start a pump group fallback; the last step's stop does.
- **Practice.** Practice programs work everything out and check every command
  with FieldNET, but send nothing.

Positions are only as fresh as the panel's last report, so the engine estimates
progress from the step's speed and asks the panel for a fresh position (the
`poll` the tools use) every 30 minutes and as each change gets near. On W-26's
33-hour even-up that's about 75 polls.

| Variable | |
| --- | --- |
| `PROGRAMS_ENABLED` | `1` to run the engine (`true`/`yes` work too) |
| `PUMPGROUPS_DB_URL` | the same `pumpgroups_rw` URL as the watcher (programs live in the `pumpgroups` schema) |
| `PROGRAMS_ALLOW_LIVE` | `1` lets programs armed *Live* send commands. Unset: every program runs as practice |
| `PUMPGROUPS_NOTIFY_URL` / `PUMPGROUPS_NOTIFY_SECRET` | as for the watcher: program alerts become push notifications too |
| `PROGRAMS_INTERVAL_S` | seconds between checks while nothing is close (default 180, minimum 60) |

- Like the watcher, it needs an **always-on** Render plan.
- It doesn't need `MCP_WRITE_TOKEN`. Every command is logged (`CONTROL …`) and
  every decision goes to the program's run log on the dashboard.
- `python test_engine.py` runs whole programs against a simulated pivot. The pivot
  moves at its commanded speed, stops at its service stop like a real panel, and
  keeps track of the water it puts down. The tests cover a full W-26 run (it ends
  3.65–3.70″), practice mode, manual stops, faults, overrides, losing contact
  through a change, failed sends, overruns and a restart part-way through.

## Pivot log (who changed what)

On whenever `PUMPGROUPS_DB_URL` is set. It's a record of every change to every pivot and
who made it, for the dashboard's **Irrigation** pages. It only reads from FieldNET. Details are
at the top of [`pivotlog.py`](pivotlog.py).

- **Commands.** Every command this server sends is written as it goes. Each records the
  source, what it asked for, and whether the panel took it. The sources are:
  - a person on the dashboard (from phase 2);
  - a program;
  - a pump group;
  - Claude, through the FieldNET Control connector.
- **Changes.** Every 2½ minutes it reads all pivots in one FieldNET call and compares each
  pivot with its last look. That last look is kept in the database, so deploys don't lose it.
  - **What it compares:** running or stopped, direction, wet or dry, speed, service stop,
    plan, status and contact. Not the angle, and not the end gun (the list only shows
    what it's doing right now).
  - **A change that matches a recent command** marks that command *seen on the panel*.
  - **Changes no command explains** are logged as *outside the portal* (the panel or the
    FieldNET app). Starts and turn-rounds can also be the panel's own auto restart or
    auto reverse.
  - **The pivot's own reports** are logged as such: a service stop or barrier reached, a
    fault or shutdown, and lost or regained contact.

| Variable | |
| --- | --- |
| `PUMPGROUPS_DB_URL` | the same `pumpgroups_rw` URL; the log lives in `pumpgroups.pivot_log` and `pivot_seen` |
| `PIVOT_LOG_ENABLED` | `0` to switch it off |
| `PIVOT_LOG_INTERVAL_S` | seconds between looks (default 150, minimum 60) |

- It needs the tables from the dashboard's `npm run create-pumpgroups-db` (re-run it once).
  Until then it says so in the log and looks again every 10 minutes.
- Like the watcher, it needs an **always-on** Render plan to catch changes as they happen.
  After a sleep it still logs what changed, with the time FieldNET gives for the status.
- `python test_pivotlog.py` tests the comparison, wording and alerts with the farm's real readings.

**Alerts.** The pivot log also raises push notifications. It marks the entry, and the
dashboard sends it, through the same `PUMPGROUPS_NOTIFY_URL` / `PUMPGROUPS_NOTIFY_SECRET` as
the watcher, to every subscribed phone that can see Irrigation:

| Alert | When |
| --- | --- |
| Shut down | a running pivot goes to a fault or shutdown status (low pressure, alignment, power…) |
| Lost contact while running | a running pivot goes offline |
| Stopped outside the portal | a running pivot stops and no command explains it |
| Change not taken | a change sent from the dashboard or Claude still isn't showing on the panel 15 minutes later |
| Chemigation on, not watering | aux 1 is still on after the pivot stops or goes dry (read from the panel at that moment) |

- Normal ends (service stops, barriers) don't alert, nor does anything that happens while a
  pivot is already stopped.
- The same pivot and kind alert at most once every 30 minutes.
- Programs and pump groups alert on their own commands themselves.
- Unsent alerts are nudged again every ~15 minutes for a day, in case the dashboard was
  asleep or deploying.
- Alerts need the `alert_kind` and `notified_at` columns: re-run the dashboard's
  `npm run create-pumpgroups-db`. Until then the log says it needs the update and pauses.
  Nothing is lost: changes made meanwhile are logged once it's back.

`GET /api/pivots/{id}/raw` (read key) is `pivot_raw` over HTTP, for the dashboard's owners-only
*FieldNET details* section.
