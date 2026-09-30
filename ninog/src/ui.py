# ==============================================================
#   NiNog Raker v2.0 | THE RATTIKANS
#   src/ui.py | themes, gradient, prompts, transitions,
#              bouncing-R live displays
# ==============================================================
#
#   Presentation only. May import src.core, must not import src.ops.
#
#   The ASCII background experiment was removed. Its replacement is
#   the bouncing ANSI-Shadow R: OpProgress (the status spinner) and
#   TodoBoard (the workflow checklist). Both draw into a private
#   screen region from a daemon thread so they animate while the
#   main thread is blocked on network I/O.
# ==============================================================

import getpass
import math
import random
import re
import sys
import threading
import time

import pyfiglet
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from .core import MAINTAINER, RATE_LIMIT_PRESETS, TOOL_NAME, TOOL_VERSION

# ------------------------------------------------------------
# SECTION 1 | THEMES
# ------------------------------------------------------------

THEMES = {
    "rattikans": {
        "label": "Rattikans | deep orange, white",
        "brand": "bold #FF6A00",
        "orange": "#FF6A00",
        "deep": "bold #CC4A00",
        "white": "bold white",
        "dim": "grey50",
        "faint": "grey35",
        "good": "bold #7CFF9B",
        "warn": "bold #FFB020",
        "bad": "bold #FF4040",
    },
    "abyss": {
        "label": "Abyss | cyan, indigo",
        "brand": "bold #35E8FF",
        "orange": "#35E8FF",
        "deep": "bold #1668B3",
        "white": "bold white",
        "dim": "grey58",
        "faint": "grey35",
        "good": "bold #7CFF9B",
        "warn": "bold #FFB020",
        "bad": "bold #FF4040",
    },
    "toxic": {
        "label": "Toxic | acid green, black",
        "brand": "bold #A6FF3C",
        "orange": "#8BFF3C",
        "deep": "bold #3FA12B",
        "white": "bold white",
        "dim": "grey54",
        "faint": "grey35",
        "good": "bold #A6FF3C",
        "warn": "bold #FFD24A",
        "bad": "bold #FF5C5C",
    },
    "crimson": {
        "label": "Crimson | blood red, ash",
        "brand": "bold #FF4A5A",
        "orange": "#FF4A5A",
        "deep": "bold #9E1B2A",
        "white": "bold white",
        "dim": "grey54",
        "faint": "grey35",
        "good": "bold #7CFF9B",
        "warn": "bold #FFB020",
        "bad": "bold #FF8A8A",
    },
    "mono": {
        "label": "Mono | greyscale",
        "brand": "bold grey85",
        "orange": "grey78",
        "deep": "bold grey62",
        "white": "bold white",
        "dim": "grey50",
        "faint": "grey35",
        "good": "bold grey93",
        "warn": "bold grey85",
        "bad": "bold grey70",
    },
}

THEME_KEYS = list(THEMES.keys())
# Dropdowns need (key, label) pairs. THEMES already carries a human label,
# so expose it here instead of making the settings screen print raw keys.
THEME_OPTIONS = [(k, THEMES[k]["label"]) for k in THEME_KEYS]
LAYOUTS = [("panels", "Panels | boxed grid, full width"),
           ("compact", "Compact | dense two-column list"),
           ("list", "List | one op per line")]
TRANSITIONS = [("mercury", "Mercury Diffusion | scrambled text refinement (default)"),
               ("fade", "Fade | lines cascade in top to bottom"),
               ("wipe", "Wipe | characters sweep in reading order"),
               ("none", "None | instant redraw")]
RATE_LEVELS = [(k, v["label"]) for k, v in RATE_LIMIT_PRESETS.items()]


def normalize_options(options):
    """Accept ['a', 'b'] or [('a', 'Label'), ...]; return (key, label) pairs.

    SETTINGS_SPEC historically mixed both shapes: LAYOUTS and TRANSITIONS are
    pairs, while THEME_KEYS was a bare list of strings. Every consumer of
    `options` unpacks two values, so the bare list raised
    "too many values to unpack" and -- worse -- indexing it with [0] handed
    back a single character instead of a key. Everything that reads options
    now goes through here so the two shapes cannot drift apart again.
    """
    pairs = []
    for opt in options or []:
        if isinstance(opt, (tuple, list)):
            key = opt[0]
            label = opt[1] if len(opt) > 1 else str(opt[0])
        else:
            key = opt
            label = str(opt)
        pairs.append((key, label))
    return pairs


