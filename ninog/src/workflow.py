# ==============================================================
#   NiNog Raker v2.0 | THE RATTIKANS
#   src/workflow.py | workflow engine — chained ops, pre-set inputs
# ==============================================================
#
#   A workflow is dead simple by design:
#
#     name      what it is called
#     trigger   ONE of: manual / on server select / on ban detected /
#               every N seconds
#     steps     an ordered, unlimited chain of ops. Each op carries the
#               answers it will need, recorded ONCE at build time, so a
#               run never pauses to ask for them mid-fire.
#
#   That is the whole model. No condition editor, no loop builder, no
#   branch tree in the UI — the engine still EXECUTES legacy files
#   with those kinds (wait/condition/loop/note/confirm), but the
#   builder only produces clean op chains with pre-set inputs.
#
#   Editing a workflow means exactly two things: re-record a step's
#   answers, or delete the workflow. Nothing else exists to break.
#
#   This module imports src.core and src.ui only. It never imports
#   src.ops -- ops.py imports this module and hands the op registry
#   in as a parameter, which keeps the one dangerous edge from
#   being circular.
# ==============================================================

import json
import re
import time
from collections import deque

from .core import (
    PERMISSION_BITS,
    WHITELIST_FILE,
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
    notice,
    press_enter,
    sanitize_name,
    screen_title,
    set_input_feed,
    start_prompt_recorder,
    stop_prompt_recorder,
)

MAX_LOOP_TIMES = 50          # per-loop pass cap in legacy files; the chain has no cap
EXCLUDED_SLUGS = {"workflow_engine"}   # the manager can never chain itself

# ------------------------------------------------------------
# PERMISSION MODEL + INDEX
# ------------------------------------------------------------

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


# ------------------------------------------------------------
# DESCRIBE
# ------------------------------------------------------------

def describe_step(step, index=None):
    k = step.get("kind", "?")
    if k == "op":
        slug = step.get("op", "?")
        entry = (index or {}).get(slug)
        name = entry["name"] if entry else slug
        n = len(step.get("answers") or [])
        return name if not n else f"{name} [{n} pre-set]"
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


# ------------------------------------------------------------
# WALKTHROUGH RECORDER — configure the op as if it were firing
# ------------------------------------------------------------
# V1's model, done properly: when a step is added, the tool runs the op's
# real flow against the real server — same questions, same pickers, real
# member/channel lists — while two guards make it safe and capture the
# configuration:
#
#   _RehearsalREST    every read (get_*) goes to Discord like normal,
#                     every write (create/delete/patch/ban/kick/send/
#                     webhook...) returns a canned success and never
#                     leaves the machine.
#   prompt recorder   ui.ask/confirm capture (question, raw answer)
#
# What survives a walkthrough is exactly the answer list, replayed at
# fire time. Nothing else. No static question maps.

class _CannedR:
    """requests.Response stand-in for swallowed writes."""

    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class _SilentLogger:
    def log(self, *a, **k):
        pass


class _RehearsalREST:
    """Read-through, write-swallow wrapper around the live REST client."""

    _READ_EXTRA = {"download"}

    def __init__(self, real):
        self._real = real
        self.limiter = getattr(real, "limiter", None)

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr):
            return attr
        if name.startswith("get_") or name in self._READ_EXTRA:
            return attr
        if name == "request":  # generic escape hatch: GET passes, rest canned
            def _guarded(method, path, **kwargs):
                if str(method).upper() == "GET":
                    return attr(method, path, **kwargs)
                return _CannedR(204)
            return _guarded
        return lambda *a, **k: self._canned(name, a, k)

    @staticmethod
    def _canned(name, args, kwargs):
        rid = f"999{int(time.time() * 1000) % 10**12:012d}"
        body = {}
        for a in args:
            if isinstance(a, dict):
                body = a
        for v in kwargs.values():
            if isinstance(v, dict):
                body = v
        if name in ("create_role", "create_channel"):
            return _CannedR(200, dict(body, id=rid))
        if name == "create_webhook":
            return _CannedR(200, {"id": rid, "token": "rehearsal-token"})
        if name == "create_dm":
            return _CannedR(200, {"id": rid})
        if name == "create_invite":
            return _CannedR(200, {"code": "rehearsal"})
        if name == "send_message":
            return _CannedR(200, {"id": rid})
        return _CannedR(204)


