"""Regression coverage for the eight findings in the second review."""

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from PySide import QtCore, QtNetwork

from claudecode_wb import approval, claude_runner, mcp_bridge, paths, rpc_server, runner_base

REAL_LISTEN = QtNetwork.QTcpServer.listen
REAL_SERVER_PORT = QtNetwork.QTcpServer.serverPort


class Recorder(runner_base.AgentRunner):
    def __init__(self):
        super().__init__()
        self.events = []

    def _handle_event(self, event):
        self.events.append(event)


@pytest.fixture
def server(qapp, monkeypatch):
    rpc = rpc_server.FreeCADRpcServer()
    rpc.begin_turn()
    import FreeCAD as App
    monkeypatch.setattr(App, "setActiveTransaction", Mock(return_value=42), raising=False)
    monkeypatch.setattr(App, "closeActiveTransaction", Mock(), raising=False)
    yield rpc
    rpc.close()


@pytest.mark.parametrize("attached,extended", [(False, False), (True, False), (False, True), (True, True)])
def test_claude_tool_configuration_is_explicit(qapp, monkeypatch, attached, extended):
    runner = claude_runner.ClaudeRunner(1234, "TEST_TOKEN")
    monkeypatch.setattr(claude_runner, "find_claude", lambda: "fake-claude")
    launched = []

    def start(program, args, **kwargs):
        launched.extend(args)
        runner._turn_active = True
        runner._process = Mock()
        runner._process.state.return_value = QtCore.QProcess.NotRunning

    monkeypatch.setattr(runner, "_start_process", start)
    attachments = [runner.stage_attachment("part.txt", data=b"fake attachment")] if attached else []
    try:
        assert runner.send_prompt("test", extended_tools=extended, attachments=attachments)
        assert "--strict-mcp-config" in launched
        assert "--setting-sources=" in launched
        # The addon never configures the CLI to approve tools automatically.
        assert not any("dangerously" in arg or arg in ("bypassPermissions", "acceptEdits")
                       for arg in launched)
        assert launched[launched.index("--permission-mode") + 1] == "default"
        tools = launched[launched.index("--tools") + 1]
        assert tools == ("default" if extended else "Read" if attached else "")
        allowed = launched[launched.index("--allowedTools") + 1].split(",")
        assert all(tool.startswith(("mcp__freecad__", "Read(")) for tool in allowed)
        assert ("Read(./attachments/**)" in allowed) == attached
        config = json.loads(Path(runner._mcp_config_path).read_text())
        env = config["mcpServers"]["freecad"]["env"]
        assert env["FREECAD_PERMISSION_TOOL"] == ("1" if extended else "0")
        if extended:
            assert launched[launched.index("--permission-prompt-tool") + 1] == "mcp__freecad__approve_tool"
            settings_path = launched[launched.index("--settings") + 1]
            settings = json.loads(Path(settings_path).read_text())
            assert set(settings) == {"hooks"}
            hook = settings["hooks"]["PreToolUse"][0]
            assert hook["matcher"] == "*"
            assert "permission_hook.py" in hook["hooks"][0]["command"]
            if os.name == "posix":
                assert stat.S_IMODE(os.stat(settings_path).st_mode) & 0o077 == 0
        else:
            assert "--permission-prompt-tool" not in launched
            assert "--settings" not in launched
    finally:
        runner._complete_turn(False)
        runner.close()


@pytest.mark.parametrize("event,expected", [
    ({"tool_name": "Bash", "tool_input": {"command": "echo hi"}}, "ask"),
    ({"tool_name": "Read", "tool_input": {"file_path": "/etc/hostname"}}, "ask"),
    ({"tool_name": "Write", "tool_input": {"file_path": "x", "content": "y"}}, "ask"),
    ({"tool_name": "WebFetch", "tool_input": {"url": "https://example.com"}}, "ask"),
    ({"tool_name": "mcp__freecad__exec_python", "tool_input": {"code": "1"}}, None),
    ({"tool_name": "TodoWrite", "tool_input": {"todos": []}}, None),
    ({"tool_name": "ToolSearch", "tool_input": {"query": "select:x"}}, None),
    ({"tool_name": 3}, "ask"),
    (["not", "an", "object"], "ask"),
    (None, "ask"),
])
def test_permission_hook_asks_for_every_side_effect(event, expected):
    from claudecode_wb import permission_hook

    assert permission_hook.decision(event) == expected


