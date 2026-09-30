#  NiNog Raker v2.0 | THE RATTIKANS
#  tests/integration_test.py — offline regression suite.
#
#  Drives the real ops/workflow/UI code against a scripted terminal and a
#  fake REST end, so every interactive flow is exercised without Discord.
#  Run from anywhere:  python tests/integration_test.py
#  (the script pins its own cwd to the ninog/ package root)

import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import contextlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import src.core as core
import src.ui as ui
import src.ops as ops
import src.workflow as wf

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"PASS {name}")
    else:
        FAIL.append(name)
        print(f"FAIL {name} :: {detail}")


# ------------------------------------------------------------
# scripted terminal
# ------------------------------------------------------------

class Script:
    """Feeds answers to raw input() in call order."""

    def __init__(self, answers):
        self.answers = list(answers)

    def feed(self, prompt=""):
        if not self.answers:
            raise AssertionError("script ran dry — flow asked more than expected")
        return self.answers.pop(0)

    def dry(self):
        return not self.answers


import builtins
_real_input = builtins.input
SCRIPT = Script([])


def patched_input(prompt=""):
    return SCRIPT.feed(prompt)


builtins.input = patched_input


class FakeStatus:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False

    def update(self, *a, **k):
        pass

    def stop(self):
        pass


class FakeConsole:
    """Minimal console stand-in; everything ui/ops/workflow print lands here."""

    def __init__(self, width=100):
        self.width = width
        self.buffer = io.StringIO()

    def print(self, *a, **k):
        print(*[str(x) for x in a], file=self.buffer, **{kk: vv for kk, vv in k.items() if kk == "sep"})

    def status(self, *a, **k):
        return FakeStatus()

    def capture(self):
        console = self

        class _Cap:
            def __enter__(self):
                console._pre = console.buffer.tell()
                return self

            def __exit__(self, *e):
                return False

            def get(self):
                pos = console.buffer.tell()
                console.buffer.seek(getattr(console, "_pre", 0))
                chunk = console.buffer.read()
                console.buffer.seek(pos)
                return chunk

        return _Cap()

    def bell(self):
        pass

    @property
    def text(self):
        return self.buffer.getvalue()


console = FakeConsole()
for mod in (ui, ops, wf):
    mod.console = console


def make_ctx():
    core.ensure_directories()
    cfg = core.Config()
    cfg.set_setting("transition", "off")
    cfg.set_setting("animations", False)
    cfg.set_setting("gradient", False)
    cfg.set_setting("auto_snapshot", False)
    logger = core.ReportLogger()
    ctx = core.Ctx(cfg, logger)
    ctx.me = {"id": "900000000000000000", "username": "rakerbot"}
    ctx.rest = FakeREST(ctx)
    return ctx


# ------------------------------------------------------------
# fake REST end
# ------------------------------------------------------------

class R:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeREST:
    """Route-table REST double. Records every mutation for assertions."""

    def __init__(self, ctx=None):
        self.ctx = ctx
        self.calls = []
        self.members = [
            {"user": {"id": "100000000000000001", "username": "alice"}, "roles": []},
            {"user": {"id": "100000000000000002", "username": "bob"}, "roles": []},
            {"user": {"id": "100000000000000003", "username": "carol"}, "roles": []},
            {"user": {"id": "100000000000000004", "username": "helper", "bot": True}, "roles": []},
        ]
        self.roles = [{"id": "500000000000000001", "name": "@everyone"}]
        self._role_seq = 500000000000000010
        self.webhook_sends = []
        self.webhooks = {
            "300000000000000001": [
                {"id": "700000000000000001", "token": "hooktok_alice", "channel_id": "300000000000000001"},
                {"id": "700000000000000002", "token": "hooktok_beta", "channel_id": "300000000000000001"},
            ],
        }
        self.channels = [
            {"id": "300000000000000001", "name": "general", "type": 0},
        ]
        self.limiter = core.RateLimiter(config=(ctx.config if ctx else None))
        self.logger_ = []
        self._member_status = 200

    # -- plumbing ------------------------------------------------
    def __getattr__(self, name):
        # Any endpoint the fake does not model answers an empty 200 so ops
        # that probe auxiliary endpoints (gateway, onboarding...) keep going.
        if name.startswith("__"):
            raise AttributeError(name)

        def _stub(*a, **k):
            self.calls.append(("STUB", name, a))
            return R(200, {})

        return _stub

    def _rec(self, method, path, body=None):
        self.calls.append((method, path, body))

    def get_all_members(self, gid, on_page=None):
        self._rec("GET", f"/guilds/{gid}/members?limit=1000")
        return R(200), list(self.members)

    def get_member(self, gid, uid):
        self._rec("GET", f"/guilds/{gid}/members/{uid}")
        if uid == "@me":
            return R(self._member_status, {})
        for m in self.members:
            if m["user"]["id"] == str(uid):
                return R(200, m)
        return R(404, {})

    def get_roles(self, gid):
        self._rec("GET", f"/guilds/{gid}/roles")
        return R(200, list(self.roles))

    def create_role(self, gid, body, reason=""):
        self._rec("POST", f"/guilds/{gid}/roles", body)
        self._role_seq += 1
        role = dict(body, id=str(self._role_seq))
        self.roles.append(role)
        return R(200, role)

    def delete_role(self, gid, rid):
        self._rec("DELETE", f"/guilds/{gid}/roles/{rid}")
        self.roles = [r for r in self.roles if r["id"] != str(rid)]
        return R(204)

    def modify_member(self, gid, uid, body):
        self._rec("PATCH", f"/guilds/{gid}/members/{uid}", body)
        return R(200, body)

    def get_guild_webhooks(self, gid):
        self._rec("GET", f"/guilds/{gid}/webhooks")
        hooks = [h for hs in self.webhooks.values() for h in hs]
        return R(200, hooks)

    def get_channel_webhooks(self, cid):
        self._rec("GET", f"/channels/{cid}/webhooks")
        return R(200, list(self.webhooks.get(str(cid), [])))

    def get_channels(self, gid):
        self._rec("GET", f"/guilds/{gid}/channels")
        return R(200, list(self.channels))

    def execute_webhook(self, url, body, _attempt=0):
        self.webhook_sends.append((url, body))
        return R(204)

    def log(self, *a, **k):
        pass