def _walkthrough(ctx, slug, entry):
    """Run the op like it's firing; keep only the answers. Returns
    (answers, labels) or None when the walkthrough was declined/aborted."""
    name = entry["name"]
    notice("WALKTHROUGH | NOTHING IS SENT",
           [f"configure [white]{name}[/white] exactly as if it were firing",
            "right now: same questions, same pickers, real server data.",
            "",
            "every ban, kick, delete, create, rename, message and webhook",
            "call is swallowed by the engine — only your answers are kept."],
           "warn")
    if not confirm(f"walk through [white]{name}[/white] now?", True):
        return None

    real_rest = ctx.rest
    real_logger = ctx.logger
    wl_backup = WHITELIST_FILE.read_bytes() if WHITELIST_FILE.exists() else None
    snap_flag = ctx.config.data.get("auto_snapshot", True)
    ctx.config.data["auto_snapshot"] = False
    ctx.rest = _RehearsalREST(real_rest)
    ctx.logger = _SilentLogger()
    start_prompt_recorder()
    aborted = None
    try:
        entry["fn"](ctx)
    except ApiNetworkError as e:
        aborted = f"network error: {e}"
    except KeyboardInterrupt:
        aborted = "interrupted"
    except Exception as e:
        aborted = f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        transcript = stop_prompt_recorder()
        ctx.rest = real_rest
        ctx.logger = real_logger
        ctx.config.data["auto_snapshot"] = snap_flag
        # Undo any local-state churn (whitelist adds/removals during the
        # walkthrough) the mutation guard could not see.
        if wl_backup is None:
            if WHITELIST_FILE.exists():
                WHITELIST_FILE.unlink()
        else:
            WHITELIST_FILE.write_bytes(wl_backup)

    if aborted:
        notice("WALKTHROUGH ABORTED",
               [aborted, "no answers were kept for this step."], "warn")
        ctx.logger.log("WF_WALKTHROUGH_ABORT", f"{slug} | {aborted}")
        return None

    picked = [q for q in transcript if q[0] in ("ask", "confirm", "ask_int")]
    answers = [a for _, _, a in picked]
    labels = [q for _, q, _ in picked]

    console.print()
    screen_title(f"INPUTS CAPTURED | {name}", "what the fire will answer")
    if not answers:
        console.print("  [dim]the op asked nothing — it fires with no inputs.[/dim]")
    for i, (lab, ans) in enumerate(zip(labels, answers), 1):
        short = lab if len(lab) <= 62 else lab[:59] + "..."
        console.print(f"    [dim]{i:02d}.[/dim] {short}")
        console.print(f"        [orange]=[/orange] [white]{ans!r}[/white]")
    if answers and not confirm(f"keep these {len(answers)} answers for this step?", True):
        return None
    return answers, labels


def _pick_trigger():
    """One trigger, picked at creation. No separate editor exists."""
    console.print()
    screen_title("TRIGGER", "when this workflow fires")
    console.print("  [orange][1][/orange] [white]Manual only[/white] [dim]| run it yourself[/dim]")
    console.print("  [orange][2][/orange] [white]On server select[/white] [dim]| right after picking a server[/dim]")
    console.print("  [orange][3][/orange] [white]On ban detected[/white] [dim]| when the bot is kicked/banned[/dim]")
    console.print("  [orange][4][/orange] [white]Every N seconds[/white] [dim]| while menus are up[/dim]")
    raw = ask("1-4 | B back").lower().strip()
    if raw in ("b", "", "q"):
        return None
    if raw == "1":
        return {"kind": "manual"}
    if raw == "2":
        return {"kind": "on_guild_select"}
    if raw == "3":
        return {"kind": "on_ban_detected"}
    if raw == "4":
        secs = ask_int("seconds between fires", 300)
        return {"kind": "interval", "seconds": max(30, int(secs or 300))}
    notice("OUT OF RANGE", ["1-4 | B back."], "warn")
    return None


