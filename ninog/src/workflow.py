# ==============================================================
#   NiNog Raker v2.0 | THE RATTIKANS
#   src/workflow.py | workflow engine: chains, triggers
# ==============================================================
#
#   A workflow is an ordered list of steps, with NO cap on how many
#   ops you chain. Step kinds:
#
#     op        run any op from the registry, referenced by slug
#     wait      fixed delay, or poll-until-condition with a timeout
#     condition fetch a live guild stat and branch on it (then / else)
#     confirm   a gate: the run stops cleanly if the user declines
#     loop      run a nested list of steps N times
#     note      an informational line, marked done immediately
#
#   Every workflow also carries triggers:
#
#     manual           only runs from the execute menu (default)
#     on_guild_select  fires right after a guild is selected
#     on_ban_detected  fires when the watchdog reports the bot was
#                      kicked or banned from the selected guild
#     interval         fires every N seconds while menus are up
#
#   Engine screen follows the V1 layout: numbered actions, saved
#   workflows listed with step counts and their triggers.
#
#   This module imports src.core and src.ui only. It never imports
#   src.ops -- ops.py imports this module and hands the op registry
#   in as a parameter, which keeps the one dangerous edge from
#   being circular. The engine entry point itself is excluded from
#   the chainable index so a workflow cannot run the manager.
# ==============================================================

import json
import re
import time
from datetime import datetime

from rich import box
from rich.panel import Panel
from rich.table import Table

from .core import (
    PERMISSION_BITS,
    WORKFLOWS_DIR,
    ApiNetworkError,
)
from .ui import (
    DONE,
    FAILED,
    PENDING,
    RUNNING,
    SKIPPED,
    TodoBoard,
    ask,
    ask_int,
    confirm,
    console,
    grad,
    notice,
    present_frame,
    press_enter,
    print_header,
    sanitize_name,
    screen_title,
)

MAX_DEPTH = 3            # nesting limit for conditions and loops
MAX_LOOP_TIMES = 50      # per-loop pass cap; the CHAIN ITSELF has no cap
MAX_STEPS_SHOWN = 18     # builder table page size

# The engine manages workflows; running it from inside its own run would
# recurse with no guard, so these slugs never appear in the chain index.
EXCLUDED_SLUGS = {"workflow_engine"}

# What each op needs, keyed by slug. An op with no entry either needs nothing
# beyond what every bot has, or needs a privileged intent that permissions
# cannot express -- those are listed separately in _INTENT_ONLY.
OP_PERMISSIONS = {
    "delete_all_channels": ["MANAGE_CHANNELS"],
    "create_channels": ["MANAGE_CHANNELS"],
    "channel_control": ["MANAGE_CHANNELS"],
    "delete_all_roles": ["MANAGE_ROLES"],
    "create_roles": ["MANAGE_ROLES"],
    "role_control": ["MANAGE_ROLES"],
    "role_engine": ["MANAGE_ROLES"],
    "ban_all": ["BAN_MEMBERS"],
    "ban_member": ["BAN_MEMBERS"],
    "unban_all": ["BAN_MEMBERS"],
    "unban_member": ["BAN_MEMBERS"],
    "view_ban_list": ["BAN_MEMBERS"],
    "mass_ban": ["BAN_MEMBERS"],
    "mass_unban": ["BAN_MEMBERS"],
    "kick_all": ["KICK_MEMBERS"],
    "kick_member": ["KICK_MEMBERS"],
    "change_server_name": ["MANAGE_GUILD"],
    "restore_full": ["MANAGE_ROLES", "MANAGE_CHANNELS", "MANAGE_GUILD"],
    "restore_channels": ["MANAGE_CHANNELS"],
    "restore_roles": ["MANAGE_ROLES"],
    "restore_settings": ["MANAGE_GUILD"],
    "send_message": ["SEND_MESSAGES"],
    "purge_messages": ["MANAGE_MESSAGES"],
    "create_webhooks": ["MANAGE_WEBHOOKS"],
    "webhook_spam": ["MANAGE_WEBHOOKS"],
    "delete_all_webhooks": ["MANAGE_WEBHOOKS"],
    "extract_webhooks": ["MANAGE_WEBHOOKS"],
    "create_invite": ["CREATE_INSTANT_INVITE"],
}

# Ops that need a privileged gateway intent rather than a role permission.
# They are reported as "limited" rather than "skipped" because the bot may
# still be able to run them depending on its developer settings.
_INTENT_ONLY = {
    "member_lookup": "SERVER MEMBERS intent",
    "scan_bots": "SERVER MEMBERS intent",
    "dm_all": "no permission needed, but DMs may be closed",
    "snapshot_now": "full capture needs SERVER MEMBERS intent",
}

COMPARATORS = [">", ">=", "<", "<=", "==", "!="]

CONDITION_CHECKS = [
    ("channel_count", "number of channels in the guild"),
    ("role_count", "number of roles in the guild"),
    ("member_count", "fetched member count"),
    ("ban_count", "number of active bans"),
    ("webhook_count", "webhooks on the guild"),
    ("bot_is_admin", "does the bot hold administrator"),
    ("always", "always true (use for else-only branching)"),
]

