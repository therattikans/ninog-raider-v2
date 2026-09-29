#!/usr/bin/env python3
# ==============================================================
#   NiNog Raker V2.3 | THE RATTIKANS
#   main.py | entry point
#
#   Usage:   python main.py        (from inside ninog/)
#            python ninog/main.py  (from the parent dir)
#
#   Layout:
#     main.py      this file -- bootstrap + session loop
#     src/core.py  paths, config, report log, vault, REST client
#     src/ui.py    theme, gradient, ascii bg, prompts, transitions
#     src/ops.py   every flow and operation
#
#   config/, reportlog/ and snapshots/ are created on demand next
#   to this file, exactly as nnv2.py did.
# ==============================================================

import importlib
import os
import platform
import subprocess
import sys
from pathlib import Path

# ------------------------------------------------------------
# BOOTSTRAP
# ------------------------------------------------------------
# This has to run before `from src... import` below, because those
# modules import rich / pyfiglet / requests at module scope. Only
# the standard library is allowed above this line.

REQUIRED_PACKAGES = {
    "rich": "rich",
    "pyfiglet": "pyfiglet",
    "requests": "requests",
    # Optional: enables online-first member sorting in the pickers. The tool
    # runs fine without it and says so in the diagnoser.
    # "websockets": "websockets",
}


def ensure_dependencies():
    missing = []
    for module, package in REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(package)
    if not missing:
        return
    print(f"[ setup ] installing missing packages: {', '.join(missing)}")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", *missing]
        )
    except Exception:
        print("[ setup ] pip failed | install manually with:")
        print(f"           {sys.executable} -m pip install {' '.join(missing)}")
        sys.exit(1)


# Make `import src...` work no matter which directory we were
# launched from. Python only auto-adds the script's own directory,
# which is enough for `python main.py`, but not for a wrapper that
# runs us from elsewhere.
PKG_ROOT = Path(__file__).resolve().parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

ensure_dependencies()

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

if platform.system() == "Windows":
    os.system("")

# Safe now: third-party packages are guaranteed present.
from src.core import (
    MAINTAINER_CONTACT,
    MAINTAINER_SERVER,
    PLATFORM_INFO,
    PYTHON_INFO,
    TOOL_NAME,
    TOOL_VERSION,
    Config,
    Ctx,
    ReportLogger,
    bind_crash_context,
    build_crash_report,
    ensure_directories,
    install_crash_hooks,
)
from src.ops import attach_guild, authenticate_token, page_loop, pick_guild
from src.ui import (
    apply_theme,
    console,
    notice,
    present_frame,
    press_enter,
    print_header,
    set_context,
)


# ------------------------------------------------------------
# SESSION LOOP
# ------------------------------------------------------------

def main():
    ensure_directories()
    config = Config()
    logger = ReportLogger()
    logger.log("SESSION_START",
               f"{TOOL_NAME} v{TOOL_VERSION} | {PLATFORM_INFO} | {PYTHON_INFO}")
    ctx = Ctx(config, logger)
    set_context(ctx)
    bind_crash_context(ctx)
    apply_theme(ctx)

    try:

        def boot():
            print_header(ctx)
            console.print()
            if config.first_run:
                notice("FIRST RUN",
                       ["no vault found yet.",
                        "first step: store your first bot token."])
                console.print()

        present_frame(ctx, boot)
        if config.first_run:
            press_enter()

        while True:
            # Leaving a guild means its watchdog is stale; stop it either way.
            if ctx.watchdog is not None:
                ctx.watchdog.stop()
                ctx.watchdog = None

            auth = authenticate_token(ctx)
            if auth is None:
                break
            ctx.rest, ctx.me, ctx.meta = auth

            guild = pick_guild(ctx, ctx.rest, ctx.me)
            if guild is None:
                continue
            attach_guild(ctx, guild)

            signal = page_loop(ctx)
            if signal == "quit":
                break

    except KeyboardInterrupt:
        console.print()
    finally:
        if ctx.watchdog is not None:
            ctx.watchdog.stop()
            ctx.watchdog = None
        logger.log("SESSION_END", f"{logger.actions} logged actions")
        notice(
            "SESSION ENDED",
            [
                f"actions logged: [white]{logger.actions}[/white]",
                f"report log:    [white]{logger.path}[/white]",
            ],
        )


def guarded_main():
    """Last line of defense: anything that escapes the session loop becomes
    a crash bundle before the traceback ever reaches the terminal."""
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        try:
            report = build_crash_report(type(e), e, e.__traceback__,
                                        ctx=None, origin="guarded_main")
            print(f"\n[NiNog Raker] fatal error | report written to:\n  {report}\n"
                  f"  send it to {MAINTAINER_CONTACT} or {MAINTAINER_SERVER}")
        except Exception:
            raise


if __name__ == "__main__":
    install_crash_hooks()
    guarded_main()
