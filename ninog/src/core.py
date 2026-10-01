# ==============================================================
#   NiNog Raker v2.0 | THE RATTIKANS
#   src/core.py | identity, paths, config, report log,
#                vault + whitelist persistence, REST client
# ==============================================================
#
#   IMPORTANT!!! Bottom of the stack. This module must never import src.ui or
#   src.ops -- ui and ops are allowed to import this, not the other
#   way round. Every function here is either pure I/O on local files
#   or a network call; nothing draws on screen except the REST
#   client's logger hooks, which are write-only
# ==============================================================

import json
import os
import platform
import random
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests

# ------------------------------------------------------------
# SECTION 1 | IDENTITY
# ------------------------------------------------------------

TOOL_NAME = "NiNog Raker"
TOOL_VERSION = "2.0"
MAINTAINER = "THE RATTIKANS"
MAINTAINER_CONTACT = "discord username: therattikans."
MAINTAINER_SERVER = "https://discord.gg/M2fGay6MVn"

PLATFORM_INFO = f"{platform.system()} {platform.release()} ({platform.machine()})"
PYTHON_INFO = f"Python {platform.python_version()}"

# ------------------------------------------------------------
# SECTION 2 | PATHS
# ------------------------------------------------------------

# nnv2.py lived at the project root, so `Path(__file__).resolve().parent`
# *was* the root. core.py lives one level deeper inside src/, so we have to
# walk up one more level or config/, snapshots/ and reportlog/ would get
# created inside src/ instead of next to main.py.
ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
SNAPSHOTS_DIR = ROOT / "snapshots"
REPORTLOG_DIR = ROOT / "reportlog"
EXPORTS_DIR = ROOT / "exports"
CONFIG_FILE = CONFIG_DIR / "config.json"
TOKENS_FILE = CONFIG_DIR / "tokens.json"
WHITELIST_FILE = CONFIG_DIR / "whitelist.json"
# Saved workflow chains live under config/ so the whole user-owned state
# tree stays in one place.
WORKFLOWS_DIR = CONFIG_DIR / "workflows"