def _pick_op(ctx, index):
    """Category first (one screen), then the op. No 44-line scrolls."""
    # preserve the order pages were registered in
    categories = []
    for slug, e in index.items():
        page = e.get("page", "") or "OPS"
        if page not in [c[0] for c in categories]:
            categories.append((page, []))
        for c in categories:
            if c[0] == page:
                c[1].append((slug, e))
                break
    console.print()
    screen_title("ADD OP", "pick a category")
    for i, (cat, ops_list) in enumerate(categories, 1):
        console.print(f"  [orange][{i}][/orange] [white]{cat}[/white] "
                      f"[dim]{len(ops_list)} ops[/dim]")
    raw = ask(f"1-{len(categories)} | B back").lower().strip()
    if raw in ("b", "", "q"):
        return None
    if not (raw.isdigit() and 1 <= int(raw) <= len(categories)):
        notice("OUT OF RANGE", [f"1-{len(categories)} | B back."], "warn")
        return None
    cat, ops_list = categories[int(raw) - 1]
    console.print()
    screen_title(f"ADD OP | {cat}", "one number away")
    for i, (slug, e) in enumerate(ops_list, 1):
        console.print(f"  [orange][{i:02d}][/orange] [white]{e['name']}[/white] "
                      f"[dim]{e.get('desc', '')}[/dim]")
    raw = ask(f"1-{len(ops_list)} | B back").lower().strip()
    if raw in ("b", "", "q"):
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(ops_list):
        return ops_list[int(raw) - 1]
    notice("OUT OF RANGE", [f"1-{len(ops_list)} | B back."], "warn")
    return None


def create_workflow(ctx, index):
    """name -> trigger -> chain ops with pre-set inputs -> save. Done."""
    screen_title("NEW WORKFLOW", "simple on purpose")
    name = ask("workflow name")
    if not (name or "").strip():
        notice("CANCELLED", ["no name, no workflow."], "warn")
        return None
    trigger = _pick_trigger()
    if trigger is None:
        return None

    steps = []
    while True:
        console.print()
        if steps:
            console.print(f"  [dim]chain so far ({len(steps)}):[/dim]")
            for i, s in enumerate(steps, 1):
                console.print(f"    [dim]{i:02d}.[/dim] {describe_step(s, index)}")
        picked = _pick_op(ctx, index)
        if picked is None:
            break
        slug, entry = picked
        # The contract (V1's): the op is configured NOW, by walking its
        # real flow with writes swallowed. Fire time only replays answers.
        got = _walkthrough(ctx, slug, entry)
        if got is None:
            continue
        answers, labels = got
        step = {"kind": "op", "op": slug, "answers": answers}
        if labels:
            step["labels"] = labels
        steps.append(step)

    if not steps:
        notice("EMPTY", ["a workflow with no steps was not saved."], "warn")
        return None
    flow = {"name": name.strip(), "steps": steps, "triggers": [trigger]}
    path = save_workflow(flow)
    ctx.logger.log("WORKFLOW_SAVED",
                   f"{flow['name']} | {len(steps)} steps | {trigger_summary(flow)}")
    notice("WORKFLOW SAVED",
           [f"[white]{flow['name']}[/white] | {len(steps)} op(s) chained.",
            f"trigger: [white]{trigger_summary(flow)}[/white]",
            f"[dim]{path}[/dim]"], "good")
    return flow


# ------------------------------------------------------------
# THE ONLY EDITS THAT EXIST: re-record one step's inputs, or delete
# ------------------------------------------------------------

def _rerecord_inputs(ctx, index, flows):
    if not flows:
        notice("NONE", ["no workflows saved yet."], "warn")
        return False
    for i, f in enumerate(flows, 1):
        console.print(f"  [orange][{i}][/orange] [white]{f.get('name')}[/white] "
                      f"[dim]{len(f.get('steps', []))} steps | {trigger_summary(f)}[/dim]")
    raw = ask(f"which workflow 1-{len(flows)} | B back").lower().strip()
    if raw in ("b", "", "q") or not raw.isdigit() or not 1 <= int(raw) <= len(flows):
        return False
    flow = flows[int(raw) - 1]
    steps = flow.get("steps", [])
    if not steps:
        notice("EMPTY", ["that workflow has no steps."], "warn")
        return False
    console.print()
    for i, s in enumerate(steps, 1):
        console.print(f"  [orange][{i}][/orange] {describe_step(s, index)}")
    raw = ask(f"which step 1-{len(steps)} | B back").lower().strip()
    if raw in ("b", "", "q") or not raw.isdigit() or not 1 <= int(raw) <= len(steps):
        return False
    si = int(raw) - 1
    step = steps[si]
    if step.get("kind") != "op":
        notice("NOT AN OP", ["only op steps carry inputs."], "warn")
        return False
    old = step.get("answers") or []
    old_labels = step.get("labels") or []
    if old:
        console.print()
        console.print("  [white]currently stored:[/white]")
        for i, a in enumerate(old):
            lab = old_labels[i] if i < len(old_labels) else f"answer #{i + 1}"
            console.print(f"    [dim]{i + 1}.[/dim] {lab}  [orange]=[/orange] [white]{a!r}[/white]")
    else:
        console.print("  [dim]nothing stored yet — this op asks live when it fires.[/dim]")
    entry = index.get(step.get("op", "")) or {}
    if not callable(entry.get("fn")):
        notice("UNKNOWN OP", ["this step points at an op that no longer exists."], "bad")
        return False
    got = _walkthrough(ctx, step.get("op", ""), entry)
    if got is None:
        notice("UNCHANGED", ["kept the previously stored answers."], "warn")
        return False
    answers, labels = got
    step["answers"] = answers
    if labels:
        step["labels"] = labels
    else:
        step.pop("labels", None)
    save_workflow(flow)
    ctx.logger.log("WORKFLOW_EDITED", f"{flow.get('name')} | step {si + 1} re-recorded")
    notice("UPDATED",
           [f"step {si + 1} of [white]{flow.get('name')}[/white] "
            "now fires with the new answers."], "good")
    return True


