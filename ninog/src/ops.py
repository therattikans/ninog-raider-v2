import base64
import difflib
import json
import random
import re
import shutil
import sys
from datetime import datetime, timezone
from functools import partial

from rich import box
from rich.panel import Panel
from rich.table import Table

from .core import (
    ApiNetworkError,
    CONFIG_DIR,
    CONFIG_FILE,
    EXPORTS_DIR,
    MAINTAINER_CONTACT,
    MAINTAINER_SERVER,
    PERMISSION_BITS,
    RATE_LIMIT_PRESETS,
    REPORTLOG_DIR,
    SNAPSHOTS_DIR,
    TOKENS_FILE,
    TOOL_NAME,
    TOOL_VERSION,
    WHITELIST_FILE,
    DiscordREST,
    GuildWatchdog,
    apply_rate_level,
    bot_display,
    build_crash_report,
    copy_to_clipboard,
    dm_send,
    fetch_online_ids,
    load_tokens,
    load_whitelist,
    save_tokens,
    save_whitelist,
    sha256_file,
    source_files,
    unique_label,
    webhook_url_of,
    whitelist_ids,
)
from .workflow import build_index, page_ops, run_due_workflows
from .ui import (
    LAYOUTS,
    RATE_LEVELS,
    THEME_OPTIONS,
    TRANSITIONS,
    apply_theme,
    ask,
    ask_int,
    ask_password,
    chunk,
    confirm,
    console,
    fmt_ts,
    grad,
    gradient_on,
    normalize_options,
    notice,
    present_frame,
    press_enter,
    print_header,
    sanitize_name,
    screen_title,
)

# ------------------------------------------------------------
# SECTION 1 | TOKEN VAULT FLOWS
# ------------------------------------------------------------

def add_token_flow(ctx):
    def render():
        print_header(ctx)
        console.print()
        screen_title("ADD TOKEN", "tokens are validated against discord before storage")
        console.print()

    present_frame(ctx, render)
    bot_user = None
    while True:
        raw = ask_password("bot token")
        if not raw:
            return None
        if raw.count(".") < 2:
            notice("ODD FORMAT", ["expected 3 parts in a normal bot token."], "warn")
            if not confirm("continue anyway?"):
                continue
        rest = DiscordREST(raw, ctx.logger, config=ctx.config)
        try:
            me = rest.get_me()
        except ApiNetworkError as e:
            notice("NETWORK ERROR", [str(e)], "warn")
            if confirm("save anyway as unverified?"):
                bot_user = "unverified"
                break
            continue
        if me.status_code == 401:
            ctx.logger.log("TOKEN_REJECTED", "401 from /users/@me")
            notice("REJECTED", ["401 | token is invalid or revoked."], "bad")
            if not confirm("try again?"):
                return None
            continue
        if me.status_code != 200:
            notice("API ERROR", [f"status {me.status_code} | {me.text[:150]}"], "warn")
            if confirm("save anyway as unverified?"):
                bot_user = "unverified"
                break
            continue
        bot_user = bot_display(me.json())
        ctx.logger.log("TOKEN_VALIDATED", bot_user)
        break

    if not confirm("save this token to the vault?", True):
        return {
            "label": bot_user,
            "data": raw,
            "bot_user": bot_user,
            "added": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "ephemeral": True,
        }

    tokens = load_tokens()
    default_label = bot_user if bot_user != "unverified" else f"token {len(tokens) + 1}"
    label = ask("label", default_label) or default_label
    label = unique_label(tokens, label)

    meta = {
        "label": label,
        "data": raw,
        "bot_user": bot_user,
        "added": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    tokens.append(meta)
    save_tokens(tokens)
    ctx.logger.log("TOKEN_SAVED", f"label={label} bot={bot_user}")
    notice("SAVED", [f"[white]{label}[/white] added to the vault."], "good")
    return meta


def delete_token_flow(ctx, tokens):
    screen_title("DELETE TOKEN", "pick the number shown on the current page")
    if not tokens:
        notice("EMPTY", ["nothing to delete."], "warn")
        return
    idx = ask_int("token number to delete", 0)
    if idx < 1 or idx > len(tokens):
        notice("OUT OF RANGE", [f"expected 1-{len(tokens)}."], "warn")
        return
    victim = tokens[idx - 1]
    if confirm(f"delete [white]{victim.get('label')}[/white]?"):
        tokens.pop(idx - 1)
        save_tokens(tokens)
        ctx.logger.log("TOKEN_DELETED", victim.get("label", "?"))
        notice("DELETED", [f"{victim.get('label')} removed."], "good")


def token_browser(ctx):
    page_idx = 0
    while True:
        tokens = load_tokens()
        if not tokens:
            def render_empty():
                print_header(ctx)
                console.print()
                notice("EMPTY VAULT", ["no tokens stored yet."])
                console.print()

            present_frame(ctx, render_empty)
            if confirm("add a token now?", True):
                meta = add_token_flow(ctx)
                if meta and meta.get("ephemeral"):
                    if confirm("use this unsaved token for this session?", True):
                        return meta
                if meta is None and not sys.stdin.isatty():
                    # On EOF (piped/run headless) the empty-vault loop would
                    # spin forever: confirm returns its default, the add flow
                    # returns None, repeat. Bail instead.
                    return None
                continue
            return None

        # clamped: a hand-edited config.json with page_size 0 would make
        # chunk() raise ValueError("range() arg 3 must not be zero")
        page_size = max(1, int(ctx.config.setting("page_size", 10)))
        pages = chunk(tokens, page_size)
        page_idx = max(0, min(page_idx, len(pages) - 1))
        page = pages[page_idx]
        offset = page_idx * page_size

        def render():
            print_header(ctx)
            console.print()
            console.print(
                f"[white]TOKEN VAULT[/white] [dim]| {len(tokens)} stored | "
                f"page {page_idx + 1}/{len(pages)}[/dim]"
            )
            console.print()
            table = Table(
                box=box.SIMPLE_HEAVY,
                header_style="brand",
                border_style="deep",
                padding=(0, 2),
            )
            table.add_column("#", justify="right", style="dim")
            table.add_column("LABEL", style="white")
            table.add_column("BOT", style="dim")
            table.add_column("ADDED", style="dim")
            for i, t in enumerate(page):
                table.add_row(
                    str(offset + i + 1),
                    t.get("label", "?"),
                    t.get("bot_user", "?"),
                    t.get("added", "?"),
                )
            console.print(table)
            console.print()

        present_frame(ctx, render)
        hints = [f"[white]1-{len(page)}[/white] select"]
        if len(pages) > 1:
            hints.append("[white]N[/white] next  [white]P[/white] prev")
        hints.append("[white]A[/white] add  [white]D[/white] delete  [white]Q[/white] quit")
        choice = ask("  [dim]|[/dim] ".join(hints)).lower()

        if choice.isdigit():
            n = int(choice)
            if 1 <= n <= len(page):
                return tokens[offset + n - 1]
            notice("OUT OF RANGE", [f"expected 1-{len(page)} on this page."], "warn")
            press_enter()
        elif choice == "n" and len(pages) > 1:
            page_idx = (page_idx + 1) % len(pages)
        elif choice == "p" and len(pages) > 1:
            page_idx = (page_idx - 1) % len(pages)
        elif choice == "a":
            meta = add_token_flow(ctx)
            if meta and meta.get("ephemeral"):
                if confirm("use this unsaved token for this session?", True):
                    return meta
        elif choice == "d":
            delete_token_flow(ctx, tokens)
            press_enter()
        elif choice == "q":
            return None


def authenticate_token(ctx):
    while True:
        meta = token_browser(ctx)
        if meta is None:
            return None

        rest = DiscordREST(meta["data"], ctx.logger, config=ctx.config)
        try:
            me = rest.get_me()
        except ApiNetworkError as e:
            notice("NETWORK ERROR", [str(e)], "bad")
            continue

        if me.status_code == 200:
            user = bot_display(me.json())
            if not meta.get("ephemeral"):
                all_tokens = load_tokens()
                for t in all_tokens:
                    if t.get("data") == meta.get("data"):
                        t["bot_user"] = user
                save_tokens(all_tokens)
            meta["bot_user"] = user
            ctx.logger.log("TOKEN_SELECTED", f"{user} ({meta.get('label')})")
            return rest, me.json(), meta

        if me.status_code == 401:
            ctx.logger.log("TOKEN_REJECTED", f"401 {meta.get('label')}")
            notice("DEAD TOKEN", ["401 | token revoked or invalid."], "bad")
            if not meta.get("ephemeral") and confirm("remove it from the vault?"):
                all_tokens = [t for t in load_tokens() if t.get("data") != meta.get("data")]
                save_tokens(all_tokens)
                ctx.logger.log("TOKEN_DELETED", f"auto {meta.get('label')}")
            continue

        notice("API ERROR", [f"status {me.status_code} | {me.text[:150]}"], "warn")
        continue


# ------------------------------------------------------------
# SECTION 2 | GUILD SELECTION
# ------------------------------------------------------------

CHANNEL_TYPES = {
    0: "text",
    2: "voice",
    4: "category",
    5: "announcement",
    13: "stage",
    15: "forum",
}
VERIF_LEVELS = {0: "none", 1: "low", 2: "medium", 3: "high", 4: "very high"}


def render_guild_list(ctx, me, guilds):
    print_header(ctx)
    console.print()
    console.print("[white]SELECT GUILD[/white] [dim]| servers this bot is in[/dim]")
    console.print()
    if not guilds:
        console.print(
            f"[dim]no guilds yet. invite link:[/dim]\n"
            f"[white]https://discord.com/oauth2/authorize?client_id="
            f"{me.get('id')}&permissions=8&scope=bot[/white]\n"
        )
        return
    table = Table(
        box=box.SIMPLE_HEAVY,
        header_style="brand",
        border_style="deep",
        padding=(0, 2),
    )
    table.add_column("#", justify="right", style="dim")
    table.add_column("NAME", style="white")
    table.add_column("ID", style="dim")
    table.add_column("OWNER", justify="center")
    table.add_column("ADMIN", justify="center")
    for i, g in enumerate(guilds):
        is_admin = bool(int(g.get("permissions", "0")) & (1 << 3))
        table.add_row(
            str(i + 1),
            g.get("name", "?"),
            g.get("id", "?"),
            "[orange]yes[/orange]" if g.get("owner") else "[faint]|[/faint]",
            "[orange]yes[/orange]" if is_admin else "[faint]|[/faint]",
        )
    console.print(table)
    console.print()


def pick_guild(ctx, rest, me):
    while True:
        try:
            r = rest.get_guilds()
        except ApiNetworkError as e:
            notice("NETWORK ERROR", [str(e)], "bad")
            if confirm("retry?", True):
                continue
            return None

        if r.status_code != 200:
            notice("API ERROR", [f"status {r.status_code} | {r.text[:150]}"], "bad")
            if confirm("retry?", True):
                continue
            return None

        guilds = r.json()
        present_frame(ctx, lambda: render_guild_list(ctx, me, guilds))

        if not guilds:
            if confirm("refresh after inviting?", True):
                continue
            return None

        choice = ask(f"1-{len(guilds)} select | R refresh | B back").lower()
        if choice == "r":
            continue
        if choice == "b":
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(guilds):
            chosen = guilds[int(choice) - 1]
            full = rest.get_guild(chosen["id"])
            guild = full.json() if full.status_code == 200 else chosen
            # The guild-list entry carries the bot's effective permission bits;
            # the full guild object does not. Capture it here so the workflow
            # engine can predict which ops are runnable.
            try:
                ctx.perms = int(chosen.get("permissions", "0"))
            except (TypeError, ValueError):
                ctx.perms = 0
            ctx.logger.log("GUILD_SELECTED", f"{guild.get('name')} ({guild.get('id')})")
            return guild
        notice("OUT OF RANGE", [f"expected 1-{len(guilds)}."], "warn")
        press_enter()


def attach_guild(ctx, guild):
    """Bind a freshly selected guild into the session.

    Restarts the ban/kick watchdog against the new guild and raises the
    on_guild_select flag consumed by workflow triggers.
    """
    if ctx.watchdog is not None:
        ctx.watchdog.stop()
        ctx.watchdog = None
    ctx.guild = guild
    ctx.guild_lost = False
    ctx.just_selected_guild = True
    if not bool(ctx.config.setting("ban_watch", True)):
        return

    def _lost():
        name = (ctx.guild or {}).get("name", "?")
        ctx.guild_lost = True
        ctx.pending_alerts.append(("GUILD_LOST", name))
        try:
            ctx.logger.log("GUILD_LOST",
                           f"{name} | bot is no longer a member (kicked or banned)")
        except Exception:
            pass

    interval = int(ctx.config.setting("ban_watch_secs", 15) or 15)
    ctx.watchdog = GuildWatchdog(ctx.rest, guild["id"], interval=interval, on_lost=_lost)
    ctx.watchdog.start()
    ctx.logger.log("WATCHDOG", f"watching {guild.get('name')} every {interval}s")


def drain_alerts(ctx):
    """Show anything async (watchdog events) on the next quiet moment."""
    while ctx.pending_alerts:
        kind, detail = ctx.pending_alerts.pop(0)
        if kind == "GUILD_LOST":
            notice(
                "BAN DETECTED",
                [
                    f"the bot is no longer a member of [white]{detail}[/white].",
                    "it was kicked or banned. ops against this guild will fail.",
                    "switch servers from settings, or re-invite the bot.",
                ],
                "bad",
            )


def _op_crash_report(ctx, exc, origin):
    """An op failed with something nobody planned for. Bundle everything."""
    try:
        report = build_crash_report(type(exc), exc, exc.__traceback__,
                                    ctx=ctx, origin=origin)
    except Exception as e2:
        notice("CRASH REPORT FAILED", [f"{type(e2).__name__}: {e2}"], "bad")
        return
    notice(
        "OP CRASHED",
        [
            f"[white]{type(exc).__name__}[/white]: {str(exc)[:150]}",
            "",
            "a full report was written to:",
            f"[white]{report}[/white]",
            "",
            f"send it to {MAINTAINER_CONTACT}",
            f"or {MAINTAINER_SERVER} and a fix will be supplied.",
        ],
        "bad",
    )


# ------------------------------------------------------------
# SECTION 3 | SNAPSHOT ENGINE
# ------------------------------------------------------------

def fetch_messages_for_channels(ctx, channels):
    rest = ctx.rest
    store = {}
    textish = {0, 5}
    targets = [c for c in channels if c.get("type") in textish]
    for i, c in enumerate(targets):
        console.print(f"[dim]messages {i + 1}/{len(targets)} | {c.get('name')}[/dim]")
        r = rest.get_messages(c["id"], limit=100)
        if r.status_code != 200:
            store[c["id"]] = {"error": r.status_code, "messages": []}
            continue
        msgs = []
        for m in reversed(r.json()):
            msgs.append(
                {
                    "id": m.get("id"),
                    "author": bot_display(m.get("author", {})),
                    "author_id": m.get("author", {}).get("id"),
                    "content": m.get("content", ""),
                    "timestamp": m.get("timestamp"),
                    "attachments": [
                        a.get("url") for a in m.get("attachments", []) if a.get("url")
                    ],
                }
            )
        store[c["id"]] = {"error": None, "messages": msgs}
    return store


def take_snapshot(ctx, reason="manual"):
    rest, guild = ctx.rest, ctx.guild
    gid = guild["id"]
    gname = guild.get("name", "?")

    notice(
        "AUTO-SNAPSHOT",
        [
            f"[white]{gname}[/white] ({gid})",
            "capturing roles, channels, members, bans, settings,",
            "onboarding, last 100 messages per channel",
        ],
    )

    # Warnings are collected rather than printed: console.status() now draws a
    # private screen region for the bouncing R, and anything printed inside it
    # would land in the middle of the animation.
    warnings = []
    with console.status("[orange]backing up server before destructive action...[/orange]") as st:
        st.update("[orange]fetching guild + roles + channels...[/orange]")
        g_r = rest.get_guild(gid)
        guild_full = g_r.json() if g_r.status_code == 200 else guild

        roles_r = rest.get_roles(gid)
        roles = roles_r.json() if roles_r.status_code == 200 else []
        if roles_r.status_code != 200:
            warnings.append(f"[warn]roles fetch failed: {roles_r.status_code}[/warn]")

        channels_r = rest.get_channels(gid)
        channels = channels_r.json() if channels_r.status_code == 200 else []
        if channels_r.status_code != 200:
            warnings.append(f"[warn]channels fetch failed: {channels_r.status_code}[/warn]")

        st.update("[orange]walking members + bans...[/orange]")
        mem_r, members = rest.get_all_members(gid)
        if mem_r.status_code == 403:
            warnings.append(
                "[warn]members 403 | enable SERVER MEMBERS intent for member capture[/warn]"
            )

        bans_r, bans = rest.get_all_bans(gid)
        if bans_r.status_code == 403:
            warnings.append("[dim]bans 403 | skipped (missing ban permission)[/dim]")

        st.update("[orange]reading onboarding...[/orange]")
        onb_r = rest.get_onboarding(gid)
        onboarding = onb_r.json() if onb_r.status_code == 200 else None
        if onb_r.status_code != 200:
            warnings.append(f"[dim]onboarding {onb_r.status_code} | skipped[/dim]")

    for line in warnings:
        console.print(line)

    messages_store = fetch_messages_for_channels(ctx, channels)

    settings_fields = [
        "name", "icon", "banner", "verification_level",
        "default_message_notifications", "explicit_content_filter", "afk_timeout",
        "afk_channel_id", "system_channel_id", "system_channel_flags",
        "rules_channel_id", "public_updates_channel_id", "preferred_locale",
        "description", "premium_tier", "vanity_url_code",
    ]
    settings = {k: guild_full.get(k) for k in settings_fields}

    blob = {
        "tool": TOOL_NAME,
        "version": TOOL_VERSION,
        "reason": reason,
        "taken_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "guild": {
            "id": gid,
            "name": gname,
            "owner_id": guild_full.get("owner_id"),
        },
        "settings": settings,
        "onboarding": onboarding,
        "roles": roles,
        "channels": channels,
        "members": [
            {
                "id": m.get("user", {}).get("id"),
                "name": bot_display(m.get("user", {})),
                "bot": m.get("user", {}).get("bot", False),
                "nick": m.get("nick"),
                "roles": m.get("roles", []),
                "joined_at": m.get("joined_at"),
            }
            for m in members
        ],
        "bans": [
            {
                "id": b.get("user", {}).get("id"),
                "name": bot_display(b.get("user", {})),
                "reason": b.get("reason"),
            }
            for b in bans
        ],
        "messages": messages_store,
    }

    server_dir = SNAPSHOTS_DIR / sanitize_name(gname)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir = server_dir / f"snap_{stamp}"
    internals = run_dir / "internals"
    for sub in ("Roles", "Members", "Server Settings", "Channels",
                "Messages", "Bans", "Onboarding"):
        (internals / sub).mkdir(parents=True, exist_ok=True)

    (run_dir / "server.dat").write_text(json.dumps(blob, indent=2), encoding="utf-8")

    info_txt = (
        f"NiNog Raker snapshot\n"
        f"====================\n"
        f"server name : {gname}\n"
        f"guild id    : {gid}\n"
        f"owner id    : {guild_full.get('owner_id')}\n"
        f"taken at    : {blob['taken_at']}\n"
        f"reason      : {reason}\n"
        f"roles       : {len(roles)}\n"
        f"channels    : {len(channels)}\n"
        f"members     : {len(blob['members'])}\n"
        f"bans        : {len(blob['bans'])}\n"
    )
    (run_dir / "server_info.txt").write_text(info_txt, encoding="utf-8")

    (internals / "Roles" / "roles.json").write_text(
        json.dumps(roles, indent=2), encoding="utf-8")
    (internals / "Channels" / "channels.json").write_text(
        json.dumps(channels, indent=2), encoding="utf-8")
    (internals / "Members" / "members.json").write_text(
        json.dumps(blob["members"], indent=2), encoding="utf-8")
    (internals / "Bans" / "bans.json").write_text(
        json.dumps(blob["bans"], indent=2), encoding="utf-8")
    (internals / "Server Settings" / "settings.json").write_text(
        json.dumps(settings, indent=2), encoding="utf-8")
    (internals / "Onboarding" / "onboarding.json").write_text(
        json.dumps(onboarding, indent=2) if onboarding else "null", encoding="utf-8")
    for cid, mstore in messages_store.items():
        cname = next((c.get("name", cid) for c in channels if c.get("id") == cid), cid)
        (internals / "Messages" / f"{sanitize_name(cname)}_{cid}.json").write_text(
            json.dumps(mstore, indent=2), encoding="utf-8"
        )

    notice(
        "SNAPSHOT SAVED",
        [
            f"[white]{gname}[/white] captured to:",
            f"[white]{run_dir}[/white]",
            f"[dim]roles {len(roles)} | channels {len(channels)} | members {len(blob['members'])} "
            f"| bans {len(blob['bans'])}[/dim]",
        ],
        "good",
    )
    return run_dir