def ensure_directories():
    for d in (CONFIG_DIR, SNAPSHOTS_DIR, REPORTLOG_DIR, WORKFLOWS_DIR, EXPORTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------
# SECTION 3 | REPORT LOGGER
# ------------------------------------------------------------

class ReportLogger:
    def __init__(self):
        REPORTLOG_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
        self.path = REPORTLOG_DIR / f"session_{stamp}.log"
        self.actions = 0

    def log(self, event, detail=""):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {event}" + (f" | {detail}" if detail else "")
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        if event.startswith("OP_") or event == "SETTING_CHANGED":
            self.actions += 1


# ------------------------------------------------------------
# SECTION 4 | CONFIG
# ------------------------------------------------------------

DEFAULT_CONFIG = {
    "version": TOOL_VERSION,
    "settings": {
        "auto_snapshot": True,
        "page_size": 10,
        "transition": "none",
        "transition_ms": 0,
        "theme": "rattikans",
        "layout": "list",
        "gradient": False,
        # --- client-side rate limiting ---
        "rate_limit": True,
        "rate_limit_level": "medium",
        "rl_wait_until": True,
        "rl_timeout": 25,
        "rl_spacing_ms": 150,
        "rl_safety_ms": 250,
        "rl_global_wait": True,
        "rl_max_429": 6,
        "rl_max_5xx": 2,
        "rl_jitter": True,
        # --- ban / kick detection ---
        "ban_watch": True,
        "ban_watch_secs": 15,
    },
}


def _atomic_json(path, data, private=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        if private and os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


class Config:
    def __init__(self):
        self.first_run = not CONFIG_FILE.exists()
        if self.first_run:
            self.data = json.loads(json.dumps(DEFAULT_CONFIG))
            self.data["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        else:
            try:
                loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("config root must be an object")
                self.data = loaded
            except (OSError, ValueError, json.JSONDecodeError):
                stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
                try:
                    CONFIG_FILE.replace(CONFIG_FILE.with_name(f"config.corrupt_{stamp}.json"))
                except OSError:
                    pass
                self.data = json.loads(json.dumps(DEFAULT_CONFIG))
        self._migrate()
        self.save()

    def _migrate(self):
        settings = self.data.get("settings")
        if not isinstance(settings, dict):
            settings = {}
            self.data["settings"] = settings

        if "animation_ms" in settings and "transition_ms" not in settings:
            settings["transition_ms"] = settings["animation_ms"]
        settings.pop("animation_ms", None)
        settings.pop("animation", None)
        settings.pop("bg_ascii", None)

        if "rate_limit_safety" in settings and "rl_safety_ms" not in settings:
            try:
                settings["rl_safety_ms"] = int(float(settings["rate_limit_safety"]) * 1000)
            except (TypeError, ValueError):
                settings["rl_safety_ms"] = 250
        settings.pop("rate_limit_safety", None)

        for key, value in DEFAULT_CONFIG["settings"].items():
            settings.setdefault(key, value)
        self.data["version"] = TOOL_VERSION

    def setting(self, key, fallback=None):
        return self.data.get("settings", {}).get(key, fallback)

    def set_setting(self, key, value):
        self.update_settings({key: value})

    def update_settings(self, values):
        self.data.setdefault("settings", {}).update(values)
        self.save()

    def save(self):
        _atomic_json(CONFIG_FILE, self.data)


# ------------------------------------------------------------
# SECTION 5 | TOKEN VAULT (persistence only)
# ------------------------------------------------------------
# The interactive add/delete/browse flows live in src/ops.py.
# Only the file-backed half is here, because it is plain I/O with
# no drawing involved.

def load_tokens():
    if not TOKENS_FILE.exists():
        return []
    try:
        data = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
        tokens = data.get("tokens", [])
        if not isinstance(tokens, list):
            raise ValueError("tokens must be a list")
        return [token for token in tokens if isinstance(token, dict)]
    except (OSError, ValueError, json.JSONDecodeError):
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
        try:
            TOKENS_FILE.replace(TOKENS_FILE.with_name(f"tokens.corrupt_{stamp}.json"))
        except OSError:
            pass
        return []


def save_tokens(tokens):
    _atomic_json(TOKENS_FILE, {"tokens": tokens}, private=True)


def unique_label(tokens, label):
    labels = {t.get("label", "") for t in tokens}
    if label not in labels:
        return label
    n = 2
    while f"{label} ({n})" in labels:
        n += 1
    return f"{label} ({n})"


def bot_display(user_json):
    if not isinstance(user_json, dict):
        return "?"
    name = user_json.get("global_name") or user_json.get("username", "?")
    discrim = user_json.get("discriminator", "0")
    if discrim not in (None, "", "0"):
        return f"{name}#{discrim}"
    return name


# ------------------------------------------------------------
# SECTION 6 | WHITELIST (persistence only)
# ------------------------------------------------------------

def load_whitelist():
    if not WHITELIST_FILE.exists():
        return []
    try:
        data = json.loads(WHITELIST_FILE.read_text(encoding="utf-8"))
        entries = data.get("entries", [])
        if not isinstance(entries, list):
            raise ValueError("entries must be a list")
        return [entry for entry in entries if isinstance(entry, dict)]
    except (OSError, ValueError, json.JSONDecodeError):
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
        try:
            WHITELIST_FILE.replace(
                WHITELIST_FILE.with_name(f"whitelist.corrupt_{stamp}.json")
            )
        except OSError:
            pass
        return []


def save_whitelist(entries):
    _atomic_json(WHITELIST_FILE, {"entries": entries})


def whitelist_ids():
    return {e.get("id") for e in load_whitelist() if e.get("id")}


# ------------------------------------------------------------
# SECTION 7 | RATE LIMITER
# ------------------------------------------------------------
# Three jobs: keep a minimum gap between requests, honour the bucket headers
# Discord returns, and back off on 429/5xx instead of hammering.
#
# "Off" means exactly that -- no spacing, no header waits, no 429 backoff.
# The settings screen shows a warning before allowing it, because with pacing
# disabled a mass op will trip Discord's limits almost immediately.

RATE_LIMIT_PRESETS = {
    "low": {
        "label": "Low | fastest, highest 429 risk",
        "spacing": 0.05, "safety": 0.10, "timeout": 25,
        "max_429": 5, "max_5xx": 2, "jitter": False,
        "wait_until": True, "global_wait": True,
    },
    "medium": {
        "label": "Medium | balanced (default)",
        "spacing": 0.15, "safety": 0.25, "timeout": 25,
        "max_429": 6, "max_5xx": 2, "jitter": True,
        "wait_until": True, "global_wait": True,
    },
    "high": {
        "label": "High | conservative",
        "spacing": 0.40, "safety": 0.50, "timeout": 30,
        "max_429": 8, "max_5xx": 3, "jitter": True,
        "wait_until": True, "global_wait": True,
    },
    "max": {
        "label": "Max | slowest, safest",
        "spacing": 1.00, "safety": 1.00, "timeout": 30,
        "max_429": 10, "max_5xx": 3, "jitter": True,
        "wait_until": True, "global_wait": True,
    },
}

# Discord permission bit positions, used by the workflow engine to predict
# which steps a non-administrator bot will not be able to run.
PERMISSION_BITS = {
    "CREATE_INSTANT_INVITE": 1 << 0,
    "KICK_MEMBERS": 1 << 1,
    "BAN_MEMBERS": 1 << 2,
    "ADMINISTRATOR": 1 << 3,
    "MANAGE_CHANNELS": 1 << 4,
    "MANAGE_GUILD": 1 << 5,
    "VIEW_AUDIT_LOG": 1 << 7,
    "VIEW_CHANNEL": 1 << 10,
    "SEND_MESSAGES": 1 << 11,
    "MANAGE_MESSAGES": 1 << 13,
    "MANAGE_NICKNAMES": 1 << 27,
    "MANAGE_ROLES": 1 << 28,
    "MANAGE_WEBHOOKS": 1 << 29,
    "MODERATE_MEMBERS": 1 << 40,
}


def apply_rate_level(config, level):
    """Write a preset's concrete values into config.

    The advanced page edits these concrete values directly, so a level is
    really a starting point rather than a live override -- changing the level
    resets the advanced knobs, and "Reset To Level" in that page restores them.
    """
    if level not in RATE_LIMIT_PRESETS:
        level = "medium"
    preset = RATE_LIMIT_PRESETS[level]
    values = {
        "rate_limit_level": level,
        "rl_wait_until": preset["wait_until"],
        "rl_timeout": preset["timeout"],
        "rl_spacing_ms": int(preset["spacing"] * 1000),
        "rl_safety_ms": int(preset["safety"] * 1000),
        "rl_global_wait": preset["global_wait"],
        "rl_max_429": preset["max_429"],
        "rl_max_5xx": preset["max_5xx"],
        "rl_jitter": preset["jitter"],
    }
    if hasattr(config, "update_settings"):
        config.update_settings(values)
    else:
        for key, value in values.items():
            config.set_setting(key, value)
    return preset


class RateLimiter:
    """Client-side pacing and backoff for DiscordREST."""

    def _configure(self, config):
        level = str(config.setting("rate_limit_level", "medium") or "medium").lower()
        preset = RATE_LIMIT_PRESETS.get(level, RATE_LIMIT_PRESETS["medium"])

        def number(key, default, minimum=0):
            try:
                return max(minimum, int(config.setting(key, default)))
            except (TypeError, ValueError, OverflowError):
                return max(minimum, int(default))

        def flag(key, default):
            value = config.setting(key, default)
            if isinstance(value, str):
                return value.strip().lower() not in ("", "0", "false", "no", "off")
            return bool(value)

        self.level = preset["label"]
        self.enabled = flag("rate_limit", True)
        self.wait_until = flag("rl_wait_until", preset["wait_until"])
        self.global_wait = flag("rl_global_wait", preset["global_wait"])
        self.jitter = flag("rl_jitter", preset["jitter"])
        self.timeout = number("rl_timeout", preset["timeout"], 5)
        self.spacing = number("rl_spacing_ms", 150) / 1000.0
        self.safety = number("rl_safety_ms", 250) / 1000.0
        self.max_429 = number("rl_max_429", preset["max_429"])
        self.max_5xx = number("rl_max_5xx", preset["max_5xx"])

    # Public alias. ops.py syncs settings through limiter.configure() --
    # without this name every settings change raised AttributeError and
    # killed the session.
    configure = _configure

    def __init__(self, config=None, safety=0.25):
        self.enabled = True
        self.level = RATE_LIMIT_PRESETS["medium"]["label"]
        self.wait_until = True
        self.global_wait = True
        self.jitter = True
        self.timeout = 25
        self.spacing = 0.15
        self.safety = float(safety or 0.0)
        self.max_429 = 6
        self.max_5xx = 2

        self._last = 0.0
        self._bucket_until = 0.0
        self._global_until = 0.0
        self._lock = threading.RLock()

        # session counters, surfaced on the ratelimit settings page
        self.waits = 0
        self.slept_for = 0.0
        self.hit_429 = 0

        if config is not None:
            self._configure(config)

    # Public on purpose: DiscordREST calls this directly when it needs to
    # back off between a 429 and its retry.
    def sleep_for(self, seconds):
        # Jitter spreads retries out so several parallel clients do not all
        # wake on the same tick and re-collide.
        if self.jitter and seconds > 0:
            seconds += seconds * random.uniform(0.0, 0.15) + random.uniform(0.0, 0.05)
        if seconds > 0:
            time.sleep(seconds)
            with self._lock:
                self.slept_for += seconds

    def acquire(self):
        """Block until the next request is allowed. No-op when disabled."""
        if not self.enabled:
            return
        with self._lock:
            now = time.monotonic()
            target = now
            if self.spacing > 0:
                target = max(target, self._last + self.spacing)
            if self.wait_until:
                target = max(target, self._bucket_until)
            if self.global_wait:
                target = max(target, self._global_until)
            if target > now:
                self.waits += 1
                self.sleep_for(target - now)
            self._last = time.monotonic()

    def note_response(self, r):
        """Read bucket headers off a non-429 response and schedule the wait."""
        if not self.enabled or not self.wait_until:
            return
        if r.headers.get("X-RateLimit-Remaining") != "0":
            return
        reset_after = r.headers.get("X-RateLimit-Reset-After")
        reset_at = r.headers.get("X-RateLimit-Reset")
        until = None
        if reset_after:
            try:
                until = time.monotonic() + float(reset_after)
            except (TypeError, ValueError):
                until = None
        elif reset_at:
            # Reset is an epoch timestamp on Discord's clock; convert the
            # remaining wall-clock delta into monotonic time.
            try:
                until = time.monotonic() + (float(reset_at) - time.time())
            except (TypeError, ValueError):
                until = None
        if until is not None:
            self._bucket_until = max(self._bucket_until, until + self.safety)

    def note_429(self, r):
        """Record a 429 and return how long to sleep before retrying."""
        self.hit_429 += 1
        try:
            body = r.json()
        except Exception:
            body = {}
        raw = body.get("retry_after") or r.headers.get("Retry-After") or 1.0
        try:
            retry = float(raw)
        except (TypeError, ValueError):
            retry = 1.0
        is_global = bool(body.get("global")) or \
            str(r.headers.get("X-RateLimit-Global", "")).lower() == "true"
        until = time.monotonic() + retry + self.safety
        if is_global:
            self._global_until = max(self._global_until, until)
        else:
            self._bucket_until = max(self._bucket_until, until)
        return retry + self.safety

    def snapshot(self):
        return {
            "enabled": self.enabled,
            "level": self.level,
            "spacing": self.spacing,
            "safety": self.safety,
            "timeout": self.timeout,
            "waits": self.waits,
            "slept_for": round(self.slept_for, 2),
            "hit_429": self.hit_429,
        }


# ------------------------------------------------------------
# SECTION 8 | DISCORD REST CLIENT
# ------------------------------------------------------------

class ApiNetworkError(Exception):
    pass


class DiscordREST:
    BASE = "https://discord.com/api/v10"

    def __init__(self, token, logger=None, safety=0.25, config=None):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                "User-Agent": f"NiNogRaker/{TOOL_VERSION} (RATTIKANS)",
            }
        )
        self.external = requests.Session()
        self.external.headers.update(
            {"User-Agent": f"NiNogRaker/{TOOL_VERSION} (RATTIKANS)"}
        )
        self.logger = logger
        # The limiter owns pacing, header waits and retry backoff. `safety` is
        # only consulted when no config object is supplied (token validation
        # before a vault entry exists).
        self.limiter = RateLimiter(config=config, safety=safety)

    def _throttle(self):
        self.limiter.acquire()

    def request(self, method, path, *, params=None, body=None, reason="", _attempt=0):
        lim = self.limiter
        lim.acquire()
        headers = None
        if reason:
            headers = {"X-Audit-Log-Reason": quote(str(reason)[:512], safe="")}
        try:
            r = self.session.request(
                method,
                self.BASE + path,
                params=params,
                json=body,
                headers=headers,
                timeout=lim.timeout,
            )
        except requests.RequestException as e:
            raise ApiNetworkError(f"connection failed: {e}") from e

        if r.status_code == 429:
            # With pacing disabled the 429 is handed straight back to the
            # caller -- that is the risk the settings screen warns about.
            if not lim.enabled:
                if self.logger:
                    self.logger.log("RATE_LIMIT", f"429 unhandled (pacing off) {method} {path}")
                return r
            if _attempt >= lim.max_429:
                if self.logger:
                    self.logger.log(
                        "RATE_LIMIT", f"429 giving up after {_attempt} | {method} {path}"
                    )
                return r
            wait = lim.note_429(r)
            if self.logger:
                self.logger.log(
                    "RATE_LIMIT",
                    f"{method} {path} | waiting {wait:.2f}s | attempt {_attempt + 1}",
                )
            lim.sleep_for(wait)
            return self.request(
                method, path, params=params, body=body, reason=reason,
                _attempt=_attempt + 1
            )

        lim.note_response(r)

        if r.status_code >= 500 and _attempt < lim.max_5xx:
            lim.sleep_for(min(4.0, 2 ** _attempt))
            return self.request(
                method, path, params=params, body=body, reason=reason,
                _attempt=_attempt + 1
            )
        return r

    def get_me(self):
        return self.request("GET", "/users/@me")

    def get_guilds(self):
        return self.request("GET", "/users/@me/guilds", params={"limit": 200})

    def get_guild(self, guild_id):
        return self.request("GET", f"/guilds/{guild_id}", params={"with_counts": "true"})

    def patch_guild(self, guild_id, body):
        return self.request("PATCH", f"/guilds/{guild_id}", body=body)

    def get_channels(self, guild_id):
        return self.request("GET", f"/guilds/{guild_id}/channels")

    def create_channel(self, guild_id, body):
        return self.request("POST", f"/guilds/{guild_id}/channels", body=body)

    def delete_channel(self, channel_id, reason=""):
        return self.request("DELETE", f"/channels/{channel_id}", reason=reason)

    def patch_channel(self, channel_id, body):
        return self.request("PATCH", f"/channels/{channel_id}", body=body)

    def get_roles(self, guild_id):
        return self.request("GET", f"/guilds/{guild_id}/roles")

    def create_role(self, guild_id, body, reason=""):
        return self.request(
            "POST", f"/guilds/{guild_id}/roles", body=body, reason=reason
        )

    def delete_role(self, guild_id, role_id):
        return self.request("DELETE", f"/guilds/{guild_id}/roles/{role_id}")

    def patch_role(self, guild_id, role_id, body):
        return self.request("PATCH", f"/guilds/{guild_id}/roles/{role_id}", body=body)

    def patch_role_positions(self, guild_id, positions):
        return self.request("PATCH", f"/guilds/{guild_id}/roles", body=positions)

    def get_members_page(self, guild_id, after="0"):
        return self.request(
            "GET",
            f"/guilds/{guild_id}/members",
            params={"limit": 1000, "after": after},
        )

    def get_all_members(self, guild_id, on_page=None):
        members = []
        after = "0"
        while True:
            r = self.get_members_page(guild_id, after)
            if r.status_code != 200:
                return r, members
            batch = r.json()
            if not isinstance(batch, list) or not batch:
                return r, members
            members.extend(batch)
            if on_page:
                on_page(len(members))
            if len(batch) < 1000:
                return r, members
            next_after = str((batch[-1].get("user") or {}).get("id", ""))
            if not next_after or next_after == after:
                return r, members
            after = next_after

    def get_member(self, guild_id, user_id):
        return self.request("GET", f"/guilds/{guild_id}/members/{user_id}")

    def modify_member(self, guild_id, user_id, body):
        return self.request("PATCH", f"/guilds/{guild_id}/members/{user_id}", body=body)

    def kick_member(self, guild_id, user_id):
        return self.request("DELETE", f"/guilds/{guild_id}/members/{user_id}")

    def ban_member(self, guild_id, user_id, delete_message_seconds=0):
        # delete_message_days was removed from the v10 API; seconds is the
        # only accepted field now.
        return self.request(
            "PUT",
            f"/guilds/{guild_id}/bans/{user_id}",
            body={"delete_message_seconds": int(delete_message_seconds)},
        )

    def unban_member(self, guild_id, user_id):
        return self.request("DELETE", f"/guilds/{guild_id}/bans/{user_id}")

    def get_bans_page(self, guild_id, after="0"):
        return self.request(
            "GET",
            f"/guilds/{guild_id}/bans",
            params={"limit": 1000, "after": after},
        )

    def get_all_bans(self, guild_id, on_page=None):
        bans = []
        after = "0"
        while True:
            r = self.get_bans_page(guild_id, after)
            if r.status_code != 200:
                return r, bans
            batch = r.json()
            if not isinstance(batch, list) or not batch:
                return r, bans
            bans.extend(batch)
            if on_page:
                on_page(len(bans))
            if len(batch) < 1000:
                return r, bans
            next_after = str((batch[-1].get("user") or {}).get("id", ""))
            if not next_after or next_after == after:
                return r, bans
            after = next_after

    def get_messages(self, channel_id, limit=100):
        return self.request(
            "GET", f"/channels/{channel_id}/messages", params={"limit": limit}
        )

    def send_message(self, channel_id, content):
        return self.request(
            "POST", f"/channels/{channel_id}/messages", body={"content": content}
        )

    def bulk_delete(self, channel_id, message_ids):
        return self.request(
            "POST",
            f"/channels/{channel_id}/messages/bulk-delete",
            body={"messages": message_ids},
        )

    def delete_message(self, channel_id, message_id):
        return self.request("DELETE", f"/channels/{channel_id}/messages/{message_id}")

    def create_webhook(self, channel_id, name):
        return self.request("POST", f"/channels/{channel_id}/webhooks", body={"name": name})

    def get_guild_webhooks(self, guild_id):
        return self.request("GET", f"/guilds/{guild_id}/webhooks")

    def get_channel_webhooks(self, channel_id):
        # The guild endpoint can miss hooks the token cannot see guild-wide;
        # per-channel sweeps catch the rest.
        return self.request("GET", f"/channels/{channel_id}/webhooks")

    def get_gateway(self):
        return self.request("GET", "/gateway")

    def delete_webhook_id(self, webhook_id):
        return self.request("DELETE", f"/webhooks/{webhook_id}")

    def execute_webhook(self, webhook_url, body, _attempt=0):
        # Webhook execution does not go through request() because the URL is
        # absolute rather than an API path, so the limiter is applied here by
        # hand. Skipping this was how webhook spam escaped pacing entirely.
        lim = self.limiter
        lim.acquire()
        try:
            r = self.external.post(webhook_url, json=body, timeout=lim.timeout)
        except requests.RequestException as e:
            raise ApiNetworkError(f"webhook failed: {e}") from e
        if r.status_code == 429:
            if not lim.enabled or _attempt >= lim.max_429:
                return r
            wait = lim.note_429(r)
            lim.sleep_for(wait)
            return self.execute_webhook(webhook_url, body, _attempt + 1)
        lim.note_response(r)
        return r

    def delete_webhook_by_url(self, webhook_url):
        lim = self.limiter
        lim.acquire()
        try:
            r = self.external.delete(webhook_url, timeout=lim.timeout)
        except requests.RequestException as e:
            raise ApiNetworkError(f"webhook delete failed: {e}") from e
        lim.note_response(r)
        return r

    def get_dm_channels(self):
        return self.request("GET", "/users/@me/channels")

    def create_dm(self, user_id):
        return self.request(
            "POST", "/users/@me/channels", body={"recipient_id": user_id}
        )

    def get_onboarding(self, guild_id):
        return self.request("GET", f"/guilds/{guild_id}/onboarding")

    def put_onboarding(self, guild_id, body):
        return self.request("PUT", f"/guilds/{guild_id}/onboarding", body=body)

    def create_invite(self, channel_id, max_age=86400):
        return self.request(
            "POST",
            f"/channels/{channel_id}/invites",
            body={"max_age": max_age, "max_uses": 0},
        )

    def download(self, url, timeout=25):
        lim = self.limiter
        lim.acquire()
        try:
            r = self.external.get(url, timeout=timeout or lim.timeout)
        except requests.RequestException as e:
            raise ApiNetworkError(f"download failed: {e}") from e
        lim.note_response(r)
        return r


def dm_send(rest, user_id, content):
    r = rest.create_dm(user_id)
    if r.status_code != 200:
        return False
    dm_id = r.json().get("id")
    if not dm_id:
        return False
    m = rest.send_message(dm_id, content[:2000])
    return m.status_code in (200, 201)


# ------------------------------------------------------------
# SECTION 9 | GUILD WATCHDOG (ban / kick detection)
# ------------------------------------------------------------
# Polls the bot's own member object on an interval. When the guild starts
# answering 404/403 for the bot itself, the bot has been kicked or banned,
# so the context flag flips and the UI sounds off on its next render.

class GuildWatchdog:
    def __init__(self, rest, guild_id, user_id, interval=15, on_lost=None):
        self.rest = rest
        self.guild_id = guild_id
        self.user_id = user_id
        self.interval = max(5, int(interval or 15))
        self.on_lost = on_lost
        self._stop = threading.Event()
        self._thread = None
        self.last_status = "unknown"

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def check_once(self):
        """One probe. Returns 'ok', 'lost', or 'unknown' (network error)."""
        try:
            r = self.rest.get_member(self.guild_id, self.user_id)
        except ApiNetworkError:
            self.last_status = "unknown"
            return "unknown"
        if r.status_code == 200:
            self.last_status = "ok"
            return "ok"
        if r.status_code in (403, 404):
            self.last_status = "lost"
            return "lost"
        self.last_status = f"http {r.status_code}"
        return "unknown"

    def _loop(self):
        # First probe quickly so a stale session is caught early, then settle
        # into the configured interval.
        while not self._stop.wait(2.0):
            status = self.check_once()
            if status == "lost":
                if self.on_lost:
                    try:
                        self.on_lost()
                    except Exception:
                        pass
                return
            self._stop.wait(self.interval)


def webhook_url_of(hook):
    """Best-effort executable URL for a webhook object from the REST API.

    Bot-authenticated webhook objects normally carry `token`, so the URL can
    always be rebuilt; `url` itself is only present in some auth flows.
    """
    url = hook.get("url")
    if url:
        return url
    wid, token = hook.get("id"), hook.get("token")
    if wid and token:
        return f"https://discord.com/api/webhooks/{wid}/{token}"
    return None


# ------------------------------------------------------------
# SECTION 10 | CLIPBOARD
# ------------------------------------------------------------

def copy_to_clipboard(text):
    """Copy text to the system clipboard with platform fallbacks.

    Returns (ok, detail). Headless machines simply have no clipboard, so a
    failure here is normal and the caller should offer a file instead.
    """
    try:
        import pyperclip  # type: ignore
        pyperclip.copy(text)
        return True, "pyperclip"
    except Exception:
        pass
    import subprocess
    candidates = []
    if os.environ.get("TERMUX_VERSION") or "com.termux" in os.environ.get("PREFIX", ""):
        # Termux:API clipboard, the only clipboard a phone session has.
        candidates.append((["termux-clipboard-set"], None))
    if platform.system() == "Windows":
        candidates.append((["clip"], None))
    elif platform.system() == "Darwin":
        candidates.append((["pbcopy"], None))
    else:
        candidates.append((["xclip", "-selection", "clipboard"], None))
        candidates.append((["wl-copy"], None))
        candidates.append((["termux-clipboard-set"], None))  # PREFIX not always set
    for cmd, _ in candidates:
        try:
            proc = subprocess.run(cmd, input=text.encode("utf-8"),
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=5)
            if proc.returncode == 0:
                return True, cmd[0]
        except Exception:
            continue
    return False, "no clipboard tool found (pyperclip, clip, pbcopy, xclip, wl-copy)"


# ------------------------------------------------------------
# SECTION 11 | OPTIONAL GATEWAY PRESENCE (best effort)
# ------------------------------------------------------------
# REST alone cannot say who is online; presence lives on the gateway behind
# the GUILD_PRESENCES intent. A short-lived official bot gateway connection
# reads initial GUILD_CREATE presence data and subsequent PRESENCE_UPDATE
# events. Any failure returns None and the member picker notes that presence
# is unavailable.

def fetch_online_ids(token, guild_id, timeout=8.0):
    result = {"ids": None}

    def _worker():
        try:
            import asyncio
            import websockets  # type: ignore
        except Exception:
            return

        async def _run():
            online = set()
            sequence = {"value": None}
            target_seen = False
            async with websockets.connect(
                    "wss://gateway.discord.gg/?v=10&encoding=json",
                    max_size=8 * 1024 * 1024) as ws:
                hello = json.loads(await ws.recv())
                interval = (hello.get("d") or {}).get("heartbeat_interval", 40000) / 1000.0

                async def _heart():
                    try:
                        while True:
                            await asyncio.sleep(interval)
                            await ws.send(json.dumps({"op": 1, "d": sequence["value"]}))
                    except Exception:
                        return

                heart = asyncio.create_task(_heart())
                await ws.send(json.dumps({
                    "op": 2,
                    "d": {
                        "token": token,
                        "intents": (1 << 0) | (1 << 8),
                        "properties": {"os": "linux", "browser": "ninog", "device": "ninog"},
                    },
                }))
                end = asyncio.get_running_loop().time() + timeout
                try:
                    while asyncio.get_running_loop().time() < end:
                        remaining = end - asyncio.get_running_loop().time()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=min(1.5, remaining))
                        except asyncio.TimeoutError:
                            if target_seen:
                                break
                            continue
                        ev = json.loads(raw)
                        if ev.get("s") is not None:
                            sequence["value"] = ev["s"]
                        event, data = ev.get("t"), ev.get("d") or {}
                        if event == "GUILD_CREATE" and str(data.get("id")) == str(guild_id):
                            target_seen = True
                            for presence in data.get("presences", []):
                                uid = (presence.get("user") or {}).get("id")
                                if uid and presence.get("status") in ("online", "idle", "dnd"):
                                    online.add(uid)
                        elif event == "PRESENCE_UPDATE" and str(data.get("guild_id")) == str(guild_id):
                            uid = (data.get("user") or {}).get("id")
                            if uid:
                                if data.get("status") in ("online", "idle", "dnd"):
                                    online.add(uid)
                                else:
                                    online.discard(uid)
                finally:
                    heart.cancel()
            result["ids"] = online if target_seen else None

        try:
            asyncio.run(_run())
        except Exception:
            result["ids"] = None

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout + 5.0)
    return result["ids"]


