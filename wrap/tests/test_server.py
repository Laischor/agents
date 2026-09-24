"""Tests for wrap.server helpers (catalog, titles, transcript binding).

Env is pinned to a temp dir *before* importing the server so no real state,
project tree, or CLI is touched.

    python3 -m unittest discover -s wrap/tests -t wrap -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid as uuidlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TMP = Path(tempfile.mkdtemp(prefix="wrap-test-server-"))
os.environ["WRAP_STATE"] = str(_TMP / "state.json")
os.environ["HOST_PROJECTS"] = str(_TMP)
os.environ["HERMES"] = "0"
os.environ["WRAP_TITLE"] = "0"

import server  # noqa: E402
import transcripts as tr  # noqa: E402


def write_jsonl(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def pi_records(prompt: str, reply: str = "ok", name: str = "") -> list[dict]:
    recs = [
        {"type": "session", "id": "sess-uuid", "cwd": "/x", "timestamp": "t0"},
        {
            "type": "message",
            "id": "u1",
            "parentId": "sess-uuid",
            "timestamp": "t1",
            "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
        },
        {
            "type": "message",
            "id": "a1",
            "parentId": "u1",
            "timestamp": "t2",
            "message": {"role": "assistant", "content": [{"type": "text", "text": reply}]},
        },
    ]
    if name:
        recs.append(
            {"type": "session_info", "id": "si1", "parentId": "a1", "name": name}
        )
    return recs


class _PiHomeMixin(unittest.TestCase):
    """Patch transcripts.PI_HOME at a temp dir for one test."""

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="wrap-test-pihome-"))
        self.cwd = Path(tempfile.mkdtemp(prefix="wrap-test-proj-"))
        self._pi_home = mock.patch.object(tr, "PI_HOME", self.home)
        self._pi_home.start()

    def tearDown(self) -> None:
        self._pi_home.stop()

    def add_pi_session(self, session_uuid: str, records: list[dict]) -> Path:
        d = tr.pi_project_dir(self.cwd)
        return write_jsonl(
            d / f"2026-01-01T00-00-00-000Z_{session_uuid}.jsonl", records
        )


class CatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        server._catalog_cache["data"] = None
        server._catalog_cache["at"] = 0.0

    def test_agent_registry_has_pi(self) -> None:
        self.assertIn("pi", server.AGENTS)
        self.assertEqual(server.AGENT_LABELS["pi"], "Pi")

    def test_parse_labeled_models(self) -> None:
        out = server.parse_labeled_models(
            ["Available models:", "gpt-4o - GPT 4o", "sonnet", "sonnet", ""]
        )
        self.assertEqual(out, [
            {"id": "gpt-4o", "label": "GPT 4o"},
            {"id": "sonnet", "label": "sonnet"},
        ])

    def test_pi_models_skips_no_models_prose(self) -> None:
        prose = [
            "No models available. Use /login to log into a provider via OAuth or API key. See:",
            "  /usr/local/lib/node_modules/@earendil-works/pi-coding-agent/docs/providers.md",
        ]
        with mock.patch.object(server, "run_lines", return_value=prose):
            self.assertEqual(server.pi_models(), [{"id": "", "label": "CLI default"}])

    def test_pi_models_parses_table(self) -> None:
        table = [
            "provider      model                context  max-out  thinking  images",
            "ollama-cloud  deepseek-v4.1-flash  128K     16.4K    yes       yes",
            "ollama-cloud  glm-5.3-flash        128K     16.4K    yes       yes",
            "ollama-cloud  deepseek-v4.1-flash  128K     16.4K    yes       yes",
        ]
        with mock.patch.object(server, "run_lines", return_value=table):
            out = server.pi_models()
        ids = [m["id"] for m in out]
        self.assertEqual(ids, [
            "",
            "ollama-cloud/deepseek-v4.1-flash",
            "ollama-cloud/glm-5.3-flash",
        ])
        self.assertEqual(out[1]["label"], "deepseek-v4.1-flash · ollama-cloud")

    def test_pi_models_empty_on_error_is_handled_by_catalog(self) -> None:
        with mock.patch.object(server, "oc") as oc_mock:
            oc_mock.providers.return_value = []
            with mock.patch.object(server, "run_lines", return_value=[]):
                with mock.patch.object(server, "hermes_on", return_value=False):
                    with mock.patch.object(
                        server, "pi_models", side_effect=RuntimeError("boom")
                    ):
                        data = server.catalog(fresh=True)
        self.assertEqual(data["pi"]["models"], [{"id": "", "label": "CLI default"}])

    def test_catalog_includes_pi_agent_models_and_title_agents(self) -> None:
        table = [
            "provider      model                context  max-out  thinking  images",
            "ollama-cloud  deepseek-v4.1-flash  128K     16.4K    yes       yes",
        ]
        with mock.patch.object(server, "run_lines", return_value=table):
            with mock.patch.object(server.oc, "providers", return_value=[]):
                with mock.patch.object(server, "hermes_on", return_value=False):
                    data = server.catalog(fresh=True)
        self.assertIn("pi", [a["id"] for a in data["agents"]])
        self.assertEqual(
            [m["id"] for m in data["pi"]["models"]],
            ["", "ollama-cloud/deepseek-v4.1-flash"],
        )
        self.assertEqual(
            [e["id"] for e in data["pi"]["effort"]],
            ["", "off", "minimal", "low", "medium", "high", "xhigh", "max"],
        )
        self.assertIn("pi", [a["id"] for a in data["title"]["agents"]])


class TitleFallbackTest(unittest.TestCase):
    def test_pi_is_accepted(self) -> None:
        self.assertEqual(
            server._normalize_title_fallback({"agent": "pi", "model": "ollama-cloud/x"}),
            {"agent": "pi", "model": "ollama-cloud/x"},
        )

    def test_unknown_agent_is_dropped(self) -> None:
        self.assertEqual(
            server._normalize_title_fallback({"agent": "gpt", "model": "x"}),
            {"agent": "", "model": ""},
        )

    def test_non_dict_input(self) -> None:
        self.assertEqual(server._normalize_title_fallback(None), {"agent": "", "model": ""})


class ApplyNativeTitleTest(_PiHomeMixin):
    def test_pi_uses_first_prompt_when_no_name(self) -> None:
        sid = str(uuidlib.uuid4())
        path = self.add_pi_session(sid, pi_records("Fix the parser"))
        sess = {
            "id": "pi-1",
            "agent": "pi",
            "cwd": str(self.cwd),
            "cli_session": sid,
            "transcript": str(path),
            "title": "proj · pi",
            "title_source": "wrap",
        }
        changed = server.apply_native_title(sess, persist=False)
        self.assertTrue(changed)
        self.assertEqual(sess["title"], "Fix the parser")
        self.assertEqual(sess["title_source"], "prompt")

    def test_pi_prefers_session_info_name(self) -> None:
        sid = str(uuidlib.uuid4())
        path = self.add_pi_session(sid, pi_records("Fix the parser", name="Parser work"))
        sess = {
            "id": "pi-2",
            "agent": "pi",
            "cwd": str(self.cwd),
            "cli_session": sid,
            "transcript": str(path),
            "title": "proj · pi",
            "title_source": "wrap",
        }
        self.assertTrue(server.apply_native_title(sess, persist=False))
        self.assertEqual(sess["title"], "Parser work")
        self.assertEqual(sess["title_source"], "native")

    def test_user_title_is_left_alone(self) -> None:
        sid = str(uuidlib.uuid4())
        path = self.add_pi_session(sid, pi_records("Fix the parser"))
        sess = {
            "id": "pi-3",
            "agent": "pi",
            "cwd": str(self.cwd),
            "cli_session": sid,
            "transcript": str(path),
            "title": "my name",
            "title_source": "user",
        }
        self.assertFalse(server.apply_native_title(sess, persist=False))
        self.assertEqual(sess["title"], "my name")


class TranscriptBindingTest(_PiHomeMixin):
    def setUp(self) -> None:
        super().setUp()
        self._saved = dict(server.SESSIONS)
        self._titles = dict(server.TITLES)
        server.SESSIONS.clear()
        server.TITLES.clear()

    def tearDown(self) -> None:
        server.SESSIONS.clear()
        server.SESSIONS.update(self._saved)
        server.TITLES.clear()
        server.TITLES.update(self._titles)
        super().tearDown()

    def _sess(self, session_uuid: str, transcript: str = "") -> dict:
        return {
            "id": f"pi-{session_uuid[:6]}",
            "agent": "pi",
            "cwd": str(self.cwd),
            "cli_session": session_uuid,
            "transcript": transcript,
            "seen_transcripts": {},
        }

    def test_pick_transcript_binds_by_cli_session(self) -> None:
        sid1, sid2 = str(uuidlib.uuid4()), str(uuidlib.uuid4())
        p1 = self.add_pi_session(sid1, pi_records("one"))
        self.add_pi_session(sid2, pi_records("two"))
        sess = self._sess(sid1)
        self.assertEqual(server.pick_transcript(sess), p1)

    def test_pick_transcript_does_not_steal_sibling(self) -> None:
        sid1, sid2 = str(uuidlib.uuid4()), str(uuidlib.uuid4())
        p1 = self.add_pi_session(sid1, pi_records("one"))
        self.add_pi_session(sid2, pi_records("two"))
        server.SESSIONS["pi-other"] = self._sess(sid2)
        server.SESSIONS["pi-other"]["id"] = "pi-other"
        # A pi session with an unknown id must not fall back to mtime.
        self.assertIsNone(server.pick_transcript(self._sess(str(uuidlib.uuid4()))))
        # The owner still binds its own file.
        self.assertEqual(server.pick_transcript(self._sess(sid1)), p1)

    def test_ensure_transcript_sets_transcript(self) -> None:
        sid = str(uuidlib.uuid4())
        p = self.add_pi_session(sid, pi_records("hello"))
        sess = self._sess(sid)
        server.SESSIONS[sess["id"]] = sess
        found = server.ensure_transcript(sess)
        self.assertEqual(found, p)
        self.assertEqual(sess["transcript"], str(p))

    def test_history_transcript_finds_pi_by_uuid(self) -> None:
        sid = str(uuidlib.uuid4())
        p = self.add_pi_session(sid, pi_records("hello"))
        self.assertEqual(server.history_transcript("pi", self.cwd, sid), p)
        self.assertIsNone(server.history_transcript("pi", self.cwd, "missing"))

    def test_native_key_pi_uses_uuid(self) -> None:
        sid = str(uuidlib.uuid4())
        p = self.add_pi_session(sid, pi_records("hello"))
        self.assertEqual(server.native_key(self._sess(sid)), ("pi", sid))
        # Fallback: derive the uuid from the transcript filename when cli_session is empty.
        sess = self._sess(sid, transcript=str(p))
        sess["cli_session"] = ""
        self.assertEqual(server.native_key(sess), ("pi", sid))

    def test_history_public_exposes_pi_messages(self) -> None:
        sid = str(uuidlib.uuid4())
        self.add_pi_session(sid, pi_records("do the thing", reply="done"))
        row = server.history_public("pi", sid, self.cwd)
        self.assertEqual(row["agent"], "pi")
        self.assertEqual(row["native_id"], sid)
        self.assertEqual(row["cli_session"], sid)
        self.assertEqual(
            [(m["role"], m["text"]) for m in row["messages"]],
            [("user", "do the thing"), ("assistant", "done")],
        )
        self.assertEqual(row["title"], "do the thing")

    def test_open_session_pi_generates_uuid_cli_session(self) -> None:
        sess = server.open_session("pi", self.cwd, model="ollama-cloud/x", effort="low")
        try:
            uuidlib.UUID(sess["cli_session"])
            self.assertEqual(sess["agent"], "pi")
            self.assertIsNone(sess["transcript"])
            self.assertFalse(sess["fast"])
        finally:
            server.SESSIONS.pop(sess["id"], None)

    def test_open_session_pi_resumes_from_history(self) -> None:
        sid = str(uuidlib.uuid4())
        p = self.add_pi_session(sid, pi_records("hello"))
        sess = server.open_session("pi", self.cwd, resume_id=sid)
        try:
            self.assertEqual(sess["cli_session"], sid)
            self.assertEqual(sess["transcript"], str(p))
        finally:
            server.SESSIONS.pop(sess["id"], None)


if __name__ == "__main__":
    unittest.main()