def guild_for(ctx):
    return {"id": "400000000000000001", "name": "fake-hall",
            "owner_id": "100000000000000099"}


# ------------------------------------------------------------
# TEST 1 | static invariants — version, registry, headers
# ------------------------------------------------------------

def test_static():
    check("version is exactly 2.0", core.TOOL_VERSION == "2.0",
          f"TOOL_VERSION={core.TOOL_VERSION!r}")
    stale = []
    for p in sorted(ROOT.glob("**/*.py")) + [ROOT.parent / "README.md"]:
        if not p.exists() or "tests" in p.parts:
            continue
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r"[Vv]?2\.[1-9]\b", text):
            stale.append(f"{p.name}: {m.group(0)}")
    check("no stale version names (only v2.0)", not stale, "; ".join(stale[:5]))

    idx = ops.OP_INDEX
    bad = [s for s, e in idx.items() if not callable(e.get("fn"))]
    check("OP_INDEX entries all callable", not bad, str(bad[:5]))
    unknown_perm_slugs = [s for s in wf.OP_PERMISSIONS if s not in idx]
    check("OP_PERMISSIONS slugs resolvable", not unknown_perm_slugs,
          str(unknown_perm_slugs[:5]))
    dupes = [n for n in [e["name"] for e in idx.values()]
             if [e["name"] for e in idx.values()].count(n) > 2]
    check("no runaway name duplication", not dupes, str(set(dupes)))


# ------------------------------------------------------------
# TEST 2 | rate limiter math + 429 retry loop
# ------------------------------------------------------------

def test_limiter():
    cfg = core.Config()
    core.apply_rate_level(cfg, "medium")
    lim = core.RateLimiter(config=cfg)
    lim.configure(cfg)  # the alias whose absence killed v2.3 sessions
    check("limiter.configure alias live", lim.level is not None)

    slept = []
    _real_sleep = core.time.sleep
    core.time.sleep = slept.append
    try:
        rest = core.DiscordREST("x", config=cfg)
        seq = [R(429, {"retry_after": 0.01}), R(200, {"ok": True})]
        rest.session = type("S", (), {
            "request": lambda self, *a, **k: seq.pop(0),
            "headers": {},
        })()
        r = rest.get_me()
        check("429 retried to 200", r.status_code == 200, f"got {r.status_code}")
        check("429 produced a wait", bool(slept), "no sleep recorded")

        # 5xx retry path
        seq5 = [R(502, {}), R(200, {"ok": True})]
        rest.session = type("S", (), {
            "request": lambda self, *a, **k: seq5.pop(0),
            "headers": {},
        })()
        r = rest.get_me()
        check("5xx retried to 200", r.status_code == 200, f"got {r.status_code}")
    finally:
        core.time.sleep = _real_sleep


# ------------------------------------------------------------
# TEST 3 | role engine flows
# ------------------------------------------------------------

