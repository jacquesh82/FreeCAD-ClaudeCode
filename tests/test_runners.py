import json
import os
import sys
import threading
from unittest.mock import Mock

import pytest
from PySide import QtCore

from claudecode_wb import claude_runner, runner_base, zcode_runner


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


def test_zcode_final_event_flushes_pending_text(qapp):
    runner = zcode_runner.ZCodeRunner()
    runner._turn_active = True
    runner._process = Mock()
    event = {"type": "model.streaming", "payload": {"kind": "text_delta", "delta": "pièce"}}
    runner._process.readAllStandardOutput.return_value = json.dumps(event).encode()
    runner._process.readAllStandardError.return_value = b""
    text = []
    runner.text_received.connect(text.append)
    runner._on_finished(0, QtCore.QProcess.NormalExit)
    assert text == ["pièce"]


def test_zcode_preparation_keeps_gui_responsive_and_cancellation_prevents_launch(
    qapp, monkeypatch, wait_until
):
    entered, release = threading.Event(), threading.Event()
    thread_ids = []

    def locate(cancelled=None):
        thread_ids.append(threading.get_ident())
        entered.set()
        release.wait(3)
        return ("unused", "unused.cjs")

    monkeypatch.setattr(zcode_runner, "find_zcode", locate)
    sync = Mock(return_value=None)
    monkeypatch.setattr(zcode_runner, "_sync_cli_model_config", sync)
    runner = zcode_runner.ZCodeRunner(1234, "token")
    launch = Mock()
    monkeypatch.setattr(runner, "_start_process", launch)
    finished = []
    runner.turn_finished.connect(lambda *args: finished.append(args))
    assert runner.send_prompt("hello")
    try:
        wait_until(entered.is_set)
        assert runner.running
        assert thread_ids == [thread_ids[0]] and thread_ids[0] != threading.get_ident()
        ticks = []
        QtCore.QTimer.singleShot(0, lambda: ticks.append(True))
        wait_until(lambda: bool(ticks))
        runner.stop()
    finally:
        release.set()
        runner._preparation.wait(3000)
    wait_until(lambda: bool(finished))
    assert not runner.running
    assert finished == [(False, "")]
    launch.assert_not_called()
    sync.assert_not_called()


@pytest.mark.parametrize("failure", ["missing", "config", "exception"])
def test_zcode_preparation_failures_end_the_turn(qapp, monkeypatch, wait_until, failure):
    locate = Mock(return_value=None if failure == "missing" else ("zcode", "cli.cjs"))
    if failure == "exception":
        locate.side_effect = OSError("cannot extract")
    monkeypatch.setattr(zcode_runner, "find_zcode", locate)
    monkeypatch.setattr(zcode_runner, "_sync_cli_model_config", lambda: "invalid configuration")
    runner = zcode_runner.ZCodeRunner(1234, "token")
    errors, finished = [], []
    runner.error.connect(errors.append)
    runner.turn_finished.connect(lambda *args: finished.append(args))
    assert runner.send_prompt("hello")
    wait_until(lambda: bool(finished))
    assert errors and finished == [(False, "")]
    assert not runner.running and runner._process is None


def test_zcode_launch_happens_on_gui_thread(qapp, monkeypatch, wait_until):
    monkeypatch.setattr(zcode_runner, "find_zcode", lambda **kw: ("zcode", "cli.cjs"))
    monkeypatch.setattr(zcode_runner, "_sync_cli_model_config", lambda: None)
    runner = zcode_runner.ZCodeRunner(1234, "token")
    launch_threads = []

    def start(*args, **kwargs):
        launch_threads.append(threading.get_ident())
        runner._complete_turn(True)

    monkeypatch.setattr(runner, "_start_process", start)
    assert runner.send_prompt("hello")
    wait_until(lambda: bool(launch_threads))
    assert launch_threads == [threading.get_ident()]


@pytest.mark.skipif(sys.platform == "win32", reason="AppImage extraction is Linux-only")
def test_cancelling_extraction_reaps_child_and_cleans_up(tmp_path, monkeypatch):
    marker = tmp_path / "child.pid"
    appimage = tmp_path / "fake.AppImage"
    appimage.write_text(
        "#!%s\n" % sys.executable
        + "import os, pathlib, time\n"
        + "pathlib.Path(%r).write_text(str(os.getpid()))\n" % str(marker)
        + "pathlib.Path('squashfs-root').mkdir()\n"
        + "time.sleep(30)\n"
    )
    appimage.chmod(0o700)
    cache = tmp_path / "zcode-cli"
    assert zcode_runner._extract_cli(str(appimage), cancelled=marker.exists) is None
    assert marker.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)
    assert not list(cache.glob("extract-*"))
