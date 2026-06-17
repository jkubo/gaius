"""Tests for gaius record — session capture for vLLM/open models."""
import json
import os
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ["GAIUS_CONFIG"] = "/dev/null"
_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO))

from gaius.record import record_stdin, get_sessions_dir, DEFAULT_SESSIONS_DIR


class TestGetSessionsDir:
    def test_default_when_no_config(self):
        with patch("gaius.record.Path.home", return_value=Path("/fake")):
            # No config file → default
            result = get_sessions_dir()
            assert result == DEFAULT_SESSIONS_DIR

    def test_uses_config_sessions_dir(self, tmp_path):
        config = tmp_path / ".gaius" / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("backend: vllm\nsessions_dir: /custom/sessions\n")

        with patch("gaius.record.Path.home", return_value=tmp_path):
            result = get_sessions_dir()
            assert result == Path("/custom/sessions")


class TestRecordStdin:
    def test_captures_user_assistant_pair(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        input_data = "USER: what is 2+2?\nASSISTANT: 4\n"

        with patch("gaius.record.get_sessions_dir", return_value=sessions_dir):
            with patch("sys.stdin", StringIO(input_data)):
                path = record_stdin(model="test-model", session_type="test")

        assert path.exists()
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1

        entry = json.loads(lines[0])
        assert entry["query"] == "what is 2+2?"
        assert entry["response"] == "4"
        assert entry["model"] == "test-model"
        assert entry["session_type"] == "test"
        assert entry["source"] == "gaius-record"
        assert "ts" in entry

    def test_multiple_turns(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        input_data = (
            "USER: hello\n"
            "ASSISTANT: hi there\n"
            "USER: how are you?\n"
            "ASSISTANT: good thanks\n"
        )

        with patch("gaius.record.get_sessions_dir", return_value=sessions_dir):
            with patch("sys.stdin", StringIO(input_data)):
                path = record_stdin(model="gemma4")

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["query"] == "hello"
        assert json.loads(lines[1])["query"] == "how are you?"

    def test_unpaired_user_not_written(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        # USER without ASSISTANT → no output
        input_data = "USER: orphaned question\n"

        with patch("gaius.record.get_sessions_dir", return_value=sessions_dir):
            with patch("sys.stdin", StringIO(input_data)):
                path = record_stdin(model="test")

        content = path.read_text().strip()
        assert content == ""

    def test_assistant_without_user_not_written(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        # ASSISTANT without prior USER → no output
        input_data = "ASSISTANT: orphaned response\n"

        with patch("gaius.record.get_sessions_dir", return_value=sessions_dir):
            with patch("sys.stdin", StringIO(input_data)):
                path = record_stdin(model="test")

        content = path.read_text().strip()
        assert content == ""

    def test_output_is_valid_jsonl(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        input_data = "USER: test\nASSISTANT: response\n"

        with patch("gaius.record.get_sessions_dir", return_value=sessions_dir):
            with patch("sys.stdin", StringIO(input_data)):
                path = record_stdin(model="nemotron")

        for line in path.read_text().strip().splitlines():
            entry = json.loads(line)  # must not raise
            assert isinstance(entry, dict)
            assert "ts" in entry
            assert "query" in entry
            assert "response" in entry
            assert "model" in entry