def test_permission_hook_only_skips_reads_inside_attachments(tmp_path):
    from claudecode_wb import permission_hook

    attachments = tmp_path / "attachments"
    attachments.mkdir()
    (attachments / "part.step").write_text("data")
    inside = {"tool_name": "Read", "tool_input": {"file_path": str(attachments / "part.step")}}
    escape = {"tool_name": "Read", "tool_input": {"file_path": str(attachments / ".." / "secret")}}
    sibling = {"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "attachments-x")}}
    assert permission_hook.decision(inside, str(attachments)) is None
    assert permission_hook.decision(escape, str(attachments)) == "ask"
    assert permission_hook.decision(sibling, str(attachments)) == "ask"


def test_permission_hook_script_prints_an_ask_decision():
    hook = Path(__file__).resolve().parents[1] / "claudecode_wb/permission_hook.py"
    result = subprocess.run(
        [sys.executable, str(hook), ""],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}),
        capture_output=True, text=True, timeout=30,
    )
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "ask"
    silent = subprocess.run(
        [sys.executable, str(hook), ""],
        input=json.dumps({"tool_name": "mcp__freecad__get_scene", "tool_input": {}}),
        capture_output=True, text=True, timeout=30,
    )
    assert silent.stdout.strip() == ""


def test_hook_command_quotes_shell_metacharacters():
    assert claude_runner._shell_quote('a b"$`\\c') == '"a b\\"\\$\\`\\\\c"'


def test_windows_batch_shims_are_rejected_before_launch(qapp, monkeypatch):
    runner = claude_runner.ClaudeRunner(1234, "TEST")
    monkeypatch.setattr(claude_runner, "IS_WINDOWS", True)
    monkeypatch.setattr(claude_runner, "find_claude", lambda: "fake-claude.cmd")
    start = Mock()
    monkeypatch.setattr(runner, "_start_process", start)
    try:
        with pytest.raises(ValueError, match="native claude.exe"):
            runner.send_prompt("test")
        start.assert_not_called()
    finally:
        runner.close()


def test_escaped_json_survives_every_chunk_boundary(qapp):
    event = {"type": "assistant", "text": 'pièce {"quoted"}\\\n',
             "values": [True, False, None, -1.25e-12, {"nested": "🛠️"}]}
    encoded = json.dumps(event, ensure_ascii=True).encode()
    runner = Recorder()
    try:
        for split in range(len(encoded) + 1):
            runner.events.clear()
            pending = runner._drain_events(encoded[:split])
            assert runner._drain_events(pending + encoded[split:]) == b""
            assert runner.events == [event], split
    finally:
        runner.close()