TRIGGER_KINDS = [
    ("manual", "manual | run from the execute menu only"),
    ("on_guild_select", "on guild select | fires after a server is picked"),
    ("on_ban_detected", "on ban detected | fires when the bot is kicked/banned"),
    ("interval", "interval | fires every N seconds"),
]


# ------------------------------------------------------------
# REGISTRY
# ------------------------------------------------------------

def slugify(name):
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_") or "op"


def build_index(pages):
    """slug -> {'name', 'desc', 'fn', 'page'} for every chainable op."""
    index = {}
    for page in pages:
        for entry in page.get("ops", []):
            slug = slugify(entry["name"])
            if slug in EXCLUDED_SLUGS:
                continue
            # Two ops cannot share a slug; the second one gets a suffix so the
            # reference stays resolvable instead of silently pointing at the
            # wrong op.
            if slug in index:
                slug = f"{slug}_{len(index)}"
            index[slug] = {
                "name": entry["name"],
                "desc": entry.get("desc", ""),
                "fn": entry["fn"],
                "page": page.get("name", ""),
            }
    return index


def missing_perms(slug, ctx):
    """Permission names this op needs that the bot does not hold."""
    if ctx.perms & PERMISSION_BITS["ADMINISTRATOR"]:
        return []
    return [p for p in OP_PERMISSIONS.get(slug, [])
            if not (ctx.perms & PERMISSION_BITS[p])]


def op_limitations(slug, ctx):
    """Non-permission reasons an op may be limited, for the preview panel."""
    if ctx.perms & PERMISSION_BITS["ADMINISTRATOR"]:
        return None
    return _INTENT_ONLY.get(slug)


# ------------------------------------------------------------
# DESCRIBE / FLATTEN
# ------------------------------------------------------------

def describe_step(step, index=None):
    k = step.get("kind", "?")
    if k == "op":
        slug = step.get("op", "?")
        entry = (index or {}).get(slug)
        return entry["name"] if entry else slug
    if k == "wait":
        if step.get("until"):
            c = step["until"]
            return (f"wait until {c.get('check', '?')} {c.get('cmp', '?')} "
                    f"{c.get('value', '?')} (max {step.get('seconds', 120)}s)")
        return f"wait {step.get('seconds', 0)}s"
    if k == "condition":
        return (f"if {step.get('check', '?')} {step.get('cmp', '?')} "
                f"{step.get('value', '?')}")
    if k == "confirm":
        return f"gate | {step.get('prompt', 'continue?')}"
    if k == "loop":
        return f"loop x{step.get('times', 1)}"
    if k == "note":
        return f"note | {step.get('text', '')}"
    return f"unknown ({k})"


def step_kind(step):
    return step.get("kind", "")


def flatten_steps(steps, depth=0, path=()):
    """Linear, ordered view of a nested step tree for the to-do board."""
    out = []
    for i, s in enumerate(steps):
        sid = path + (i,)
        out.append((sid, depth, s))
        if step_kind(s) == "condition":
            out += flatten_steps(s.get("then") or [], depth + 1, sid + ("t",))
            out += flatten_steps(s.get("else") or [], depth + 1, sid + ("e",))
        elif step_kind(s) == "loop":
            out += flatten_steps(s.get("steps") or [], depth + 1, sid + ("l",))
    return out


def container_chain(sid):
    """Container ids that must be 'taken' before the step at `sid` can run."""
    chains = []
    i = 0
    while i < len(sid):
        if isinstance(sid[i], int):
            step_id = sid[: i + 1]
            i += 1
            if i < len(sid) and not isinstance(sid[i], int):
                chains.append(step_id + (sid[i],))
                i += 1
        else:
            i += 1
    return chains


def _is_runnable(sid, taken):
    return all(c in taken for c in container_chain(sid))


def _mark_branch(board, plan, branch_id):
    """Mark every item inside an un-entered branch as skipped."""
    for bi, (sid, _, _) in enumerate(plan):
        if branch_id in container_chain(sid):
            board.skip(bi, "branch not taken")


# ------------------------------------------------------------
# CONDITION EVALUATION
# ------------------------------------------------------------

def _guild_stat(ctx, key, cache):
    if key in cache:
        return cache[key]
    gid = (ctx.guild or {}).get("id")
    if not gid:
        cache[key] = None
        return None
    rest = ctx.rest
    value = None
    if key == "channel_count":
        r = rest.get_channels(gid)
        value = len(r.json()) if r.status_code == 200 else None
    elif key == "role_count":
        r = rest.get_roles(gid)
        value = len(r.json()) if r.status_code == 200 else None
    elif key == "ban_count":
        r, bans = rest.get_all_bans(gid)
        value = len(bans) if r.status_code == 200 else None
    elif key == "webhook_count":
        r = rest.get_guild_webhooks(gid)
        value = len(r.json()) if r.status_code == 200 else None
    elif key == "member_count":
        r, members = rest.get_all_members(gid)
        value = len(members) if r.status_code == 200 else None
    cache[key] = value
    return value


def _compare(left, cmp, right):
    try:
        a, b = float(left), float(right)
    except (TypeError, ValueError):
        a, b = left, right
    try:
        if cmp == ">":
            return a > b
        if cmp == ">=":
            return a >= b
        if cmp == "<":
            return a < b
        if cmp == "<=":
            return a <= b
        if cmp == "==":
            return a == b
        if cmp == "!=":
            return a != b
    except TypeError:
        return False
    return False


