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

    def test_member_role_uses_idempotent_endpoint(self):
        rest = self.make_rest()
        rest.add_member_role("1", "2", "3", reason="restore")
        args = rest.session.request.call_args.args
        kwargs = rest.session.request.call_args.kwargs
        self.assertEqual(args[:2], ("PUT", "https://discord.com/api/v10/guilds/1/members/2/roles/3"))
        self.assertIsNone(kwargs["json"])
        self.assertEqual(kwargs["headers"]["X-Audit-Log-Reason"], "restore")

    def test_bulk_ban_caps_payload_and_message_window(self):
        rest = self.make_rest()
        rest.bulk_ban("1", range(250), delete_message_seconds=999999)
        args = rest.session.request.call_args.args
        body = rest.session.request.call_args.kwargs["json"]
        self.assertEqual(args[:2], ("POST", "https://discord.com/api/v10/guilds/1/bulk-ban"))
        self.assertEqual(len(body["user_ids"]), 200)
        self.assertEqual(body["delete_message_seconds"], 604800)

    def test_invalid_webhook_url_is_rejected_before_network(self):
        rest = self.make_rest()
        rest.external.post = Mock()
        with self.assertRaises(core.ApiNetworkError):
            rest.execute_webhook("https://example.com/api/webhooks/1/token", {"content": "x"})
        rest.external.post.assert_not_called()

    def test_route_keys_keep_major_ids_and_normalize_message_ids(self):
        one = core.DiscordREST._route_key("DELETE", "/channels/123/messages/456")
        two = core.DiscordREST._route_key("DELETE", "/channels/123/messages/789")
        other_channel = core.DiscordREST._route_key("DELETE", "/channels/999/messages/456")
        self.assertEqual(one, two)
        self.assertNotEqual(one, other_channel)

    def test_bucket_hash_is_scoped_to_major_resource(self):
        limiter = core.RateLimiter(safety=0)
        headers = {"X-RateLimit-Bucket": "same", "X-RateLimit-Remaining": "1"}
        limiter.note_response(Response(headers=headers), "GET /channels/123/messages")
        limiter.note_response(Response(headers=headers), "GET /channels/999/messages")
        self.assertNotEqual(
            limiter._route_buckets["GET /channels/123/messages"],
            limiter._route_buckets["GET /channels/999/messages"],
        )


class WatchdogTests(unittest.TestCase):
    def test_watchdog_uses_the_bot_snowflake(self):
        rest = Mock()
        rest.get_member.return_value = Response(200)
        watchdog = core.GuildWatchdog(rest, "guild", "bot", interval=5)
        self.assertEqual(watchdog.check_once(), "ok")
        rest.get_member.assert_called_once_with("guild", "bot")


if __name__ == "__main__":
    unittest.main()