def theme_from_name(name):
    t = THEMES.get(name) or THEMES["rattikans"]
    styles = {k: v for k, v in t.items() if k != "label"}
    styles.update(
        {
            "prompt": styles["orange"],
            "prompt.choices": styles["white"],
            "prompt.default": styles["dim"],
            "confirm": styles["orange"],
        }
    )
    return Theme(styles)


# ------------------------------------------------------------
# SECTION 2 | GRADIENT (experimental)
# ------------------------------------------------------------

_GRAD_PHASE = 0.0


def gradient_on(ctx):
    return bool(ctx.config.setting("gradient", False))


def accent_rgb(ctx=None):
    ctx = ctx or _CTX
    fallback = THEMES["rattikans"]
    style = fallback["orange"]
    if ctx is not None:
        style = THEMES.get(ctx.config.setting("theme", "rattikans"), fallback)["orange"]
    m = re.search(r"#([0-9A-Fa-f]{6})", style)
    if m:
        h = m.group(1)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (255, 106, 0)


def mix(c1, c2, t):
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))


def grad(text, ctx=None):
    ctx = ctx or _CTX
    if ctx is None or not gradient_on(ctx):
        return f"[orange]{text}[/orange]"
    a = accent_rgb(ctx)
    light = tuple(min(255, int(c + (255 - c) * 0.65)) for c in a)
    dark = tuple(int(c * 0.55) for c in a)
    rows = text.split("\n")
    H = max(len(rows), 1)
    W = max(len(r) for r in rows) or 1
    out = []
    for r, row in enumerate(rows):
        for ci, ch in enumerate(row):
            if ch == " ":
                out.append(" ")
                continue
            u = ci / max(W - 1, 1)
            v = r / max(H - 1, 1)
            t = 0.5 + 0.5 * math.sin(2 * math.pi * (u * 1.4 + v * 0.7 - _GRAD_PHASE))
            col = mix(dark, light, t)
            out.append(f"[#{col[0]:02X}{col[1]:02X}{col[2]:02X}]{ch}[/]")
        if r != len(rows) - 1:
            out.append("\n")
    return "".join(out)


# ------------------------------------------------------------
# SECTION 3 | LIVE DISPLAY (bouncing ANSI-Shadow R)
# ------------------------------------------------------------
# A _LiveRegion owns N screen rows. redraw() overwrites them in place, so the
# cursor never moves and nothing above or below the region is disturbed.
# While suspended, redraw() is a no-op -- that is how the workflow board gets
# out of the way while an op prints or asks for input.

ANSI_RE = re.compile(r"(\x1b\[[0-9;]*m)")
SCRAMBLE_GLYPHS = "▓▒░#@%&*=-<>/\\|~^!?;:"


def _visible_len(line):
    return len(ANSI_RE.sub("", line))


def _clip_ansi(line, width):
    """Truncate an ANSI-colored line to `width` visible characters.

    Narrow terminals (phones, Termux) soft-wrap anything longer, which
    breaks every cursor-up line count the transition and live-region
    engines rely on. Clipping keeps 1 logical line == 1 terminal row.
    """
    if width <= 0 or _visible_len(line) <= width:
        return line
    out, seen, open_sgr = [], 0, ""
    for tok in ANSI_RE.split(line):
        if not tok:
            continue
        if ANSI_RE.fullmatch(tok):
            out.append(tok)
            open_sgr = tok if tok != "\x1b[0m" else ""
            continue
        room = width - seen
        if room <= 0:
            break
        out.append(tok[:room])
        seen += min(len(tok), room)
    out.append("\x1b[39m" if open_sgr else "")
    return "".join(out)


def _clip_lines(text, width):
    return "\n".join(_clip_ansi(ln, width) for ln in text.split("\n"))

R_TRAVEL = 3          # rows the R moves through
R_FRAME_S = 0.085     # seconds between bounce frames

GOOD_RGB = (124, 255, 155)
WARN_RGB = (255, 176, 32)
BAD_RGB = (255, 64, 64)
DIM_RGB = (130, 130, 130)
WHITE_RGB = (235, 235, 235)

# ANSI Shadow "R". Used verbatim when pyfiglet lacks the font.
_R_FALLBACK = [
    "██████╗ ",
    "██╔══██╗",
    "██████╔╝",
    "██╔══██╗",
    "██║  ██║",
    "╚═╝  ╚═╝",
]