def _eval_condition(ctx, cond, cache):
    """Returns (result, reason). A stat that cannot be read is never true."""
    check = cond.get("check", "always")
    if check == "always":
        return True, "always"
    if check == "bot_is_admin":
        ok = bool(ctx.perms & PERMISSION_BITS["ADMINISTRATOR"])
        return ok, "administrator held" if ok else "no administrator"
    value = _guild_stat(ctx, check, cache)
    if value is None:
        return False, f"{check} unreadable (403, network, or no guild selected)"
    ok = _compare(value, cond.get("cmp", ">="), cond.get("value", 0))
    return ok, f"{check}={value} {cond.get('cmp', '>=')} {cond.get('value', 0)}"


# ------------------------------------------------------------
# STORAGE
# ------------------------------------------------------------

def normalize_triggers(wf):
    """Every workflow carries a trigger list; old files default to manual."""
    trig = wf.get("triggers")
    if not isinstance(trig, list) or not trig:
        return [{"kind": "manual"}]
    out = []
    for t in trig:
        if isinstance(t, dict) and t.get("kind"):
            out.append(t)
    return out or [{"kind": "manual"}]


def trigger_summary(wf):
    parts = []
    for t in normalize_triggers(wf):
        k = t.get("kind", "manual")
        if k == "interval":
            parts.append(f"interval {int(t.get('seconds', 300))}s")
        else:
            parts.append(k)
    return ", ".join(parts) or "manual"


def load_workflows():
    if not WORKFLOWS_DIR.exists():
        return []
    out = []
    for path in sorted(WORKFLOWS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get("steps"), list):
            out.append(data)
    out.sort(key=lambda w: str(w.get("name", "")).lower())
    return out


def save_workflow(wf):
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    path = WORKFLOWS_DIR / f"{sanitize_name(wf.get('name', 'workflow'))}.json"
    path.write_text(json.dumps(wf, indent=2), encoding="utf-8")
    return path


def delete_workflow_file(name):
    path = WORKFLOWS_DIR / f"{sanitize_name(name)}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def _pick_workflow(ctx, verb="select"):
    flows = load_workflows()
    if not flows:
        notice("NO WORKFLOWS", ["nothing saved in config/workflows/ yet."], "warn")
        return None
    table = Table(box=box.SIMPLE_HEAVY, header_style="brand",
                  border_style="deep", padding=(0, 2))
    table.add_column("#", justify="right", style="dim")
    table.add_column("NAME", style="white")
    table.add_column("STEPS", justify="right", style="dim")
    table.add_column("TRIGGERS", style="dim")
    for i, wf in enumerate(flows):
        table.add_row(
            str(i + 1),
            wf.get("name", "?"),
            str(len(flatten_steps(wf.get("steps", [])))),
            trigger_summary(wf),
        )
    console.print(table)
    choice = ask(f"1-{len(flows)} {verb} | B back").lower()
    if choice == "b":
        return None
    if not (choice.isdigit() and 1 <= int(choice) <= len(flows)):
        notice("OUT OF RANGE", [f"expected 1-{len(flows)}."], "warn")
        return None
    return flows[int(choice) - 1]


# ------------------------------------------------------------
# BUILDER
# ------------------------------------------------------------

def _render_steps(ctx, index, steps, depth=0):
    lines = []
    for i, s in enumerate(steps):
        pad = "    " * depth
        lines.append(f"[orange]{i + 1:02d}[/orange] {pad}[white]{describe_step(s, index)}[/white]")
        if step_kind(s) == "condition":
            if s.get("then"):
                lines.append(f"{pad}   [dim]then[/dim]")
                lines += _render_steps(ctx, index, s["then"], depth + 1)
            if s.get("else"):
                lines.append(f"{pad}   [dim]else[/dim]")
                lines += _render_steps(ctx, index, s["else"], depth + 1)
        elif step_kind(s) == "loop":
            if s.get("steps"):
                lines.append(f"{pad}   [dim]body[/dim]")
                lines += _render_steps(ctx, index, s["steps"], depth + 1)
    return lines


def _chainable_entries(index):
    """Flat list of (slug, entry) in registry order."""
    return sorted(index.items(), key=lambda kv: (kv[1].get("page", ""), kv[1]["name"]))


def _pick_op_chained(ctx, index):
    """V1-style 'Available actions' list. Numbered, paginated, 0 cancels."""
    entries = _chainable_entries(index)
    if not entries:
        notice("EMPTY", ["no chainable ops found."], "warn")
        return None
    page_idx = 0
    per = 20
    pages = [entries[i:i + per] for i in range(0, len(entries), per)]
    while True:
        chunk_ = pages[page_idx]
        console.print()
        console.print("  [white]Available actions:[/white]")
        console.print()
        for i, (slug, entry) in enumerate(chunk_):
            n = page_idx * per + i + 1
            console.print(
                f"    [orange][{n:2d}][/orange] [white]{entry['name']}[/white] "
                f"[dim]{entry.get('page', '')}[/dim]"
            )
        console.print()
        nav = f"  [dim]page {page_idx + 1}/{len(pages)}"
        if len(pages) > 1:
            nav += " | N next | P prev"
        nav += " | 0 cancel[/dim]"
        console.print(nav)
        raw = ask("pick action number").lower().strip()
        if raw in ("0", "b", ""):
            return None
        if raw == "n" and len(pages) > 1:
            page_idx = (page_idx + 1) % len(pages)
            continue
        if raw == "p" and len(pages) > 1:
            page_idx = (page_idx - 1) % len(pages)
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(entries):
            return entries[int(raw) - 1][0]
        notice("OUT OF RANGE", [f"expected 1-{len(entries)} | 0 cancel."], "warn")