def ensure_pre_op_snapshot(ctx, op_name):
    if not bool(ctx.config.setting("auto_snapshot", True)):
        return None
    try:
        return take_snapshot(ctx, reason=f"pre-op: {op_name}")
    except ApiNetworkError as e:
        notice("SNAPSHOT FAILED", [str(e), "continuing anyway."], "warn")
        return None


def load_snapshot_runs():
    runs = []
    if not SNAPSHOTS_DIR.exists():
        return runs
    for server_dir in sorted(SNAPSHOTS_DIR.iterdir()):
        if not server_dir.is_dir():
            continue
        for run_dir in sorted(server_dir.iterdir(), reverse=True):
            if run_dir.is_dir() and (run_dir / "server.dat").exists():
                runs.append((server_dir, run_dir))
    return runs


def pick_snapshot(ctx):
    runs = load_snapshot_runs()
    if not runs:
        notice("NO SNAPSHOTS", ["nothing stored in snapshots/ yet."], "warn")
        return None

    table = Table(
        box=box.SIMPLE_HEAVY,
        header_style="brand",
        border_style="deep",
        padding=(0, 2),
    )
    table.add_column("#", justify="right", style="dim")
    table.add_column("SERVER", style="white")
    table.add_column("RUN", style="orange")
    table.add_column("DETAILS", style="dim")
    for i, (sd, rd) in enumerate(runs):
        try:
            blob = json.loads((rd / "server.dat").read_text(encoding="utf-8"))
            detail = (
                f"{len(blob.get('roles', []))} roles | {len(blob.get('channels', []))} ch | "
                f"{len(blob.get('members', []))} members"
            )
        except Exception:
            blob, detail = None, "unreadable"
        label = blob["guild"]["name"] if blob else sd.name
        table.add_row(str(i + 1), label, rd.name.replace("snap_", ""), detail)
    console.print(table)

    choice = ask(f"1-{len(runs)} select | B back").lower()
    if choice == "b":
        return None
    if not (choice.isdigit() and 1 <= int(choice) <= len(runs)):
        notice("OUT OF RANGE", [f"expected 1-{len(runs)}."], "warn")
        return None

    sd, rd = runs[int(choice) - 1]
    try:
        blob = json.loads((rd / "server.dat").read_text(encoding="utf-8"))
    except Exception as e:
        notice("CORRUPT", [f"could not read server.dat | {e}"], "bad")
        return None
    ctx.logger.log("SNAPSHOT_LOADED", str(rd))
    return blob


# ------------------------------------------------------------
# SECTION 4 | RESTORE ENGINE
# ------------------------------------------------------------

def image_to_data_uri(ctx, url):
    if not url:
        return None
    try:
        r = ctx.rest.download(url)
        if r.status_code == 200 and r.content:
            ctype = r.headers.get("Content-Type", "image/png").split(";")[0]
            return f"data:{ctype};base64," + base64.b64encode(r.content).decode("ascii")
    except ApiNetworkError:
        return None
    return None


def restore_roles(ctx, blob):
    rest, gid = ctx.rest, ctx.guild["id"]
    roles = [r for r in blob.get("roles", []) if not r.get("managed") and r.get("id") != gid]
    roles.sort(key=lambda r: r.get("position", 0))

    id_map = {}
    created = failed = 0
    with console.status(f"[orange]recreating {len(roles)} roles...[/orange]") as status:
        for i, r in enumerate(roles):
            status.update(f"[orange]roles {i + 1}/{len(roles)} | {r.get('name')}[/orange]")
            body = {
                "name": r.get("name", "role"),
                "permissions": r.get("permissions", "0"),
                "color": r.get("color", 0),
                "hoist": r.get("hoist", False),
                "mentionable": r.get("mentionable", False),
            }
            resp = rest.create_role(gid, body, reason="NiNog Raker restore")
            if resp.status_code in (200, 201):
                id_map[r["id"]] = resp.json()["id"]
                created += 1
            else:
                failed += 1

    if id_map:
        positions = []
        for old_id, new_id in id_map.items():
            old = next((r for r in blob.get("roles", []) if r.get("id") == old_id), None)
            if old:
                positions.append({"id": new_id, "position": old.get("position", 1)})
        if positions:
            positions.sort(key=lambda p: p["position"])
            rest.patch_role_positions(gid, positions)

    ctx.logger.log("OP_RESULT", f"restore_roles | created={created} failed={failed}")
    console.print(
        f"[dim]roles |[/dim] [white]{created}[/white] [dim]created |[/dim] "
        f"[white]{failed}[/white] [dim]failed[/dim]"
    )
    return id_map


def remap_overwrites(overwrites, role_map):
    out = []
    for ow in overwrites or []:
        oid = ow.get("id")
        if ow.get("type") == 0 and oid in role_map:
            out.append({**ow, "id": role_map[oid]})
        elif ow.get("type") == 1:
            out.append(ow)
    return out


def restore_channels(ctx, blob, role_map):
    rest, gid = ctx.rest, ctx.guild["id"]
    channels = blob.get("channels", [])
    id_map = {}
    created = failed = 0

    categories = [c for c in channels if c.get("type") == 4]
    children = [c for c in channels if c.get("type") != 4]
    children.sort(key=lambda c: (c.get("parent_id") is None, c.get("position", 0)))

    def build_body(c):
        body = {
            "name": c.get("name", "channel"),
            "type": c.get("type", 0),
            "position": c.get("position", 0),
            "permission_overwrites": remap_overwrites(c.get("permission_overwrites"), role_map),
        }
        if c.get("type") in (0, 5):
            body["topic"] = c.get("topic")
            body["nsfw"] = c.get("nsfw", False)
            if c.get("rate_limit_per_user"):
                body["rate_limit_per_user"] = c["rate_limit_per_user"]
        if c.get("type") == 2:
            body["bitrate"] = min(c.get("bitrate", 64000), 96000)
            if c.get("user_limit"):
                body["user_limit"] = c["user_limit"]
        parent = c.get("parent_id")
        if parent and parent in id_map:
            body["parent_id"] = id_map[parent]
        return body

    with console.status(f"[orange]recreating {len(channels)} channels...[/orange]") as status:
        for i, c in enumerate(categories):
            status.update(
                f"[orange]categories {i + 1}/{len(categories)} | {c.get('name')}[/orange]"
            )
            resp = rest.create_channel(gid, build_body(c))
            if resp.status_code in (200, 201):
                id_map[c["id"]] = resp.json()["id"]
                created += 1
            else:
                failed += 1

        for i, c in enumerate(children):
            status.update(
                f"[orange]channels {i + 1}/{len(children)} | {c.get('name')}[/orange]"
            )
            resp = rest.create_channel(gid, build_body(c))
            if resp.status_code in (200, 201):
                id_map[c["id"]] = resp.json()["id"]
                created += 1
            else:
                failed += 1

    ctx.logger.log("OP_RESULT", f"restore_channels | created={created} failed={failed}")
    console.print(
        f"[dim]channels |[/dim] [white]{created}[/white] [dim]created |[/dim] "
        f"[white]{failed}[/white] [dim]failed[/dim]"
    )
    return id_map


def restore_settings(ctx, blob, channel_map, role_map):
    rest, gid = ctx.rest, ctx.guild["id"]
    s = blob.get("settings", {})

    body = {
        "name": s.get("name"),
        "verification_level": s.get("verification_level"),
        "default_message_notifications": s.get("default_message_notifications"),
        "explicit_content_filter": s.get("explicit_content_filter"),
        "afk_timeout": s.get("afk_timeout"),
        "system_channel_flags": s.get("system_channel_flags"),
        "preferred_locale": s.get("preferred_locale"),
        "description": s.get("description") or "",
    }
    for field in ("afk_channel_id", "system_channel_id", "rules_channel_id",
                  "public_updates_channel_id"):
        old = s.get(field)
        if old and old in channel_map:
            body[field] = channel_map[old]

    # Asset hashes belong to the SOURCE guild; building the CDN URL with the
    # target guild id 404s and the icon/banner restore silently skipped.
    src_gid = (blob.get("guild") or {}).get("id") or gid
    for asset_field in ("icon", "banner"):
        old_asset = s.get(asset_field)
        if not old_asset:
            continue
        ext = "gif" if "a_" in str(old_asset) else "png"
        url = (f"https://cdn.discordapp.com/"
               f"{'icons' if asset_field == 'icon' else 'banners'}/{src_gid}/{old_asset}.{ext}?size=1024")
        data_uri = image_to_data_uri(ctx, url)
        if data_uri:
            body[asset_field] = data_uri

    body = {k: v for k, v in body.items() if v is not None}
    r = rest.patch_guild(gid, body)
    ok = r.status_code == 200
    ctx.logger.log("OP_RESULT", f"restore_settings | {r.status_code}")
    console.print(
        "[dim]settings |[/dim] "
        + ("[good]applied[/good]" if ok else f"[bad]failed ({r.status_code})[/bad]")
    )
    return ok


def restore_onboarding(ctx, blob, channel_map, role_map):
    rest, gid = ctx.rest, ctx.guild["id"]
    onb = blob.get("onboarding")
    if not onb or not isinstance(onb, dict) or "prompts" not in onb:
        console.print("[dim]onboarding | not captured, skipped[/dim]")
        return

    prompts = []
    for p in onb.get("prompts", []):
        options = []
        for o in p.get("options", []):
            chan_ids = [channel_map[c] for c in o.get("channel_ids", []) if c in channel_map]
            role_ids = [role_map[r] for r in o.get("role_ids", []) if r in role_map]
            options.append(
                {
                    "id": o.get("id"),
                    "title": o.get("title"),
                    "description": o.get("description") or "",
                    "emoji": o.get("emoji") or {},
                    "channel_ids": chan_ids,
                    "role_ids": role_ids,
                }
            )
        prompts.append(
            {
                "id": p.get("id"),
                "title": p.get("title"),
                "type": p.get("type", 0),
                "options": options,
                "single_select": p.get("single_select", True),
                "required": p.get("required", False),
                "in_onboarding": p.get("in_onboarding", False),
            }
        )

    default_ids = [channel_map[c] for c in onb.get("default_channel_ids", [])
                   if c in channel_map]
    body = {"prompts": prompts, "default_channel_ids": default_ids,
            "enabled": onb.get("enabled", False)}

    r = rest.put_onboarding(gid, body)
    ctx.logger.log("OP_RESULT", f"restore_onboarding | {r.status_code}")
    console.print(
        "[dim]onboarding |[/dim] "
        + ("[good]applied[/good]" if r.status_code == 200
           else f"[warn]failed ({r.status_code})[/warn]")
    )


def restore_messages(ctx, blob, channel_map):
    rest = ctx.rest
    store = blob.get("messages", {})
    total = sum(len(v.get("messages", [])) for v in store.values())
    if total == 0:
        console.print("[dim]messages | none captured, skipped[/dim]")
        return

    replayed = skipped = 0
    pairs = [(cid, mstore) for cid, mstore in store.items() if cid in channel_map]
    with console.status(f"[orange]replaying {total} messages...[/orange]") as status:
        for i, (old_cid, mstore) in enumerate(pairs):
            new_cid = channel_map[old_cid]
            msgs = mstore.get("messages", [])
            if not msgs:
                continue
            status.update(
                f"[orange]channel {i + 1}/{len(pairs)} | {len(msgs)} messages[/orange]"
            )
            wh = rest.create_webhook(new_cid, "raker-restore")
            if wh.status_code not in (200, 201):
                skipped += len(msgs)
                continue
            url = wh.json().get("url")
            if not url:
                skipped += len(msgs)
                continue
            for m in msgs:
                content = m.get("content") or ""
                for att in m.get("attachments", []):
                    content += f"\n{att}"
                content = content.strip()
                if not content:
                    skipped += 1
                    continue
                try:
                    resp = rest.execute_webhook(
                        url,
                        {"content": content[:2000], "username": m.get("author", "unknown")},
                    )
                    if resp.status_code in (200, 204):
                        replayed += 1
                    else:
                        skipped += 1
                except ApiNetworkError:
                    skipped += 1
            rest.delete_webhook_by_url(url)

    ctx.logger.log("OP_RESULT", f"restore_messages | replayed={replayed} skipped={skipped}")
    console.print(
        f"[dim]messages |[/dim] [white]{replayed}[/white] [dim]replayed |[/dim] "
        f"[white]{skipped}[/white] [dim]skipped (embeds are not restorable)[/dim]"
    )


def maybe_restore_bans(ctx, blob):
    bans = blob.get("bans", [])
    if not bans:
        return
    if not confirm(f"re-apply [white]{len(bans)}[/white] bans from snapshot?"):
        return
    rest, gid = ctx.rest, ctx.guild["id"]
    applied = 0
    with console.status(f"[orange]re-applying {len(bans)} bans...[/orange]"):
        for b in bans:
            if b.get("id"):
                r = rest.ban_member(gid, b["id"])
                if r.status_code in (200, 204, 403):
                    applied += 1
    ctx.logger.log("OP_RESULT", f"restore_bans | applied={applied}/{len(bans)}")
    console.print(f"[dim]bans |[/dim] [white]{applied}[/white] [dim]re-applied[/dim]")


def print_member_note_and_invite(ctx, blob, channel_map):
    members = blob.get("members", [])
    humans = [m for m in members if not m.get("bot")]
    console.print(
        f"[dim]members |[/dim] [white]{len(members)}[/white] [dim]recorded "
        f"({len(humans)} humans). the API cannot force-join members | "
        f"they rejoin via invite.[/dim]"
    )
    if not confirm("create an invite link to share?", True):
        return
    s = blob.get("settings", {})
    target = None
    old = s.get("system_channel_id")
    if old and old in channel_map:
        target = channel_map[old]
    if target is None:
        for old_cid, new_cid in channel_map.items():
            old_ch = next((c for c in blob.get("channels", []) if c.get("id") == old_cid), None)
            if old_ch and old_ch.get("type") == 0:
                target = new_cid
                break
    if target is None:
        notice("NO TARGET", ["no text channel available for an invite."], "warn")
        return
    r = ctx.rest.create_invite(target, max_age=86400)
    if r.status_code == 200:
        code = r.json().get("code")
        notice("INVITE READY", [f"[white]https://discord.gg/{code}[/white] [dim](24h)[/dim]"], "good")
        ctx.logger.log("OP_RESULT", f"invite created {code}")
    else:
        notice("INVITE FAILED", [f"status {r.status_code}"], "warn")


def run_restore(ctx, scope):
    blob = pick_snapshot(ctx)
    if blob is None:
        return

    src = f"{blob['guild']['name']} ({blob['guild']['id']})"
    dst = f"{ctx.guild.get('name')} ({ctx.guild['id']})"
    notice(
        "RESTORE TARGET",
        [
            f"snapshot: [white]{src}[/white]",
            f"target:   [white]{dst}[/white]",
            f"scope:    [white]{scope}[/white]",
            "applied on top of the current server.",
        ],
    )
    if not confirm("proceed?"):
        return

    role_map = {}
    channel_map = {}
    if scope in ("full", "roles"):
        role_map = restore_roles(ctx, blob)
    if scope in ("full", "channels"):
        channel_map = restore_channels(ctx, blob, role_map)
    if scope in ("full", "settings"):
        restore_settings(ctx, blob, channel_map, role_map)
        restore_onboarding(ctx, blob, channel_map, role_map)
    if scope == "full":
        restore_messages(ctx, blob, channel_map)
        maybe_restore_bans(ctx, blob)
        print_member_note_and_invite(ctx, blob, channel_map)

    ctx.logger.log("OP_DONE", f"restore {scope} from {src}")
    notice("RESTORE COMPLETE", [f"scope [white]{scope}[/white] finished."], "good")


# ------------------------------------------------------------
# SECTION 5 | OFFENCE OPS
# ------------------------------------------------------------

def op_ban_all(ctx):
    rest, guild = ctx.rest, ctx.guild
    gid = guild["id"]
    me_id = ctx.me.get("id")
    owner_id = guild.get("owner_id")
    wl = whitelist_ids()

    ensure_pre_op_snapshot(ctx, "BAN ALL")

    r, members = rest.get_all_members(gid)
    if r.status_code == 403:
        notice("FORBIDDEN", ["member listing needs the SERVER MEMBERS intent."], "bad")
        return
    if r.status_code != 200 and not members:
        notice("API ERROR", [f"status {r.status_code}"], "bad")
        return

    targets, skipped_wl = [], 0
    for m in members:
        uid = m.get("user", {}).get("id")
        if uid in (me_id, owner_id):
            continue
        if uid in wl:
            skipped_wl += 1
            continue
        targets.append(m)

    if not targets:
        notice("NOTHING TO BAN", ["no bannable members found."], "warn")
        return
    extra = f" | [white]{skipped_wl}[/white] whitelisted skipped" if skipped_wl else ""
    if not confirm(f"ban [white]{len(targets)}[/white] members{extra}?"):
        return

    banned = failed = 0
    with console.status(f"[orange]banning {len(targets)} members...[/orange]") as status:
        for i, m in enumerate(targets):
            uid = m["user"]["id"]
            status.update(
                f"[orange]banning {i + 1}/{len(targets)} | {bot_display(m['user'])}[/orange]"
            )
            resp = rest.ban_member(gid, uid)
            if resp.status_code in (200, 204):
                banned += 1
            else:
                failed += 1

    ctx.logger.log("OP_RESULT",
                   f"ban_all | banned={banned} failed={failed} wl_skipped={skipped_wl}")
    console.print(
        f"[dim]done |[/dim] [white]{banned}[/white] [dim]banned |[/dim] "
        f"[white]{failed}[/white] [dim]failed |[/dim] [white]{skipped_wl}[/white] [dim]whitelist skipped[/dim]"
    )


