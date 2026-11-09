import json
import os
import sys
import threading
from unittest.mock import Mock

import pytest
from PySide import QtCore

from claudecode_wb import claude_runner, runner_base


class RecordingRunner(runner_base.AgentRunner):
    def __init__(self):
        super().__init__()
        self.events = []

    def _handle_event(self, event):
        self.events.append(event)


def test_utf8_survives_every_possible_chunk_boundary(qapp):
    event = {"type": "assistant", "text": "Une pièce 🛠️ 中文"}
    encoded = json.dumps(event, ensure_ascii=False).encode("utf-8")
    runner = RecordingRunner()
    for split in range(len(encoded) + 1):
        runner.events.clear()
        remaining = runner._drain_events(encoded[:split])
        remaining = runner._drain_events(remaining + encoded[split:])
        assert runner.events == [event], split
        assert remaining == b""


def test_banners_and_mixed_json_delimiters_are_consumed_immediately(qapp):
    runner = RecordingRunner()
    remaining = runner._drain_events(
        b'warning\n{"type":"init"}\n{"type":"text"} '
        b'notice\n{"type":"result"}'
    )
    assert remaining == b""
    assert [event["type"] for event in runner.events] == ["init", "text", "result"]


def test_failed_process_launch_finishes_once_and_allows_retry(qapp, tmp_path, wait_until):
    runner = RecordingRunner()
    finished, errors = [], []
    runner.turn_finished.connect(lambda *args: finished.append(args))
    runner.error.connect(errors.append)
    runner._start_process(str(tmp_path / "missing-executable"), [])
    wait_until(lambda: bool(finished))
    assert finished == [(False, "")]
    assert errors and not runner.running

    runner._start_process(sys.executable, ["-c", 'print(\'{"type":"result"}\')'])
    wait_until(lambda: len(finished) == 2)
    assert finished[-1] == (True, "")
    assert runner.events == [{"type": "result"}]


def test_final_stdout_is_drained_before_completion(qapp):
    runner = RecordingRunner()
    runner._turn_active = True
    runner._process = Mock()
    runner._process.readAllStandardOutput.return_value = b'{"type":"result"}'
    runner._process.readAllStandardError.return_value = b""
    seen = []
    runner.turn_finished.connect(lambda *args: seen.extend(runner.events))
    runner._on_finished(0, QtCore.QProcess.NormalExit)
    assert seen == [{"type": "result"}]


def test_new_conversation_does_not_restore_a_stopped_session(qapp):
    runner = claude_runner.ClaudeRunner()
    runner._turn_active = True
    runner.session_id = "old"
    runner.reset_session()
    runner._handle_event({"type": "result", "session_id": "old"})
    runner._complete_turn(False)
    assert runner.session_id == ""
