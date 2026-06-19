"""Tests for the Grok CLI + Codex CLI session parsers (peer coding agents).

These cover the format adapters that let `gaius retire` ingest local Grok and
Codex sessions the same first-class way it ingests Claude/Gemini/Ollama.

Run:
    pytest tests/test_peer_agents.py -v
"""
import json
import os
import sys
from pathlib import Path

# Force blank config so built-in defaults are predictable
os.environ["GAIUS_CONFIG"] = "/dev/null"

_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO))

from gaius._core import (  # noqa: E402
    parse_grok_events,
    parse_codex_events,
    _discover_grok_sessions,
    _discover_codex_sessions,
    _content_blocks_to_text,
)


def _write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class TestContentBlocks:
    def test_str_passthrough(self):
        assert _content_blocks_to_text("  hi  ") == "hi"

    def test_block_list(self):
        c = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        assert _content_blocks_to_text(c) == "ab"

    def test_mixed_and_missing(self):
        c = [{"type": "text", "text": "x"}, {"type": "image"}, "y"]
        assert _content_blocks_to_text(c) == "xy"

    def test_none(self):
        assert _content_blocks_to_text(None) == ""


class TestGrokParser:
    def _session(self, tmp_path, rows, summary=None):
        sess = tmp_path / "%2Fhome%2Fjkubo%2Fansible" / "019ed463-uuid"
        _write_jsonl(sess / "chat_history.jsonl", rows)
        if summary is not None:
            (sess / "summary.json").write_text(json.dumps(summary))
        return sess

    def test_terminal_answer_kept(self, tmp_path):
        rows = [
            {"type": "system", "content": "you are an assistant"},
            {"type": "user", "content": [{"type": "text", "text": "what is the cluster flannel MTU?"}]},
            {"type": "assistant",
             "content": "Flannel MTU is 1050 on cross-site Tailscale paths.",
             "model_id": "grok-composer-2.5-fast"},
        ]
        sess = self._session(tmp_path, rows, summary={
            "info": {"id": "abc-123"},
            "updated_at": "2026-06-17T07:05:24Z",
            "current_model_id": "grok-composer-2.5-fast",
        })
        events = parse_grok_events(sess)
        assert len(events) == 1
        ev = events[0]
        assert ev["agent"] == "grok"
        assert ev["type"] == "decision"
        assert ev["subject"].startswith("what is the cluster flannel MTU")
        assert "1050" in ev["description"]
        assert ev["session_uuid"] == "abc-123"
        assert ev["model_version"] == "grok-composer-2.5-fast"
        assert ev["model_family"] == "grok"

    def test_user_query_wrapper_stripped(self, tmp_path):
        rows = [
            {"type": "user", "content": [{"type": "text",
             "text": "<user_query>\ncheck the latest malware threats\n</user_query>"}]},
            {"type": "assistant", "content": "Atomic Arch AUR supply-chain attack is the top Linux story this week."},
        ]
        sess = self._session(tmp_path, rows)
        events = parse_grok_events(sess)
        assert len(events) == 1
        assert "<user_query>" not in events[0]["subject"]
        assert events[0]["subject"].startswith("check the latest malware threats")

    def test_toolcall_narration_skipped(self, tmp_path):
        # Assistant messages WITH tool_calls are mid-turn narration, not answers.
        rows = [
            {"type": "user", "content": [{"type": "text", "text": "check threats"}]},
            {"type": "assistant",
             "content": "I'll pull current threat intel from your MALINT stack.",
             "tool_calls": [{"id": "call_1"}]},
        ]
        sess = self._session(tmp_path, rows)
        assert parse_grok_events(sess) == []

    def test_empty_and_short_answers_skipped(self, tmp_path):
        rows = [
            {"type": "user", "content": [{"type": "text", "text": "ok?"}]},
            {"type": "assistant", "content": ""},
            {"type": "assistant", "content": "yes"},
        ]
        sess = self._session(tmp_path, rows)
        assert parse_grok_events(sess) == []

    def test_credential_leak_skipped(self, tmp_path):
        rows = [
            {"type": "user", "content": [{"type": "text", "text": "show env"}]},
            {"type": "assistant", "content": "Here it is: FORGEJO_TOKEN=abcdef and more text padding padding"},
        ]
        sess = self._session(tmp_path, rows)
        assert parse_grok_events(sess) == []

    def test_missing_summary_falls_back_to_dirname(self, tmp_path):
        rows = [
            {"type": "user", "content": [{"type": "text", "text": "describe the storage tiers please"}]},
            {"type": "assistant", "content": "SATA on RPi, NVMe on fwd-gpu, edge on DGX — all DRBD-backed."},
        ]
        sess = self._session(tmp_path, rows)  # no summary.json
        events = parse_grok_events(sess)
        assert len(events) == 1
        assert events[0]["session_uuid"] == "019ed463-uuid"
        assert events[0]["model_version"] == "grok-composer-2.5"  # MODEL_INFO default

    def test_no_chat_history_returns_empty(self, tmp_path):
        sess = tmp_path / "x" / "y"
        sess.mkdir(parents=True)
        assert parse_grok_events(sess) == []

    def test_discover(self, tmp_path):
        sessions = tmp_path / "sessions"
        s1 = sessions / "%2Fhome%2Fjkubo%2Fansible" / "uuid1"
        _write_jsonl(s1 / "chat_history.jsonl", [{"type": "system", "content": "x"}])
        # a stray non-session dir without chat_history must be ignored
        (sessions / "%2Fhome%2Fjkubo%2Fansible" / "not-a-session").mkdir(parents=True)
        found = list(_discover_grok_sessions(sessions))
        assert s1 in found
        assert all((p / "chat_history.jsonl").exists() for p in found)