def _build_condition(ctx):
    console.print()
    for i, (key, label) in enumerate(CONDITION_CHECKS):
        console.print(f"  [orange][{i + 1:02d}][/orange] [white]{key}[/white] [dim]| {label}[/dim]")
    pick = ask(f"1-{len(CONDITION_CHECKS)} check").lower()
    if not (pick.isdigit() and 1 <= int(pick) <= len(CONDITION_CHECKS)):
        notice("OUT OF RANGE", [f"expected 1-{len(CONDITION_CHECKS)}."], "warn")
        return None
    check = CONDITION_CHECKS[int(pick) - 1][0]

    if check == "always":
        return {"kind": "condition", "check": "always", "cmp": "==", "value": 1,
                "then": [], "else": []}

    console.print()
    for i, cmp in enumerate(COMPARATORS):
        console.print(f"  [orange][{i + 1}][/orange] [white]{cmp}[/white]")
    pick = ask(f"1-{len(COMPARATORS)} comparator").lower()
    cmp = COMPARATORS[int(pick) - 1] if (pick.isdigit() and 1 <= int(pick) <= len(COMPARATORS)) else ">="
    value = ask("compare value")
    if value == "":
        value = "0"
    return {"kind": "condition", "check": check, "cmp": cmp, "value": value,
            "then": [], "else": []}


def _build_list(ctx, index, title, depth=0):
    """Edit one list of steps. Returns the list, or None if cancelled.

    The chain has NO maximum length: keep adding ops until you press D.
    """
    steps = []
    while True:
        console.print()
        screen_title(title, f"depth {depth}/{MAX_DEPTH} | {len(steps)} steps chained")
        body = _render_steps(ctx, index, steps) or ["[faint](chain is empty)[/faint]"]
        console.print(Panel("\n".join(body), border_style="deep", box=box.ROUNDED,
                            padding=(0, 1)))
        console.print(
            "[dim][A] add op  [W] wait  [C] condition  [G] gate  [L] loop  [N] note  "
            "[D] done  [Q] cancel[/dim]"
        )
        console.print("[dim][X n] delete step   [M n] move step up   | no limit on chain length[/dim]")
        raw = ask("Add").lower().strip()

        if raw in ("q", "quit"):
            return None
        if raw in ("d", "done", ""):
            return steps

        if raw == "a":
            slug = _pick_op_chained(ctx, index)
            if slug:
                steps.append({"kind": "op", "op": slug})
                console.print(f"  [good]+[/good] {index[slug]['name']} [dim]| step {len(steps)}[/dim]")
                missing = missing_perms(slug, ctx)
                if missing:
                    notice("PERMISSION NOTE",
                           [f"[white]{index[slug]['name']}[/white] needs "
                            f"[white]{', '.join(missing)}[/white].",
                            "it will be skipped at run time unless the bot "
                            "gains that permission."], "warn")
            continue

        if raw == "w":
            seconds = ask_int("seconds to wait", 5)
            seconds = max(1, min(3600, seconds))
            step = {"kind": "wait", "seconds": seconds}
            if confirm("wait for a condition instead of a fixed time?", False):
                cond = _build_condition(ctx)
                if cond:
                    step = {
                        "kind": "wait",
                        "seconds": ask_int("give up after (seconds)", 120),
                        "poll": max(1, ask_int("check every (seconds)", 3)),
                        "until": {
                            "check": cond["check"], "cmp": cond["cmp"],
                            "value": cond["value"],
                        },
                    }
            steps.append(step)
            continue

        if raw == "c":
            if depth >= MAX_DEPTH:
                notice("TOO DEEP", [f"nesting is capped at {MAX_DEPTH} levels."], "warn")
                continue
            cond = _build_condition(ctx)
            if not cond:
                continue
            cond["then"] = _build_list(ctx, index, "THEN BRANCH", depth + 1) or []
            if confirm("add an else branch?", False):
                cond["else"] = _build_list(ctx, index, "ELSE BRANCH", depth + 1) or []
            steps.append(cond)
            continue

        if raw == "g":
            prompt = ask("gate prompt", "continue the workflow?")
            if prompt:
                steps.append({"kind": "confirm", "prompt": prompt})
            continue

        if raw == "l":
            if depth >= MAX_DEPTH:
                notice("TOO DEEP", [f"nesting is capped at {MAX_DEPTH} levels."], "warn")
                continue
            times = ask_int("how many passes", 2)
            times = max(1, min(MAX_LOOP_TIMES, times))
            body = _build_list(ctx, index, "LOOP BODY", depth + 1)
            if body:
                steps.append({"kind": "loop", "times": times, "steps": body})
            continue

        if raw == "n":
            text = ask("note text")
            if text:
                steps.append({"kind": "note", "text": text})
            continue

        m = re.match(r"^\s*x\s*(\d+)\s*$", raw)
        if m:
            n = int(m.group(1))
            if 1 <= n <= len(steps):
                removed = steps.pop(n - 1)
                console.print(f"  [warn]-[/warn] {describe_step(removed, index)}")
            else:
                notice("OUT OF RANGE", [f"expected 1-{len(steps)}."], "warn")
            continue

        m = re.match(r"^\s*m\s*(\d+)\s*$", raw)
        if m:
            n = int(m.group(1))
            if 2 <= n <= len(steps):
                steps.insert(n - 2, steps.pop(n - 1))
            elif n == 1:
                notice("NOPE", ["step 1 is already first."], "warn")
            else:
                notice("OUT OF RANGE", [f"expected 1-{len(steps)}."], "warn")
            continue

        notice("UNKNOWN", ["A W C G L N D Q | X n | M n."], "warn")