def op_unban_all(ctx):
    rest, gid = ctx.rest, ctx.guild["id"]

    r, bans = rest.get_all_bans(gid)
    if r.status_code == 403:
        notice("FORBIDDEN", ["missing BAN MEMBERS permission."], "bad")
        return
    if r.status_code != 200:
        notice("API ERROR", [f"status {r.status_code}"], "bad")
        return
    if not bans:
        notice("EMPTY", ["no bans on this server."], "good")
        return
    if not confirm(f"unban all [white]{len(bans)}[/white]?"):
        return

    unbanned = failed = 0
    with console.status(f"[orange]unbanning {len(bans)}...[/orange]") as status:
        for i, b in enumerate(bans):
            uid = b.get("user", {}).get("id")
            status.update(f"[orange]unbanning {i + 1}/{len(bans)}[/orange]")
            resp = rest.unban_member(gid, uid)
            if resp.status_code in (200, 204):
                unbanned += 1
            else:
                failed += 1

    ctx.logger.log("OP_RESULT", f"unban_all | unbanned={unbanned} failed={failed}")
    console.print(
        f"[dim]done |[/dim] [white]{unbanned}[/white] [dim]unbanned |[/dim] "
        f"[white]{failed}[/white] [dim]failed[/dim]"
    )


def op_wipe_channels(ctx):
    rest, gid = ctx.rest, ctx.guild["id"]
    ensure_pre_op_snapshot(ctx, "WIPE CHANNELS")

    r = rest.get_channels(gid)
    if r.status_code != 200:
        notice("API ERROR", [f"status {r.status_code}"], "bad")
        return
    channels = r.json()
    if not channels:
        notice("EMPTY", ["no channels to delete."], "warn")
        return
    if not confirm(f"delete ALL [white]{len(channels)}[/white] channels?"):
        return

    deleted = failed = 0
    with console.status(f"[orange]deleting {len(channels)} channels...[/orange]") as status:
        for i, c in enumerate(channels):
            status.update(f"[orange]channel {i + 1}/{len(channels)} | {c.get('name')}[/orange]")
            resp = rest.delete_channel(c["id"], reason="NiNog Raker")
            if resp.status_code in (200, 204):
                deleted += 1
            else:
                failed += 1

    ctx.logger.log("OP_RESULT", f"wipe_channels | deleted={deleted} failed={failed}")
    console.print(
        f"[dim]done |[/dim] [white]{deleted}[/white] [dim]deleted |[/dim] "
        f"[white]{failed}[/white] [dim]failed[/dim]"
    )


def op_flood_channels(ctx):
    rest, gid = ctx.rest, ctx.guild["id"]
    ensure_pre_op_snapshot(ctx, "FLOOD CHANNELS")

    name = ask("channel name", "raker")
    if not name:
        return
    count = ask_int("how many", 50)
    count = max(1, min(500, count))
    ctype = ask_int("type (0 text / 2 voice)", 0)
    ctype = 0 if ctype != 2 else 2
    if not confirm(f"create [white]{count}[/white] channels named [white]{name}[/white]?"):
        return

    created = failed = 0
    with console.status(f"[orange]creating {count} channels...[/orange]") as status:
        for i in range(count):
            status.update(f"[orange]channel {i + 1}/{count}[/orange]")
            resp = rest.create_channel(gid, {"name": f"{name}-{i + 1}", "type": ctype})
            if resp.status_code in (200, 201):
                created += 1
            else:
                failed += 1

    ctx.logger.log("OP_RESULT", f"flood_channels | created={created} failed={failed}")
    console.print(
        f"[dim]done |[/dim] [white]{created}[/white] [dim]created |[/dim] "
        f"[white]{failed}[/white] [dim]failed[/dim]"
    )


def op_wipe_roles(ctx):
    rest, gid = ctx.rest, ctx.guild["id"]
    ensure_pre_op_snapshot(ctx, "WIPE ROLES")

    r = rest.get_roles(gid)
    if r.status_code != 200:
        notice("API ERROR", [f"status {r.status_code}"], "bad")
        return
    roles = [x for x in r.json() if x.get("id") != gid and not x.get("managed")]
    if not roles:
        notice("EMPTY", ["no deletable roles."], "warn")
        return
    if not confirm(f"delete ALL [white]{len(roles)}[/white] deletable roles?"):
        return

    deleted = failed = 0
    with console.status(f"[orange]deleting {len(roles)} roles...[/orange]") as status:
        for i, role in enumerate(roles):
            status.update(f"[orange]role {i + 1}/{len(roles)} | {role.get('name')}[/orange]")
            resp = rest.delete_role(gid, role["id"])
            if resp.status_code in (200, 204):
                deleted += 1
            else:
                failed += 1

    ctx.logger.log("OP_RESULT", f"wipe_roles | deleted={deleted} failed={failed}")
    console.print(
        f"[dim]done |[/dim] [white]{deleted}[/white] [dim]deleted |[/dim] "
        f"[white]{failed}[/white] [dim]failed[/dim]"
    )


def op_flood_roles(ctx):
    rest, gid = ctx.rest, ctx.guild["id"]
    ensure_pre_op_snapshot(ctx, "FLOOD ROLES")

    name = ask("role name", "raided")
    if not name:
        return
    count = ask_int("how many", 50)
    count = max(1, min(500, count))
    color = ask("color hex (e.g. FF6A00, blank = default)", "").lstrip("#")
    if not confirm(f"create [white]{count}[/white] roles named [white]{name}[/white]?"):
        return

    body = {"hoist": True, "mentionable": True}
    if re.fullmatch(r"[0-9a-fA-F]{6}", color or ""):
        body["color"] = int(color, 16)

    created = failed = 0
    with console.status(f"[orange]creating {count} roles...[/orange]") as status:
        for i in range(count):
            status.update(f"[orange]role {i + 1}/{count}[/orange]")
            resp = rest.create_role(gid, {**body, "name": f"{name}-{i + 1}"},
                                    reason="NiNog Raker")
            if resp.status_code in (200, 201):
                created += 1
            else:
                failed += 1

    ctx.logger.log("OP_RESULT", f"flood_roles | created={created} failed={failed}")
    console.print(
        f"[dim]done |[/dim] [white]{created}[/white] [dim]created |[/dim] "
        f"[white]{failed}[/white] [dim]failed[/dim]"
    )


def op_mass_kick(ctx):
    rest, guild = ctx.rest, ctx.guild
    gid = guild["id"]
    me_id = ctx.me.get("id")
    owner_id = guild.get("owner_id")
    wl = whitelist_ids()
    ensure_pre_op_snapshot(ctx, "MASS KICK")

    r, members = rest.get_all_members(gid)
    if r.status_code == 403:
        notice("FORBIDDEN", ["member listing needs the SERVER MEMBERS intent."], "bad")
        return
    if r.status_code != 200 and not members:
        notice("API ERROR", [f"status {r.status_code}"], "bad")
        return

    targets, skipped_wl = [], 0
    for m in members:
        uid = m.get("user", {}).get("id")
        if uid in (me_id, owner_id):
            continue
        if uid in wl:
            skipped_wl += 1
            continue
        targets.append(m)

    if not targets:
        notice("NOTHING TO KICK", ["no kickable members found."], "warn")
        return
    extra = f" | [white]{skipped_wl}[/white] whitelisted skipped" if skipped_wl else ""
    if not confirm(f"kick [white]{len(targets)}[/white] members{extra}?"):
        return

    kicked = failed = 0
    with console.status(f"[orange]kicking {len(targets)} members...[/orange]") as status:
        for i, m in enumerate(targets):
            uid = m["user"]["id"]
            status.update(
                f"[orange]kicking {i + 1}/{len(targets)} | {bot_display(m['user'])}[/orange]"
            )
            resp = rest.kick_member(gid, uid)
            if resp.status_code in (200, 204):
                kicked += 1
            else:
                failed += 1

    ctx.logger.log("OP_RESULT",
                   f"mass_kick | kicked={kicked} failed={failed} wl_skipped={skipped_wl}")
    console.print(
        f"[dim]done |[/dim] [white]{kicked}[/white] [dim]kicked |[/dim] "
        f"[white]{failed}[/white] [dim]failed |[/dim] [white]{skipped_wl}[/white] [dim]whitelist skipped[/dim]"
    )


def op_rename_server(ctx):
    rest, gid = ctx.rest, ctx.guild["id"]
    ensure_pre_op_snapshot(ctx, "RENAME SERVER")

    new_name = ask("new server name")
    if not new_name:
        return
    if not confirm(f"rename to [white]{new_name}[/white]?"):
        return
    r = rest.patch_guild(gid, {"name": new_name})
    ok = r.status_code == 200
    ctx.logger.log("OP_RESULT", f"rename_server | {r.status_code} | {new_name}")
    notice(
        "RENAMED" if ok else "FAILED",
        [new_name if ok else f"status {r.status_code}"],
        "good" if ok else "bad",
    )


# ------------------------------------------------------------
# SECTION 5b | UNIVERSAL MEMBER PICKER
# ------------------------------------------------------------
# Every op that used to demand a raw user id now opens this. Five ways to
# find a person; single-pick and multi-pick share the same machinery.

SERVER_PICK_CAP = 2000        # hard cap for the server browser
SERVER_PICK_PAGE = 200        # rows per page in the server browser
FUZZY_PICK_PAGE = 25          # rows per page in search results


def _rest_token(ctx):
    auth = ctx.rest.session.headers.get("Authorization", "")
    return auth.split(" ", 1)[1] if " " in auth else auth


def _member_norm(m):
    u = m.get("user", {}) if isinstance(m, dict) else {}
    return {"id": u.get("id"), "name": bot_display(u), "member": m}


def _fetch_members_capped(ctx, cap=SERVER_PICK_CAP):
    """Members via the paginator, capped. Returns (members, over_cap, err)."""
    rest, gid = ctx.rest, ctx.guild["id"]
    approx = ctx.guild.get("approximate_member_count") or 0
    r, members = rest.get_all_members(gid)
    if r.status_code == 403:
        return [], approx > cap, "403 | member listing needs the SERVER MEMBERS intent"
    if r.status_code != 200 and not members:
        return [], approx > cap, f"status {r.status_code}"
    over_cap = (approx and approx > cap) or len(members) > cap
    return members[:cap], bool(over_cap), None


def _presence_order(ctx, members):
    """Sort online first, offline second. Returns (ordered, presence_known).

    Presence comes from the optional one-shot gateway fetch in core; without
    the websockets package or the PRESENCE intent the list keeps API order
    and the browser says so instead of faking dots.
    """
    with console.status("[orange]fetching who is online via gateway...[/orange]"):
        online_ids = fetch_online_ids(_rest_token(ctx), ctx.guild["id"])
    if online_ids is None:
        return list(members), False
    for m in members:
        m["_online"] = m.get("user", {}).get("id") in online_ids
    ordered = sorted(members, key=lambda m: 0 if m.get("_online") else 1)
    return ordered, True


def _browse_rows(rows, title, multi, page_size, note=""):
    """Paginated pick table. rows = [(markup, data)]. Returns [data] or None."""
    if not rows:
        notice("EMPTY", ["nothing to show here."], "warn")
        return None
    page_size = max(1, page_size)
    pages = chunk(rows, page_size)
    page_idx = 0
    chosen = set()
    while True:
        page = pages[page_idx]
        offset = page_idx * page_size
        console.print()
        console.print(f"[white]{title}[/white] [dim]| page {page_idx + 1}/{len(pages)}"
                      f" | {len(rows)} rows[/dim]")
        if note:
            console.print(f"[dim]{note}[/dim]")
        console.print()
        for i, (text, _data) in enumerate(page):
            gi = offset + i
            mark = "[orange][x][/orange] " if (multi and gi in chosen) else ""
            console.print(f"  {mark}[orange][{gi + 1:03d}][/orange] {text}")
        console.print()
        hints = []
        if len(pages) > 1:
            hints += ["[white]N[/white] next", "[white]P[/white] prev"]
        if multi:
            hints += ["[white]#[/white] toggle", "[white]A[/white] page",
                      "[white]D[/white] done"]
        else:
            hints.append("[white]#[/white] select")
        hints.append("[white]B[/white] back")
        raw = ask("  [dim]|[/dim]  ".join(hints)).lower().strip()
        if raw in ("b", "q"):
            return None
        if raw == "n" and len(pages) > 1:
            page_idx = (page_idx + 1) % len(pages)
            continue
        if raw == "p" and len(pages) > 1:
            page_idx = (page_idx - 1) % len(pages)
            continue
        if multi and raw == "d":
            return [rows[i][1] for i in sorted(chosen)] or None
        if multi and raw == "a":
            ids_here = {offset + i for i in range(len(page))}
            if ids_here <= chosen:
                chosen -= ids_here
            else:
                chosen |= ids_here
            continue
        if raw.isdigit():
            gi = int(raw) - 1
            if 0 <= gi < len(rows):
                if multi:
                    chosen.symmetric_difference_update({gi})
                else:
                    return [rows[gi][1]]
            continue
        notice("OUT OF RANGE", [f"expected 1-{len(rows)} | N P B."], "warn")


def _pick_by_id(ctx, multi):
    if multi:
        raw = ask("user ids, comma or space separated")
        ids = [t for t in re.split(r"[,\s]+", raw or "") if t.isdigit()]
        if not ids:
            notice("BAD IDS", ["no digit tokens found."], "warn")
            return None
        return [{"id": uid, "name": f"user {uid}", "member": None} for uid in ids]
    uid = ask("user id")
    if not uid or not uid.isdigit():
        notice("BAD ID", ["digits only."], "warn")
        return None
    r = ctx.rest.get_member(ctx.guild["id"], uid)
    if r.status_code == 200:
        return [_member_norm(r.json())]
    if confirm(f"could not verify {uid} against the guild | use it anyway?"):
        return [{"id": uid, "name": f"user {uid}", "member": None}]
    return None


def _pick_from_whitelist(ctx, multi):
    entries = load_whitelist()
    if not entries:
        notice("EMPTY", ["whitelist is empty."], "warn")
        return None
    rows = [
        (f"[white]{e.get('name', '?')}[/white] [dim]({e.get('id')})[/dim]",
         {"id": e.get("id"), "name": e.get("name", "?"), "member": None})
        for e in entries
    ]
    note = "whitelisted users | checkboxes, D when done" if multi else "whitelisted users"
    return _browse_rows(rows, "PICK FROM WHITELIST", multi, FUZZY_PICK_PAGE, note)


def _pick_from_server(ctx, multi):
    approx = ctx.guild.get("approximate_member_count") or 0
    over_cap = bool(approx and approx > SERVER_PICK_CAP)
    if over_cap:
        notice("LARGE SERVER",
               [f"this server reports about {approx} members.",
                f"only the first {SERVER_PICK_CAP} fetched members can be shown."],
               "warn")
        if not confirm("continue with the first 2000?", True):
            return None
    with console.status("[orange]fetching member list...[/orange]"):
        members, _trunc, err = _fetch_members_capped(ctx)
    if err:
        notice("CANNOT LIST", [err], "bad")
        return None
    members, presence = _presence_order(ctx, members)

    def row(m):
        u = m.get("user", {})
        dot = ""
        if presence:
            dot = "[good]●[/good] " if m.get("_online") else "[dim]○[/dim] "
        bot = " [faint](bot)[/faint]" if u.get("bot") else ""
        return (f"{dot}[white]{bot_display(u)}[/white]{bot} [dim]({u.get('id')})[/dim]",
                _member_norm(m))

    rows = [row(m) for m in members]
    note = ("online members first, offline second"
            if presence else
            "presence unavailable (needs websockets pkg + PRESENCE intent) | api order")
    if over_cap:
        note += f" | server over 2k | showing first {SERVER_PICK_CAP}"
    return _browse_rows(rows, "PICK FROM SERVER", multi, SERVER_PICK_PAGE, note)


def _pick_by_username(ctx, multi):
    query = ask("username to search")
    if not query:
        return None
    with console.status("[orange]scanning server members...[/orange]"):
        r, members = ctx.rest.get_all_members(ctx.guild["id"])
    if r.status_code == 403:
        notice("FORBIDDEN", ["member listing needs the SERVER MEMBERS intent."], "bad")
        return None
    if r.status_code != 200 and not members:
        notice("API ERROR", [f"status {r.status_code}"], "bad")
        return None

    # Index every name a member answers to: display name, username, nick.
    lookup = {}
    for m in members:
        u = m.get("user", {})
        norm = _member_norm(m)
        for key in (bot_display(u), u.get("username", ""), m.get("nick") or ""):
            if key:
                lookup.setdefault(key, norm)

    if not lookup:
        notice("EMPTY", ["no members returned by the API."], "warn")
        return None

    exact = [norm for key, norm in lookup.items() if key.lower() == query.lower()]
    ranked_keys = difflib.get_close_matches(query, list(lookup.keys()), n=50, cutoff=0.35)
    ranked = exact + [lookup[k] for k in ranked_keys]
    seen, results = set(), []
    for norm in ranked:
        if norm["id"] not in seen:
            seen.add(norm["id"])
            results.append(norm)
    if not results:
        notice("NO MATCHES", [f"nothing similar to '{query}' found."], "warn")
        return None
    rows = [
        (f"[white]{n['name']}[/white] [dim]({n['id']})[/dim]"
         + (" [faint](bot)[/faint]" if (n["member"] or {}).get("user", {}).get("bot") else ""),
         n)
        for n in results
    ]
    return _browse_rows(rows, f'MATCHES FOR "{query}"', multi, FUZZY_PICK_PAGE,
                        "exact hits first, then closest matches")