def test_stop_terminates_child_processes_and_revokes_rpc_first(qapp, tmp_path, wait_until):
    marker, ready = tmp_path / "after-stop", tmp_path / "child-ready"
    child_script = (
        "import pathlib,time; pathlib.Path(%r).write_text('ready'); "
        "time.sleep(0.5); pathlib.Path(%r).write_text('should not happen'); time.sleep(30)"
    ) % (str(ready), str(marker))
    parent_script = (
        "import subprocess,sys,json,time; child=subprocess.Popen([sys.executable,'-c',%r]); "
        "print(json.dumps({'pid':child.pid}),flush=True); time.sleep(30)"
    ) % child_script
    runner = Recorder()
    revoked = []
    runner.set_rpc(1234, "TEST", revoke=lambda: revoked.append(runner.running))
    try:
        runner._start_process(sys.executable, ["-c", parent_script])
        wait_until(lambda: bool(runner.events) and ready.exists())
        runner.stop()
        assert revoked == [True]
        wait_until(lambda: not runner.running)
        time.sleep(0.7)
        assert not marker.exists(), "The child continued after the agent was stopped"
    finally:
        runner.close()
        if marker.exists() and runner.events:
            try:
                import signal
                os.kill(runner.events[0]["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass


def test_rpc_tokens_expire_and_late_completions_cannot_revoke_new_turn(server):
    old = server.token
    assert server._authorized({"token": old})
    server.end_turn(old)
    assert not server._authorized({"token": old})
    new = server.begin_turn()
    assert new != old
    assert not server._authorized({"token": old})
    server.end_turn(old)
    assert server._authorized({"token": new})
    assert not server._authorized({"token": "\ud800"})


def test_normal_completion_revokes_rpc_and_kills_remaining_children(server, qapp, tmp_path, wait_until):
    ready, marker = tmp_path / "ready-normal", tmp_path / "after-normal-exit"
    child_script = (
        "import pathlib,time; pathlib.Path(%r).write_text('ready'); "
        "time.sleep(0.5); pathlib.Path(%r).write_text('should not happen'); time.sleep(30)"
    ) % (str(ready), str(marker))
    parent_script = (
        "import subprocess,sys,json,time,pathlib\n"
        "child = subprocess.Popen([sys.executable, '-c', %r])\n"
        "while not pathlib.Path(%r).exists(): time.sleep(0.01)\n"
        "print(json.dumps({'pid':child.pid}), flush=True)\n"
    ) % (child_script, str(ready))
    token = server.token
    runner = Recorder()
    runner.set_rpc(server.port, token, revoke=lambda: server.end_turn(token))
    try:
        runner._start_process(sys.executable, ["-c", parent_script])
        wait_until(lambda: not runner.running)
        assert runner.events and not server._authorized({"token": token})
        time.sleep(0.7)
        assert not marker.exists()
    finally:
        runner.close()
        if marker.exists() and runner.events:
            try:
                import signal
                os.kill(runner.events[0]["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("rotate", [False, True])
def test_revocation_during_approval_prevents_execution(server, monkeypatch, rotate):
    def review(*args):
        if rotate:
            server.begin_turn()
        else:
            server.end_turn(server.token)
        return True

    monkeypatch.setattr(approval, "request_exec_approval", review)
    result = server._op_exec("raise AssertionError('must not execute')")
    assert not result["ok"] and "before execution" in result["error"]
    import FreeCAD as App
    App.setActiveTransaction.assert_not_called()


def test_denied_code_never_opens_a_transaction(server, monkeypatch):
    monkeypatch.setattr(approval, "request_exec_approval", lambda *a: False)
    result = server._op_exec("raise AssertionError('must not execute')")
    assert result["denied"] and not result["ok"]
    import FreeCAD as App
    App.setActiveTransaction.assert_not_called()


@pytest.mark.parametrize("allowed", [True, False])
def test_tool_permission_shows_the_complete_input_and_reports_the_decision(server, monkeypatch, allowed):
    seen, decisions = [], []
    monkeypatch.setattr(
        approval, "request_tool_approval",
        lambda name, tool_input, agent: seen.append((name, tool_input)) or allowed,
    )
    server.decision_callback = lambda *args: decisions.append(args)
    tool_input = {"command": "rm -rf build", "description": "clean"}
    result = server._dispatch({"op": "permission", "tool_name": "Bash", "input": tool_input})
    assert result == {"ok": True, "allow": allowed}
    assert seen == [("Bash", tool_input)]
    assert decisions == [("tool", allowed, "Bash")]


def test_tool_permission_ignores_the_exec_auto_run_opt_in(server, monkeypatch):
    server.auto_approve = True
    review = Mock(return_value=False)
    monkeypatch.setattr(approval, "request_tool_approval", review)
    result = server._dispatch({"op": "permission", "tool_name": "Write", "input": {"file_path": "x"}})
    assert result["allow"] is False
    review.assert_called_once()


def test_tool_permission_is_denied_when_the_turn_ends_during_review(server, monkeypatch):
    def review(*args):
        server.end_turn(server.token)
        return True

    monkeypatch.setattr(approval, "request_tool_approval", review)
    result = server._dispatch({"op": "permission", "tool_name": "Bash", "input": {"command": "ls"}})
    assert result["allow"] is False


def test_tool_permission_never_asks_twice_at_once(server, monkeypatch):
    review = Mock(return_value=True)
    monkeypatch.setattr(approval, "request_tool_approval", review)
    server._approval_open = True
    result = server._dispatch({"op": "permission", "tool_name": "Bash", "input": {"command": "ls"}})
    assert result["allow"] is False
    review.assert_not_called()


@pytest.mark.parametrize("request_", [
    {"op": "permission", "tool_name": "Bash"},
    {"op": "permission", "tool_name": 1, "input": {}},
    {"op": "permission", "tool_name": "Bash", "input": "rm -rf /"},
])
def test_malformed_permission_requests_are_rejected(request_):
    assert not rpc_server.FreeCADRpcServer._valid_request(request_)


def test_tool_approval_dialog_shows_every_field_verbatim(qapp):
    content = "line 1\nline 2 with \"quotes\""
    dialog = approval.tool_approval_dialog(
        "Write", {"file_path": "/home/u/part.py", "content": content, "extra": [1, 2]}, "Claude Code"
    )
    shown = dialog.code_view.toPlainText()
    assert "/home/u/part.py" in shown and content in shown and "1," in shown
    assert dialog.deny_btn.isDefault() and not dialog.run_btn.isDefault()
    dialog.deleteLater()


@pytest.mark.parametrize("response,behavior", [
    ({"ok": True, "allow": True}, "allow"),
    ({"ok": True, "allow": False}, "deny"),
    ({"ok": True, "allow": "yes"}, "deny"),
    ({"ok": False, "error": "unauthorized"}, "deny"),
])
def test_bridge_permission_decision_runs_only_the_shown_input(monkeypatch, response, behavior):
    sent = []
    monkeypatch.setattr(mcp_bridge, "RPC_TOKEN", "TOKEN")
    monkeypatch.setattr(mcp_bridge, "rpc_call", lambda payload, timeout: sent.append(payload) or response)
    tool_input = {"command": "ls -la"}
    result = mcp_bridge.permission_decision({"tool_name": "Bash", "input": tool_input})
    decision = json.loads(result["content"][0]["text"])
    assert decision["behavior"] == behavior
    assert sent[0]["op"] == "permission" and sent[0]["input"] == tool_input
    if behavior == "allow":
        assert decision["updatedInput"] == tool_input
    else:
        assert "updatedInput" not in decision


def test_bridge_permission_failure_denies(monkeypatch):
    monkeypatch.setattr(mcp_bridge, "rpc_call", Mock(side_effect=ConnectionRefusedError()))
    result = mcp_bridge.permission_decision({"tool_name": "Bash", "input": {"command": "ls"}})
    assert json.loads(result["content"][0]["text"])["behavior"] == "deny"


def test_bridge_lists_the_permission_tool_only_when_enabled(monkeypatch):
    monkeypatch.setattr(mcp_bridge, "PERMISSION_TOOL_ENABLED", False)
    names = [tool["name"] for tool in mcp_bridge.listed_tools()]
    assert names == ["exec_python", "get_scene", "screenshot"]
    monkeypatch.setattr(mcp_bridge, "PERMISSION_TOOL_ENABLED", True)
    assert "approve_tool" in [tool["name"] for tool in mcp_bridge.listed_tools()]
    monkeypatch.setattr(mcp_bridge, "PERMISSION_TOOL_ENABLED", False)
    monkeypatch.setattr(mcp_bridge, "RPC_PORT", 1234)
    monkeypatch.setattr(mcp_bridge, "RPC_TOKEN", "TOKEN")
    result = mcp_bridge.handle_tool_call("approve_tool", {"tool_name": "Bash", "input": {}})
    assert result["isError"]


def test_runner_workspaces_configs_and_attachments_are_independent(qapp):
    first = claude_runner.ClaudeRunner(1234, "FIRST_TEST_TOKEN")
    second = claude_runner.ClaudeRunner(2345, "SECOND_TEST_TOKEN")
    try:
        assert first._workdir != second._workdir
        first._write_mcp_config()
        second._write_mcp_config()
        config = json.loads(Path(first._mcp_config_path).read_text())
        assert config["mcpServers"]["freecad"]["env"]["FREECAD_RPC_TOKEN"] == "FIRST_TEST_TOKEN"
        attachment = first.stage_attachment("part.txt", data=b"first")
        second.reset_session()
        assert Path(attachment).read_bytes() == b"first"
        second.close()
        assert Path(first._workdir).is_dir()
    finally:
        first.close()
        second.close()


def test_screenshot_workspaces_are_independent(qapp):
    first, second = rpc_server.FreeCADRpcServer(), rpc_server.FreeCADRpcServer()
    try:
        assert first._screenshot_dir != second._screenshot_dir
        first.close()
        assert Path(second._screenshot_dir).is_dir()
    finally:
        first.close()
        second.close()


def test_private_writes_start_with_mode_600_and_ignore_legacy_tmp_symlinks(tmp_path, monkeypatch):
    target, victim = tmp_path / "config.json", tmp_path / "victim.json"
    victim.write_text('"sentinel"')
    if os.name == "posix":
        Path(str(target) + ".tmp").symlink_to(victim)
    created_modes = []
    original = paths.tempfile.mkstemp

    def create(*args, **kwargs):
        fd, path = original(*args, **kwargs)
        created_modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return fd, path

    monkeypatch.setattr(paths.tempfile, "mkstemp", create)
    paths.write_private_file(target, '{"apiKey":"FAKE_TEST_KEY"}')
    if os.name == "posix":
        assert created_modes == [0o600]
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert victim.read_text() == '"sentinel"'
    assert json.loads(target.read_text())["apiKey"] == "FAKE_TEST_KEY"
    assert not list(tmp_path.glob(".config.json-*"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink test")
def test_private_writes_reject_symlink_destinations(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("sentinel")
    link = tmp_path / "config.json"
    link.symlink_to(victim)
    with pytest.raises(OSError, match="symbolic link"):
        paths.write_private_file(link, "replacement")
    assert victim.read_text() == "sentinel"


def accept_socket(server, sock, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(server._server, "hasPendingConnections", Mock(side_effect=[True, False]))
        patch.setattr(server._server, "nextPendingConnection", lambda: sock)
        server._on_new_connection()


def flush_deletes(qapp):
    qapp.processEvents()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)


def test_disconnected_qt_sockets_are_deleted(server, monkeypatch, qapp):
    for _ in range(12):
        sock = QtNetwork.QTcpSocket(server._server)
        accept_socket(server, sock, monkeypatch)
        sock.disconnected.emit()
    flush_deletes(qapp)
    assert not server._buffers and not server._timers
    assert not server._server.findChildren(QtNetwork.QTcpSocket)


def test_idle_rpc_connections_expire(server, monkeypatch, wait_until, qapp):
    monkeypatch.setattr(rpc_server, "REQUEST_TIMEOUT_MS", 5)
    sock = QtNetwork.QTcpSocket(server._server)
    accept_socket(server, sock, monkeypatch)
    wait_until(lambda: not server._buffers)
    flush_deletes(qapp)
    assert not server._timers
    assert not server._server.findChildren(QtNetwork.QTcpSocket)


def test_rpc_connection_count_is_bounded(server, monkeypatch, qapp):
    for _ in range(rpc_server.MAX_CONNECTIONS + 3):
        accept_socket(server, QtNetwork.QTcpSocket(server._server), monkeypatch)
    flush_deletes(qapp)
    assert len(server._buffers) == rpc_server.MAX_CONNECTIONS
    assert len(server._server.findChildren(QtNetwork.QTcpSocket)) == rpc_server.MAX_CONNECTIONS
    server.end_turn(server.token)
    flush_deletes(qapp)
    assert not server._server.findChildren(QtNetwork.QTcpSocket)


class InputSocket(QtCore.QObject):
    """Transport double to exercise the complete untrusted-input dispatch path."""
    readyRead = QtCore.Signal()
    disconnected = QtCore.Signal()

    def __init__(self, payload, parent):
        super().__init__(parent)
        self.payload = payload
        self.written = bytearray()
        self.closed = False

    def setReadBufferSize(self, size):
        self.limit = size

    def bytesAvailable(self):
        return len(self.payload)

    def readAll(self):
        data, self.payload = self.payload, b""
        return QtCore.QByteArray(data)

    def write(self, data):
        self.written.extend(data)
        return len(data)

    def flush(self):
        pass

    def state(self):
        return (QtNetwork.QAbstractSocket.UnconnectedState if self.closed
                else QtNetwork.QAbstractSocket.ConnectedState)

    def disconnectFromHost(self):
        if not self.closed:
            self.closed = True
            self.disconnected.emit()

    abort = disconnectFromHost


@pytest.mark.parametrize("kind", ["missing-token", "bad-token", "expired-token", "surrogate", "malformed", "bad-op"])
def test_rpc_rejects_untrusted_requests_before_execution(server, monkeypatch, kind):
    request = {"token": server.token, "op": "exec", "code": "raise AssertionError('must not execute')"}
    if kind == "missing-token":
        del request["token"]
    elif kind == "bad-token":
        request["token"] = "INVALID_TEST_TOKEN"
    elif kind == "expired-token":
        server.begin_turn()
    elif kind == "surrogate":
        request["token"] = "\ud800"
    elif kind == "bad-op":
        request["op"] = "unknown"
    payload = b"[]\n" if kind == "malformed" else (json.dumps(request) + "\n").encode()
    sock = InputSocket(payload, server._server)
    accept_socket(server, sock, monkeypatch)
    assert sock.closed
    assert not server._buffers and not server._timers
    import FreeCAD as App
    App.setActiveTransaction.assert_not_called()


def test_rpc_caps_individual_and_total_request_buffers(server, monkeypatch):
    monkeypatch.setattr(rpc_server, "MAX_REQUEST_BYTES", 16)
    monkeypatch.setattr(rpc_server, "MAX_TOTAL_BUFFER_BYTES", 20)
    first = InputSocket(b"a" * 12, server._server)
    accept_socket(server, first, monkeypatch)
    assert not first.closed
    second = InputSocket(b"b" * 12, server._server)
    accept_socket(server, second, monkeypatch)
    assert second.closed and not first.closed
    oversized = InputSocket(b"c" * 17, server._server)
    accept_socket(server, oversized, monkeypatch)
    assert oversized.closed


def test_disconnected_client_without_a_signal_is_cleaned(server, monkeypatch):
    class SilentSocket(InputSocket):
        def disconnectFromHost(self):
            self.closed = True

    sock = SilentSocket(b'{"op":"scene","token":"invalid"}\n', server._server)
    accept_socket(server, sock, monkeypatch)
    assert not server._buffers and not server._timers


def test_slow_response_readers_have_a_deadline(server, monkeypatch, wait_until):
    class SlowSocket(InputSocket):
        def disconnectFromHost(self):
            pass  # Simulate a socket waiting to drain its output buffer.

    import FreeCAD as App
    monkeypatch.setattr(App, "listDocuments", lambda: {}, raising=False)
    monkeypatch.setattr(rpc_server, "REQUEST_TIMEOUT_MS", 5)
    payload = (json.dumps({"op": "scene", "token": server.token}) + "\n").encode()
    sock = SlowSocket(payload, server._server)
    accept_socket(server, sock, monkeypatch)
    assert sock.written and server._buffers
    wait_until(lambda: not server._buffers)
    assert not server._timers


def test_real_local_rpc_transport_rejects_stale_tokens(qapp, monkeypatch, wait_until):
    # Ordinary CI runners can use loopback. Some managed Codex sandboxes
    # prohibit listen(); skip only this optional transport integration there.
    monkeypatch.setattr(QtNetwork.QTcpServer, "listen", REAL_LISTEN)
    monkeypatch.setattr(QtNetwork.QTcpServer, "serverPort", REAL_SERVER_PORT)
    try:
        rpc = rpc_server.FreeCADRpcServer()
    except RuntimeError as exc:
        pytest.skip("Loopback listening unavailable: %s" % exc)
    import FreeCAD as App
    monkeypatch.setattr(App, "listDocuments", lambda: {}, raising=False)

    def exchange(token):
        client = QtNetwork.QTcpSocket()
        data = bytearray()
        request = (json.dumps({"op": "scene", "token": token}) + "\n").encode()
        client.connected.connect(lambda: client.write(request))
        client.readyRead.connect(lambda: data.extend(bytes(client.readAll())))
        client.connectToHost(QtNetwork.QHostAddress.LocalHost, rpc.port)
        try:
            wait_until(lambda: bool(data) and client.state() == QtNetwork.QAbstractSocket.UnconnectedState)
            return json.loads(data)
        finally:
            client.abort()
            client.deleteLater()

    try:
        token = rpc.begin_turn()
        assert exchange(token)["ok"]
        rpc.end_turn(token)
        assert not exchange(token)["ok"]
        fresh = rpc.begin_turn()
        assert not exchange(token)["ok"]
        assert exchange(fresh)["ok"]
        wait_until(lambda: not rpc._buffers)
    finally:
        rpc.close()


def test_truncated_and_oversized_stream_events_are_bounded(qapp, monkeypatch):
    runner = Recorder()
    errors = []
    runner.error.connect(errors.append)
    monkeypatch.setattr(runner_base, "MAX_EVENT_BYTES", 32)
    try:
        assert runner._drain_events(b'{"unfinished":"' + b"x" * 40) == b""
        assert runner._drain_events(b'{"type":"result"', final=True) == b""
        assert runner.events == [] and len(errors) == 2
        assert runner._drain_events(b'{"type":"result"}') == b""
        assert runner.events == [{"type": "result"}]
    finally:
        runner.close()


def test_duplicate_attachment_names_never_replace_existing_copies(qapp, tmp_path):
    runner = Recorder()
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    (first / "part.txt").write_text("first")
    (second / "part.txt").write_text("second")
    try:
        a = runner.stage_attachment("part.txt", source_path=first / "part.txt")
        b = runner.stage_attachment("part.txt", source_path=second / "part.txt")
        c = runner.stage_attachment("part.txt", data=b"image")
        assert len({a, b, c}) == 3
        assert [Path(path).read_bytes() for path in (a, b, c)] == [b"first", b"second", b"image"]
    finally:
        runner.close()


@pytest.mark.parametrize("noise", [
    b"warning {not-json} ", b"warning {unclosed\n", b'{"bad":oops}\n',
    b"warning {not-json ",
    b'warning {"x":"a } brace",invalid} ',
])
def test_malformed_banners_do_not_hide_following_events(qapp, noise):
    runner = Recorder()
    try:
        assert runner._drain_events(noise + b'{"type":"result"}') == b""
        assert runner.events == [{"type": "result"}]
    finally:
        runner.close()


def test_mcp_survives_malformed_requests_and_still_answers_ping():
    bridge = Path(__file__).resolve().parents[1] / "claudecode_wb/mcp_bridge.py"
    bad = [
        "[]", "{", '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":[]}',
        '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"exec_python","arguments":[]}}',
        '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"screenshot","arguments":{"width":true}}}',
        '{"jsonrpc":"2.0","id":4,"method":"initialize","params":{"protocolVersion":[]}}',
    ]
    ping = {"jsonrpc": "2.0", "id": 99, "method": "ping"}
    result = subprocess.run([sys.executable, "-B", str(bridge)],
                            input="\n".join(bad + [json.dumps(ping)]) + "\n",
                            text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(responses) == len(bad) + 1
    assert all("error" in response for response in responses[:-1])
    assert responses[-1] == {"jsonrpc": "2.0", "id": 99, "result": {}}