def _delete_flow(ctx, flows):
    if not flows:
        notice("NONE", ["no workflows saved yet."], "warn")
        return False
    for i, f in enumerate(flows, 1):
        console.print(f"  [orange][{i}][/orange] [white]{f.get('name')}[/white]")
    raw = ask(f"which workflow 1-{len(flows)} | B back").lower().strip()
    if raw in ("b", "", "q") or not raw.isdigit() or not 1 <= int(raw) <= len(flows):
        return False
    victim = flows[int(raw) - 1]
    if not confirm(f"delete [white]{victim.get('name')}[/white]?"):
        return False
    if delete_workflow_file(victim.get("name", "")):
        ctx.logger.log("WORKFLOW_DELETED", victim.get("name", "?"))
        notice("DELETED", [f"{victim.get('name')} removed."], "good")
        return True
    notice("FAILED", ["file could not be removed."], "bad")
    return False


# ------------------------------------------------------------
# ENGINE SCREEN — the whole workflow UI fits in one loop
# ------------------------------------------------------------

def workflow_engine(ctx, *, index):
    """List, run, new, re-record, delete. Nothing else."""
    while True:
        flows = load_workflows()
        console.print()
        screen_title("WORKFLOW ENGINE", "chained ops with pre-set inputs")
        if not flows:
            console.print("  [dim]no workflows yet. N makes one.[/dim]")
        for i, f in enumerate(flows, 1):
            steps = f.get("steps", [])
            preset = sum(len(s.get("answers") or []) for s in steps)
            console.print(
                f"  [orange][{i}][/orange] [white]{f.get('name')}[/white] "
                f"[dim]{len(steps)} ops | {preset} pre-set inputs | "
                f"{trigger_summary(f)}[/dim]")
            chain = " -> ".join(
                (index.get(s.get("op", "")) or {}).get("name", s.get("op", "?"))
                for s in steps if s.get("kind") == "op")
            if chain:
                console.print(f"      [dim]{chain[:200]}[/dim]")
        console.print()
        console.print("  [dim]# run | N new | E re-record step inputs | D delete | B back[/dim]")
        raw = ask("action").strip()
        low = raw.lower()
        if low in ("b", "", "q"):
            return
        if low == "n":
            create_workflow(ctx, index)
            continue
        if low == "e":
            _rerecord_inputs(ctx, index, flows)
            continue
        if low == "d":
            _delete_flow(ctx, flows)
            continue
        if low.isdigit() and 1 <= int(low) <= len(flows):
            execute_workflow(ctx, index, flows[int(low) - 1])
            press_enter()
            continue
        notice("OUT OF RANGE", ["pick a number, N, E, D, or B."], "warn")


def page_ops():
    """The WORKFLOWS page exposes exactly one entry: the engine itself."""
    return [
        ("Workflow Engine", "chained ops, pre-set inputs, one trigger", workflow_engine),
    ]


# ------------------------------------------------------------
# EXECUTION — runs new chains and legacy files alike
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
            # Replay the answers recorded at build time. When the recording
            # runs dry, prompts fall through to live input automatically.
            set_input_feed(deque(step.get("answers") or []))
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
                set_input_feed(None)
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


# ------------------------------------------------------------
# TRIGGERS
# ------------------------------------------------------------



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