# ------------------------------------------------------------
# SECTION 12 | SHA-256 + CRASH REPORTER
# ------------------------------------------------------------
# When anything escapes the session loop, this builds a self-contained
# bundle a maintainer can debug from cold: full traceback, platform info,
# verbatim source of every .py file, a SHA-256 manifest of those files,
# a raw snapshot of config/ (token values redacted, integrity hashes kept),
# session logs, and live tool state.

import hashlib
import traceback as _traceback_mod

TOKEN_RELIEF_RE = re.compile(r"[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{20,}")
# A webhook URL's last path segment is its token; everything before it is
# safe to keep because the id alone cannot execute the hook.
WEBHOOK_TOKEN_RE = re.compile(
    r"(discord(?:app)?\.com/api/webhooks/\d+/)[A-Za-z0-9_\-]{10,}")
_SECRET_KEY_HINTS = ("token", "secret", "pass", "session", "auth")


def _secret_values(node):
    """Literal secret strings from a config tree (values under key hints)."""
    found = []
    if isinstance(node, dict):
        for k, v in node.items():
            if any(h in str(k).lower() for h in _SECRET_KEY_HINTS):
                if isinstance(v, str) and len(v) >= 10:
                    found.append(v)
                else:
                    found.extend(_secret_values(v))
            else:
                found.extend(_secret_values(v))
    elif isinstance(node, (list, tuple)):
        for v in node:
            found.extend(_secret_values(v))
    return found