def _role_flow(shared, exclude_bob=False):
    ctx = make_ctx()
    ctx.guild = guild_for(ctx)
    ctx.perms = wf.PERMISSION_BITS["ADMINISTRATOR"]
    rest = ctx.rest

    answers = []
    if exclude_bob:
        answers += ["5"]                # targets: everyone EXCEPT
        ops.pick_members = lambda c, multi, title: [
            {"id": "100000000000000002", "name": "bob", "member": None}]
        answers += ["y"]                # confirm N humans
    else:
        answers += ["4", "y"]           # everyone + confirm
    answers += ["1"]                    # preset: Administrator
    answers += ["1" if shared else "2"]  # delivery
    answers += ["raker-role", ""]       # name, color blank
    answers += ["n"]                    # hoist
    answers += ["y"]                    # execute
    SCRIPT.answers = answers

    with contextlib.redirect_stdout(io.StringIO()):
        ops.op_role_engine(ctx)
    return ctx, rest


def test_role_engine():
    ops_pick_members = ops.pick_members  # restored in finally below

    ctx, rest = _role_flow(shared=True)
    created = [c for c in rest.calls if c[0] == "POST" and "/roles" in c[1]]
    patched = [c for c in rest.calls if c[0] == "PATCH" and "/members/" in c[1]]
    ok = len(created) == 1 and len(patched) == 3  # alice,bob,carol (helper is a bot)
    team_targets = all("900000000000000000" not in (c[2] or {}).get("roles", [""])[0]
                       for c in patched)
    admin_bit = str(wf.PERMISSION_BITS["ADMINISTRATOR"])
    ok = ok and (created[0][2] or {}).get("permissions") == admin_bit
    try:
        ctx, rest2 = _role_flow(shared=False)
        created2 = [c for c in rest2.calls if c[0] == "POST" and "/roles" in c[1]]
        ok2 = len(created2) == 3 and len({(c[2] or {}).get("name") for c in created2}) == 3
    finally:
        ops.pick_members = ops_pick_members

    ctx3, rest3 = _role_flow(shared=True, exclude_bob=True)
    patched3 = [c for c in rest3.calls if c[0] == "PATCH" and "/members/" in c[1]]
    ok3 = len(patched3) == 2 and all("00000002" not in p[1] for p in patched3)

    check("role engine shared admin -> everyone (bot skipped)", ok and team_targets,
          f"created={len(created)} patched={len(patched)}")
    check("role engine separate unique roles", ok2,
          f"created={len(created2)}")
    check("role engine everyone EXCEPT bob", ok3,
          f"patched={len(patched3)}")


# ------------------------------------------------------------
# TEST 4 | webhook extraction + relay batching
# ------------------------------------------------------------

def test_webhooks():
    ctx = make_ctx()
    ctx.guild = guild_for(ctx)
    ctx.perms = wf.PERMISSION_BITS["ADMINISTRATOR"]
    # flood the fake end so the relay needs several 1900-char batches
    for ch in range(4):
        cid = f"30000000000000000{ch}"
        ctx.rest.webhooks.setdefault(cid, [])
        for i in range(12):
            ctx.rest.webhooks[cid].append({
                "id": f"70000000000000{ch}{i:03d}", "token": f"tok_{ch}_{i}_" + "x" * 60,
                "channel_id": cid})
    ctx.rest.channels = [{"id": f"30000000000000000{ch}", "name": f"c{ch}", "type": 0}
                         for ch in range(4)]

    SCRIPT.answers = []  # no stray prompts on the collect path
    urls = ops.collect_all_webhooks(ctx)
    check("webhook sweep collected all urls", len(urls) == 48, f"got {len(urls)}")

    # mode 3 relay — batching must never split a url across messages
    SCRIPT.answers = ["3", "https://discord.com/api/webhooks/1/dest", "", "b"]
    with contextlib.redirect_stdout(io.StringIO()):
        ops.op_extract_webhooks(ctx)
    bodies = [b["content"] for _, b in ctx.rest.webhook_sends]
    ok = bool(bodies) and all(len(b) <= 1900 for b in bodies)
    rebuilt = [line for b in bodies for line in b.split("\n") if line]
    ok = ok and sorted(rebuilt) == sorted(urls)
    check("relay batches respect 1900 chars, urls intact", ok,
          f"batches={len(bodies)} max={max((len(b) for b in bodies), default=0)} rebuilt={len(rebuilt)}")

    # mode 4 file export
    before = set(core.EXPORTS_DIR.glob("*.txt")) if core.EXPORTS_DIR.exists() else set()
    SCRIPT.answers = ["4", "", "b"]
    with contextlib.redirect_stdout(io.StringIO()):
        ops.op_extract_webhooks(ctx)
    after = set(core.EXPORTS_DIR.glob("*.txt"))
    new = after - before
    ok = len(new) == 1
    if ok:
        got = new.pop().read_text().splitlines()
        ok = sorted(got) == sorted(urls)
    check("export file contains every url", ok)


