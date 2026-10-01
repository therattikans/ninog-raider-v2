import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ninog"))

from src import core


class Response:
    def __init__(self, status=200, headers=None, data=None):
        self.status_code = status
        self.headers = headers or {}
        self._data = data
        self.text = ""

    def json(self):
        return self._data


class ConfigTests(unittest.TestCase):
    def test_legacy_values_are_migrated_before_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "config.json"
            config_file.write_text(json.dumps({
                "settings": {
                    "animation_ms": 725,
                    "rate_limit_safety": 0.8,
                }
            }), encoding="utf-8")
            with patch.object(core, "CONFIG_FILE", config_file):
                config = core.Config()
            self.assertEqual(config.setting("transition_ms"), 725)
            self.assertEqual(config.setting("rl_safety_ms"), 800)
            self.assertNotIn("animation_ms", config.data["settings"])
            self.assertNotIn("rate_limit_safety", config.data["settings"])

    def test_non_object_config_is_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "config.json"
            config_file.write_text("[]", encoding="utf-8")
            with patch.object(core, "CONFIG_FILE", config_file):
                config = core.Config()
            self.assertEqual(config.data["version"], core.TOOL_VERSION)
            self.assertEqual(len(list(Path(tmp).glob("config.corrupt_*.json"))), 1)


class RestTests(unittest.TestCase):
    def make_rest(self):
        rest = core.DiscordREST("secret", safety=0)
        rest.limiter.enabled = False
        rest.session.request = Mock(return_value=Response())
        return rest

    def test_audit_reason_is_a_header_not_json(self):
        rest = self.make_rest()
        rest.create_role("1", {"name": "role"}, reason="space & slash/")
        kwargs = rest.session.request.call_args.kwargs
        self.assertEqual(kwargs["json"], {"name": "role"})
        self.assertEqual(kwargs["headers"]["X-Audit-Log-Reason"], "space%20%26%20slash%2F")

    def test_role_positions_are_sent_as_an_array(self):
        rest = self.make_rest()
        positions = [{"id": "2", "position": 3}]
        rest.patch_role_positions("1", positions)
        self.assertIs(rest.session.request.call_args.kwargs["json"], positions)

    def test_malformed_rate_values_fall_back(self):
        config = Mock()
        values = {"rl_timeout": None, "rl_spacing_ms": "bad", "rate_limit": "false"}
        config.setting.side_effect = lambda key, default=None: values.get(key, default)
        limiter = core.RateLimiter(config=config)
        self.assertEqual(limiter.timeout, 25)
        self.assertEqual(limiter.spacing, 0.15)
        self.assertFalse(limiter.enabled)

    def test_absolute_download_does_not_use_authenticated_session(self):
        rest = self.make_rest()
        rest.external.get = Mock(return_value=Response())
        rest.download("https://cdn.example/file")
        rest.external.get.assert_called_once()
        rest.session.request.assert_not_called()
        self.assertNotIn("Authorization", rest.external.headers)


class WatchdogTests(unittest.TestCase):
    def test_watchdog_uses_the_bot_snowflake(self):
        rest = Mock()
        rest.get_member.return_value = Response(200)
        watchdog = core.GuildWatchdog(rest, "guild", "bot", interval=5)
        self.assertEqual(watchdog.check_once(), "ok")
        rest.get_member.assert_called_once_with("guild", "bot")


if __name__ == "__main__":
    unittest.main()