def _pick_from_snapshot(ctx, multi):
    blob = pick_snapshot(ctx)
    if not blob:
        return None
    members = blob.get("members", [])
    if not members:
        notice("EMPTY", ["that snapshot recorded no members."], "warn")
        return None
    gname = (blob.get("guild") or {}).get("name", "?")
    rows = [
        (f"[white]{m.get('name', '?')}[/white] [dim]({m.get('id')})[/dim]"
         + (" [faint](bot)[/faint]" if m.get("bot") else ""),
         {"id": m.get("id"), "name": m.get("name", "?"),
          "member": {"user": {"id": m.get("id")}, "roles": m.get("roles", [])}})
        for m in members if m.get("id")
    ]
    return _browse_rows(rows, "PICK FROM SNAPSHOT", multi, FUZZY_PICK_PAGE,
                        f"recorded in {gname} | works without the members intent")


_PICK_METHODS = [
    ("User ID", "direct entry, verified when possible", _pick_by_id),
    ("From Whitelist", "saved whitelist, checkboxes", _pick_from_whitelist),
    ("From Server", "paginated browser | online first | 2k cap", _pick_from_server),
    ("Username", "fuzzy scan, paginated matches", _pick_by_username),
    ("From Snapshot", "members recorded in a snapshot", _pick_from_snapshot),
]


def pick_members(ctx, multi=False, title="PICK TARGET", allow_bots=True):
    """The one entry point for choosing people. Returns [norm] or None."""
    approx = (ctx.guild or {}).get("approximate_member_count") or 0
    while True:
        console.print()
        console.print(f"[white]{title}[/white] [dim]| {'multi-select' if multi else 'single'}[/dim]")
        console.print()
        for i, (name, desc, _fn) in enumerate(_PICK_METHODS):
            greyed = (name == "From Server" and approx and approx > SERVER_PICK_CAP)
            style = "dim" if greyed else "white"
            tail = " [warn](server over 2k members)[/warn]" if greyed else f" [dim]| {desc}[/dim]"
            console.print(f"  [orange][{i + 1}][/orange] [{style}]{name}[/{style}]{tail}")
        console.print("  [orange][B][/orange] [dim]back[/dim]")
        raw = ask("pick a method").lower().strip()
        if raw in ("b", "q", ""):
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(_PICK_METHODS):
            fn = _PICK_METHODS[int(raw) - 1][2]
            got = fn(ctx, multi)
            if not got:
                continue
            if not allow_bots:
                got = [g for g in got if not (g["member"] or {}).get("user", {}).get("bot")]
                if not got:
                    notice("ONLY BOTS", ["selection was only bots. try again."], "warn")
                    continue
            return got
        notice("OUT OF RANGE", [f"1-{len(_PICK_METHODS)} | B."], "warn")


def pick_one(ctx, title="PICK TARGET", allow_bots=True):
    got = pick_members(ctx, multi=False, title=title, allow_bots=allow_bots)
    return got[0] if got else None


# ------------------------------------------------------------
# SECTION 5c | ROLE ENGINE
# ------------------------------------------------------------
# The old Get Admin op is dead; this is its grown-up replacement. Build a
# permission profile, aim it at any population (whitelist, handpicked,
# everyone, everyone-but-N), choose shared or per-user roles, execute.

MAX_GUILD_ROLES = 250       # Discord's hard cap; overflow is detected before writing

ROLE_PRESETS = [
    ("Administrator", ["ADMINISTRATOR"]),
    ("Moderator", ["BAN_MEMBERS", "KICK_MEMBERS", "MANAGE_MESSAGES",
                   "MANAGE_NICKNAMES", "MODERATE_MEMBERS"]),
    ("Channel Manager", ["MANAGE_CHANNELS", "MANAGE_WEBHOOKS", "MANAGE_MESSAGES"]),
    ("View Only", ["VIEW_CHANNEL"]),
    ("Custom", []),
]

_ROLE_SYL_A = ["ra", "tzi", "no", "gul", "vek", "sha", "kor", "zi", "mite",
               "thal", "ber", "quen", "dro", "vas", "nyx", "orr", "sai", "lume"]
_ROLE_SYL_B = ["kan", "gore", "vex", "tine", "rax", "maw", "lith", "dusk",
               "fang", "wraith", "brand", "hex", "claw", "mire", "shade"]


def random_role_name(used):
    for _ in range(100):
        n = random.choice(_ROLE_SYL_A) + random.choice(_ROLE_SYL_B)
        if random.random() < 0.35:
            n += random.choice(_ROLE_SYL_B)
        n = n.capitalize()
        if random.random() < 0.25:
            n += f"-{random.randint(10, 99)}"
        if n not in used:
            used.add(n)
            return n
    n = f"role-{random.randint(1000, 9999)}"
    used.add(n)
    return n


def _perm_bits(names):
    bits = 0
    for n in names:
        bits |= PERMISSION_BITS.get(n, 0)
    return bits


def _role_permission_picker(ctx):
    """Returns (permission_names, bits) or None."""
    console.print()
    screen_title("ROLE PERMISSIONS", "preset, or a custom toggle list")
    for i, (name, perms) in enumerate(ROLE_PRESETS):
        detail = ", ".join(perms) if perms else "pick each bit yourself"
        console.print(f"  [orange][{i + 1}][/orange] [white]{name}[/white] [dim]| {detail}[/dim]")
    raw = ask(f"1-{len(ROLE_PRESETS)} | B back").lower().strip()
    if raw in ("b", "", "q"):
        return None
    if not (raw.isdigit() and 1 <= int(raw) <= len(ROLE_PRESETS)):
        notice("OUT OF RANGE", [f"1-{len(ROLE_PRESETS)}."], "warn")
        return None
    preset_name, preset_perms = ROLE_PRESETS[int(raw) - 1]
    if preset_name != "Custom":
        if ("ADMINISTRATOR" in preset_perms
                and not (ctx.perms & PERMISSION_BITS["ADMINISTRATOR"])):
            notice("NOTE", ["the bot is not administrator; granting an admin role",
                            "can be denied by the role hierarchy."], "warn")
        return preset_perms, _perm_bits(preset_perms)

    keys = list(PERMISSION_BITS.keys())
    on = set()
    while True:
        console.print()
        for i, k in enumerate(keys):
            mark = "[orange][x][/orange]" if k in on else "[faint][ ][/faint]"
            console.print(f"  {mark} [orange][{i + 1:02d}][/orange] [white]{k}[/white]")
        raw = ask("toggle number | D done | B cancel").lower().strip()
        if raw in ("b", "q"):
            return None
        if raw in ("d", ""):
            if not on:
                notice("NONE", ["no permissions selected."], "warn")
                continue
            chosen = sorted(on)
            return chosen, _perm_bits(chosen)
        if raw.isdigit() and 1 <= int(raw) <= len(keys):
            on.symmetric_difference_update({keys[int(raw) - 1]})
            continue
        notice("OUT OF RANGE", [f"1-{len(keys)} | D | B."], "warn")


def _role_targets(ctx):
    """Pick the population to receive roles. Returns [norm] or None."""
    console.print()
    screen_title("ROLE TARGETS", "who gets the roles")
    options = [
        "All whitelisted users",
        "Handpick from whitelist",
        "Handpick from the server",
        "Everyone in the server",
        "Everyone in the server EXCEPT...",
        "A specific user (any picker method)",
    ]
    for i, o in enumerate(options):
        console.print(f"  [orange][{i + 1}][/orange] [white]{o}[/white]")
    raw = ask(f"1-{len(options)} | B back").lower().strip()
    if raw in ("b", "", "q"):
        return None
    if not (raw.isdigit() and 1 <= int(raw) <= len(options)):
        notice("OUT OF RANGE", [f"1-{len(options)}."], "warn")
        return None
    mode = int(raw)

    me_id = (ctx.me or {}).get("id")
    owner_id = ctx.guild.get("owner_id")

    def finish(norm_list):
        if not norm_list:
            return None
        out, dropped = [], 0
        for n in norm_list:
            if not n.get("id") or n["id"] in (me_id, owner_id):
                dropped += 1
                continue
            out.append(n)
        if dropped:
            console.print(f"[dim]{dropped} target(s) dropped | the bot itself and the "
                          f"guild owner cannot be edited.[/dim]")
        if not out:
            notice("NO TARGETS", ["nothing left after filtering."], "warn")
            return None
        console.print(f"[dim]{len(out)} target(s) selected.[/dim]")
        return out

    if mode == 1:
        entries = load_whitelist()
        if not entries:
            notice("EMPTY", ["whitelist is empty."], "warn")
            return None
        return finish([{"id": e.get("id"), "name": e.get("name", "?"), "member": None}
                       for e in entries])
    if mode == 2:
        got = _pick_from_whitelist(ctx, multi=True)
        return finish(got) if got else None
    if mode == 3:
        got = _pick_from_server(ctx, multi=True)
        return finish(got) if got else None
    if mode in (4, 5):
        with console.status("[orange]fetching every member...[/orange]"):
            r, members = ctx.rest.get_all_members(ctx.guild["id"])
        if r.status_code == 403:
            notice("FORBIDDEN", ["mass targeting needs the SERVER MEMBERS intent."], "bad")
            return None
        if r.status_code != 200 and not members:
            notice("API ERROR", [f"status {r.status_code}"], "bad")
            return None
        everyone = [_member_norm(m) for m in members if not m.get("user", {}).get("bot")]
        if mode == 4:
            if not confirm(f"target [white]{len(everyone)}[/white] humans?"):
                return None
            return finish(everyone)
        excluded = pick_members(ctx, multi=True, title="EXCLUDE THESE") or []
        excluded_ids = {e["id"] for e in excluded}
        kept = [n for n in everyone if n["id"] not in excluded_ids]
        console.print(f"[dim]{len(kept)} kept, {len(everyone) - len(kept)} excluded.[/dim]")
        if not kept:
            notice("NO TARGETS", ["the exclusion list swallowed everyone."], "warn")
            return None
        if not confirm(f"target [white]{len(kept)}[/white] humans?"):
            return None
        return finish(kept)
    if mode == 6:
        got = pick_members(ctx, multi=False, title="ROLE TARGET")
        return finish(got) if got else None
    return None


def _check_role_overflow(existing_count, needed):
    """Role overflow detection. Returns how many roles we may create."""
    room = MAX_GUILD_ROLES - existing_count
    console.print(
        f"[dim]role budget | existing {existing_count} | needed {needed} | "
        f"guild cap {MAX_GUILD_ROLES} | room {room}[/dim]"
    )
    if needed <= room:
        return needed
    notice(
        "ROLE OVERFLOW",
        [
            f"creating [white]{needed}[/white] roles would pass the "
            f"{MAX_GUILD_ROLES}-role cap.",
            f"there is room for [white]{max(0, room)}[/white] more.",
            "",
            "the run will be trimmed to fit. delete some roles if you need more.",
        ],
        "bad",
    )
    if room <= 0:
        return 0
    if not confirm(f"continue with only [white]{room}[/white] roles?"):
        return 0
    return room


def op_role_engine(ctx):
    rest, gid = ctx.rest, ctx.guild["id"]
    screen_title("ROLE ENGINE", "roles with real permissions, aimed at anyone")

    targets = _role_targets(ctx)
    if not targets:
        return

    picked = _role_permission_picker(ctx)
    if not picked:
        return
    perm_names, perm_bits = picked

    console.print()
    screen_title("DELIVERY", "how the roles are shaped")
    console.print("  [orange][1][/orange] [white]One shared role[/white]"
                  " [dim]| assign it to every target[/dim]")
    console.print("  [orange][2][/orange] [white]Separate role per user[/white]"
                  " [dim]| a unique role for each target[/dim]")
    raw = ask("mode | B back").lower().strip()
    if raw in ("b", "", "q"):
        return
    if raw not in ("1", "2"):
        notice("OUT OF RANGE", ["1 | 2 | B."], "warn")
        return
    separate = raw == "2"

    name = ask("role name (blank = randomized names)", "")
    random_names = not (name or "").strip()
    color_raw = (ask("color hex, R random per role, blank none", "") or "").strip()
    random_color = color_raw.lower() == "r"
    fixed_color = None
    if color_raw and not random_color and re.fullmatch(r"#?[0-9a-fA-F]{6}", color_raw):
        fixed_color = int(color_raw.lstrip("#"), 16)
    hoist = confirm("hoist the role(s) on the member list?", False)

    # --- overflow detection, before a single write -------------------
    rr = rest.get_roles(gid)
    if rr.status_code != 200:
        notice("API ERROR", [f"could not count existing roles | {rr.status_code}"], "bad")
        return
    existing = len(rr.json())
    needed = len(targets) if separate else 1
    allowed = _check_role_overflow(existing, needed)
    if allowed <= 0:
        ctx.logger.log("OP_RESULT", "role_engine | aborted (role cap)")
        return
    if separate and allowed < len(targets):
        targets = targets[:allowed]

    perm_str = ", ".join(perm_names)
    notice(
        "ROLE PLAN",
        [
            f"targets:     [white]{len(targets)}[/white]",
            f"mode:        [white]{'separate role per user' if separate else 'one shared role'}[/white]",
            f"permissions: [white]{perm_str}[/white]",
            f"naming:      [white]{'randomized' if random_names else name}[/white]",
            f"hoist:       [white]{'yes' if hoist else 'no'}[/white]",
        ],
    )
    if not confirm("execute the role engine?"):
        return

    used_names = set()
    created = assigned = failed = 0
    admin_requested = bool(perm_bits & PERMISSION_BITS["ADMINISTRATOR"])

    def make_role(suffix=None):
        rname = random_role_name(used_names) if random_names else (
            f"{name}-{suffix}" if suffix else name)
        body = {
            "name": rname,
            "permissions": str(perm_bits),
            "hoist": hoist,
            "mentionable": False,
        }
        if random_color:
            body["color"] = random.randint(0, 0xFFFFFF)
        elif fixed_color is not None:
            body["color"] = fixed_color
        r = rest.create_role(gid, body, reason="NiNog Raker role engine")
        return r.json().get("id") if r.status_code in (200, 201) else None

    def current_roles_of(n):
        m = n.get("member")
        if isinstance(m, dict) and "roles" in m:
            return list(m.get("roles", []))
        r = rest.get_member(gid, n["id"])
        return list(r.json().get("roles", [])) if r.status_code == 200 else None

    with console.status("[orange]running role engine...[/orange]") as status:
        shared_id = None
        if not separate:
            status.update("[orange]creating shared role...[/orange]")
            shared_id = make_role()
            if not shared_id:
                status.stop()
                notice("FAILED", ["shared role creation denied | bot needs MANAGE ROLES."], "bad")
                return
            created = 1

        for i, n in enumerate(targets):
            status.update(f"[orange]{i + 1}/{len(targets)} | {n['name']}[/orange]")
            rid = shared_id
            if separate:
                rid = make_role(suffix=str(i + 1))
                if not rid:
                    failed += 1
                    continue
                created += 1
            base = current_roles_of(n)
            if base is None:
                failed += 1
                if separate and rid:
                    rest.delete_role(gid, rid)   # never litter on failure
                    created -= 1
                continue
            if rid in base:
                assigned += 1   # already holds it (shared re-run)
                continue
            ar = rest.modify_member(gid, n["id"], {"roles": base + [rid]})
            if ar.status_code in (200, 204):
                assigned += 1
            else:
                failed += 1
                if separate and rid:
                    rest.delete_role(gid, rid)
                    created -= 1

    ctx.logger.log(
        "OP_RESULT",
        f"role_engine | targets={len(targets)} created={created} "
        f"assigned={assigned} failed={failed} perms={perm_str}"
        + (" ADMIN" if admin_requested else ""),
    )
    notice(
        "ROLE ENGINE DONE",
        [
            f"roles created:  [white]{created}[/white]",
            f"assigned:       [white]{assigned}[/white]",
            f"failed:         [white]{failed}[/white]",
            f"permissions:    [white]{perm_str}[/white]",
        ],
        "good" if not failed else "warn",
    )



def op_server_info(ctx):
    rest, guild = ctx.rest, ctx.guild
    r = rest.get_guild(guild["id"])
    g = r.json() if r.status_code == 200 else guild

    ch = rest.get_channels(guild["id"])
    role_r = rest.get_roles(guild["id"])

    table = Table(box=box.SIMPLE_HEAVY, header_style="brand",
                  border_style="deep", padding=(0, 2))
    table.add_column("FIELD", style="orange")
    table.add_column("VALUE", style="white")
    table.add_row("name", str(g.get("name", "?")))
    table.add_row("guild id", str(g.get("id", "?")))
    table.add_row("owner id", str(g.get("owner_id", "?")))
    table.add_row("members (approx)", str(g.get("approximate_member_count", "?")))
    table.add_row("online (approx)", str(g.get("approximate_presence_count", "?")))

    if ch.status_code == 200:
        counts = {}
        for c in ch.json():
            label = CHANNEL_TYPES.get(c.get("type"), f"type {c.get('type')}")
            counts[label] = counts.get(label, 0) + 1
        breakdown = " | ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        table.add_row("channels", breakdown or "none")
    if role_r.status_code == 200:
        table.add_row("roles", str(len(role_r.json())))
    table.add_row("verification", VERIF_LEVELS.get(g.get("verification_level"), "?"))
    table.add_row("boost tier", str(g.get("premium_tier", 0)))

    features = g.get("features", [])
    if features:
        shown = ", ".join(features[:6]) + (", ..." if len(features) > 6 else "")
        table.add_row("features", shown)

    console.print(table)
    ctx.logger.log("OP_RESULT", f"server_info {g.get('name')} ({g.get('id')})")


def op_scan_bots(ctx):
    rest, guild = ctx.rest, ctx.guild

    r, members = rest.get_all_members(guild["id"])
    if r.status_code == 403:
        notice("FORBIDDEN", ["member listing needs the SERVER MEMBERS intent."], "bad")
        ctx.logger.log("OP_RESULT", "scan_bots | 403 missing members intent")
        return
    if r.status_code != 200 and not members:
        notice("API ERROR", [f"status {r.status_code}"], "bad")
        return

    bots = [m for m in members if m.get("user", {}).get("bot")]
    humans = len(members) - len(bots)

    if bots:
        table = Table(box=box.SIMPLE_HEAVY, header_style="brand",
                      border_style="deep", padding=(0, 2))
        table.add_column("#", justify="right", style="dim")
        table.add_column("BOT", style="white")
        table.add_column("ID", style="dim")
        for i, b in enumerate(bots):
            table.add_row(str(i + 1), bot_display(b["user"]), b["user"].get("id", "?"))
        console.print(table)
    else:
        notice("NO BOTS", ["no other bots found in this server."], "good")

    console.print(
        f"[dim]scan |[/dim] [white]{len(bots)}[/white] [dim]bots |[/dim] "
        f"[white]{humans}[/white] [dim]humans |[/dim] [white]{len(members)}[/white] [dim]total fetched[/dim]"
    )
    ctx.logger.log("OP_RESULT", f"scan_bots | {len(bots)} bots / {len(members)} members")


