from unittest.mock import Mock

import pytest
import FreeCAD as App

from claudecode_wb.rpc_server import FreeCADRpcServer


@pytest.fixture
def server(monkeypatch):
    rpc = FreeCADRpcServer()
    rpc.begin_turn()
    rpc.auto_approve = True
    monkeypatch.setattr(App, "setActiveTransaction", Mock(return_value=42), raising=False)
    monkeypatch.setattr(App, "closeActiveTransaction", Mock(), raising=False)
    yield rpc
    rpc.close()


def test_exec_opens_global_transaction_without_an_active_document(server):
    response = server._op_exec('print("ok")')
    assert response == {"ok": True, "stdout": "ok\n"}
    App.setActiveTransaction.assert_called_once_with("The agent: agent code", True)
    App.closeActiveTransaction.assert_called_once_with(False, 42)


def test_exception_keeps_partial_changes_in_a_closed_undo_transaction(server):
    response = server._op_exec('print("before error")\nraise ValueError("bad code")')
    assert not response["ok"] and "ValueError" in response["error"]
    assert response["stdout"] == "before error\n"
    App.closeActiveTransaction.assert_called_once_with(False, 42)


def test_transaction_failure_prevents_execution(server):
    App.setActiveTransaction.side_effect = RuntimeError("undo unavailable")
    response = server._op_exec('App.should_not_exist = True')
    assert not response["ok"] and "undo unavailable" in response["error"]
    assert not hasattr(App, "should_not_exist")
    App.closeActiveTransaction.assert_not_called()