def _fg(rgb, s):
    return f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m{s}\x1b[39m"


def _plain(s):
    """Strip rich markup down to the text a raw-ANSI draw can show."""
    try:
        return Text.from_markup(str(s)).plain
    except Exception:
        return re.sub(r"\[/?[^\]]*\]", "", str(s))


def _r_art():
    for font in ("ansi_shadow", "ANSI Shadow", "shadow"):
        try:
            art = pyfiglet.figlet_format("R", font=font)
        except Exception:
            continue
        lines = [l.rstrip() for l in art.rstrip("\n").split("\n")]
        if lines and any(l.strip() for l in lines):
            return lines
    return list(_R_FALLBACK)


def _bounce_offset(frame, travel):
    """Triangle wave 0..travel..0 so the R eases between the rows."""
    period = travel * 2
    if period <= 0:
        return 0
    p = frame % period
    return p if p <= travel else period - p


def _is_tty():
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


class _LiveRegion:
    """N terminal rows that can be redrawn in place, thread-safely."""

    def __init__(self):
        self._lock = threading.Lock()
        self._rows = 0
        self._paused = True

    def redraw(self, lines):
        with self._lock:
            if self._paused:
                return
            # Clip to terminal width: a soft-wrapped line on a narrow terminal
            # (Termux, phone) makes every cursor-up count wrong and the region
            # smears across the whole screen.
            width = console.width or 80
            lines = [_clip_ansi(ln, width) for ln in lines]
            out = sys.stdout
            if self._rows:
                out.write(f"\x1b[{self._rows}A")
            for ln in lines:
                out.write("\x1b[2K" + ln + "\n")
            for _ in range(max(0, self._rows - len(lines))):
                out.write("\x1b[2K\n")
            out.flush()
            self._rows = max(self._rows, len(lines))

    def clear(self):
        with self._lock:
            if not self._rows:
                return
            out = sys.stdout
            out.write(f"\x1b[{self._rows}A")
            for _ in range(self._rows):
                out.write("\x1b[2K\n")
            out.write(f"\x1b[{self._rows}A")
            out.flush()
            self._rows = 0

    def pause(self):
        self.clear()
        self._paused = True

    def unpause(self):
        self._paused = False


class _Bouncer:
    """Shared animation loop. One daemon thread per live display."""

    def __init__(self, render):
        self._render = render
        self._frame = 0
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._thread = None

    def _spin(self):
        while not self._stop.wait(R_FRAME_S):
            self._frame += 1
            try:
                self._render(self._frame)
            except Exception:
                # A drawing error must never take down the op that is running.
                self._stop.set()
                return


class OpProgress:
    """Drop-in replacement for rich's Console.status().

    Renders the bouncing ANSI-Shadow R with a status caption underneath.
    Exposes start/stop/update/__enter__/__exit__ so every existing
    `with console.status(...) as status: status.update(...)` site in ops.py
    keeps working without modification.
    """

    def __init__(self, status_text="", spinner="dots", speed=1.0,
                 refresh_per_second=12):
        self._label = _plain(status_text)
        self._region = _LiveRegion()
        self._bouncer = _Bouncer(self._render)
        self._live = False
        self._art = _r_art()
        self._width = max(len(l) for l in self._art)

    # -- rich Status API ------------------------------------------------
    def start(self):
        if self._live or not _is_tty():
            return
        self._live = True
        self._region.unpause()
        self._render(0)
        self._bouncer.start()

    def stop(self):
        if not self._live:
            return
        self._live = False
        self._bouncer.stop()
        self._region.clear()

    def update(self, status=""):
        self._label = _plain(status)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    # -- drawing --------------------------------------------------------
    def _render(self, frame=0):
        accent = accent_rgb()
        off = _bounce_offset(frame, R_TRAVEL)
        rows = []
        for y in range(len(self._art) + R_TRAVEL):
            if off <= y < off + len(self._art):
                rows.append("   " + _fg(accent, self._art[y - off]))
            else:
                rows.append("")
        rows.append("")
        rows.append("   " + _fg(WHITE_RGB, self._label))
        self._region.redraw(rows)


class RakerConsole(Console):
    """Console whose status spinner is the bouncing R."""

    def status(self, status=None, *, spinner="dots", spinner_style="status.spinner",
               speed=1.0, refresh_per_second=12):
        return OpProgress(
            status or "", spinner=spinner, speed=speed,
            refresh_per_second=refresh_per_second,
        )


console = RakerConsole(
    theme=theme_from_name("rattikans"),
    force_terminal=True,
    highlight=False,
)

_CTX = None


def set_context(ctx):
    global _CTX
    _CTX = ctx


def apply_theme(ctx):
    name = ctx.config.setting("theme", "rattikans")
    console.push_theme(theme_from_name(name), inherit=True)


# ------------------------------------------------------------
# SECTION 4 | WORKFLOW TO-DO BOARD
# ------------------------------------------------------------

PENDING, RUNNING, DONE, SKIPPED, FAILED = "pending", "running", "done", "skipped", "failed"

_CHECKS = {
    PENDING: ("[ ]", DIM_RGB),
    RUNNING: ("[~]", None),   # accent
    DONE: ("[x]", GOOD_RGB),
    SKIPPED: ("[-]", WARN_RGB),
    FAILED: ("[!]", BAD_RGB),
}


class TodoBoard:
    """Live checklist with the bouncing R underneath.

    `items` is a list of (depth, title). depth drives indentation so nested
    condition branches and loops read as a tree. Call begin/done/skip/fail to
    move an item along; status() sets the caption under the R.

    suspend() blanks the region so an op can print or prompt freely, then
    resume() puts the board back. Anything that draws to the screen while a
    board is up MUST go through suspend/resume.
    """

    def __init__(self, items, title="", subtitle=""):
        # Items are (depth, title) pairs. A bare string is tolerated and
        # treated as depth 0 -- one caller passed pre-indented strings and
        # _touch/_render unpacked them into a crash mid-run.
        norm = []
        for it in items:
            if isinstance(it, (tuple, list)) and len(it) == 2:
                norm.append((int(it[0]), str(it[1])))
            else:
                norm.append((0, str(it)))
        self.items = norm
        self._states = [PENDING] * len(self.items)
        self._notes = [""] * len(self.items)
        self._cursor = 0
        self._caption = ""
        self._title = _plain(title)
        self._subtitle = _plain(subtitle)
        self._region = _LiveRegion()
        self._art = _r_art()
        self._live = False
        self._bouncer = _Bouncer(self._render)

    def start(self):
        if self._live or not _is_tty():
            return
        self._live = True
        self._region.unpause()
        self._render(0)
        self._bouncer.start()

    def stop(self, final=None):
        if not self._live:
            return
        self._live = False
        self._bouncer.stop()
        self._region.clear()
        if final:
            console.print(final)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    def suspend(self):
        if self._live:
            self._region.pause()

    def resume(self):
        if self._live:
            self._region.unpause()
            self._render(0)

    # -- state transitions ---------------------------------------------
    def begin(self, i, note=""):
        self._touch(i, RUNNING, note)
        self._cursor = i

    def done(self, i, note=""):
        self._touch(i, DONE, note)

    def skip(self, i, note=""):
        self._touch(i, SKIPPED, note)

    def fail(self, i, note=""):
        self._touch(i, FAILED, note)

    def reset(self, i):
        self._touch(i, PENDING, "")

    def status(self, text):
        self._caption = _plain(text)

    def state(self, i):
        """Current state of item i, for callers summarising a finished run."""
        if 0 <= i < len(self._states):
            return self._states[i]
        return PENDING

    def _touch(self, i, state, note):
        if not 0 <= i < len(self.items):
            return
        self._states[i] = state
        self._notes[i] = _plain(note)
        if not _is_tty() and state in (RUNNING, DONE, SKIPPED, FAILED):
            # No terminal region available: fall back to plain lines so a
            # piped run still shows progress in order.
            depth, title = self.items[i]
            print("  " * depth + f"[{state}] {title}" + (f" | {note}" if note else ""))

    # -- drawing --------------------------------------------------------
    def _render(self, frame=0):
        accent = accent_rgb()
        rows = []
        if self._title:
            rows.append("  " + _fg(accent, self._title)
                        + (f"  {_fg(DIM_RGB, self._subtitle)}" if self._subtitle else ""))
            rows.append("")

        start, end = self._window()
        if start > 0:
            rows.append("  " + _fg(DIM_RGB, "..."))
        for i in range(start, end):
            depth, title = self.items[i]
            glyph, rgb = _CHECKS[self._states[i]]
            if rgb is None:
                rgb = accent
            mark = _fg(rgb, glyph)
            note = f" {_fg(DIM_RGB, self._notes[i])}" if self._notes[i] else ""
            rows.append("  " + "    " * depth + f"{mark} {_fg(WHITE_RGB, title)}{note}")
        if end < len(self.items):
            rows.append("  " + _fg(DIM_RGB, "..."))

        rows.append("")
        off = _bounce_offset(frame, R_TRAVEL)
        for y in range(len(self._art) + R_TRAVEL):
            if off <= y < off + len(self._art):
                rows.append("   " + _fg(accent, self._art[y - off]))
            else:
                rows.append("")
        if self._caption:
            rows.append("")
            rows.append("   " + _fg(WHITE_RGB, self._caption))
        self._region.redraw(rows)

    def _window(self):
        n = len(self.items)
        # Leave room for the header, the R block, the caption and a margin.
        budget = max(3, (console.height or 30) - (len(self._art) + R_TRAVEL) - 8)
        if n <= budget:
            return 0, n
        start = max(0, min(self._cursor - budget // 2, n - budget))
        return start, start + budget


# ------------------------------------------------------------
# SECTION 5 | UI HELPERS
# ------------------------------------------------------------

def divider(width=None):
    w = width or min((console.width or 80) - 2, 58)
    return "[orange]" + ("─" * max(w, 10)) + "[/orange]"


def figlet_art():
    width = console.width or 80
    for font in ("slant", "small"):
        try:
            art = pyfiglet.figlet_format("NINOG RAKER", font=font)
        except Exception:
            continue
        lines = [l.rstrip() for l in art.rstrip("\n").split("\n")]
        if lines and max(len(l) for l in lines) <= width - 2:
            return "\n".join(lines)
    return "NINOG RAKER"


def print_header(ctx):
    console.print(grad(figlet_art(), ctx))
    console.print(
        f"[white]{TOOL_NAME} v{TOOL_VERSION}[/white] [dim]|[/dim] [orange]{MAINTAINER}[/orange]"
    )
    console.print("[dim]RATELIMIT: ON[/dim]")
    console.print(divider())


def screen_title(title, subtitle=""):
    console.print(grad(f"▸ {title}"))
    if subtitle:
        console.print(f"[dim]{subtitle}[/dim]")


def notice(title, lines, style="orange"):
    body = "\n".join(lines)
    w = min(60, console.width or 80)
    console.print(
        Panel(
            body,
            title=f"[{style}]{title}[/{style}]",
            border_style=style,
            padding=(0, 1),
            width=max(w, 24),
            box=box.ROUNDED,
        )
    )


# ------------------------------------------------------------
# SCRIPTED INPUT FEED (workflow fire-time replay)
# ------------------------------------------------------------
# When a workflow step runs, the engine feeds the op the answers that were
# recorded at build time through this hook. Empty feed = normal live prompts.
_INPUT_FEED = None


def set_input_feed(feed):
    """Install/clear a deque of pre-recorded prompt answers.

    Any non-None value marks 'a workflow step is running': press_enter()
    pauses are skipped entirely so a fire is never held up by an op's
    trailing 'press enter'. Real prompts fall through to live input once
    the recording runs dry.
    """
    global _INPUT_FEED
    _INPUT_FEED = feed


def _fed():
    """Next recorded answer, or None when the feed is empty/absent."""
    if _INPUT_FEED is None:
        return None
    try:
        v = _INPUT_FEED.popleft()
        console.print(f"[dim]> replay: {v}[/dim]")
        return v
    except IndexError:
        return None

def ask(text, default=None):
    show_default = default not in (None, "")
    prompt = f"[orange]{text}[/orange]"
    if show_default:
        prompt += f" [dim]({default})[/dim]"
    fed = _fed()
    if fed is not None:
        console.print(prompt + f" [white]›[/white] [white]{fed}[/white]")
        return fed if fed else ("" if default is None else default)
    console.print(prompt + " [white]›[/white]", end=" ")
    try:
        raw = input().strip()
    except EOFError:
        return "" if default is None else default
    if not raw:
        return "" if default is None else default
    return raw


def ask_password(text):
    console.print(f"[orange]{text}[/orange] [white]›[/white]", end=" ")
    try:
        return getpass.getpass("").strip()
    except Exception:
        try:
            return input().strip()
        except EOFError:
            return ""


def ask_int(text, default):
    raw = ask(f"{text} [dim](default {default})[/dim]")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def confirm(text, default=False):
    if default:
        d = "[white]y[/white]/[dim]n[/dim]"
    else:
        d = "[dim]y[/dim]/[white]n[/white]"
    while True:
        fed = _fed()
        if fed is not None:
            raw = fed.strip().lower()
            console.print(f"[orange]{text}[/orange] [dim]([/dim]{d}[dim])[/dim] [white]{raw}[/white]")
        else:
            console.print(f"[orange]{text}[/orange] [dim]([/dim]{d}[dim])[/dim]", end=" ")
            try:
                raw = input().strip().lower()
            except EOFError:
                console.print()
                return default
        if not raw:
            console.print()
            return default
        if raw in ("y", "yes"):
            console.print()
            return True
        if raw in ("n", "no"):
            console.print()
            return False
        console.print()
        console.print("[warn]y or n[/warn]")


def press_enter():
    if _INPUT_FEED is not None:
        return  # a workflow step is running; never pause the fire
    console.print("[orange]›[/orange] [dim]press enter[/dim]", end=" ")
    try:
        input()
    except EOFError:
        pass
    console.print()


def sanitize_name(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip() or "unnamed"


def fmt_ts(iso):
    return iso.replace("T", " ").split("+")[0].split(".")[0] if iso else "?"


def chunk(items, size):
    return [items[i : i + size] for i in range(0, len(items), size)]


# ------------------------------------------------------------
# SECTION 6 | TRANSITION ENGINE
# ------------------------------------------------------------

def _clear_screen():
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


def _redraw_lines(new_text, prev_count):
    width = console.width or 80
    lines = [_clip_ansi(ln, width) for ln in new_text.split("\n")]
    out = sys.stdout
    out.write(f"\x1b[{prev_count}A")
    for ln in lines:
        out.write("\x1b[2K" + ln + "\n")
    for _ in range(max(0, prev_count - len(lines))):
        out.write("\x1b[2K\n")
    out.flush()
    return max(prev_count, len(lines))


def _progressive_reveal(text, mode, ms, on_screen):
    tokens = ANSI_RE.split(text)
    final_chars = []
    for i in range(0, len(tokens), 2):
        final_chars.extend(tokens[i])

    scrambled = [
        ch if ch in " \n\t" else random.choice(SCRAMBLE_GLYPHS)
        for ch in final_chars
    ]

    visible = []
    v = 0
    for ch in final_chars:
        if ch in " \n\t":
            visible.append(-1)
        else:
            visible.append(v)
            v += 1
    total = v
    if total == 0:
        return on_screen

    def build(ratio):
        revealed = int(total * ratio)
        out = []
        g = 0
        for ti, tok in enumerate(tokens):
            if ti % 2 == 1:
                out.append(tok)
                continue
            buf = []
            for ch in tok:
                vi = visible[g]
                if vi == -1:
                    buf.append(ch)
                elif vi < revealed:
                    buf.append(ch)
                else:
                    buf.append(scrambled[g] if mode == "mercury" else " ")
                g += 1
            out.append("".join(buf))
        return "".join(out)

    passes = 8
    for i in range(1, passes + 1):
        t = i / passes
        ratio = 1 - (1 - t) ** 2
        f = build(ratio)
        on_screen = _redraw_lines(f, on_screen)
        time.sleep((ms / 1000.0) / passes)
    return on_screen


def present_frame(ctx, render_fn):
    mode = ctx.config.setting("transition", "mercury")
    ms = int(ctx.config.setting("transition_ms", 350))
    gr_on = gradient_on(ctx)

    def render_text():
        global _GRAD_PHASE
        with console.capture() as cap:
            render_fn()
        _GRAD_PHASE = (_GRAD_PHASE + 0.11) % 1.0
        # Rich wraps at console width, but hand-built lines (headers, prompts,
        # panels with explicit widths) can still overshoot it on narrow
        # screens. Clipping here keeps the row math honest universally.
        return _clip_lines(cap.get().rstrip("\n"), console.width or 80)

    first = render_text()

    if not sys.stdout.isatty():
        sys.stdout.write(first + "\n")
        sys.stdout.flush()
        return

    _clear_screen()
    sys.stdout.write(first + "\n")
    sys.stdout.flush()
    on_screen = len(first.split("\n"))

    if ms > 0 and mode in ("mercury", "wipe"):
        on_screen = _progressive_reveal(first, mode, ms, on_screen)
    elif ms > 0 and mode == "fade":
        lines = first.split("\n")
        delay = min(0.05, (ms / 1000.0) / max(len(lines), 1))
        _clear_screen()
        for ln in lines:
            sys.stdout.write(ln + "\n")
            sys.stdout.flush()
            time.sleep(delay)
        on_screen = len(lines)

    if not gr_on:
        return

    for _ in range(6):
        f = render_text()
        on_screen = _redraw_lines(f, on_screen)
        time.sleep(0.065)

    final = render_text()
    _redraw_lines(final, on_screen)
