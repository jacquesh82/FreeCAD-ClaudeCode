"""Run the Qt regressions without launching FreeCAD or contacting an agent."""

import importlib.util
import os
import sys
import time
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if importlib.util.find_spec("PySide") is None:
    from PySide6 import QtCore, QtGui, QtNetwork, QtWidgets

    shim = types.ModuleType("PySide")
    shim.QtCore, shim.QtGui = QtCore, QtGui
    shim.QtNetwork, shim.QtWidgets = QtNetwork, QtWidgets
    sys.modules["PySide"] = shim

from PySide import QtCore, QtWidgets

app_stub = types.ModuleType("FreeCAD")
app_stub.Console = Mock()
app_stub.ActiveDocument = None
sys.modules["FreeCAD"] = app_stub
gui_stub = types.ModuleType("FreeCADGui")
gui_stub.getMainWindow = lambda: None
sys.modules["FreeCADGui"] = gui_stub


@pytest.fixture(scope="session")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def wait_until(qapp):
    def wait(predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                pytest.fail("Qt operation did not finish within %s seconds" % timeout)
            qapp.processEvents()
            QtCore.QThread.msleep(1)
        qapp.processEvents()

    return wait


@pytest.fixture(autouse=True)
def private_storage(tmp_path, monkeypatch):
    from claudecode_wb import paths, rpc_server

    monkeypatch.setattr(paths, "_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(rpc_server, "_instance", None)
    # Unit tests exercise dispatch/UI without opening a listening socket.
    monkeypatch.setattr(rpc_server.QtNetwork.QTcpServer, "listen", lambda *a: True)
    monkeypatch.setattr(rpc_server.QtNetwork.QTcpServer, "serverPort", lambda *a: 12345)
    yield
    if rpc_server._instance is not None:
        rpc_server._instance.close()


@pytest.fixture
def panel(qapp, monkeypatch):
    from claudecode_wb import approval
    from claudecode_wb.panel import ClaudeCodePanel

    monkeypatch.setattr(approval, "ensure_consent", lambda *a, **kw: True)
    dock = ClaudeCodePanel()
    dock.agent_combo.setCurrentText("Claude Code")
    yield dock
    for runner in dock.runners.values():
        runner.close()
    dock.close()
    dock.deleteLater()
    qapp.processEvents()
