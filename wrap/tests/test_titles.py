"""Tests for wrap.titles: title cleanup, prompts, per-agent dispatch.

    python3 -m unittest discover -s wrap/tests -t wrap -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import titles  # noqa: E402


class CleanTest(unittest.TestCase):
    def test_strips_quotes_and_newlines(self) -> None:
        self.assertEqual(titles.clean('  "Refactor auth"  '), "Refactor auth")
        self.assertEqual(titles.clean("Fix it\nnow"), "Fix it now")

    def test_drops_trailing_period(self) -> None:
        self.assertEqual(titles.clean("Add tests."), "Add tests")

    def test_too_short_or_long(self) -> None:
        self.assertIsNone(titles.clean("ab"))
        self.assertIsNone(titles.clean("x" * 81))
        self.assertEqual(titles.clean("x" * 80), "x" * 80)

    def test_drops_wrap_default_title(self) -> None:
        self.assertIsNone(titles.clean("proj · pi"))
        self.assertIsNone(titles.clean("proj · claude"))


class SnippetTest(unittest.TestCase):
    def test_skips_injected_and_caps_limit(self) -> None:
        msgs = [
            {"role": "user", "text": "<git_status>clean</git_status>"},
            {"role": "user", "text": "first ask"},
            {"role": "assistant", "text": "first answer"},
            {"role": "user", "text": "second ask"},
            {"role": "assistant", "text": "second answer"},
        ]
        out = titles.snippet(msgs, limit=2)
        self.assertEqual(out, "user: first ask\nassistant: first answer")


class GenerateTest(unittest.TestCase):
    def test_pi_agent_dispatches_to_run_pi(self) -> None:
        with mock.patch.object(titles, "_run_pi", return_value="Fix parser") as run:
            out = titles.generate("please fix the parser", agent="pi", model="ollama-cloud/x")
        self.assertEqual(out, "Fix parser")
        run.assert_called_once()
        prompt, model_id = run.call_args[0]
        self.assertIn("please fix the parser", prompt)
        self.assertEqual(model_id, "ollama-cloud/x")

    def test_unknown_agent_returns_none(self) -> None:
        self.assertIsNone(titles.generate("please fix the parser", agent="gpt"))

    def test_short_text_returns_none(self) -> None:
        self.assertIsNone(titles.generate("short", agent="pi"))

    def test_empty_model_output_returns_none(self) -> None:
        with mock.patch.object(titles, "_run_pi", return_value=""):
            self.assertIsNone(titles.generate("please fix the parser", agent="pi"))

    def test_fallback_agents_include_pi(self) -> None:
        self.assertIn("pi", titles._FALLBACK_AGENTS)


if __name__ == "__main__":
    unittest.main()
