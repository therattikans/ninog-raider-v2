# NiNog Raker v2.0 | THE RATTIKANS

Discord guild administration and anti-nuke testing tool. Drives a bot token
through the REST API to manage servers, run raid simulations against your own
test guilds, and snapshot/restore everything in between.

## Run it

```bash
pip install rich pyfiglet requests
python ninog/main.py          # from the repo root
# or
cd ninog && python main.py
```

`config/`, `snapshots/`, `reportlog/` and `exports/` are created on demand
next to `main.py` and are git-ignored.

## What's inside (v2.0)

- **Role Engine** (replaces Get Admin): build a permission profile — admin,
  moderator, channel manager, custom bit-toggles — and apply it to *anyone*:
  all whitelisted users, handpicked users (from whitelist or the server),
  everyone, everyone-except-N. One shared role or a separate role per user,
  with **role overflow detection** (250-role guild cap) and **randomized role
  names/colors**.
- **Universal member picker**: every target prompt offers five methods —
  user ID, whitelist checkboxes (D when done), the paginated server browser
  (200/page, online members first, 2,000-member cap with a large-server
  warning), fuzzy username search, and members from a stored snapshot.
- **Extract Webhooks**: pulls every webhook URL out of the guild (guild-wide
  endpoint + per-channel sweep) and delivers them via terminal, clipboard,
  relay to another webhook, or `exports/` file — one exact URL per line.
- **Ban detection**: a watchdog thread probes the bot's own membership on an
  interval and raises an alert the moment the bot is kicked or banned.
  Interval configurable in Settings ▸ Watchdog.
- **Workflow engine**: V1-style screen (Create / Execute / Delete / Set
  Triggers) with unlimited chained ops, plus waits, conditions, gates, loops
  and notes. **Triggers**: manual, on-guild-select, on-ban-detected, and
  interval (every N seconds).
- **Tool Management page**: Diagnoser (source hashes + compile check, config
  JSON/unicode validation, workflow file validation, writable-dir probes,
  API/gateway check, watchdog state, dependency versions), Report Explorer
  (session logs + crash bundles, paginated, with delete), and Support Bundle.
- **Crash reporter**: any unplanned error — inside an op or escaping the
  session — writes a full bundle to `reportlog/crashes/`: complete traceback,
  live context, **verbatim source of every `.py` file plus a SHA-256
  manifest**, a config snapshot (token values redacted, file hashes kept for
  integrity), and session logs. Send the bundle to **THE RATTIKANS**
  (`therattikans.` or <https://discord.gg/M2fGay6MVn>) and a fix will be
  supplied.
- Snapshots before every destructive op, restore for roles/channels/settings/
  messages/bans, client-side rate limiting with presets, whitelist system,
  seven themed op pages (assault, recon/restore, messaging/webhooks,
  precision, whitelist, tool management, workflows).

## Layout

```
ninog/
├── main.py          bootstrap + session loop
└── src/
    ├── core.py      paths, config, vault, REST client, watchdog, crash reporter
    ├── ui.py        themes, gradient, bouncing-R live displays, transitions
    ├── workflow.py  workflow engine: chains, conditions, triggers
    └── ops.py       every interactive flow and operation
```

## Testing

```bash
python tests/integration_test.py
```

The offline regression suite (33 checks) drives every interactive flow —
role engine, webhook extraction/relay/export, workflow engine + triggers,
crash bundles, watchdog, narrow-terminal rendering, rate-limit retries —
against a scripted terminal and a fake Discord REST end. No token needed.
