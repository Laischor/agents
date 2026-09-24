"""Tests for wrap.transcripts: JSONL parsers, diffs, titles, transcript lookup.

Run from the repo root:

    python3 -m unittest discover -s wrap/tests -t wrap -v

or directly:

    python3 wrap/tests/test_transcripts.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import transcripts as tr  # noqa: E402


def write_jsonl(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def pi_session(path: Path, records: list[dict]) -> Path:
    return write_jsonl(path, records)


class PiParseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wrap-tr-pi-"))
        self.path = self.tmp / "2026-01-01T00-00-00-000Z_abc-uuid.jsonl"

    def _base(self) -> list[dict]:
        return [
            {"type": "session", "id": "sess-uuid", "cwd": "/x", "timestamp": "t0"},
            {"type": "model_change", "id": "m1", "parentId": "sess-uuid", "timestamp": "t1"},
        ]

    def test_user_and_assistant_text_thinking_skipped(self) -> None:
        recs = self._base() + [
            {
                "type": "message",
                "id": "u1",
                "parentId": "m1",
                "timestamp": "t2",
                "message": {"role": "user", "content": [{"type": "text", "text": "do a thing"}]},
            },
            {
                "type": "message",
                "id": "a1",
                "parentId": "u1",
                "timestamp": "t3",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "secret"},
                        {"type": "text", "text": "done"},
                    ],
                },
            },
            {
                "type": "message",
                "id": "tr1",
                "parentId": "a1",
                "timestamp": "t4",
                "message": {"role": "toolResult", "content": [{"type": "text", "text": "ok"}]},
            },
        ]
        pi_session(self.path, recs)
        msgs = tr.parse_jsonl("pi", self.path)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertEqual(msgs[0]["text"], "do a thing")
        self.assertEqual(msgs[1]["text"], "done")
        # thinking must not leak into the rendered parts
        self.assertEqual([p["type"] for p in msgs[1]["parts"]], ["text"])

    def test_toolcall_becomes_diff_or_tool_hint(self) -> None:
        recs = self._base() + [
            {
                "type": "message",
                "id": "a1",
                "parentId": "m1",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "name": "write",
                            "arguments": {"path": "/x/hello.txt", "content": "alpha\n"},
                        },
                        {
                            "type": "toolCall",
                            "name": "edit",
                            "arguments": {
                                "path": "/x/hello.txt",
                                "oldText": "alpha",
                                "newText": "beta",
                            },
                        },
                        {
                            "type": "toolCall",
                            "name": "bash",
                            "arguments": {"command": "ls -la /x"},
                        },
                    ],
                },
            }
        ]
        pi_session(self.path, recs)
        msgs = tr.parse_jsonl("pi", self.path)
        self.assertEqual(len(msgs), 1)
        parts = msgs[0]["parts"]
        self.assertEqual([p["type"] for p in parts], ["diff", "diff", "tool"])
        self.assertEqual(parts[0]["path"], "/x/hello.txt")
        self.assertEqual(parts[0]["kind"], "write")
        self.assertIn("+alpha", parts[0]["diff"])
        self.assertEqual(parts[1]["kind"], "edit")
        self.assertIn("-alpha", parts[1]["diff"])
        self.assertIn("+beta", parts[1]["diff"])
        self.assertEqual(parts[2]["name"], "bash")
        self.assertEqual(parts[2]["detail"], "ls -la /x")

    def test_multiedit_edits_array(self) -> None:
        recs = self._base() + [
            {
                "type": "message",
                "id": "a1",
                "parentId": "m1",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "name": "edit",
                            "arguments": {
                                "path": "/x/a.txt",
                                "edits": [
                                    {"oldText": "one", "newText": "1"},
                                    {"oldText": "two", "newText": "2"},
                                ],
                            },
                        }
                    ],
                },
            }
        ]
        pi_session(self.path, recs)
        parts = tr.parse_jsonl("pi", self.path)[0]["parts"]
        self.assertEqual([p["type"] for p in parts], ["diff", "diff"])

    def test_branching_picks_active_leaf(self) -> None:
        recs = self._base() + [
            {
                "type": "message",
                "id": "u1",
                "parentId": "m1",
                "message": {"role": "user", "content": [{"type": "text", "text": "root"}]},
            },
            {
                "type": "message",
                "id": "old",
                "parentId": "u1",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "abandoned"}]},
            },
            {
                "type": "message",
                "id": "new",
                "parentId": "u1",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "active"}]},
            },
        ]
        pi_session(self.path, recs)
        msgs = tr.parse_jsonl("pi", self.path)
        self.assertEqual([m["text"] for m in msgs], ["root", "active"])

    def test_injected_user_message_filtered(self) -> None:
        recs = self._base() + [
            {
                "type": "message",
                "id": "u0",
                "parentId": "m1",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "<git_status>clean</git_status>"}],
                },
            },
            {
                "type": "message",
                "id": "u1",
                "parentId": "u0",
                "message": {"role": "user", "content": [{"type": "text", "text": "real ask"}]},
            },
        ]
        pi_session(self.path, recs)
        msgs = tr.parse_jsonl("pi", self.path)
        self.assertEqual([m["text"] for m in msgs], ["real ask"])

    def test_first_prompt_title_and_session_info_name(self) -> None:
        recs = self._base() + [
            {
                "type": "message",
                "id": "u1",
                "parentId": "m1",
                "message": {"role": "user", "content": [{"type": "text", "text": "Fix the parser"}]},
            },
            {
                "type": "session_info",
                "id": "si1",
                "parentId": "u1",
                "name": "Parser work",
            },
        ]
        pi_session(self.path, recs)
        self.assertEqual(tr.first_prompt_title("pi", self.path), "Fix the parser")
        self.assertEqual(
            tr.native_session_title(
                "pi",
                transcript=self.path,
                claude_home=self.tmp / "claude",
                cursor_home=self.tmp / "cursor",
            ),
            "Parser work",
        )

    def test_wrap_default_title_regex_matches_pi(self) -> None:
        self.assertTrue(tr.is_wrap_default_title("proj · pi"))
        self.assertTrue(tr.is_wrap_default_title("proj · Pi · 3"))
        self.assertFalse(tr.is_wrap_default_title("proj · pisces"))

    def test_parse_jsonl_dispatches_to_pi(self) -> None:
        recs = self._base() + [
            {
                "type": "message",
                "id": "u1",
                "parentId": "m1",
                "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            }
        ]
        pi_session(self.path, recs)
        self.assertEqual(tr.parse_jsonl("pi", self.path)[0]["text"], "hi")


class PiPathsTest(unittest.TestCase):
    def test_project_dir_encoding(self) -> None:
        self.assertEqual(
            tr.pi_project_dir(Path("/Users/mr/projects/agents")).name,
            "--Users-mr-projects-agents--",
        )
        self.assertEqual(
            tr.pi_project_dir(Path("/private/tmp/pitest")).name,
            "--private-tmp-pitest--",
        )

    def test_session_id_from_filename(self) -> None:
        p = Path("2026-09-17T23-40-25-360Z_01a0dead-beef-7000-8000-000000000001.jsonl")
        self.assertEqual(tr.pi_session_id(p), "01a0dead-beef-7000-8000-000000000001")
        self.assertEqual(tr.pi_session_id(Path("plain.jsonl")), "plain")

    def test_list_transcripts_uses_pi_home(self) -> None:
        home = Path(tempfile.mkdtemp(prefix="wrap-tr-pihome-"))
        cwd = Path("/Users/mr/projects/demo")
        with mock.patch.object(tr, "PI_HOME", home):
            d = tr.pi_project_dir(cwd)
            write_jsonl(d / "2026-01-01T00-00-00-000Z_uuid-1.jsonl", [{"type": "session"}])
            write_jsonl(d / "2026-01-02T00-00-00-000Z_uuid-2.jsonl", [{"type": "session"}])
            found = tr.list_transcripts("pi", cwd, home / "claude", home / "cursor")
        self.assertEqual([p.name for p in found], [
            "2026-01-01T00-00-00-000Z_uuid-1.jsonl",
            "2026-01-02T00-00-00-000Z_uuid-2.jsonl",
        ])

    def test_list_transcripts_empty_when_missing(self) -> None:
        home = Path(tempfile.mkdtemp(prefix="wrap-tr-pihome-"))
        with mock.patch.object(tr, "PI_HOME", home):
            self.assertEqual(
                tr.list_transcripts("pi", Path("/nope/nope"), home, home), []
            )


class ClaudeParseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wrap-tr-claude-"))
        self.path = self.tmp / "sess.jsonl"

    def test_user_assistant_and_write_diff(self) -> None:
        write_jsonl(
            self.path,
            [
                {
                    "type": "user",
                    "uuid": "u1",
                    "message": {"role": "user", "content": [{"type": "text", "text": "make it"}]},
                },
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "ok"},
                            {
                                "type": "tool_use",
                                "name": "Write",
                                "input": {"file_path": "/p/a.txt", "contents": "x\n"},
                            },
                        ],
                    },
                },
            ],
        )
        msgs = tr.parse_jsonl("claude", self.path)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        diff_parts = [p for p in msgs[1]["parts"] if p["type"] == "diff"]
        self.assertEqual(diff_parts[0]["path"], "/p/a.txt")
        self.assertEqual(diff_parts[0]["kind"], "write")

    def test_sidechain_and_meta_skipped(self) -> None:
        write_jsonl(
            self.path,
            [
                {"type": "user", "uuid": "u1", "isSidechain": True,
                 "message": {"role": "user", "content": [{"type": "text", "text": "sub"}]}},
                {"type": "assistant", "uuid": "a1", "isMeta": True,
                 "message": {"role": "assistant", "content": [{"type": "text", "text": "meta"}]}},
                {"type": "user", "uuid": "u2",
                 "message": {"role": "user", "content": [{"type": "text", "text": "real"}]}},
            ],
        )
        msgs = tr.parse_jsonl("claude", self.path)
        self.assertEqual([m["text"] for m in msgs], ["real"])

    def test_local_command_caveat_skipped(self) -> None:
        write_jsonl(
            self.path,
            [
                {"type": "user", "uuid": "u1",
                 "message": {"role": "user", "content": "<local-command-caveat>nope</local-command-caveat>"}},
                {"type": "user", "uuid": "u2",
                 "message": {"role": "user", "content": [{"type": "text", "text": "real"}]}},
            ],
        )
        self.assertEqual([m["text"] for m in tr.parse_jsonl("claude", self.path)], ["real"])

    def test_tool_result_user_row_skipped(self) -> None:
        write_jsonl(
            self.path,
            [
                {"type": "user", "uuid": "u1", "toolUseResult": {"ok": True},
                 "message": {"role": "user", "content": [{"type": "text", "text": "tool output"}]}},
            ],
        )
        self.assertEqual(tr.parse_jsonl("claude", self.path), [])


class CursorParseTest(unittest.TestCase):
    def test_user_timestamp_stripped_and_ts_kept(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="wrap-tr-cursor-"))
        path = tmp / "sess.jsonl"
        write_jsonl(
            path,
            [
                {
                    "role": "user",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "<timestamp>2026-01-01T00:00:00Z</timestamp>\nhello",
                            }
                        ]
                    },
                },
                {
                    "role": "assistant",
                    "message": {"content": [{"type": "text", "text": "hi"}]},
                },
            ],
        )
        msgs = tr.parse_jsonl("cursor", path)
        self.assertEqual([m["text"] for m in msgs], ["hello", "hi"])
        self.assertEqual(msgs[0]["ts"], "2026-01-01T00:00:00Z")


class MergeTurnsTest(unittest.TestCase):
    def test_consecutive_assistant_merged(self) -> None:
        msgs = [
            {"role": "user", "text": "q", "parts": [{"type": "text", "text": "q"}]},
            {"role": "assistant", "text": "a", "parts": [{"type": "text", "text": "a"}]},
            {"role": "assistant", "text": "b", "parts": [{"type": "text", "text": "b"}]},
        ]
        out = tr.merge_turns(msgs)
        self.assertEqual(len(out), 2)
        self.assertEqual([p["text"] for p in out[1]["parts"]], ["a\n\nb"])

    def test_duplicate_user_dropped(self) -> None:
        msgs = [
            {"role": "user", "text": "same", "parts": [{"type": "text", "text": "same"}]},
            {"role": "assistant", "text": "a", "parts": [{"type": "text", "text": "a"}]},
            {"role": "user", "text": "same", "parts": [{"type": "text", "text": "same"}]},
        ]
        self.assertEqual([m["role"] for m in tr.merge_turns(msgs)], ["user", "assistant"])


class DiffsFromToolTest(unittest.TestCase):
    def test_write_and_edit_variants(self) -> None:
        w = tr.diffs_from_tool("write", {"path": "/a.txt", "content": "x\n"})
        self.assertEqual(w[0]["kind"], "write")
        self.assertIn("+x", w[0]["diff"])

        claude_edit = tr.diffs_from_tool(
            "edit", {"path": "/a.txt", "old_string": "x", "new_string": "y"}
        )
        self.assertIn("-x", claude_edit[0]["diff"])
        self.assertIn("+y", claude_edit[0]["diff"])

        pi_edit = tr.diffs_from_tool(
            "edit", {"path": "/a.txt", "oldText": "x", "newText": "z"}
        )
        self.assertIn("+z", pi_edit[0]["diff"])

    def test_multiedit_and_noop(self) -> None:
        multi = tr.diffs_from_tool(
            "multiedit",
            {"path": "/a.txt", "edits": [{"old_string": "a", "new_string": "b"}]},
        )
        self.assertEqual(len(multi), 1)
        self.assertEqual(tr.diffs_from_tool("bash", {"command": "ls"}), [])
        self.assertEqual(tr.diffs_from_tool("edit", {"path": "/a", "old": "x", "new": "x"}), [])

    def test_non_dict_input(self) -> None:
        self.assertEqual(tr.diffs_from_tool("write", None), [])


class ToolHintTest(unittest.TestCase):
    def test_bash_and_read(self) -> None:
        self.assertEqual(tr.tool_hint("bash", {"command": "ls -la"}), "ls -la")
        self.assertEqual(tr.tool_hint("read", {"path": "/a/b/c.txt"}), "/a/b/c.txt")
        self.assertEqual(tr.tool_hint("read", None), "")


class TouchedPathsTest(unittest.TestCase):
    def test_claude_and_pi_toolcalls(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="wrap-tr-touch-"))
        claude = write_jsonl(
            tmp / "c.jsonl",
            [
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "/x/a.txt"}},
                ]}},
            ],
        )
        pi = write_jsonl(
            tmp / "p.jsonl",
            [
                {"type": "message", "message": {"role": "assistant", "content": [
                    {"type": "toolCall", "name": "write", "arguments": {"path": "/x/b.txt"}},
                ]}},
            ],
        )
        self.assertEqual(tr.touched_paths_jsonl(claude), ["/x/a.txt"])
        self.assertEqual(tr.touched_paths_jsonl(pi), ["/x/b.txt"])


class InjectedUserTest(unittest.TestCase):
    def test_variants(self) -> None:
        self.assertTrue(tr.is_injected_user_message("<git_status>clean</git_status>"))
        self.assertTrue(tr.is_injected_user_message("[Request interrupted by user]"))
        self.assertTrue(tr.is_injected_user_message(
            "Briefly inform the user about the task result"
        ))
        self.assertFalse(tr.is_injected_user_message("normal prompt"))

    def test_extract_user_query(self) -> None:
        self.assertEqual(tr.extract_user_query("<user_query>inner</user_query>"), "inner")
        self.assertEqual(
            tr.extract_user_query("<timestamp>t</timestamp>\nplain"), "plain"
        )


if __name__ == "__main__":
    unittest.main()