def create_workflow(ctx, index):
    """V1 two-screen flow: name, then Available actions until Done."""
    if index is None or not index:
        notice("NO OPS", ["the op registry is empty."], "bad")
        return
    if not (ctx.perms & PERMISSION_BITS["ADMINISTRATOR"]):
        notice("NOT ADMINISTRATOR",
               ["this bot does not hold administrator.",
                "ops needing permissions you lack will be flagged in the preview",
                "and skipped at run time."], "warn")

    name = ask("Workflow name")
    if not name:
        return
    if any(w.get("name") == name for w in load_workflows()):
        if not confirm(f"[white]{name}[/white] already exists | overwrite?"):
            return

    steps = _build_list(ctx, index, f"WORKFLOW | {name}")
    if steps is None:
        notice("CANCELLED", ["nothing saved."], "warn")
        return
    if not steps:
        notice("EMPTY", ["a workflow needs at least one step."], "warn")
        return

    wf = {
        "name": name,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "stop_on_error": confirm("stop the run when a step fails?", True),
        "triggers": [{"kind": "manual"}],
        "steps": steps,
    }
    # Offer triggers immediately; that is where most people want them anyway.
    if confirm("set triggers now? (default: manual)", False):
        _edit_triggers(ctx, wf)
    path = save_workflow(wf)
    ctx.logger.log("WORKFLOW_SAVED",
                   f"{name} | {len(steps)} steps | triggers {trigger_summary(wf)}")
    notice("SAVED", [f"[white]{name}[/white] | {len(steps)} steps",
                     f"triggers: [white]{trigger_summary(wf)}[/white]",
                     f"[dim]{path}[/dim]"], "good")


# ------------------------------------------------------------
# PREVIEW
# ------------------------------------------------------------

def _preview_lines(ctx, index, steps, depth=0):
    lines = []
    for i, s in enumerate(steps):
        pad = "    " * depth
        label = describe_step(s, index)
        slug = s.get("op") if step_kind(s) == "op" else None
        tag = ""
        if slug:
            missing = missing_perms(slug, ctx)
            limited = op_limitations(slug, ctx)
            if missing:
                tag = f" [bad]needs {'/'.join(missing)}[/bad]"
            elif limited:
                tag = f" [warn]limited | {limited}[/warn]"
        lines.append(f"[orange]{i + 1:02d}[/orange]{pad} [white]{label}[/white]{tag}")
        if step_kind(s) == "condition":
            if s.get("then"):
                lines.append(f"{pad}   [dim]then[/dim]")
                lines += _preview_lines(ctx, index, s["then"], depth + 1)
            if s.get("else"):
                lines.append(f"{pad}   [dim]else[/dim]")
                lines += _preview_lines(ctx, index, s["else"], depth + 1)
        elif step_kind(s) == "loop":
            if s.get("steps"):
                lines.append(f"{pad}   [dim]x{int(s.get('times', 1))}[/dim]")
                lines += _preview_lines(ctx, index, s["steps"], depth + 1)
    return lines