# ------------------------------------------------------------
# TEST 5 | crash bundle redaction
# ------------------------------------------------------------

def test_crash_bundle():
    ctx = make_ctx()
    ctx.config.set_setting(
        "tokens",
        [{"label": "main", "token": "AAAAAAAAAAAAAAAAAAAAAAAA.BCDEF1."
          "CCCCCCCCCCCCCCCCCCCCCCCCCCC"}])
    # a webhook url rides in the session log; its token tail must be masked
    ctx.logger.log("OP_RESULT",
                   "extract_webhooks | relay https://discord.com/api/webhooks/700000000000000001/hooktok_alice sent=1")
    try:
        raise RuntimeError("boom-stack-marker")
    except RuntimeError as exc:
        with contextlib.redirect_stdout(io.StringIO()):
            ops._op_crash_report(ctx, exc, "integration")  # must never raise
            path = core.build_crash_report(RuntimeError, exc, exc.__traceback__,
                                           ctx=ctx, origin="integration")
    ok = path is not None and Path(path).exists()
    token_found = marker_found = version_ok = False
    if ok:
        for sub in Path(path).rglob("*"):
            if sub.is_file():
                blob = sub.read_bytes().decode("utf-8", "replace")
                if "AAAAAAAAAAAAAAAAAAAAAAAA.BCDEF1" in blob:
                    token_found = True
                if "hooktok_alice" in blob:
                    token_found = True
                if "boom-stack-marker" in blob:
                    marker_found = True
                if f"{core.TOOL_NAME} v{core.TOOL_VERSION}" in blob:
                    version_ok = f"v{core.TOOL_VERSION}" == "v2.0"
    check("crash bundle created", ok, f"path={path}")
    check("crash bundle redacts tokens", ok and not token_found)
    check("crash bundle keeps traceback", ok and marker_found)
    check("crash bundle stamped v2.0", ok and version_ok)


# ------------------------------------------------------------
# TEST 6 | watchdog
# ------------------------------------------------------------

def test_watchdog():
    ctx = make_ctx()
    rest = ctx.rest
    fired = threading.Event()
    dog = core.GuildWatchdog(rest, guild_for(ctx)["id"], interval=5,
                             on_lost=fired.set)
    rest._member_status = 200
    check("watchdog sees healthy session", dog.check_once() == "ok")
    rest._member_status = 404
    check("watchdog sees a ban/kick", dog.check_once() == "lost")
    dog2 = core.GuildWatchdog(rest, guild_for(ctx)["id"], interval=5,
                              on_lost=fired.set)
    dog2.start()
    got = fired.wait(timeout=6)
    dog2.stop()
    check("watchdog on_lost fires from the loop", got)


# ------------------------------------------------------------
# TEST 7 | interval triggers
# ------------------------------------------------------------

def test_interval_triggers():
    ctx = make_ctx()
    # clean workflow store so only ours are on disk
    for f in wf.WORKFLOWS_DIR.glob("*.json"):
        f.unlink()
    wf.save_workflow({"name": "ticker", "steps": [],
                      "triggers": [{"kind": "interval", "seconds": 30}]})
    due1 = wf.collect_due_workflows(ctx)
    due2 = wf.collect_due_workflows(ctx)
    check("interval fires when due", len(due1) == 1 and due1[0][0]["name"] == "ticker",
          str(due1))
    check("interval does not refire immediately", not due2, str(due2))
    # on_guild_select
    wf.save_workflow({"name": "greeter", "steps": [],
                      "triggers": [{"kind": "on_guild_select"}]})
    ctx.just_selected_guild = True
    ctx.trigger_state.pop("interval::ticker", None)
    names = {w["name"] for w, _ in wf.collect_due_workflows(ctx)}
    check("on_guild_select fires once per selection", "greeter" in names, str(names))
    # on_ban_detected
    wf.save_workflow({"name": "avenger", "steps": [],
                      "triggers": [{"kind": "on_ban_detected"}]})
    ctx.guild_lost = True
    names = {w["name"] for w, _ in wf.collect_due_workflows(ctx)}
    check("on_ban_detected fires when guild lost", "avenger" in names, str(names))
    for f in wf.WORKFLOWS_DIR.glob("*.json"):
        f.unlink()