class TestCodexParser:
    def _rollout(self, tmp_path, rows):
        path = tmp_path / "2026" / "06" / "17" / "rollout-2026-06-17T15-11-54-cdx-1.jsonl"
        _write_jsonl(path, rows)
        return path

    def test_answer_kept_injected_context_skipped(self, tmp_path):
        rows = [
            {"type": "session_meta", "payload": {
                "id": "cdx-1", "timestamp": "2026-03-11T12:34:38Z", "cwd": "/home/jkubo/ansible"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "response_item", "payload": {"type": "message", "role": "developer",
             "content": [{"type": "input_text", "text": "<permissions instructions> ..."}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "<environment_context><cwd>/home/jkubo/ansible</cwd>"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "which storageclass for a new DRBD PVC?"}]}},
            {"type": "response_item", "payload": {"type": "reasoning", "summary": [], "content": []}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "Use block-sata for RPi DRBD; block-nvme for fwd-gpu nodes."}]}},
        ]
        path = self._rollout(tmp_path, rows)
        events = parse_codex_events(path)
        assert len(events) == 1
        ev = events[0]
        assert ev["agent"] == "codex"
        assert ev["session_uuid"] == "cdx-1"
        assert ev["timestamp"] == "2026-03-11T12:34:38Z"
        # env-context user message must NOT become the subject
        assert ev["subject"].startswith("which storageclass")
        assert "block-sata" in ev["description"]

    def test_developer_role_ignored(self, tmp_path):
        rows = [
            {"type": "session_meta", "payload": {"id": "d-1"}},
            {"type": "response_item", "payload": {"type": "message", "role": "developer",
             "content": [{"type": "input_text", "text": "system level instructions that are long enough to pass"}]}},
        ]
        path = self._rollout(tmp_path, rows)
        assert parse_codex_events(path) == []

    def test_short_answer_skipped(self, tmp_path):
        rows = [
            {"type": "session_meta", "payload": {"id": "s-1"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "done?"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "ok"}]}},
        ]
        path = self._rollout(tmp_path, rows)
        assert parse_codex_events(path) == []

    def test_discover(self, tmp_path):
        sessions = tmp_path / "sessions"
        p = sessions / "2026" / "06" / "17" / "rollout-x-uuid.jsonl"
        _write_jsonl(p, [{"type": "session_meta", "payload": {"id": "z"}}])
        assert list(_discover_codex_sessions(sessions)) == [p]
