import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ninog"))

from src import ui, workflow
from src.ui import InputReplayError


class Context:
    def __init__(self):
        self.trigger_state = {}
        self.just_selected_guild = False
        self.guild_lost = False


class TriggerTests(unittest.TestCase):
    def test_interval_waits_for_first_period(self):
        ctx = Context()
        flow = {
            "name": "timer",
            "steps": [{"kind": "note", "text": "run"}],
            "triggers": [{"kind": "interval", "seconds": 30}],
        }
        with patch.object(workflow, "load_workflows", return_value=[flow]), \
                patch.object(workflow.time, "monotonic", side_effect=[100.0, 120.0, 131.0]):
            self.assertEqual(workflow.collect_due_workflows(ctx), [])
            self.assertEqual(workflow.collect_due_workflows(ctx), [])
            due = workflow.collect_due_workflows(ctx)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0][1], "interval 30s")


class ReplayTests(unittest.TestCase):
    def tearDown(self):
        ui.set_input_feed(None)
        ui.end_input_capture()

    def test_capture_records_prompt_type_and_value(self):
        ui.begin_input_capture()
        with patch("builtins.input", side_effect=["hello", "y"]):
            self.assertEqual(ui.ask("message"), "hello")
            self.assertTrue(ui.confirm("send it?"))
        self.assertEqual(
            ui.end_input_capture(),
            [
                {"kind": "ask", "prompt": "message", "value": "hello"},
                {"kind": "confirm", "prompt": "send it?", "value": "y"},
            ],
        )

    def test_strict_replay_rejects_changed_prompt(self):
        from collections import deque

        ui.set_input_feed(
            deque([{"kind": "ask", "prompt": "old prompt", "value": "answer"}]),
            strict=True,
        )
        with self.assertRaises(InputReplayError):
            ui.ask("new prompt")

    def test_strict_replay_rejects_exhausted_answers(self):
        from collections import deque

        ui.set_input_feed(deque(), strict=True)
        with self.assertRaises(InputReplayError):
            ui.confirm("continue?")


class PlanningTests(unittest.TestCase):
    def test_write_is_simulated_and_read_is_live(self):
        class Response:
            status_code = 200

            def json(self):
                return [{"id": "live"}]

        class Real:
            session = object()
            limiter = object()

            def get_channels(self, _guild_id):
                return Response()

            def create_channel(self, *_args, **_kwargs):
                raise AssertionError("real write called")

        planned = workflow._PlanningREST(Real())
        self.assertEqual(planned.get_channels("guild").json()[0]["id"], "live")
        result = planned.create_channel("guild", {"name": "planned"})
        self.assertEqual(result.status_code, 201)
        self.assertEqual(len(planned.writes), 1)


if __name__ == "__main__":
    unittest.main()