# ------------------------------------------------------------
# TEST 8 | workflow execution order
# ------------------------------------------------------------

def test_workflow_run():
    ctx = make_ctx()
    ctx.guild = guild_for(ctx)
    ran = []

    def rec_a(c):
        ran.append("a")

    def rec_b(c):
        ran.append("b")

    idx = wf.build_index([{"name": "T", "ops": [
        {"name": "rec a", "desc": "", "fn": rec_a},
        {"name": "rec b", "desc": "", "fn": rec_b},
    ]}])
    flow = {"name": "order-check", "steps": [
        {"kind": "op", "op": "rec_a"},
        {"kind": "note", "text": "middle"},
        {"kind": "loop", "times": 2, "steps": [{"kind": "op", "op": "rec_b"}]},
    ], "triggers": [{"kind": "manual"}]}
    SCRIPT.answers = [""]
    with contextlib.redirect_stdout(io.StringIO()):
        wf.execute_workflow(ctx, idx, flow)
    check("workflow steps run in order (loop unrolled)",
          ran == ["a", "b", "b"], str(ran))

    # Regression: a step left 'running' (interrupt/abort mid-run) used to
    # crash the post-run tally with KeyError and take the session page down.
    idx2 = wf.build_index([{"name": "T", "ops": [
        {"name": "boom", "desc": "", "fn": lambda c: (_ for _ in ()).throw(KeyboardInterrupt())},
    ]}])
    flow2 = {"name": "interrupted", "steps": [{"kind": "op", "op": "boom"},
                                              {"kind": "op", "op": "boom"}]}
    try:
        SCRIPT.answers = [""]
        with contextlib.redirect_stdout(io.StringIO()):
            wf.execute_workflow(ctx, idx2, flow2)
        check("interrupted workflow tallies without crashing", True)
    except Exception as e:
        check("interrupted workflow tallies without crashing", False,
              f"{type(e).__name__}: {e}")


# ------------------------------------------------------------
# TEST 9 | narrow-terminal rendering
# ------------------------------------------------------------

def test_narrow_terminal():
    tiny = FakeConsole(width=40)
    ui.console = tiny
    ctx = make_ctx()
    frame = "\n".join(f"\x1b[38;5;{i}m" + "X" * 90 + "\x1b[0m" for i in range(6))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            ui.present_frame(ctx, lambda: frame)
        out = tiny.buffer.getvalue()
    finally:
        ui.console = console
        for mod in (ops, wf):
            mod.console = console
    long_lines = [ln for ln in out.splitlines() if ui._visible_len(ln) > 41]
    check("present_frame clips to terminal width", not long_lines,
          f"overlong: {len(long_lines)}")
    clip = ui._clip_ansi("\x1b[31m" + "Y" * 50 + "\x1b[0m", 10)
    check("_clip_ansi preserves escapes <= width",
          ui._visible_len(clip) == 10 and clip.startswith("\x1b[31m"),
          repr(clip))
    check("_clip_lines handles blank + mixed", ui._clip_lines("a\n\n" + "z" * 200, 50)
          .splitlines()[2] and ui._visible_len(ui._clip_lines("z" * 200, 50)) == 50)


# ------------------------------------------------------------
# TEST 10 | diagnoser + report explorer
# ------------------------------------------------------------

def test_diagnoser():
    ctx = make_ctx()
    logged = []
    real_log = ctx.logger.log
    ctx.logger.log = lambda ev, msg="": (logged.append((ev, msg)), real_log(ev, msg))
    SCRIPT.answers = []
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            ops.op_diagnoser(ctx)
    finally:
        ctx.logger.log = real_log
    line = next((m for ev, m in logged if ev == "OP_RESULT" and "diagnoser" in m), "")
    check("diagnoser ran all checks and logged result",
          bool(re.search(r"diagnoser \| \d+ checks", line)), repr(line))
    # the explorer must see the session log itself and any crash bundles
    entries = ops._report_entries()
    check("report explorer lists logs + bundles", any(e[0] == "session log" for e in entries),
          str([e[0] for e in entries][-4:]))


# ------------------------------------------------------------

def main():
    tests = [
        test_static, test_limiter, test_role_engine, test_webhooks,
        test_crash_bundle, test_watchdog, test_interval_triggers,
        test_workflow_run, test_narrow_terminal, test_diagnoser,
    ]
    for t in tests:
        try:
            t()
        except Exception:
            import traceback
            check(t.__name__, False, traceback.format_exc(limit=3))
    print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
