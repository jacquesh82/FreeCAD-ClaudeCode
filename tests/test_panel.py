from unittest.mock import Mock

import pytest

from claudecode_wb import approval, claude_runner, rpc_server


def test_missing_claude_preserves_prompt_and_restores_controls(panel, monkeypatch):
    monkeypatch.setattr(claude_runner, "find_claude", lambda: None)
    panel.prompt_edit.setPlainText("Créer une pièce")
    panel._on_send()
    assert panel.send_btn.isEnabled()
    assert not panel.stop_btn.isEnabled()
    assert not panel.runner.running
    assert panel.prompt_edit.toPlainText() == "Créer une pièce"
    assert "not found" in panel.history.toPlainText()


def test_failed_qprocess_restores_panel(panel, monkeypatch, tmp_path, wait_until):
    monkeypatch.setattr(claude_runner, "find_claude", lambda: str(tmp_path / "missing"))
    panel.prompt_edit.setPlainText("hello")
    panel._on_send()
    wait_until(lambda: not panel.runner.running)
    assert panel.send_btn.isEnabled()
    assert not panel.stop_btn.isEnabled()
    assert "process error" in panel.history.toPlainText()


@pytest.mark.parametrize("stage", ["rpc", "attachments", "configuration"])
def test_preparation_errors_preserve_inputs(panel, monkeypatch, stage):
    if stage == "rpc":
        monkeypatch.setattr(rpc_server, "get_server", Mock(side_effect=RuntimeError("cannot listen")))
    elif stage == "attachments":
        monkeypatch.setattr(panel, "_stage_attachments", Mock(side_effect=OSError("unreadable")))
    else:
        monkeypatch.setattr(claude_runner, "find_claude", lambda: "claude")
        monkeypatch.setattr(panel.runner, "_write_mcp_config", Mock(side_effect=OSError("read-only")))
    panel.prompt_edit.setPlainText("hello")
    panel._on_send()
    assert panel.send_btn.isEnabled()
    assert not panel.stop_btn.isEnabled()
    assert panel.prompt_edit.toPlainText() == "hello"


def test_switching_agent_immediately_disables_rpc_auto_execution(panel):
    server = rpc_server.get_server()
    server.auto_approve = True
    panel.auto_run.setChecked(True)
    panel.agent_combo.setCurrentText("ZCode")
    assert not server.auto_approve
    assert not panel.auto_run.isChecked()
    assert not panel.skip_perms.isChecked()


def test_previous_agents_completion_does_not_enable_send_during_current_turn(panel):
    old = panel.runner
    panel.agent_combo.setCurrentText("ZCode")
    panel.runner._turn_active = True
    panel.send_btn.setEnabled(False)
    panel.stop_btn.setEnabled(True)
    old.turn_finished.emit(False, "old-session")
    assert not panel.send_btn.isEnabled()
    assert panel.stop_btn.isEnabled()
    panel.runner._complete_turn(False)


def test_consent_dialog_lists_attachments(qapp, monkeypatch):
    from PySide import QtWidgets
    from claudecode_wb.panel import AGENTS

    monkeypatch.setattr(approval, "has_consent", lambda provider: False)
    texts = []

    def cancel(box):
        texts.append(box.text())

    monkeypatch.setattr(QtWidgets.QMessageBox, "exec_", cancel)
    assert not approval.ensure_consent(AGENTS[0])
    assert "full contents of any files or images you attach" in texts[0]
