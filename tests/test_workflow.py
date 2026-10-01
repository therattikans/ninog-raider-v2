import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ninog"))

from src import workflow


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


if __name__ == "__main__":
    unittest.main()