def show_preview(ctx, index, wf):
    """Confirmation panel. Returns True when the user wants to run it."""
    console.print()
    plan = flatten_steps(wf.get("steps", []))
    body = _preview_lines(ctx, index, wf.get("steps", []))
    if not body:
        body = ["[faint](no steps)[/faint]"]
    meta = [
        f"steps: [white]{len(plan)}[/white]"
        f"   stop on error: [white]{'yes' if wf.get('stop_on_error', True) else 'no'}[/white]"
        f"   triggers: [white]{trigger_summary(wf)}[/white]"
        f"   target: [white]{(ctx.guild or {}).get('name', '?')}[/white]",
    ]
    console.print(
        Panel(
            "\n".join(body) + "\n\n" + "\n".join(f"[dim]{m}[/dim]" for m in meta),
            title=f"[orange]WORKFLOW PREVIEW | {wf.get('name', '?')}[/orange]",
            border_style="orange",
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )

    problems, limits = [], []
    for sid, _, s in plan:
        if step_kind(s) != "op":
            continue
        slug = s.get("op", "")
        entry = index.get(slug)
        name = entry["name"] if entry else slug
        missing = missing_perms(slug, ctx)
        if missing:
            problems.append(f"[white]{name}[/white] | needs {', '.join(missing)} | skipped")
        elif op_limitations(slug, ctx):
            limits.append(f"[white]{name}[/white] | {op_limitations(slug, ctx)}")

    if problems:
        notice("WILL BE SKIPPED",
               problems + ["", "the bot lacks these permissions on this guild.",
                           "grant them, or remove the steps."], "bad")
    if limits:
        notice("MAY BE LIMITED",
               limits + ["", "these depend on intents or the target's settings."], "warn")

    if not problems and not limits and \
            not (ctx.perms & PERMISSION_BITS["ADMINISTRATOR"]):
        notice("NOT ADMINISTRATOR",
               ["no step is blocked, but the bot holds no administrator.",
                "single-target ops can still fail if the target outranks it."], "warn")

    return confirm("run this workflow now?")


# ------------------------------------------------------------
# EXECUTION
# ------------------------------------------------------------

def _sleep_wait(board, seconds):
    end = time.monotonic() + max(0, float(seconds))
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        board.status(f"waiting | {int(remaining) + 1}s left")
        time.sleep(min(1.0, remaining))
    board.status("")


def _conditional_wait(ctx, board, step, cache):
    cond = step.get("until") or {}
    timeout = max(1, int(step.get("seconds", 120)))
    poll = max(1, int(step.get("poll", 3)))
    end = time.monotonic() + timeout
    while True:
        ok, why = _eval_condition(ctx, cond, cache)
        if ok:
            board.status("")
            return True, f"met | {why}"
        remaining = end - time.monotonic()
        if remaining <= 0:
            board.status("")
            return False, f"timed out after {timeout}s | {why}"
        board.status(
            f"waiting for {cond.get('check')} {cond.get('cmp')} {cond.get('value')} "
            f"| {int(remaining) + 1}s left"
        )
        time.sleep(min(float(poll), remaining))


def _run_steps(ctx, index, steps, path, board, by_id, taken, cache, state):
    stop_on_error = state.get("stop_on_error", True)

    for i, step in enumerate(steps):
        sid = path + (i,)
        bi = by_id.get(sid)
        kind = step_kind(step)

        if kind == "condition":
            if bi is not None:
                board.begin(bi)
            ok, why = _eval_condition(ctx, step, cache)
            branch = "t" if ok else "e"
            taken.add(sid + (branch,))
            if bi is not None:
                board.done(bi, "yes" if ok else "no")
            _mark_branch(board, state["plan"], sid + ("e" if ok else "t"))
            arm = (step.get("then") if ok else step.get("else")) or []
            _run_steps(ctx, index, arm, sid + (branch,),
                       board, by_id, taken, cache, state)
            continue

        if kind == "loop":
            times = max(1, min(MAX_LOOP_TIMES, int(step.get("times", 1))))
            children = step.get("steps") or []
            taken.add(sid + ("l",))
            for n in range(times):
                if bi is not None:
                    board.begin(bi, f"pass {n + 1}/{times}")
                for j in range(len(children)):
                    cbi = by_id.get(sid + ("l", j))
                    if cbi is not None:
                        board.reset(cbi)
                if _run_steps(ctx, index, children, sid + ("l",),
                              board, by_id, taken, cache, state):
                    return True
                if bi is not None:
                    board.done(bi, f"pass {n + 1}/{times}")
            if bi is not None:
                board.done(bi, f"x{times}")
            continue

        if not _is_runnable(sid, taken):
            if bi is not None:
                board.skip(bi, "branch not taken")
            continue

        if kind == "op":
            slug = step.get("op", "")
            entry = index.get(slug)
            if bi is not None:
                board.begin(bi)
            if entry is None:
                if bi is not None:
                    board.fail(bi, "unknown op")
                if stop_on_error:
                    return True
                continue
            missing = missing_perms(slug, ctx)
            if missing:
                if bi is not None:
                    board.skip(bi, "no " + ",".join(missing))
                ctx.logger.log("WF_SKIP", f"{entry['name']} | missing {','.join(missing)}")
                continue
            board.suspend()
            try:
                entry["fn"](ctx)
                if bi is not None:
                    board.done(bi)
                ctx.logger.log("WF_STEP", entry["name"])
            except ApiNetworkError as e:
                if bi is not None:
                    board.fail(bi, str(e)[:60])
                ctx.logger.log("WF_ERROR", f"{entry['name']} | {e}")
                if stop_on_error:
                    return True
            except KeyboardInterrupt:
                if bi is not None:
                    board.fail(bi, "interrupted")
                ctx.logger.log("WF_ABORT", entry["name"])
                return True
            finally:
                board.resume()
            continue

        if kind == "wait":
            if bi is not None:
                board.begin(bi)
            if step.get("until"):
                ok, why = _conditional_wait(ctx, board, step, cache)
                if bi is not None:
                    (board.done if ok else board.fail)(bi, why)
                if not ok and stop_on_error:
                    return True
            else:
                _sleep_wait(board, step.get("seconds", 0))
                if bi is not None:
                    board.done(bi)
            continue

        if kind == "confirm":
            if bi is not None:
                board.begin(bi)
            board.suspend()
            try:
                ok = confirm(step.get("prompt") or "continue the workflow?")
            finally:
                board.resume()
            if ok:
                if bi is not None:
                    board.done(bi, "approved")
            else:
                if bi is not None:
                    board.skip(bi, "declined")
                ctx.logger.log("WF_GATE", "declined")
                return True
            continue

        if kind == "note":
            if bi is not None:
                board.begin(bi)
                board.done(bi, step.get("text", ""))
            continue

        if bi is not None:
            board.skip(bi, "unknown kind")

    return False


def execute_workflow(ctx, index, wf):
    """Run a workflow against the selected guild."""
    plan = flatten_steps(wf.get("steps", []))
    if not plan:
        notice("EMPTY", ["this workflow has no steps."], "warn")
        return

    by_id = {sid: i for i, (sid, _, _) in enumerate(plan)}
    # (depth, title) pairs — the board owns indentation; pre-indenting here
    # fed strings into an unpack that killed every run in a live terminal.
    titles = [(depth, describe_step(s, index)) for _, depth, s in plan]
    taken = set()
    cache = {}
    state = {
        "plan": plan,
        "stop_on_error": bool(wf.get("stop_on_error", True)),
    }

    ctx.logger.log("WF_START", f"{wf.get('name')} | {len(plan)} steps")
    board = TodoBoard(
        titles,
        title=f"WORKFLOW | {wf.get('name', '?')}",
        subtitle=(ctx.guild or {}).get("name", "?"),
    )
    board.start()
    try:
        aborted = _run_steps(ctx, index, wf.get("steps", []), (),
                             board, by_id, taken, cache, state)
    except KeyboardInterrupt:
        aborted = True
    except Exception as e:
        # A workflow runs a user-authored chain against a live API, so a bug in
        # the engine must not take the whole session down mid-run. Contain it,
        # log it, and tell the user which run died.
        aborted = True
        ctx.logger.log("WF_CRASH", f"{type(e).__name__}: {e}")
        notice("WORKFLOW ERROR",
               [f"[white]{type(e).__name__}[/white]: {str(e)[:200]}",
                "the run stopped. the report log has the detail."], "bad")
    finally:
        board.stop()

    counts = {DONE: 0, SKIPPED: 0, FAILED: 0, PENDING: 0}
    for i in range(len(plan)):
        st = board.state(i)
        if st == RUNNING:
            # Interrupt or engine fault landed mid-step: the run is over, so
            # the step never finished and counts as never reached. Without
            # this remap the tally itself raised KeyError and Ctrl+C during
            # any workflow crashed the whole session page.
            st = PENDING
        counts[st] = counts.get(st, 0) + 1

    ctx.logger.log(
        "WF_DONE",
        f"{wf.get('name')} | done={counts['done']} skipped={counts['skipped']} "
        f"failed={counts['failed']} aborted={bool(aborted)}",
    )
    notice(
        "WORKFLOW ABORTED" if aborted else "WORKFLOW COMPLETE",
        [
            f"steps run:   [white]{counts['done']}[/white]",
            f"skipped:     [white]{counts['skipped']}[/white]",
            f"failed:      [white]{counts['failed']}[/white]",
            f"never reached: [white]{counts['pending']}[/white]",
        ],
        "warn" if (aborted or counts["failed"]) else "good",
    )
    press_enter()


# ------------------------------------------------------------
# TRIGGERS
# ------------------------------------------------------------

def _edit_triggers(ctx, wf):
    """Toggle-based trigger editor. `wf` is mutated in place."""
    triggers = normalize_triggers(wf)
    while True:
        console.print()
        screen_title("SET TRIGGERS", f"{wf.get('name', '?')} | D done")
        kinds = [k for k, _ in TRIGGER_KINDS]
        for i, (kind, label) in enumerate(TRIGGER_KINDS):
            active = next((t for t in triggers if t.get("kind") == kind), None)
            if kind == "interval" and active:
                label += f" [white](every {int(active.get('seconds', 300))}s)[/white]"
            mark = "[orange][x][/orange]" if active else "[faint][ ][/faint]"
            console.print(f"  {mark} [orange][{i + 1}][/orange] [white]{label}[/white]")
        raw = ask("toggle | D done").lower().strip()
        if raw in ("d", ""):
            wf["triggers"] = triggers or [{"kind": "manual"}]
            return wf
        if raw.isdigit() and 1 <= int(raw) <= len(kinds):
            kind = kinds[int(raw) - 1]
            existing = next((t for t in triggers if t.get("kind") == kind), None)
            if existing:
                triggers.remove(existing)
                # Removing manual while others stay is fine; removing the LAST
                # trigger leaves manual, because a workflow must be reachable.
                if not triggers:
                    triggers.append({"kind": "manual"})
                continue
            if kind == "manual":
                # Manual is mutually exclusive with everything automatic.
                triggers = [{"kind": "manual"}]
                continue
            triggers = [t for t in triggers if t.get("kind") != "manual"]
            entry = {"kind": kind}
            if kind == "interval":
                secs = ask_int("fire every how many seconds (min 30)", 300)
                entry["seconds"] = max(30, secs)
            triggers.append(entry)
            continue
        notice("OUT OF RANGE", [f"expected 1-{len(kinds)} | D done."], "warn")


def set_triggers_ui(ctx, *, index=None):
    screen_title("SET TRIGGERS", "choose a saved workflow")
    wf = _pick_workflow(ctx, "edit triggers")
    if wf is None:
        return
    _edit_triggers(ctx, wf)
    path = save_workflow(wf)
    ctx.logger.log("WORKFLOW_TRIGGERS", f"{wf.get('name')} | {trigger_summary(wf)}")
    notice("SAVED", [f"[white]{wf.get('name')}[/white]",
                     f"triggers: [white]{trigger_summary(wf)}[/white]",
                     f"[dim]{path}[/dim]"], "good")


def collect_due_workflows(ctx):
    """Which workflows want to fire right now, and why.

    Returns a list of (workflow, reason). Called from the page loop between
    screens so triggers never interrupt an op mid-run.
    """
    due = []
    now = time.monotonic()
    state = ctx.trigger_state
    for wf in load_workflows():
        name = wf.get("name", "?")
        for t in normalize_triggers(wf):
            kind = t.get("kind")
            if kind == "manual":
                continue
            if kind == "on_guild_select" and ctx.just_selected_guild:
                due.append((wf, "guild selected"))
                break
            if kind == "on_ban_detected" and ctx.guild_lost:
                if state.get(f"ban_fired::{name}"):
                    continue
                state[f"ban_fired::{name}"] = True
                due.append((wf, "bot kicked/banned"))
                break
            if kind == "interval":
                last = state.get(f"interval::{name}", 0.0)
                every = max(30, int(t.get("seconds", 300)))
                if now - last >= every:
                    state[f"interval::{name}"] = now
                    due.append((wf, f"interval {every}s"))
                    break
    ctx.just_selected_guild = False
    return due


def run_due_workflows(ctx, index):
    """Offer each due workflow to the user. Returns True if anything ran."""
    due = collect_due_workflows(ctx)
    if not due:
        return False
    ran = False
    for wf, why in due:
        console.print()
        notice("TRIGGER FIRED",
               [f"[white]{wf.get('name')}[/white] wants to run",
                f"reason: [white]{why}[/white]",
                f"steps: [white]{len(flatten_steps(wf.get('steps', [])))}[/white]"],
               "warn")
        if confirm("execute it now?", True):
            execute_workflow(ctx, index, wf)
            ran = True
        else:
            ctx.logger.log("WF_TRIGGER_DECLINED", wf.get("name", "?"))
    return ran


# ------------------------------------------------------------
# ENGINE SCREEN (V1-style)
# ------------------------------------------------------------

def _render_engine(ctx, flows):
    print_header(ctx)
    console.print()
    console.print(grad("  Workflow Engine", ctx))
    console.print()
    console.print("    [orange][1][/orange] [white]Create Workflow[/white]")
    console.print("    [orange][2][/orange] [white]Execute Workflow[/white]")
    console.print("    [orange][3][/orange] [white]Delete Workflow[/white]")
    console.print("    [orange][4][/orange] [white]Set Triggers[/white]")
    console.print()
    console.print("  [white]Saved workflows:[/white]")
    if flows:
        for wf in flows:
            steps = len(flatten_steps(wf.get("steps", [])))
            console.print(
                f"    [white]{wf.get('name', '?')}[/white] [dim]— {steps} steps, "
                f"triggers: {trigger_summary(wf)}[/dim]"
            )
    else:
        console.print("    [faint](none yet | create one with [1])[/faint]")
    console.print()
    console.print("    [orange][0][/orange] [dim]Back[/dim]")
    console.print()


def workflow_engine(ctx, *, index):
    """The V1-style engine menu that owns every workflow action."""
    while True:
        flows = load_workflows()

        def render():
            _render_engine(ctx, flows)

        present_frame(ctx, render)
        choice = ask("Select").lower().strip()
        console.print()

        if choice in ("0", "b", "q", ""):
            return
        if choice == "1":
            create_workflow(ctx, index)
            press_enter()
            continue
        if choice == "2":
            wf = _pick_workflow(ctx)
            if wf is not None and show_preview(ctx, index, wf):
                execute_workflow(ctx, index, wf)
            continue
        if choice == "3":
            delete_workflow_ui(ctx)
            press_enter()
            continue
        if choice == "4":
            set_triggers_ui(ctx, index=index)
            press_enter()
            continue
        notice("OUT OF RANGE", ["0-4."], "warn")
        press_enter()


def delete_workflow_ui(ctx, *, index=None):
    screen_title("DELETE WORKFLOW", "removes the saved chain file")
    flows = load_workflows()
    if not flows:
        notice("NO WORKFLOWS", ["nothing to delete."], "warn")
        return
    for i, wf in enumerate(flows):
        console.print(f"  [orange][{i + 1:02d}][/orange] [white]{wf.get('name')}[/white] "
                      f"[dim]| {len(flatten_steps(wf.get('steps', [])))} steps | "
                      f"{trigger_summary(wf)}[/dim]")
    pick = ask(f"1-{len(flows)} delete | B back").lower()
    if pick == "b":
        return
    if not (pick.isdigit() and 1 <= int(pick) <= len(flows)):
        notice("OUT OF RANGE", [f"expected 1-{len(flows)}."], "warn")
        return
    victim = flows[int(pick) - 1]
    if confirm(f"delete [white]{victim.get('name')}[/white]?"):
        if delete_workflow_file(victim.get("name", "")):
            ctx.logger.log("WORKFLOW_DELETED", victim.get("name", "?"))
            notice("DELETED", [f"{victim.get('name')} removed."], "good")
        else:
            notice("FAILED", ["file could not be removed."], "bad")


def page_ops():
    """The WORKFLOWS page exposes exactly one entry: the engine itself."""
    return [
        ("Workflow Engine", "chained ops, waits, conditions, triggers", workflow_engine),
    ]
