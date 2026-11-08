"""Optional real-FreeCAD coverage in an isolated interpreter and user directory."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_transactions_on_real_freecad_documents(tmp_path):
    lib_dir = Path(os.environ.get("FREECAD_LIB_DIR", "/usr/lib/freecad/lib"))
    if not lib_dir.is_dir():
        pytest.skip("Set FREECAD_LIB_DIR to run the real FreeCAD integration test")
    env = os.environ.copy()
    for key in ("XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "FREECAD_USER_HOME"):
        env[key] = str(tmp_path)
    env["FREECAD_LIB_DIR"] = str(lib_dir)
    script = r'''
import importlib.util
import os
import sys
import types

sys.path.insert(0, os.environ["FREECAD_LIB_DIR"])
import FreeCAD as App
if importlib.util.find_spec("PySide") is None:
    from PySide6 import QtCore, QtGui, QtNetwork, QtWidgets
    shim = types.ModuleType("PySide")
    shim.QtCore, shim.QtGui = QtCore, QtGui
    shim.QtNetwork, shim.QtWidgets = QtNetwork, QtWidgets
    sys.modules["PySide"] = shim
from PySide import QtWidgets
qt = QtWidgets.QApplication([])
from claudecode_wb.rpc_server import FreeCADRpcServer
from unittest.mock import patch
from PySide import QtNetwork

# Exercise the real execution/undo path without opening a network listener.
with patch.object(QtNetwork.QTcpServer, "listen", return_value=True):
    server = FreeCADRpcServer()
server.begin_turn()
server.auto_approve = True
server.agent_name = "Regression"
server.decision_callback = None

result = server._op_exec(
    'doc = App.newDocument("New")\n'
    'doc.UndoMode = 1\n'
    'doc.addObject("App::FeaturePython", "Created")'
)
assert result["ok"], result
new = App.getDocument("New")
assert new.UndoCount == 1
new.undo()
assert len(new.Objects) == 0

other = App.newDocument("Other")
other.UndoMode = 1
result = server._op_exec(
    'App.getDocument("New").addObject("App::FeaturePython", "First")\n'
    'App.getDocument("Other").addObject("App::FeaturePython", "Second")'
)
assert result["ok"], result
assert new.UndoCount == 1 and other.UndoCount == 1
new.undo()
other.undo()
assert not new.Objects and not other.Objects

result = server._op_exec(
    'App.getDocument("Other").addObject("App::FeaturePython", "Partial")\n'
    'raise ValueError("test failure")'
)
assert not result["ok"] and "ValueError" in result["error"]
assert other.UndoCount == 1
other.undo()
assert not other.Objects
assert App.getActiveTransaction() is None
for name in list(App.listDocuments()):
    App.closeDocument(name)
server.close()
print("FREECAD_INTEGRATION_OK")
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FREECAD_INTEGRATION_OK" in result.stdout