def mask_secret_text(text, config=None):
    """Make free text safe to ship in a crash bundle.

    Layers: Discord bot-token shapes, webhook-url tokens, then any literal
    secret value the live config holds (covers vault formats the regex
    cannot guess). Static earlier versions only scrubbed config_snapshot/
    files — error.txt's settings dump and the copied session logs went
    out raw, which is how real tokens escaped inside 'debug' bundles.
    """
    text = str(text)
    text = TOKEN_RELIEF_RE.sub(
        lambda m: m.group(0)[:6] + "...(REDACTED)...", text)
    text = WEBHOOK_TOKEN_RE.sub(r"\1(REDACTED)", text)
    if config is not None:
        data = getattr(config, "data", None)
        for sec in _secret_values(data):
            text = text.replace(sec, sec[:4] + "...(REDACTED)...")
    return text


def sha256_file(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(65536), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def sha256_text(text):
    return hashlib.sha256(str(text).encode("utf-8", "replace")).hexdigest()


def source_files():
    """Every .py shipped with the tool: main.py plus src/*.py."""
    files = []
    main = ROOT / "main.py"
    if main.exists():
        files.append(main)
    src = ROOT / "src"
    if src.exists():
        files.extend(sorted(p for p in src.glob("*.py") if p.is_file()))
    else:
        # Flat layout fallback: every sibling .py that is not main.py.
        files.extend(sorted(p for p in ROOT.glob("*.py") if p.is_file() and p != main))
    return files


def _redact_config_bytes(path, raw):
    """Keep corrupted files readable for debugging but never leak tokens."""
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        return b"(binary file omitted)"
    if path.name.startswith("tokens"):
        try:
            data = json.loads(text)
            for token in data.get("tokens", []):
                value = str(token.get("data", ""))
                token["data"] = (
                    value[:6] + "..." + value[-4:] if len(value) > 12 else "(redacted)"
                )
            return json.dumps(data, indent=2).encode("utf-8")
        except Exception:
            return b"(corrupt token vault omitted)"
    return mask_secret_text(text).encode("utf-8", "replace")


def build_crash_report(exc_type, exc, tb, ctx=None, origin="uncaught"):
    """Write the full diagnostic bundle. Returns the report directory."""
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
    report = REPORTLOG_DIR / "crashes" / f"crash_{stamp}"
    src_dir = report / "source"
    cfg_dir = report / "config_snapshot"
    log_dir = report / "session_logs"
    for d in (report, src_dir, cfg_dir, log_dir):
        d.mkdir(parents=True, exist_ok=True)

    tb_text = "".join(_traceback_mod.format_exception(exc_type, exc, tb))

    # -- error.txt: everything about the failure itself ----------------
    lines = [
        "NiNog Raker crash report",
        "=" * 60,
        f"tool      : {TOOL_NAME} v{TOOL_VERSION}",
        f"maintainer: {MAINTAINER}",
        f"when      : {stamp}",
        f"origin    : {origin}",
        f"platform  : {PLATFORM_INFO}",
        f"python    : {PYTHON_INFO}",
        f"executable: {sys.executable}",
        f"cwd       : {Path.cwd()}",
        "",
        "EXCEPTION",
        "-" * 60,
        f"{getattr(exc_type, '__name__', exc_type)}: {exc}",
        "",
        "TRACEBACK",
        "-" * 60,
        tb_text,
    ]
    if ctx is not None:
        lines += [
            "LIVE CONTEXT",
            "-" * 60,
            f"guild     : {(ctx.guild or {}).get('name')} ({(ctx.guild or {}).get('id')})",
            f"bot       : {bot_display(ctx.me) if ctx.me else None}",
            f"perms     : {ctx.perms}",
            f"guild_lost: {ctx.guild_lost}",
            f"settings  : {json.dumps((ctx.config.data if ctx.config else {}), indent=2)[:4000]}",
        ]
        lim = getattr(ctx.rest, "limiter", None) if ctx.rest else None
        if lim is not None:
            lines.append(f"rate limit: {json.dumps(lim.snapshot())}")
        lines.append(f"alert queue: {ctx.pending_alerts!r}")
    (report / "error.txt").write_text(
        mask_secret_text("\n".join(lines), getattr(ctx, "config", None)),
        encoding="utf-8")

    # -- source/: verbatim copies + sha256 manifest --------------------
    manifest = {}
    for path in source_files():
        rel = path.relative_to(ROOT)
        dest = src_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.write_bytes(path.read_bytes())
        except OSError as e:
            dest.write_text(f"(unreadable: {e})", encoding="utf-8")
        manifest[str(rel)] = {
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size if path.exists() else None,
        }
    (report / "source_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")

    # -- config_snapshot/: raw tree, tokens redacted, hashes kept ------
    cfg_manifest = {}
    if CONFIG_DIR.exists():
        for path in sorted(CONFIG_DIR.rglob("*")):
            rel = path.relative_to(CONFIG_DIR)
            cfg_manifest[str(rel)] = {"sha256": sha256_file(path)} if path.is_file() else {"dir": True}
            if path.is_dir():
                (cfg_dir / rel).mkdir(parents=True, exist_ok=True)
                continue
            dest = cfg_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                dest.write_bytes(_redact_config_bytes(path, path.read_bytes()))
            except OSError as e:
                dest.write_text(f"(unreadable: {e})", encoding="utf-8")
    (report / "config_manifest.json").write_text(
        json.dumps(cfg_manifest, indent=2), encoding="utf-8")

    # -- session_logs/: everything the report logger produced ----------
    try:
        for path in sorted(REPORTLOG_DIR.glob("*.log")):
            raw = path.read_bytes().decode("utf-8", "replace")
            (log_dir / path.name).write_text(
                mask_secret_text(raw, getattr(ctx, "config", None)),
                encoding="utf-8")
    except OSError:
        pass

    # -- README: where to send it --------------------------------------
    (report / "SEND_THIS_TO_THE_RATTIKANS.txt").write_text(
        "NiNog Raker generated this because something broke.\n"
        "\n"
        "Send this ENTIRE crash folder to THE RATTIKANS:\n"
        f"  - discord username : {MAINTAINER_CONTACT}\n"
        f"  - official server  : {MAINTAINER_SERVER}\n"
        "\n"
        "It contains the full traceback, the exact source you ran (with\n"
        "sha256 hashes), a redacted snapshot of your config folder, and\n"
        "your session logs. Bot token values were masked before writing.\n"
        "\n"
        "This comprehensive report guarantees that a fix will be supplied.\n",
        encoding="utf-8")
    return report


_CTX_REF = {"ctx": None}


def bind_crash_context(ctx):
    _CTX_REF["ctx"] = ctx


def _excepthook(exc_type, exc, tb):
    try:
        report = build_crash_report(exc_type, exc, tb, ctx=_CTX_REF.get("ctx"))
        print(f"\n[NiNog Raker] crashed | report written to:\n  {report}\n"
              f"  send it to {MAINTAINER_CONTACT} or {MAINTAINER_SERVER}")
    except Exception:
        print("\n[NiNog Raker] crashed and the crash reporter also failed:")
        _traceback_mod.print_exception(exc_type, exc, tb)


def install_crash_hooks():
    """Route uncaught main-thread and thread exceptions into the reporter."""
    sys.excepthook = _excepthook

    def _thread_hook(args):
        _excepthook(args.exc_type, args.exc_value, args.exc_traceback)

    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_hook


# ------------------------------------------------------------
# SECTION 8 | RUNTIME CONTEXT
# ------------------------------------------------------------
# Plain data bag threaded through every op. Held here so ui and ops
# can both accept it without importing each other.

class Ctx:
    def __init__(self, config, logger):
        self.config = config
        self.logger = logger
        self.rest = None
        self.me = None
        self.meta = None
        self.guild = None
        # Effective permission bits for the bot in the selected guild, taken
        # from the guild list response. The workflow engine uses this to
        # predict which steps a non-administrator bot cannot run.
        self.perms = 0
        # Ban/kick detection. The watchdog thread flips `guild_lost` when the
        # bot stops being a member of the selected guild; `pending_alerts`
        # carries (kind, detail) tuples the UI drains on the next render.
        self.watchdog = None
        self.guild_lost = False
        self.pending_alerts = []
        # Trigger bookkeeping for the workflow engine: flags set by events,
        # per-workflow last-run timestamps for interval triggers.
        self.just_selected_guild = False
        self.trigger_state = {}