def op_purge_messages(ctx):
    rest, gid = ctx.rest, ctx.guild["id"]
    ensure_pre_op_snapshot(ctx, "PURGE MESSAGES")

    r = rest.get_channels(gid)
    if r.status_code != 200:
        notice("API ERROR", [f"status {r.status_code}"], "bad")
        return
    textish = {0, 5}
    channels = [c for c in r.json() if c.get("type") in textish]
    if not channels:
        notice("EMPTY", ["no text channels to purge."], "warn")
        return
    if not confirm(f"purge recent messages in [white]{len(channels)}[/white] text channels?"):
        return

    cutoff = datetime.now(timezone.utc).timestamp() - (14 * 86400)
    bulked = singled = failed = 0

    with console.status(f"[orange]purging {len(channels)} channels...[/orange]") as status:
        for i, c in enumerate(channels):
            status.update(f"[orange]channel {i + 1}/{len(channels)} | {c.get('name')}[/orange]")
            mr = rest.get_messages(c["id"], limit=100)
            if mr.status_code != 200:
                failed += 1
                continue
            recent, old = [], []
            for m in mr.json():
                try:
                    ts = datetime.fromisoformat(
                        m["timestamp"].replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    ts = 0
                (recent if ts >= cutoff else old).append(m["id"])

            for j in range(0, len(recent), 100):
                batch = recent[j : j + 100]
                if len(batch) >= 2:
                    br = rest.bulk_delete(c["id"], batch)
                    if br.status_code in (200, 204):
                        bulked += len(batch)
                else:
                    for mid in batch:
                        dr = rest.delete_message(c["id"], mid)
                        if dr.status_code in (200, 204):
                            singled += 1
            for mid in old:
                dr = rest.delete_message(c["id"], mid)
                if dr.status_code in (200, 204):
                    singled += 1

    ctx.logger.log("OP_RESULT", f"purge | bulk={bulked} single={singled} chfailed={failed}")
    console.print(
        f"[dim]done |[/dim] [white]{bulked}[/white] [dim]bulk-deleted |[/dim] "
        f"[white]{singled}[/white] [dim]single-deleted |[/dim] [white]{failed}[/white] [dim]channels failed[/dim]"
    )


# ------------------------------------------------------------
# SECTION 6 | MESSAGING + WEBHOOK OPS
# ------------------------------------------------------------

def pick_text_channel(ctx):
    rest, gid = ctx.rest, ctx.guild["id"]
    r = rest.get_channels(gid)
    if r.status_code != 200:
        notice("API ERROR", [f"status {r.status_code}"], "bad")
        return None
    textish = {0, 5}
    channels = [c for c in r.json() if c.get("type") in textish]
    if not channels:
        notice("EMPTY", ["no text channels available."], "warn")
        return None

    table = Table(box=box.SIMPLE_HEAVY, header_style="brand",
                  border_style="deep", padding=(0, 2))
    table.add_column("#", justify="right", style="dim")
    table.add_column("NAME", style="white")
    table.add_column("ID", style="dim")
    for i, c in enumerate(channels):
        table.add_row(str(i + 1), c.get("name", "?"), c.get("id", "?"))
    console.print(table)

    choice = ask(f"1-{len(channels)} select | B back").lower()
    if choice == "b":
        return None
    if choice.isdigit() and 1 <= int(choice) <= len(channels):
        return channels[int(choice) - 1]
    notice("OUT OF RANGE", [f"expected 1-{len(channels)}."], "warn")
    return None


def op_send_message(ctx):
    channel = pick_text_channel(ctx)
    if channel is None:
        return
    content = ask("message content")
    if not content:
        return
    r = ctx.rest.send_message(channel["id"], content[:2000])
    ok = r.status_code in (200, 201)
    ctx.logger.log("OP_RESULT", f"send_message {channel.get('name')} | {r.status_code}")
    notice(
        "SENT" if ok else "FAILED",
        [f"#{channel.get('name')}" if ok else f"status {r.status_code} | {r.text[:120]}"],
        "good" if ok else "bad",
    )


def op_dm_all(ctx):
    rest, guild = ctx.rest, ctx.guild
    content = ask("message to DM every human member")
    if not content:
        return

    r, members = rest.get_all_members(guild["id"])
    if r.status_code == 403:
        notice("FORBIDDEN", ["member listing needs the SERVER MEMBERS intent."], "bad")
        return
    if r.status_code != 200 and not members:
        notice("API ERROR", [f"status {r.status_code}"], "bad")
        return

    me_id = ctx.me.get("id")
    targets = [m for m in members
               if not m.get("user", {}).get("bot")
               and m.get("user", {}).get("id") != me_id]
    if not targets:
        notice("EMPTY", ["no human members found."], "warn")
        return
    if not confirm(f"DM [white]{len(targets)}[/white] members?"):
        return

    sent = failed = 0
    with console.status(f"[orange]messaging {len(targets)} members...[/orange]") as status:
        for i, m in enumerate(targets):
            uid = m["user"]["id"]
            status.update(f"[orange]dm {i + 1}/{len(targets)} | {bot_display(m['user'])}[/orange]")
            if dm_send(rest, uid, content):
                sent += 1
            else:
                failed += 1

    ctx.logger.log("OP_RESULT", f"dm_all | sent={sent} failed={failed}")
    console.print(
        f"[dim]done |[/dim] [white]{sent}[/white] [dim]sent |[/dim] "
        f"[white]{failed}[/white] [dim]failed (closed DMs count as failed)[/dim]"
    )


def op_create_webhooks(ctx):
    rest, gid = ctx.rest, ctx.guild["id"]
    ensure_pre_op_snapshot(ctx, "MASS WEBHOOKS")

    name = ask("webhook name", "raker")
    if not name:
        return
    per_channel = ask_int("webhooks per channel", 1)
    per_channel = max(1, min(10, per_channel))
    r = rest.get_channels(gid)
    if r.status_code != 200:
        notice("API ERROR", [f"status {r.status_code}"], "bad")
        return
    textish = {0, 5}
    channels = [c for c in r.json() if c.get("type") in textish]
    if not channels:
        notice("EMPTY", ["no text channels available."], "warn")
        return
    total = len(channels) * per_channel
    if not confirm(f"create [white]{total}[/white] webhooks across "
                   f"[white]{len(channels)}[/white] channels?"):
        return

    created = failed = 0
    with console.status(f"[orange]creating {total} webhooks...[/orange]") as status:
        for c in channels:
            for j in range(per_channel):
                status.update(f"[orange]{c.get('name')} | webhook {j + 1}/{per_channel}[/orange]")
                resp = rest.create_webhook(c["id"], name)
                if resp.status_code in (200, 201):
                    created += 1
                else:
                    failed += 1

    ctx.logger.log("OP_RESULT", f"create_webhooks | created={created} failed={failed}")
    console.print(
        f"[dim]done |[/dim] [white]{created}[/white] [dim]created |[/dim] "
        f"[white]{failed}[/white] [dim]failed[/dim]"
    )


def op_webhook_spam(ctx):
    rest, gid = ctx.rest, ctx.guild["id"]
    r = rest.get_guild_webhooks(gid)
    if r.status_code != 200:
        notice("API ERROR", [f"status {r.status_code} | needs MANAGE WEBHOOKS."], "bad")
        return
    webhooks = r.json()
    if not webhooks:
        notice("EMPTY", ["no webhooks on this server.",
                         "create some first."], "warn")
        return

    content = ask("message to spam")
    if not content:
        return
    per_hook = ask_int("sends per webhook", 5)
    per_hook = max(1, min(100, per_hook))
    total = per_hook * len(webhooks)
    if not confirm(f"send [white]{total}[/white] messages via "
                   f"[white]{len(webhooks)}[/white] webhooks?"):
        return

    sent = failed = 0
    with console.status(f"[orange]spamming {total} messages...[/orange]") as status:
        n = 0
        for wh in webhooks:
            url = wh.get("url")
            if not url:
                continue
            for j in range(per_hook):
                n += 1
                status.update(f"[orange]message {n}/{total} | {wh.get('name')}[/orange]")
                try:
                    resp = rest.execute_webhook(url, {"content": content[:2000]})
                    if resp.status_code in (200, 204):
                        sent += 1
                    else:
                        failed += 1
                except ApiNetworkError:
                    failed += 1

    ctx.logger.log("OP_RESULT", f"webhook_spam | sent={sent} failed={failed}")
    console.print(
        f"[dim]done |[/dim] [white]{sent}[/white] [dim]sent |[/dim] "
        f"[white]{failed}[/white] [dim]failed[/dim]"
    )


def op_delete_all_webhooks(ctx):
    rest, gid = ctx.rest, ctx.guild["id"]
    r = rest.get_guild_webhooks(gid)
    if r.status_code != 200:
        notice("API ERROR", [f"status {r.status_code} | needs MANAGE WEBHOOKS."], "bad")
        return
    webhooks = r.json()
    if not webhooks:
        notice("EMPTY", ["no webhooks on this server."], "good")
        return
    if not confirm(f"delete ALL [white]{len(webhooks)}[/white] webhooks?"):
        return

    deleted = failed = 0
    with console.status(f"[orange]deleting {len(webhooks)} webhooks...[/orange]") as status:
        for i, wh in enumerate(webhooks):
            status.update(f"[orange]webhook {i + 1}/{len(webhooks)} | {wh.get('name')}[/orange]")
            resp = rest.delete_webhook_id(wh["id"])
            if resp.status_code in (200, 204):
                deleted += 1
            else:
                failed += 1

    ctx.logger.log("OP_RESULT", f"delete_webhooks | deleted={deleted} failed={failed}")
    console.print(
        f"[dim]done |[/dim] [white]{deleted}[/white] [dim]deleted |[/dim] "
        f"[white]{failed}[/white] [dim]failed[/dim]"
    )



# ------------------------------------------------------------
# SECTION 6b | WEBHOOK EXTRACTION
# ------------------------------------------------------------
# Preservation play: pull every webhook URL out of the server before
# anything happens to it, then hand the list to the user however they want.

def collect_all_webhooks(ctx):
    """Guild-wide endpoint plus a per-channel sweep. Returns [hook dicts]."""
    rest, gid = ctx.rest, ctx.guild["id"]
    found = {}
    r = rest.get_guild_webhooks(gid)
    if r.status_code == 200:
        for h in r.json():
            if h.get("id"):
                found[h["id"]] = h
    # The guild endpoint can miss hooks the token only sees per-channel.
    cr = rest.get_channels(gid)
    if cr.status_code == 200:
        textish = {0, 2, 5, 13, 15}
        for c in cr.json():
            if c.get("type") not in textish:
                continue
            hr = rest.get_channel_webhooks(c["id"])
            if hr.status_code == 200:
                for h in hr.json():
                    if h.get("id"):
                        found.setdefault(h["id"], h)
    urls = []
    for h in found.values():
        url = webhook_url_of(h)
        if url:
            urls.append(url)
    return sorted(set(urls))


def _export_webhooks_to_file(ctx, urls):
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    gname = (ctx.guild or {}).get("name", "guild")
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = EXPORTS_DIR / f"webhooks_{sanitize_name(gname)}_{stamp}.txt"
    path.write_text("\n".join(urls) + "\n", encoding="utf-8")
    ctx.logger.log("OP_RESULT", f"extract_webhooks | file {path} ({len(urls)} urls)")
    return path


def op_extract_webhooks(ctx):
    ensure_pre_op_snapshot(ctx, "EXTRACT WEBHOOKS")
    with console.status("[orange]sweeping guild for webhooks...[/orange]"):
        urls = collect_all_webhooks(ctx)
    if not urls:
        notice("NONE", ["no webhooks found on this server.",
                        "needs MANAGE WEBHOOKS to see the guild-wide list."], "warn")
        return
    console.print(f"[dim]{len(urls)} webhook urls collected.[/dim]")
    ctx.logger.log("OP_RESULT", f"extract_webhooks | {len(urls)} urls")

    while True:
        console.print()
        screen_title("DELIVER WEBHOOK URLS", "each exact url on its own line")
        console.print("  [orange][1][/orange] [white]Display in terminal[/white]")
        console.print("  [orange][2][/orange] [white]Copy to clipboard[/white]")
        console.print("  [orange][3][/orange] [white]Send to a webhook[/white]")
        console.print("  [orange][4][/orange] [white]Save to exports/ file[/white]")
        console.print("  [orange][B][/orange] [dim]done[/dim]")
        raw = ask("how do you want them?").lower().strip()
        if raw in ("b", "", "q"):
            return

        if raw == "1":
            console.print()
            for u in urls:
                console.print(f"[white]{u}[/white]")
            press_enter()
            continue

        if raw == "2":
            ok, detail = copy_to_clipboard("\n".join(urls))
            if ok:
                notice("COPIED", [f"{len(urls)} urls on the clipboard via {detail}.",
                                  "paste anywhere | one url per line."], "good")
            else:
                notice("CLIPBOARD FAILED", [detail, "falling back to a file."])
                path = _export_webhooks_to_file(ctx, urls)
                notice("SAVED INSTEAD", [f"[white]{path}[/white]"], "good")
            press_enter()
            continue

        if raw == "3":
            dest = ask("destination webhook url")
            if not dest or not dest.startswith("http"):
                notice("BAD URL", ["needs a full https webhook url."], "warn")
                continue
            # 2000-char message cap, one exact url per line, as many
            # messages as it takes so the list is never cut mid-url.
            batch, batches, size = [], [], 0
            for u in urls:
                if size + len(u) + 1 > 1900:
                    batches.append(batch)
                    batch, size = [], 0
                batch.append(u)
                size += len(u) + 1
            if batch:
                batches.append(batch)
            sent = failed = 0
            with console.status(f"[orange]sending {len(batches)} message(s)..."
                                "[/orange]"):
                for b in batches:
                    try:
                        r = ctx.rest.execute_webhook(
                            dest, {"content": "\n".join(b)})
                        if r.status_code in (200, 204):
                            sent += 1
                        else:
                            failed += 1
                    except ApiNetworkError:
                        failed += 1
            ctx.logger.log("OP_RESULT",
                           f"extract_webhooks | relay sent={sent} failed={failed}")
            notice("RELAYED" if sent else "FAILED",
                   [f"[white]{sent}[/white] message(s) delivered"
                    + (f", [white]{failed}[/white] failed" if failed else "")],
                   "good" if sent and not failed else "warn")
            press_enter()
            continue

        if raw == "4":
            path = _export_webhooks_to_file(ctx, urls)
            notice("SAVED", [f"[white]{path}[/white]",
                             f"[dim]{len(urls)} urls, one per line[/dim]"], "good")
            press_enter()
            continue

        notice("OUT OF RANGE", ["1-4 | B done."], "warn")



def op_create_invite(ctx):
    channel = pick_text_channel(ctx)
    if channel is None:
        return
    r = ctx.rest.create_invite(channel["id"], max_age=86400)
    if r.status_code == 200:
        code = r.json().get("code")
        ctx.logger.log("OP_RESULT", f"invite {channel.get('name')} {code}")
        notice("INVITE READY",
               [f"[white]https://discord.gg/{code}[/white] [dim](24h)[/dim]"], "good")
    else:
        notice("FAILED", [f"status {r.status_code} | {r.text[:120]}"], "bad")


# ------------------------------------------------------------
# SECTION 7 | PRECISION OPS
# ------------------------------------------------------------

def op_member_lookup(ctx):
    rest, gid = ctx.rest, ctx.guild["id"]
    target = pick_one(ctx, "MEMBER LOOKUP", allow_bots=True)
    if not target:
        return
    uid = target["id"]
    r = rest.get_member(gid, uid)
    if r.status_code != 200:
        notice("NOT FOUND",
               [f"status {r.status_code} | wrong id, not in server, or missing intent."], "bad")
        return
    m = r.json()
    if target.get("member") and target["member"].get("_fetched") is None:
        # reuse already-fetched member payloads when the picker has them,
        # but only the fields that the fresh API also provides
        pass
    roles_r = rest.get_roles(gid)
    role_names = {}
    if roles_r.status_code == 200:
        role_names = {x["id"]: x.get("name", "?") for x in roles_r.json()}

    table = Table(box=box.SIMPLE_HEAVY, header_style="brand",
                  border_style="deep", padding=(0, 2))
    table.add_column("FIELD", style="orange")
    table.add_column("VALUE", style="white")
    table.add_row("user", bot_display(m.get("user", {})))
    table.add_row("id", m.get("user", {}).get("id", "?"))
    table.add_row("bot", "yes" if m.get("user", {}).get("bot") else "no")
    table.add_row("nickname", m.get("nick") or "|")
    table.add_row("joined", fmt_ts(m.get("joined_at")))
    table.add_row("boosting since",
                  fmt_ts(m.get("premium_since")) if m.get("premium_since") else "|")
    table.add_row("timeout until",
                  fmt_ts(m.get("communication_disabled_until"))
                  if m.get("communication_disabled_until") else "|")
    role_list = ", ".join(role_names.get(rid, rid) for rid in m.get("roles", [])) or "none"
    table.add_row("roles", role_list)
    console.print(table)
    ctx.logger.log("OP_RESULT", f"member_lookup {uid}")


def op_kick_member(ctx):
    rest, gid = ctx.rest, ctx.guild["id"]
    target = pick_one(ctx, "KICK MEMBER", allow_bots=True)
    if not target:
        return
    uid = target["id"]
    if uid in whitelist_ids():
        notice("WHITELISTED", ["this user is on the whitelist | remove them first."], "warn")
        return
    if not confirm(f"kick [white]{target['name']}[/white] ({uid})?"):
        return
    r = rest.kick_member(gid, uid)
    ok = r.status_code in (200, 204)
    ctx.logger.log("OP_RESULT", f"kick_member {uid} | {r.status_code}")
    notice("KICKED" if ok else "FAILED",
           [uid if ok else f"status {r.status_code} | target may outrank the bot."],
           "good" if ok else "bad")


def op_ban_member(ctx):
    rest, gid = ctx.rest, ctx.guild["id"]
    target = pick_one(ctx, "BAN MEMBER", allow_bots=True)
    if not target:
        return
    uid = target["id"]
    if uid in whitelist_ids():
        notice("WHITELISTED", ["this user is on the whitelist | remove them first."], "warn")
        return
    days = ask_int("delete message history days (0-7)", 0)
    days = max(0, min(7, days))
    if not confirm(f"ban [white]{target['name']}[/white] ({uid})?"):
        return
    r = rest.ban_member(gid, uid, delete_message_seconds=days * 86400)
    ok = r.status_code in (200, 204)
    ctx.logger.log("OP_RESULT", f"ban_member {uid} | {r.status_code}")
    notice("BANNED" if ok else "FAILED",
           [uid if ok else f"status {r.status_code}"], "good" if ok else "bad")


def op_unban_member(ctx):
    rest, gid = ctx.rest, ctx.guild["id"]

    r, bans = rest.get_all_bans(gid)
    if r.status_code == 403:
        notice("FORBIDDEN", ["missing BAN MEMBERS permission."], "bad")
        return
    if r.status_code != 200:
        notice("API ERROR", [f"status {r.status_code}"], "bad")
        return
    if not bans:
        notice("EMPTY", ["no bans on this server."], "good")
        return

    table = Table(box=box.SIMPLE_HEAVY, header_style="brand",
                  border_style="deep", padding=(0, 2))
    table.add_column("#", justify="right", style="dim")
    table.add_column("USER", style="white")
    table.add_column("ID", style="dim")
    table.add_column("REASON", style="dim")
    for i, b in enumerate(bans):
        table.add_row(str(i + 1), bot_display(b.get("user", {})),
                      b.get("user", {}).get("id", "?"), (b.get("reason") or "|")[:60])
    console.print(table)

    choice = ask(f"1-{len(bans)} unban | B back").lower()
    if choice == "b":
        return
    if not (choice.isdigit() and 1 <= int(choice) <= len(bans)):
        notice("OUT OF RANGE", [f"expected 1-{len(bans)}."], "warn")
        return
    uid = bans[int(choice) - 1].get("user", {}).get("id")
    rr = rest.unban_member(gid, uid)
    ok = rr.status_code in (200, 204)
    ctx.logger.log("OP_RESULT", f"unban_member {uid} | {rr.status_code}")
    notice("UNBANNED" if ok else "FAILED",
           [uid if ok else f"status {rr.status_code}"], "good" if ok else "bad")


def op_view_ban_list(ctx):
    rest, gid = ctx.rest, ctx.guild["id"]

    r, bans = rest.get_all_bans(gid)
    if r.status_code == 403:
        notice("FORBIDDEN", ["missing BAN MEMBERS permission."], "bad")
        return
    if r.status_code != 200:
        notice("API ERROR", [f"status {r.status_code}"], "bad")
        return
    if not bans:
        notice("EMPTY", ["no bans on this server."], "good")
        return

    page_size = max(1, int(ctx.config.setting("page_size", 10)))
    pages = chunk(bans, page_size)
    page_idx = 0
    while True:
        page = pages[page_idx]
        offset = page_idx * page_size
        table = Table(box=box.SIMPLE_HEAVY, header_style="brand",
                      border_style="deep", padding=(0, 2))
        table.add_column("#", justify="right", style="dim")
        table.add_column("USER", style="white")
        table.add_column("ID", style="dim")
        table.add_column("REASON", style="dim")
        for i, b in enumerate(page):
            table.add_row(str(offset + i + 1), bot_display(b.get("user", {})),
                          b.get("user", {}).get("id", "?"), (b.get("reason") or "|")[:60])
        console.print(table)
        console.print(f"[dim]page {page_idx + 1}/{len(pages)}[/dim]")
        choice = ask("N next | P prev | B back").lower()
        if choice == "n" and len(pages) > 1:
            page_idx = (page_idx + 1) % len(pages)
        elif choice == "p" and len(pages) > 1:
            page_idx = (page_idx - 1) % len(pages)
        elif choice == "b":
            break
    ctx.logger.log("OP_RESULT", f"view_ban_list | {len(bans)} bans")


def op_channel_control(ctx):
    rest, gid = ctx.rest, ctx.guild["id"]
    while True:
        r = rest.get_channels(gid)
        channels = r.json() if r.status_code == 200 else []
        table = Table(box=box.SIMPLE_HEAVY, header_style="brand",
                      border_style="deep", padding=(0, 2))
        table.add_column("#", justify="right", style="dim")
        table.add_column("NAME", style="white")
        table.add_column("TYPE", style="dim")
        table.add_column("ID", style="dim")
        for i, c in enumerate(channels):
            table.add_row(str(i + 1), c.get("name", "?"),
                          CHANNEL_TYPES.get(c.get("type"), str(c.get("type"))),
                          c.get("id", "?"))
        console.print(table)
        choice = ask(f"1-{len(channels)} target | C create | B back").lower()
        if choice == "b":
            return
        if choice == "c":
            name = ask("channel name")
            if not name:
                continue
            ctype = ask_int("type (0 text / 2 voice / 4 category)", 0)
            ctype = ctype if ctype in (0, 2, 4) else 0
            cr = rest.create_channel(gid, {"name": name, "type": ctype})
            ok = cr.status_code in (200, 201)
            ctx.logger.log("OP_RESULT", f"channel_create {name} | {cr.status_code}")
            notice("CREATED" if ok else "FAILED",
                   [name if ok else f"status {cr.status_code}"], "good" if ok else "bad")
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(channels):
            target = channels[int(choice) - 1]
            action = ask("R rename | D delete | B back").lower()
            if action == "r":
                new_name = ask("new name", target.get("name", ""))
                if new_name:
                    rr = rest.patch_channel(target["id"], {"name": new_name})
                    ok = rr.status_code == 200
                    ctx.logger.log("OP_RESULT",
                                   f"channel_rename {target.get('name')} to {new_name} | {rr.status_code}")
                    notice("RENAMED" if ok else "FAILED",
                           [new_name if ok else f"status {rr.status_code}"],
                           "good" if ok else "bad")
            elif action == "d":
                if confirm(f"delete [white]{target.get('name')}[/white]?"):
                    dr = rest.delete_channel(target["id"], reason="NiNog Raker")
                    ok = dr.status_code in (200, 204)
                    ctx.logger.log("OP_RESULT",
                                   f"channel_delete {target.get('name')} | {dr.status_code}")
                    notice("DELETED" if ok else "FAILED",
                           [target.get("name") if ok else f"status {dr.status_code}"],
                           "good" if ok else "bad")
            continue
        notice("OUT OF RANGE", [f"expected 1-{len(channels)}."], "warn")


def op_role_control(ctx):
    rest, gid = ctx.rest, ctx.guild["id"]
    while True:
        r = rest.get_roles(gid)
        roles = r.json() if r.status_code == 200 else []
        table = Table(box=box.SIMPLE_HEAVY, header_style="brand",
                      border_style="deep", padding=(0, 2))
        table.add_column("#", justify="right", style="dim")
        table.add_column("NAME", style="white")
        table.add_column("ID", style="dim")
        table.add_column("POS", justify="right", style="dim")
        table.add_column("MANAGED", justify="center", style="dim")
        for i, x in enumerate(roles):
            table.add_row(str(i + 1), x.get("name", "?"), x.get("id", "?"),
                          str(x.get("position", 0)),
                          "yes" if x.get("managed") else "|")
        console.print(table)
        choice = ask(f"1-{len(roles)} target | C create | B back").lower()
        if choice == "b":
            return
        if choice == "c":
            name = ask("role name")
            if not name:
                continue
            color = ask("color hex (blank = default)", "").lstrip("#")
            body = {"name": name, "hoist": True, "mentionable": True}
            if re.fullmatch(r"[0-9a-fA-F]{6}", color or ""):
                body["color"] = int(color, 16)
            cr = rest.create_role(gid, body, reason="NiNog Raker")
            ok = cr.status_code in (200, 201)
            ctx.logger.log("OP_RESULT", f"role_create {name} | {cr.status_code}")
            notice("CREATED" if ok else "FAILED",
                   [name if ok else f"status {cr.status_code}"], "good" if ok else "bad")
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(roles):
            target = roles[int(choice) - 1]
            if target.get("id") == gid:
                notice("PROTECTED", ["@everyone cannot be modified here."], "warn")
                continue
            if target.get("managed"):
                notice("PROTECTED", ["managed roles belong to integrations."], "warn")
                continue
            action = ask("R rename | D delete | B back").lower()
            if action == "r":
                new_name = ask("new name", target.get("name", ""))
                if new_name:
                    rr = rest.patch_role(gid, target["id"], {"name": new_name})
                    ok = rr.status_code == 200
                    ctx.logger.log("OP_RESULT",
                                   f"role_rename {target.get('name')} to {new_name} | {rr.status_code}")
                    notice("RENAMED" if ok else "FAILED",
                           [new_name if ok else f"status {rr.status_code}"],
                           "good" if ok else "bad")
            elif action == "d":
                if confirm(f"delete [white]{target.get('name')}[/white]?"):
                    dr = rest.delete_role(gid, target["id"])
                    ok = dr.status_code in (200, 204)
                    ctx.logger.log("OP_RESULT",
                                   f"role_delete {target.get('name')} | {dr.status_code}")
                    notice("DELETED" if ok else "FAILED",
                           [target.get("name") if ok else f"status {dr.status_code}"],
                           "good" if ok else "bad")
            continue
        notice("OUT OF RANGE", [f"expected 1-{len(roles)}."], "warn")


# ------------------------------------------------------------
# SECTION 8 | WHITELIST
# ------------------------------------------------------------
# load_whitelist / save_whitelist / whitelist_ids live in core.py.
# Everything that draws or talks to Discord stays here.

def whitelist_add(ctx, user_id, name):
    entries = load_whitelist()
    if any(e.get("id") == user_id for e in entries):
        notice("ALREADY THERE", [f"[white]{name}[/white] is already whitelisted."], "warn")
        return
    entries.append({
        "id": user_id,
        "name": name,
        "added": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    save_whitelist(entries)
    ctx.logger.log("WHITELIST_ADD", f"{name} ({user_id})")
    notice("ADDED", [f"[white]{name}[/white] whitelisted."], "good")


def checkbox_select(entries, title):
    chosen = set()
    while True:
        table = Table(box=None, padding=(0, 2))
        table.add_column("SEL", justify="center")
        table.add_column("#", justify="right", style="dim")
        table.add_column("NAME", style="white")
        table.add_column("ID", style="dim")
        for i, e in enumerate(entries):
            mark = "[orange][x][/orange]" if i in chosen else "[faint][ ][/faint]"
            table.add_row(mark, str(i + 1), e.get("name", "?"), str(e.get("id", "?")))
        console.print()
        screen_title(title, f"{len(chosen)} of {len(entries)} selected")
        console.print(table)
        console.print()
        choice = ask("index to toggle | A all | N none | D done").lower()
        if choice == "a":
            chosen = set(range(len(entries)))
        elif choice == "n":
            chosen = set()
        elif choice == "d":
            return [entries[i] for i in sorted(chosen)]
        elif choice.isdigit() and 1 <= int(choice) <= len(entries):
            i = int(choice) - 1
            if i in chosen:
                chosen.discard(i)
            else:
                chosen.add(i)
        else:
            notice("OUT OF RANGE", [f"expected 1-{len(entries)} | A | N | D."], "warn")


def op_whitelist_add_search(ctx):
    # The universal picker's username mode already does the fuzzy scan with
    # paginated, multi-select results -- no reason to keep the old single-shot
    # variant alive.
    got = _pick_by_username(ctx, multi=True)
    if not got:
        return
    for t in got:
        whitelist_add(ctx, t["id"], t["name"])


def op_whitelist_add_id(ctx):
    got = pick_members(ctx, multi=True, title="ADD TO WHITELIST", allow_bots=True)
    if not got:
        return
    for t in got:
        whitelist_add(ctx, t["id"], t["name"])


def op_whitelist_add_dms(ctx):
    r = ctx.rest.get_dm_channels()
    if r.status_code != 200:
        notice("API ERROR", [f"status {r.status_code}"], "bad")
        return
    dms = [c for c in r.json() if c.get("type") == 1 and c.get("recipients")]
    if not dms:
        notice("NO DMS",
               ["nobody has DM'd this bot yet.",
                "DMs are fetched live | no gateway needed."], "warn")
        return

    entries = [{"id": c["recipients"][0].get("id"),
                "name": bot_display(c["recipients"][0])} for c in dms]
    selected = checkbox_select(entries, "PICK FROM DMS")
    if not selected:
        notice("NONE", ["nothing selected."], "warn")
        return
    for e in selected:
        whitelist_add(ctx, e["id"], e["name"])


def op_whitelist_remove(ctx):
    entries = load_whitelist()
    if not entries:
        notice("EMPTY", ["whitelist is empty."], "warn")
        return
    selected = checkbox_select(entries, "REMOVE FROM WHITELIST")
    if not selected:
        notice("NONE", ["nothing selected."], "warn")
        return
    names = ", ".join(e.get("name", "?") for e in selected)
    if not confirm(f"remove [white]{len(selected)}[/white]: {names}?"):
        return
    sel_ids = {e.get("id") for e in selected}
    remaining = [e for e in entries if e.get("id") not in sel_ids]
    save_whitelist(remaining)
    ctx.logger.log("WHITELIST_REMOVE", names)
    notice("REMOVED", [f"{len(selected)} entries removed."], "good")


def whitelist_mass(ctx, action_name, apply_fn):
    entries = load_whitelist()
    if not entries:
        notice("EMPTY", ["whitelist is empty."], "warn")
        return
    selected = checkbox_select(entries, f"SELECT USERS TO {action_name.upper()}")
    if not selected:
        notice("NONE", ["nothing selected."], "warn")
        return
    names = ", ".join(e.get("name", "?") for e in selected)
    if not confirm(f"{action_name} [white]{len(selected)}[/white] users: {names}?"):
        return

    ok = failed = 0
    with console.status(f"[orange]{action_name} {len(selected)} users...[/orange]") as status:
        for i, e in enumerate(selected):
            status.update(f"[orange]{i + 1}/{len(selected)} | {e.get('name')}[/orange]")
            try:
                if apply_fn(e):
                    ok += 1
                else:
                    failed += 1
            except ApiNetworkError:
                failed += 1

    ctx.logger.log(
        "OP_RESULT",
        f"whitelist_{action_name.lower().replace(' ', '_')} | ok={ok} failed={failed}",
    )
    console.print(
        f"[dim]done |[/dim] [white]{ok}[/white] [dim]ok |[/dim] "
        f"[white]{failed}[/white] [dim]failed[/dim]"
    )


def op_whitelist_unban(ctx):
    gid = ctx.guild["id"]

    def apply(e):
        r = ctx.rest.unban_member(gid, e["id"])
        return r.status_code in (200, 204, 404)

    whitelist_mass(ctx, "unban", apply)


def op_whitelist_ban(ctx):
    gid = ctx.guild["id"]

    def apply(e):
        r = ctx.rest.ban_member(gid, e["id"])
        return r.status_code in (200, 204)

    whitelist_mass(ctx, "ban", apply)


def make_invite_link(ctx):
    channel = pick_text_channel(ctx)
    if channel is None:
        return None
    r = ctx.rest.create_invite(channel["id"], max_age=86400)
    if r.status_code == 200:
        return f"https://discord.gg/{r.json().get('code')}"
    notice("INVITE FAILED", [f"status {r.status_code}"], "warn")
    return None


def op_whitelist_dm_invite(ctx):
    link = make_invite_link(ctx)
    if not link:
        return

    def apply(e):
        return dm_send(ctx.rest, e["id"], f"{ctx.guild.get('name')} | {link}")

    whitelist_mass(ctx, "dm invite", apply)


def op_whitelist_dm_message(ctx):
    content = ask("message to DM selected users")
    if not content:
        return

    def apply(e):
        return dm_send(ctx.rest, e["id"], content)

    whitelist_mass(ctx, "dm", apply)


# ------------------------------------------------------------
# SECTION 9 | SNAPSHOT PAGE OPS
# ------------------------------------------------------------

def op_snapshot_now(ctx):
    try:
        take_snapshot(ctx, reason="manual")
    except ApiNetworkError as e:
        notice("NETWORK ERROR", [str(e)], "bad")


def op_browse_snapshots(ctx):
    runs = load_snapshot_runs()
    if not runs:
        notice("NO SNAPSHOTS", ["nothing stored in snapshots/ yet."], "warn")
        return
    table = Table(box=box.SIMPLE_HEAVY, header_style="brand",
                  border_style="deep", padding=(0, 2))
    table.add_column("SERVER", style="white")
    table.add_column("RUN", style="orange")
    table.add_column("ROLES", justify="right", style="dim")
    table.add_column("CH", justify="right", style="dim")
    table.add_column("MEMBERS", justify="right", style="dim")
    table.add_column("BANS", justify="right", style="dim")
    table.add_column("MSGS", justify="right", style="dim")
    for sd, rd in runs:
        try:
            blob = json.loads((rd / "server.dat").read_text(encoding="utf-8"))
            msg_count = sum(len(v.get("messages", []))
                            for v in blob.get("messages", {}).values())
            table.add_row(
                blob["guild"]["name"],
                rd.name.replace("snap_", ""),
                str(len(blob.get("roles", []))),
                str(len(blob.get("channels", []))),
                str(len(blob.get("members", []))),
                str(len(blob.get("bans", []))),
                str(msg_count),
            )
        except Exception:
            table.add_row(sd.name, rd.name, "?", "?", "?", "?", "?")
    console.print(table)
    ctx.logger.log("OP_RESULT", f"browse_snapshots | {len(runs)} runs")


def op_delete_snapshot(ctx):
    runs = load_snapshot_runs()
    if not runs:
        notice("NO SNAPSHOTS", ["nothing to delete."], "warn")
        return
    table = Table(box=box.SIMPLE_HEAVY, header_style="brand",
                  border_style="deep", padding=(0, 2))
    table.add_column("#", justify="right", style="dim")
    table.add_column("SERVER", style="white")
    table.add_column("RUN", style="orange")
    for i, (sd, rd) in enumerate(runs):
        table.add_row(str(i + 1), sd.name, rd.name.replace("snap_", ""))
    console.print(table)
    choice = ask(f"1-{len(runs)} delete | B back").lower()
    if choice == "b":
        return
    if not (choice.isdigit() and 1 <= int(choice) <= len(runs)):
        notice("OUT OF RANGE", [f"expected 1-{len(runs)}."], "warn")
        return
    sd, rd = runs[int(choice) - 1]
    if confirm(f"delete [white]{rd.name}[/white] from [white]{sd.name}[/white]?"):
        shutil.rmtree(rd, ignore_errors=True)
        if sd.exists() and not any(sd.iterdir()):
            sd.rmdir()
        ctx.logger.log("OP_RESULT", f"snapshot_deleted {rd}")
        notice("DELETED", [str(rd)], "good")


# ------------------------------------------------------------
# SECTION 10 | SETTINGS SCREEN (unique layout, scrollable)
# ------------------------------------------------------------

SETTINGS_SPEC = [
    ("section", "GENERAL"),
    {"key": "switch_server", "name": "Switch Server", "kind": "action"},
    {"key": "switch_token", "name": "Switch Token", "kind": "action"},
    {"key": "whitelist", "name": "Whitelist Manager", "kind": "jump"},
    {"key": "session_log", "name": "Session Log", "kind": "view"},
    {"key": "view_config", "name": "View Config", "kind": "view"},
    {"key": "auto_snapshot", "name": "Auto Snapshot", "kind": "toggle"},
    {"key": "page_size", "name": "Page Size", "kind": "number", "min": 5, "max": 50},
    ("section", "WATCHDOG"),
    {"key": "ban_watch", "name": "Ban Detection", "kind": "toggle"},
    {"key": "ban_watch_secs", "name": "Watch Interval", "kind": "number",
     "unit": " s", "min": 5, "max": 120},
    ("section", "APPEARANCE"),
    {"key": "theme", "name": "UI Theme", "kind": "dropdown", "options": THEME_OPTIONS},
    {"key": "layout", "name": "Menu Layout", "kind": "dropdown", "options": LAYOUTS},
    {"key": "transition", "name": "Transition Effect", "kind": "dropdown", "options": TRANSITIONS},
    {"key": "transition_ms", "name": "Transition Speed", "kind": "number",
     "unit": " ms", "min": 50, "max": 2000},
    ("section", "EXPERIMENTAL"),
    {"key": "gradient", "name": "Gradient Accents", "kind": "toggle"},
    ("section", "RATE LIMITING"),
    {"key": "rate_limit", "name": "Ratelimit", "kind": "toggle"},
    {"key": "rate_limit_level", "name": "Ratelimit Level", "kind": "dropdown",
     "options": RATE_LEVELS},
    {"key": "rl_settings", "name": "Ratelimit Settings", "kind": "sub"},
]


def settings_flat():
    items = []
    for entry in SETTINGS_SPEC:
        if isinstance(entry, tuple):
            continue
        items.append(entry)
    return items


def settings_rows():
    rows = []
    idx = 0
    for entry in SETTINGS_SPEC:
        if isinstance(entry, tuple):
            rows.append(("section", entry[1]))
        else:
            rows.append(("item", idx, entry))
            idx += 1
    return rows


def lookup_label(options, key):
    for k, label in normalize_options(options):
        if k == key:
            return label
    return str(key)


def setting_value_display(ctx, item):
    key, kind = item["key"], item["kind"]
    if kind in ("action", "jump", "sub"):
        return "open"
    if kind == "view":
        return "view"
    if kind == "toggle":
        return "ON" if ctx.config.setting(key) else "OFF"
    if kind == "dropdown":
        return lookup_label(item["options"], ctx.config.setting(key))
    if kind == "number":
        v = ctx.config.setting(key)
        if item.get("scale"):
            v = int(float(v) * item["scale"])
        return f"{v}{item.get('unit', '')}"
    return ""


def dropdown_screen(ctx, title, options, current):
    opts = normalize_options(options)

    def render():
        print_header(ctx)
        console.print()
        console.print(grad(f"⚙ SETTINGS | {title}", ctx))
        console.print(f"[dim]current: {lookup_label(opts, current)}[/dim]")
        console.print()
        for i, (k, label) in enumerate(opts):
            mark = "[orange]▸[/orange]" if k == current else "[faint] [/faint]"
            console.print(f"   {mark} [orange][{i + 1:02d}][/orange] [white]{label}[/white]")
        console.print()
        console.print("[dim][num] pick | [B] cancel[/dim]")
        console.print()

    present_frame(ctx, render)
    raw = ask("Select").lower()
    console.print()
    if raw == "b" or not raw:
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(opts):
        return opts[int(raw) - 1][0]
    notice("OUT OF RANGE", [f"expected 1-{len(opts)} | B cancel."], "warn")
    press_enter()
    return None


def settings_handle(ctx, item):
    key, kind = item["key"], item["kind"]

    if kind == "action":
        if key == "switch_server":
            guild = pick_guild(ctx, ctx.rest, ctx.me)
            if guild is not None:
                attach_guild(ctx, guild)
        elif key == "switch_token":
            return "switch_token"

    elif kind == "jump":
        if key == "whitelist":
            return ("page", 4)

    elif kind == "view":
        if key == "session_log":
            try:
                tail = "\n".join(
                    ctx.logger.path.read_text(encoding="utf-8").splitlines()[-10:]
                )
            except OSError:
                tail = "(unreadable)"
            notice("SESSION LOG | last 10 lines",
                   [f"[dim]{tail or '(empty)'}[/dim]",
                    f"[white]{ctx.logger.path}[/white]"])
            press_enter()
        elif key == "view_config":
            body = json.dumps(ctx.config.data, indent=2)
            console.print(
                Panel(
                    f"[dim]{body}[/dim]\n\n"
                    f"[white]config[/white]    {CONFIG_FILE}\n"
                    f"[white]vault[/white]     {TOKENS_FILE}\n"
                    f"[white]whitelist[/white] {WHITELIST_FILE}\n"
                    f"[white]snaps[/white]     {SNAPSHOTS_DIR}\n"
                    f"[white]logs[/white]      {REPORTLOG_DIR}",
                    title="[orange]CONFIG[/orange]",
                    border_style="orange",
                    width=min(60, console.width or 80),
                    box=box.ROUNDED,
                )
            )
            press_enter()

    elif kind == "sub":
        # Opens a nested settings page and returns here when it closes.
        if key == "rl_settings":
            rate_limit_settings_screen(ctx)

    elif kind == "toggle":
        new = not bool(ctx.config.setting(key))
        if key == "rate_limit" and not new and not confirm_ratelimit_off():
            # The warning was declined: leave pacing on.
            return None
        ctx.config.set_setting(key, new)
        _sync_limiter(ctx)
        ctx.logger.log("SETTING_CHANGED", f"{key}={new}")
        notice(key.upper().replace("_", " "),
               [f"[white]{'ON' if new else 'OFF'}[/white]"], "good" if new else "warn")
        press_enter()

    elif kind == "number":
        v = ctx.config.setting(key)
        if item.get("scale"):
            cur = int(float(v) * item["scale"])
        else:
            cur = int(v)
        val = ask_int(f"{item['name'].lower()} ({item['min']}-{item['max']})", cur)
        val = max(item["min"], min(item["max"], val))
        stored = val / item["scale"] if item.get("scale") else val
        ctx.config.set_setting(key, stored)
        _sync_limiter(ctx)
        ctx.logger.log("SETTING_CHANGED", f"{key}={val}")
        notice(item["name"].upper(),
               [f"[white]{val}{item.get('unit', '')}[/white]"], "good")
        press_enter()

    elif kind == "dropdown":
        picked = dropdown_screen(ctx, item["name"], item["options"], ctx.config.setting(key))
        if picked:
            if key == "rate_limit_level":
                # A level is a starting point: applying it writes the preset's
                # concrete values, which the advanced page then edits.
                apply_rate_level(ctx.config, picked)
                _sync_limiter(ctx)
                ctx.logger.log("SETTING_CHANGED",
                               f"rate_limit_level={picked} (preset applied)")
                notice("RATELIMIT LEVEL",
                       [f"[white]{lookup_label(item['options'], picked)}[/white]",
                        "[dim]advanced values were reset to this preset[/dim]"], "good")
                press_enter()
                return None
            ctx.config.set_setting(key, picked)
            _sync_limiter(ctx)
            if key == "theme":
                apply_theme(ctx)
            ctx.logger.log("SETTING_CHANGED", f"{key}={picked}")
            notice(item["name"].upper(),
                   [f"[white]{lookup_label(item['options'], picked)}[/white] applied."], "good")
            press_enter()

    return None


# ------------------------------------------------------------
# SECTION 10b | RATE LIMIT SETTINGS
# ------------------------------------------------------------

RL_SPEC = [
    ("section", "ESSENTIAL"),
    {"key": "rl_wait_until", "name": "Wait Until Bucket Reset", "kind": "toggle"},
    {"key": "rl_timeout", "name": "Request Timeout", "kind": "number",
     "unit": " s", "min": 5, "max": 120},
    {"key": "rl_spacing_ms", "name": "Min Spacing", "kind": "number",
     "unit": " ms", "min": 0, "max": 5000},
    {"key": "rl_safety_ms", "name": "Safety Margin", "kind": "number",
     "unit": " ms", "min": 0, "max": 5000},
    ("section", "RETRY"),
    {"key": "rl_max_429", "name": "429 Retry Cap", "kind": "number",
     "min": 0, "max": 20},
    {"key": "rl_max_5xx", "name": "5xx Retry Cap", "kind": "number",
     "min": 0, "max": 10},
    ("section", "EXPERIMENTAL"),
    {"key": "rl_global_wait", "name": "Global 429 Wait", "kind": "toggle"},
    {"key": "rl_jitter", "name": "Jitter", "kind": "toggle"},
    ("section", "MAINTENANCE"),
    {"key": "rl_reset", "name": "Reset To Level Preset", "kind": "action"},
]


def _sync_limiter(ctx):
    """Push the current config into the live REST client, if there is one."""
    if ctx.rest is not None and getattr(ctx.rest, "limiter", None) is not None:
        ctx.rest.limiter.configure(ctx.config)


def confirm_ratelimit_off():
    """The warning shown before client-side pacing can be disabled.

    Two misconceptions get people here: that this switch also turns off
    Discord's own limits, or that Discord has no limits and the tool is just
    throttling itself for no reason. Neither is true, and the consequences
    (429 storms, a temporary IP block at the edge) hit the whole machine
    rather than one request.
    """
    notice(
        "YOU ARE ABOUT TO DISABLE RATE LIMITING",
        [
            "this turns off [white]NiNog Raker's own pacing[/white].",
            "[white]it does NOT turn off Discord's rate limits.[/white]",
            "",
            "discord rate limits every route, and there is a global cap across",
            "all of them. those limits are real, enforced on their side, and",
            "always apply. this tool is not adding an artificial delay to feel",
            "safe | it is waiting so you do not trip them.",
            "",
            "with pacing off, a mass op fires requests as fast as python can.",
            "you will start taking 429s almost immediately, and every 429",
            "still counts against you.",
            "",
            "[bad]sustained 429s can get your IP address temporarily blocked.[/bad]",
            "that block applies to every request from your machine, not just",
            "this bot. repeated abuse can also get the token reset or the",
            "account terminated.",
            "",
            "you gain nothing: you trade a small delay for a pile of failures.",
        ],
        "bad",
    )
    return confirm("I understand the risk | disable rate limiting anyway?", False)


def _rl_rows():
    rows = []
    idx = 0
    for entry in RL_SPEC:
        if isinstance(entry, tuple):
            rows.append(("section", entry[1]))
        else:
            rows.append(("item", idx, entry))
            idx += 1
    return rows


def _rl_items():
    return [e for e in RL_SPEC if not isinstance(e, tuple)]


def _rl_display(ctx, item):
    key, kind = item["key"], item["kind"]
    if kind == "action":
        return "open"
    if kind == "toggle":
        return "ON" if ctx.config.setting(key) else "OFF"
    if kind == "number":
        return f"{ctx.config.setting(key, 0)}{item.get('unit', '')}"
    return ""


def _rl_session_line(ctx):
    lim = getattr(ctx.rest, "limiter", None) if ctx.rest else None
    if lim is None:
        return "[faint]no client yet | counters start once a token is chosen[/faint]"
    snap = lim.snapshot()
    state = "[good]pacing on[/good]" if snap["enabled"] else "[bad]pacing off[/bad]"
    return (
        f"{state} [dim]|[/dim] [white]{snap['level']}[/white] "
        f"[dim]| spacing {int(snap['spacing'] * 1000)}ms | safety "
        f"{int(snap['safety'] * 1000)}ms | timeout {snap['timeout']}s[/dim]"
        f"\n[dim]this session: [white]{snap['waits']}[/white] waits totalling "
        f"[white]{snap['slept_for']}s[/white] | [white]{snap['hit_429']}[/white] "
        f"429 responses handled[/dim]"
    )


def rate_limit_settings_screen(ctx):
    rows = _rl_rows()
    items = _rl_items()
    offset = 0
    while True:
        vh = max(6, (console.height or 30) - 12)
        offset = max(0, min(offset, max(0, len(rows) - vh)))

        def render():
            print_header(ctx)
            console.print()
            console.print(grad("⚙ RATE LIMIT SETTINGS", ctx))
            console.print("[dim]essential, retry and experimental pacing controls[/dim]")
            console.print()
            if offset > 0:
                console.print("[faint]   ▲ scroll up[/faint]")
            for row in rows[offset:offset + vh]:
                if row[0] == "section":
                    console.print(f"[deep]   ── {row[1]} ─────────────────────[/deep]")
                else:
                    _, idx, item = row
                    console.print(
                        f"   [orange][{idx + 1:02d}][/orange] [white]{item['name']}[/white]"
                        f" [dim]| {_rl_display(ctx, item)}[/dim]"
                    )
            if offset + vh < len(rows):
                console.print("[faint]   ▼ scroll down[/faint]")
            console.print()
            console.print(_rl_session_line(ctx))
            console.print()
            console.print("[dim][W] up  [S] down  [num] change  [B] back[/dim]")
            console.print()

        present_frame(ctx, render)
        raw = ask("Select").lower()
        console.print()
        if raw in ("b", "", "q", "exit"):
            return None
        if raw == "w":
            offset -= 3
            continue
        if raw == "s":
            offset += 3
            continue

        if not (raw.isdigit() and 1 <= int(raw) <= len(items)):
            notice("OUT OF RANGE", [f"expected 1-{len(items)} | W | S | B."], "warn")
            press_enter()
            continue

        item = items[int(raw) - 1]
        key, kind = item["key"], item["kind"]

        if kind == "action":
            level = ctx.config.setting("rate_limit_level", "medium")
            preset = RATE_LIMIT_PRESETS.get(level, RATE_LIMIT_PRESETS["medium"])
            if not confirm(f"reset advanced values to the [white]{level}[/white] preset?"):
                continue
            apply_rate_level(ctx.config, level)
            _sync_limiter(ctx)
            ctx.logger.log("SETTING_CHANGED", f"rate limit values reset to {level}")
            notice("RESET",
                   [f"advanced values restored to [white]{level}[/white].",
                    f"[dim]spacing {int(preset['spacing'] * 1000)}ms | safety "
                    f"{int(preset['safety'] * 1000)}ms | timeout {preset['timeout']}s[/dim]"],
                   "good")
            press_enter()
            continue

        if kind == "toggle":
            new = not bool(ctx.config.setting(key))
            if key == "rl_wait_until" and not new:
                notice("NOT RECOMMENDED",
                       ["without this, the client ignores Discord's bucket headers",
                        "and relies only on the fixed spacing. 429s become far",
                        "more likely."], "warn")
                if not confirm("disable it anyway?", False):
                    continue
            ctx.config.set_setting(key, new)
            _sync_limiter(ctx)
            ctx.logger.log("SETTING_CHANGED", f"{key}={new}")
            notice(item["name"].upper(),
                   [f"[white]{'ON' if new else 'OFF'}[/white]"],
                   "good" if new else "warn")
            press_enter()
            continue

        if kind == "number":
            cur = int(ctx.config.setting(key, item["min"]))
            val = ask_int(f"{item['name'].lower()} ({item['min']}-{item['max']})", cur)
            val = max(item["min"], min(item["max"], val))
            ctx.config.set_setting(key, val)
            _sync_limiter(ctx)
            ctx.logger.log("SETTING_CHANGED", f"{key}={val}")
            notice(item["name"].upper(),
                   [f"[white]{val}{item.get('unit', '')}[/white]"], "good")
            press_enter()
            continue


def settings_screen(ctx):
    rows = settings_rows()
    items = settings_flat()
    offset = 0
    while True:
        vh = max(8, (console.height or 30) - 9)
        offset = max(0, min(offset, max(0, len(rows) - vh)))

        def render():
            print_header(ctx)
            gid = ctx.guild.get("id", "?") if ctx.guild else "?"
            gname = ctx.guild.get("name", "?") if ctx.guild else "?"
            bot = bot_display(ctx.me) if ctx.me else "?"
            console.print()
            console.print(grad("⚙ SETTINGS", ctx) + f" [dim]| Controls ({gid})[/dim]")
            console.print(f"[dim]{bot} | {gname}[/dim]")
            console.print()
            if offset > 0:
                console.print("[faint]   ▲ scroll up[/faint]")
            window = rows[offset:offset + vh]
            for row in window:
                if row[0] == "section":
                    console.print(f"[deep]   ── {row[1]} ─────────────────────[/deep]")
                else:
                    _, idx, item = row
                    val = setting_value_display(ctx, item)
                    console.print(
                        f"   [orange][{idx + 1:02d}][/orange] [white]{item['name']}[/white]"
                        f" [dim]| {val}[/dim]"
                    )
            if offset + vh < len(rows):
                console.print("[faint]   ▼ scroll down[/faint]")
            console.print()
            console.print(
                "[dim][W] up  [S] down  [num] change  [B] back  [00] exit[/dim]"
            )
            console.print()

        present_frame(ctx, render)
        raw = ask("Select").lower()
        console.print()
        if raw in ("b", ""):
            return None
        if raw in ("00", "q", "exit"):
            return "quit"
        if raw == "w":
            offset -= 3
            continue
        if raw == "s":
            offset += 3
            continue
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(items):
                result = settings_handle(ctx, items[n - 1])
                if result == "switch_token":
                    return "switch_token"
                if isinstance(result, tuple) and result[0] == "page":
                    return result
                continue
        notice("OUT OF RANGE",
               [f"expected 1-{len(items)} | W | S | B | 00."], "warn")
        press_enter()


# ------------------------------------------------------------
# SECTION 11 | PAGE REGISTRY
# ------------------------------------------------------------


# ------------------------------------------------------------
# SECTION 10c | TOOL MANAGEMENT: DIAGNOSER + REPORT EXPLORER
# ------------------------------------------------------------

def op_diagnoser(ctx):
    """Run the full health check and print a verdict table."""
    screen_title("DIAGNOSER", "source, config, network and filesystem checks")
    rows = []

    def add(name, ok, detail="", level=None):
        rows.append((name, ok, detail, level))

    # -- source files -------------------------------------------------
    for path in source_files():
        if not path.exists():
            add(f"source {path.name}", False, "MISSING FROM DISK")
            continue
        digest = sha256_file(path)
        size = path.stat().st_size
        add(f"source {path.name}", bool(digest),
            f"sha256 {digest[:12]}... | {size} bytes" if digest else "unreadable")

    # -- python syntax -------------------------------------------------
    for path in source_files():
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
            add(f"compile {path.name}", True, "parses clean")
        except SyntaxError as e:
            add(f"compile {path.name}", False, f"line {e.lineno}: {e.msg}")

    # -- config files: parse + unicode sniff ---------------------------
    for path in (CONFIG_FILE, TOKENS_FILE, WHITELIST_FILE):
        if not path.exists():
            add(f"config {path.name}", True, "absent (fresh state)", "dim")
            continue
        raw = path.read_bytes()
        naughty = [b for b in raw if b > 127][:8]
        try:
            json.loads(raw.decode("utf-8"))
            add(f"config {path.name}", True,
                f"valid json | {len(raw)} bytes"
                + (f" | note: {len([b for b in raw if b > 127])} non-ascii bytes"
                   if naughty else ""))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            add(f"config {path.name}", False,
                f"CORRUPT: {str(e)[:80]} | non-ascii bytes: {len([b for b in raw if b > 127])}")

    # -- workflow files ------------------------------------------------
    from .core import WORKFLOWS_DIR
    if WORKFLOWS_DIR.exists():
        for path in sorted(WORKFLOWS_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                ok = isinstance(data.get("steps"), list)
                add(f"workflow {path.name}", ok,
                    f"{len(data.get('steps', []))} steps" if ok else "missing steps list")
            except Exception as e:
                add(f"workflow {path.name}", False, f"CORRUPT: {str(e)[:60]}")

    # -- filesystem writability ----------------------------------------
    for d in (CONFIG_DIR, SNAPSHOTS_DIR, REPORTLOG_DIR, EXPORTS_DIR):
        try:
            probe = d / ".diag_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            add(f"writable {d.name}/", True, "write probe passed")
        except OSError as e:
            add(f"writable {d.name}/", False, str(e)[:60])

    # -- snapshots + logs ----------------------------------------------
    snaps = load_snapshot_runs()
    add("snapshots", True, f"{len(snaps)} stored run(s)", "dim")
    logs = list(REPORTLOG_DIR.glob("*.log")) if REPORTLOG_DIR.exists() else []
    crashes = list((REPORTLOG_DIR / "crashes").glob("*")) \
        if (REPORTLOG_DIR / "crashes").exists() else []
    add("report log", True,
        f"{len(logs)} session log(s) | {len(crashes)} crash bundle(s)", "dim")

    # -- network / api ---------------------------------------------------
    if ctx.rest is not None:
        try:
            g = ctx.rest.get_gateway()
            add("discord gateway", g.status_code == 200, f"status {g.status_code}")
        except ApiNetworkError as e:
            add("discord gateway", False, str(e)[:60])
        if ctx.me:
            add("token identity", True, bot_display(ctx.me), "dim")
        if ctx.guild:
            add("guild membership", not ctx.guild_lost,
                (ctx.guild or {}).get("name", "?")
                + ("  <-- BOT WAS KICKED/BANNED" if ctx.guild_lost else ""))
            wd = "off"
            if ctx.watchdog is not None:
                wd = f"on, {ctx.watchdog.interval}s interval, last probe: {ctx.watchdog.last_status}"
            add("ban watchdog", ctx.watchdog is not None, wd,
                None if ctx.watchdog is not None else "warn")
    else:
        add("api", True, "no client yet | network checks skipped", "dim")

    # -- dependency versions ---------------------------------------------
    import importlib
    for mod in ("rich", "pyfiglet", "requests"):
        try:
            m = importlib.import_module(mod)
            add(f"dep {mod}", True, getattr(m, "__version__", "installed"), "dim")
        except ImportError:
            add(f"dep {mod}", False, "MISSING")
    try:
        importlib.import_module("websockets")
        add("dep websockets", True, "presence sorting available", "dim")
    except ImportError:
        add("dep websockets", "optional",
            "absent | online-first sorting disabled", "warn")

    # -- rate limiter snapshot -------------------------------------------
    lim = getattr(ctx.rest, "limiter", None) if ctx.rest else None
    if lim is not None:
        snap = lim.snapshot()
        add("rate limiter", True,
            f"{snap['level']} | {snap['waits']} waits | {snap['hit_429']} 429s", "dim")

    # -- render -----------------------------------------------------------
    table = Table(box=box.SIMPLE_HEAVY, header_style="brand",
                  border_style="deep", padding=(0, 2))
    table.add_column("CHECK", style="white")
    table.add_column("RESULT", justify="center")
    table.add_column("DETAIL", style="dim")
    bad = warn = 0
    for name, ok, detail, level in rows:
        if ok is True:
            mark = "[good]PASS[/good]"
        elif ok == "optional" or level == "warn":
            mark = "[warn]WARN[/warn]"
            warn += 1
        else:
            mark = "[bad]FAIL[/bad]"
            bad += 1
        table.add_row(name, mark, str(detail))
    console.print(table)
    console.print()
    state = "good" if bad == 0 and warn == 0 else ("warn" if bad == 0 else "bad")
    notice(
        "DIAGNOSIS COMPLETE",
        [f"[white]{len(rows)}[/white] checks | [white]{bad}[/white] failed | "
         f"[white]{warn}[/white] warnings",
         ("everything green." if state == "good" else
          "warnings only | tool is usable." if state == "warn" else
          f"failures need attention | send a report to {MAINTAINER_CONTACT} if stuck.")],
        state,
    )
    ctx.logger.log("OP_RESULT", f"diagnoser | {len(rows)} checks, {bad} fail, {warn} warn")


def _report_entries():
    """Everything viewable in reportlog/: session logs and crash bundles."""
    entries = []
    if REPORTLOG_DIR.exists():
        for p in sorted(REPORTLOG_DIR.glob("*.log")):
            entries.append(("session log", p.name, p))
        cd = REPORTLOG_DIR / "crashes"
        if cd.exists():
            for p in sorted(cd.iterdir(), reverse=True):
                if p.is_dir():
                    entries.append(("crash bundle", p.name, p))
    return entries


def _view_text_file(path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        notice("UNREADABLE", [str(e)], "bad")
        return
    lines = text.splitlines()
    page = 40
    start = 0
    while True:
        console.print()
        console.print(f"[white]{path.name}[/white] [dim]| lines {start + 1}-"
                      f"{min(start + page, len(lines))} of {len(lines)}[/dim]")
        console.print()
        for ln in lines[start:start + page]:
            console.print(f"[dim]{ln}[/dim]")
        if start + page >= len(lines):
            press_enter()
            return
        raw = ask("N more | B back").lower().strip()
        if raw != "n":
            return
        start += page


def op_report_explorer(ctx):
    screen_title("REPORT EXPLORER", "session logs, crash bundles, diag output")
    while True:
        entries = _report_entries()
        if not entries:
            notice("NO REPORTS", ["reportlog/ is empty so far."], "warn")
            return
        page_size = max(1, int(ctx.config.setting("page_size", 10)))
        pages = chunk(entries, page_size)
        page_idx = 0
        while True:
            page = pages[page_idx]
            offset = page_idx * page_size
            table = Table(box=box.SIMPLE_HEAVY, header_style="brand",
                          border_style="deep", padding=(0, 2))
            table.add_column("#", justify="right", style="dim")
            table.add_column("KIND", style="orange")
            table.add_column("NAME", style="white")
            for i, (kind, name, _p) in enumerate(page):
                table.add_row(str(offset + i + 1), kind, name)
            console.print(table)
            console.print(f"[dim]page {page_idx + 1}/{len(pages)}[/dim]")
            raw = ask(f"1-{len(entries)} open | N/P pages | D n delete | B back"
                      ).lower().strip()
            if raw == "b":
                return
            if raw == "n" and len(pages) > 1:
                page_idx = (page_idx + 1) % len(pages)
                continue
            if raw == "p" and len(pages) > 1:
                page_idx = (page_idx - 1) % len(pages)
                continue

            del_match = re.match(r"^d\s*(\d+)$", raw)
            if del_match:
                n = int(del_match.group(1))
                if not (1 <= n <= len(entries)):
                    notice("OUT OF RANGE", [f"1-{len(entries)}."], "warn")
                    continue
                kind, name, path = entries[n - 1]
                if confirm(f"delete {kind} [white]{name}[/white]?"):
                    if path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        path.unlink(missing_ok=True)
                    ctx.logger.log("OP_RESULT", f"report deleted | {name}")
                    notice("DELETED", [name], "good")
                break

            if raw.isdigit() and 1 <= int(raw) <= len(entries):
                kind, name, path = entries[int(raw) - 1]
                if path.is_dir():
                    _explore_crash_bundle(ctx, path)
                else:
                    _view_text_file(path)
                break
            notice("OUT OF RANGE", [f"1-{len(entries)} | N P D B."], "warn")


def _explore_crash_bundle(ctx, bundle):
    while True:
        files = sorted(p for p in bundle.rglob("*") if p.is_file())
        console.print()
        screen_title("CRASH BUNDLE", str(bundle))
        for i, f in enumerate(files):
            console.print(f"  [orange][{i + 1:02d}][/orange] [white]"
                          f"{f.relative_to(bundle)}[/white]"
                          f" [dim]{f.stat().st_size} bytes[/dim]")
        raw = ask(f"1-{len(files)} view | B back").lower().strip()
        if raw in ("b", "", "q"):
            return
        if raw.isdigit() and 1 <= int(raw) <= len(files):
            _view_text_file(files[int(raw) - 1])
            continue
        notice("OUT OF RANGE", [f"1-{len(files)} | B."], "warn")


def op_support_bundle(ctx):
    """Build the full diagnostic bundle on demand, without a crash."""
    if not confirm("build a support bundle now? (source, config snapshot, logs)"):
        return
    try:
        raise RuntimeError("manual support bundle | nothing crashed")
    except RuntimeError as e:
        report = build_crash_report(RuntimeError, e, sys.exc_info()[2],
                                    ctx=ctx, origin="manual support bundle")
    ctx.logger.log("OP_RESULT", f"support bundle | {report}")
    notice(
        "BUNDLE READY",
        [
            f"[white]{report}[/white]",
            "",
            "send the whole folder to THE RATTIKANS:",
            f"  {MAINTAINER_CONTACT}",
            f"  {MAINTAINER_SERVER}",
            "a fix will be supplied from it.",
        ],
        "good",
    )
    press_enter()

def op(name, desc, fn):
    return {"name": name, "desc": desc, "fn": fn}


PAGES = [
    {
        "name": "ASSAULT OPERATIONS",
        "desc": "mass actions | every destructive op has its opposite",
        "ops": [
            op("Delete All Channels", "wipe every channel", op_wipe_channels),
            op("Create Channels", "mass create channels", op_flood_channels),
            op("Delete All Roles", "wipe every deletable role", op_wipe_roles),
            op("Create Roles", "mass create roles", op_flood_roles),
            op("Ban All", "ban every member", op_ban_all),
            op("Unban All", "lift every ban", op_unban_all),
            op("Kick All", "kick every kickable member", op_mass_kick),
            op("Ban Member", "ban one member | any pick method", op_ban_member),
            op("Kick Member", "kick one member | any pick method", op_kick_member),
            op("Change Server Name", "rename the guild", op_rename_server),
            op("Role Engine", "roles with real permissions, aimed at anyone", op_role_engine),
            op("Server Info", "full guild readout", op_server_info),
        ],
    },
    {
        "name": "RECON | RESTORE",
        "desc": "snapshot, rebuild, inspect",
        "ops": [
            op("Snapshot Now", "capture the full server", op_snapshot_now),
            op("Browse Snapshots", "list stored snapshots", op_browse_snapshots),
            op("Delete Snapshot", "remove a stored snapshot", op_delete_snapshot),
            op("Restore Full", "roles | channels | settings | messages", lambda ctx: run_restore(ctx, "full")),
            op("Restore Channels", "recreate channel structure", lambda ctx: run_restore(ctx, "channels")),
            op("Restore Roles", "recreate role structure", lambda ctx: run_restore(ctx, "roles")),
            op("Restore Settings", "server settings | onboarding", lambda ctx: run_restore(ctx, "settings")),
            op("Member Lookup", "view one member in detail", op_member_lookup),
            op("Scan Bots", "find every bot in the server", op_scan_bots),
            op("View Ban List", "all bans, paginated", op_view_ban_list),
            op("Unban Member", "pick from ban list", op_unban_member),
        ],
    },
    {
        "name": "MESSAGING | WEBHOOKS",
        "desc": "messages, DMs, webhook work",
        "ops": [
            op("Send Message", "post to one channel", op_send_message),
            op("DM All", "DM every human member", op_dm_all),
            op("Purge Messages", "wipe recent messages per channel", op_purge_messages),
            op("Create Webhooks", "webhooks across all channels", op_create_webhooks),
            op("Webhook Spam", "execute existing webhooks", op_webhook_spam),
            op("Delete All Webhooks", "wipe every webhook", op_delete_all_webhooks),
            op("Extract Webhooks", "pull every webhook url out of the server", op_extract_webhooks),
            op("Create Invite", "generate an invite link", op_create_invite),
        ],
    },
    {
        "name": "PRECISION TOOLS",
        "desc": "targeted single-target control",
        "ops": [
            op("Channel Control", "create | rename | delete channels", op_channel_control),
            op("Role Control", "create | rename | delete roles", op_role_control),
        ],
    },
    {
        "name": "WHITELIST",
        "desc": "protected users | mass whitelist actions",
        "ops": [
            op("Add By Username", "fuzzy search the guild", op_whitelist_add_search),
            op("Add By User ID", "direct id entry", op_whitelist_add_id),
            op("Add From DMs", "pick from users who DM'd the bot", op_whitelist_add_dms),
            op("Remove Entries", "checkbox removal", op_whitelist_remove),
            op("Mass Unban", "unban selected whitelisted", op_whitelist_unban),
            op("Mass Ban", "ban selected whitelisted", op_whitelist_ban),
            op("DM Invite", "DM selected an invite link", op_whitelist_dm_invite),
            op("DM Message", "DM selected a message", op_whitelist_dm_message),
        ],
    },
    {
        "name": "TOOL MANAGEMENT",
        "desc": "the tool takes care of itself here",
        "ops": [
            op("Diagnoser", "full health check: source, config, api, fs", op_diagnoser),
            op("Report Explorer", "browse session logs and crash bundles", op_report_explorer),
            op("Support Bundle", "package everything for THE RATTIKANS", op_support_bundle),
        ],
    },
]

# The workflow engine needs to resolve "any op" by name, which means it needs
# this registry. It cannot import it (that would be circular), so it is handed
# over here and bound into the WORKFLOWS page below.
OP_INDEX = build_index(PAGES)

PAGES.append(
    {
        "name": "WORKFLOWS",
        "desc": "chained ops with pre-set inputs",
        "ops": [
            op(name, desc, partial(fn, index=OP_INDEX))
            for name, desc, fn in page_ops()
        ],
    }
)


# ------------------------------------------------------------
# SECTION 12 | PAGE RENDERER + ROUTER
# ------------------------------------------------------------

def render_ops(ctx, page):
    layout = ctx.config.setting("layout", "panels")
    ops = page["ops"]
    w = console.width or 80
    gr = gradient_on(ctx)

    if layout == "panels":
        panels = []
        for i, e in enumerate(ops):
            tag = grad(f"[{i + 1:02d}]", ctx) if gr else f"[brand][{i + 1:02d}][/brand]"
            title = f"{tag} [white]{e['name']}[/white]"
            panels.append(
                Panel(
                    f"[dim]{e['desc']}[/dim]",
                    title=title,
                    title_align="left",
                    border_style="deep",
                    box=box.ROUNDED,
                    padding=(0, 1),
                )
            )
        if w < 74:
            for p in panels:
                console.print(p)
        else:
            grid = Table(box=None, show_header=False, padding=(1, 2), expand=True)
            grid.add_column(ratio=1)
            grid.add_column(ratio=1)
            for i in range(0, len(panels), 2):
                left = panels[i]
                right = panels[i + 1] if i + 1 < len(panels) else ""
                grid.add_row(left, right)
            console.print(grid)

    elif layout == "compact" and w >= 74:
        grid = Table(box=None, show_header=False, padding=(0, 4), expand=True)
        grid.add_column(no_wrap=True)
        grid.add_column(no_wrap=True)
        half = (len(ops) + 1) // 2
        for i in range(half):
            left = f"[brand][{i + 1:02d}][/brand] [white]{ops[i]['name']}[/white]"
            j = half + i
            right = (f"[brand][{j + 1:02d}][/brand] [white]{ops[j]['name']}[/white]"
                     if j < len(ops) else "")
            grid.add_row(left, right)
        console.print(grid)

    else:
        for i, e in enumerate(ops):
            console.print(
                f"  [brand][{i + 1:02d}][/brand] [white]{e['name']}[/white] "
                f"[dim]| {e['desc']}[/dim]"
            )


def render_page(ctx, page, page_idx):
    print_header(ctx)
    gid = ctx.guild.get("id", "?") if ctx.guild else "?"
    gname = ctx.guild.get("name", "?") if ctx.guild else "?"
    bot = bot_display(ctx.me) if ctx.me else "?"
    # Surfaced on every page: ops that need permissions will fail for a
    # non-administrator bot, and it is better to see that before selecting one.
    if ctx.perms & PERMISSION_BITS["ADMINISTRATOR"]:
        rights = " [good]| administrator[/good]"
    else:
        rights = " [warn]| no administrator[/warn]"
    console.print(f"{grad(page['name'], ctx)} [dim]| Controls ({gid})[/dim]")
    console.print(f"[dim]{bot} | {gname}[/dim]{rights}")
    console.print()
    render_ops(ctx, page)
    console.print()

    nxt = PAGES[(page_idx + 1) % len(PAGES)]
    prv = PAGES[(page_idx - 1) % len(PAGES)]
    foot = Table.grid(padding=(0, 2))
    foot.add_row(
        f"[white][>>][/white] Page {(page_idx + 1) % len(PAGES) + 1} | {nxt['name']}",
        f"[white][<<][/white] Page {(page_idx - 1) % len(PAGES) + 1} | {prv['name']}",
    )
    foot.add_row(
        "[white][~][/white] Settings",
        "[white][00][/white] Exit",
    )
    console.print(Panel(foot, border_style="deep", box=box.ROUNDED, padding=(0, 2)))
    console.print()


def page_loop(ctx):
    page_idx = 0
    while True:
        # Async surface first: watchdog alerts from the daemon thread, then
        # any workflow triggers waiting to fire. Both run while we own the
        # screen, never on top of an op.
        drain_alerts(ctx)
        run_due_workflows(ctx, OP_INDEX)
        page = PAGES[page_idx]

        def render():
            render_page(ctx, page, page_idx)

        present_frame(ctx, render)
        choice = ask("Select").lower()
        console.print()

        if choice in ("00", "q", "exit", "x"):
            return "quit"
        if choice in ("~", "e"):
            result = settings_screen(ctx)
            if result == "quit":
                return "quit"
            if result == "switch_token":
                return "switch_token"
            if isinstance(result, tuple) and result[0] == "page":
                page_idx = result[1]
            continue
        if choice == ">>":
            page_idx = (page_idx + 1) % len(PAGES)
            continue
        if choice == "<<":
            page_idx = (page_idx - 1) % len(PAGES)
            continue
        if choice.isdigit():
            n = int(choice)
            if 1 <= n <= len(page["ops"]):
                entry = page["ops"][n - 1]
                ctx.logger.log("OP_RUN", f"{page['name']} > {entry['name']}")
                try:
                    entry["fn"](ctx)
                except ApiNetworkError as e:
                    notice("NETWORK ERROR", [str(e)], "bad")
                except KeyboardInterrupt:
                    notice("ABORTED", ["op interrupted."], "warn")
                except Exception as e:
                    # Unplanned failures get the full bundle treatment: the
                    # session survives, the user gets a report to send off.
                    _op_crash_report(ctx, e, f"op {page['name']} > {entry['name']}")
                press_enter()
                continue
        notice("OUT OF RANGE",
               [f"expected 1-{len(page['ops'])} | ~ settings | >> | << | 00."], "warn")
        press_enter()
