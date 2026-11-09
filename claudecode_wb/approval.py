"""User-facing approval and consent dialogs required by the FreeCAD Addon
Index rules for AI agents.

* ``request_exec_approval`` shows the complete model-generated code inside
  FreeCAD and runs it only once the user approves (the panel can opt in to
  automatic execution, which bypasses this dialog).
* ``request_tool_approval`` shows the complete input of any other agent tool
  call (shell command, file write, file read outside the attachments…) and
  allows it only once the user approves. There is no automatic mode.
* ``ensure_consent`` asks, once per remote AI service, for consent to send
  the listed kinds of data. The answer is recorded in FreeCAD's parameters
  and asked again when the list of data kinds changes.
"""

import hashlib
import html
import json
import re

import FreeCAD as App
import FreeCADGui as Gui

from PySide import QtCore, QtGui, QtWidgets

PARAM_PATH = "User parameter:BaseApp/Preferences/Mod/ClaudeCodeConnector"

# Kinds of data a turn may send to the AI service. Changing this tuple
# invalidates previously recorded consent (its hash is part of the key).
DATA_KINDS = (
    "your prompts and the whole conversation history of the session",
    "the Python code the agent writes, everything it prints, and error "
    "messages (tracebacks) from running it",
    "the names, labels, types and bounding boxes of the objects in your open "
    "documents, and the documents' file paths (get_scene tool)",
    "screenshots of the active 3D view, uploaded as PNG images "
    "(screenshot tool)",
    "the full contents of any files or images you attach to a prompt",
    "if you enable the agent's other tools: the output of each shell "
    "command, file read or web request you approve",
)

_MONO = QtGui.QFont("monospace")
_MONO.setStyleHint(QtGui.QFont.TypeWriter)
_active_dialog = None


def params():
    return App.ParamGet(PARAM_PATH)


def _main_window():
    try:
        return Gui.getMainWindow()
    except Exception:  # noqa: BLE001
        return None


# --- code approval ----------------------------------------------------------


class ApprovalDialog(QtWidgets.QDialog):
    """Shows exactly what the agent wants to run; Run (Allow) or Deny."""

    def __init__(self, title, intro_html, text, run_label, parent=None):
        super().__init__(parent)
        self.setWindowTitle("%s — Claude Code Connector" % title)
        self.setModal(True)
        self.resize(760, 520)

        layout = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(intro_html)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.code_view = QtWidgets.QPlainTextEdit()
        self.code_view.setReadOnly(True)
        self.code_view.setFont(_MONO)
        self.code_view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.code_view.setPlainText(text)
        layout.addWidget(self.code_view, stretch=1)

        buttons = QtWidgets.QDialogButtonBox()
        self.run_btn = buttons.addButton(run_label, QtWidgets.QDialogButtonBox.AcceptRole)
        self.deny_btn = buttons.addButton("Deny", QtWidgets.QDialogButtonBox.RejectRole)
        # Deny is the default so that a stray Enter never runs anything.
        self.run_btn.setAutoDefault(False)
        self.run_btn.setDefault(False)
        self.deny_btn.setAutoDefault(True)
        self.deny_btn.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def exec_approval_dialog(code, agent_name, parent=None):
    return ApprovalDialog(
        "Approve code execution",
        "<b>%s wants to run the Python code below inside FreeCAD, with your "
        "full user permissions.</b><br>Documents and other files the agent "
        "reads may contain instructions planted by someone else; reviewing "
        "this code is your main protection. Run it only if you understand "
        "what it does." % html.escape(agent_name),
        code,
        "Run this code",
        parent,
    )


def format_tool_input(tool_input):
    """Render a tool input completely and readably: every field is shown,
    string values verbatim (commands, file contents), others as JSON."""
    parts = []
    for key, value in tool_input.items():
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, indent=2, ensure_ascii=False)
        parts.append("── %s ──\n%s" % (key, text))
    return "\n\n".join(parts) if parts else "(no input)"


def tool_approval_dialog(tool_name, tool_input, agent_name, parent=None):
    return ApprovalDialog(
        "Approve agent tool",
        "<b>%s wants to use its <code>%s</code> tool with the input below, "
        "with your full user permissions.</b><br>This may run a shell "
        "command, read or modify files, or access the network; its result is "
        "sent to the AI service. Files the agent reads may contain "
        "instructions planted by someone else; allow it only if you "
        "understand what it does." % (html.escape(agent_name), html.escape(tool_name)),
        format_tool_input(tool_input),
        "Allow",
        parent,
    )


def _run_dialog(dialog):
    global _active_dialog
    _active_dialog = dialog
    try:
        return dialog.exec_() == QtWidgets.QDialog.Accepted
    finally:
        _active_dialog = None


def request_exec_approval(code, agent_name="The agent"):
    """Block until the user approves or denies the code. Returns True to run."""
    return _run_dialog(exec_approval_dialog(code, agent_name, parent=_main_window()))


def request_tool_approval(tool_name, tool_input, agent_name="The agent"):
    """Block until the user allows or denies the tool call. True to allow."""
    return _run_dialog(
        tool_approval_dialog(tool_name, tool_input, agent_name, parent=_main_window())
    )


def cancel_pending_approval():
    """Reject a pending review when the requesting turn loses RPC access."""
    if _active_dialog is not None:
        _active_dialog.reject()


# --- consent to send data ---------------------------------------------------


def _consent_key(provider):
    digest = hashlib.sha1("\n".join(DATA_KINDS).encode("utf-8")).hexdigest()[:10]
    safe = re.sub(r"[^A-Za-z0-9]+", "_", provider)
    return "Consent_%s_%s" % (safe, digest)


def has_consent(provider):
    return params().GetBool(_consent_key(provider), False)


def revoke_all_consents():
    group = params()
    for name in list(group.GetBools()):
        if name.startswith("Consent_"):
            group.RemBool(name)


def ensure_consent(agent, parent=None):
    """Ask for consent to send data to ``agent['provider']`` unless already
    recorded for the current list of data kinds. Returns True if consented."""
    provider = agent["provider"]
    if has_consent(provider):
        return True

    items = "".join("<li>%s</li>" % html.escape(kind) for kind in DATA_KINDS)
    text = (
        "<p><b>Before the first request to %(provider)s</b></p>"
        "<p>The addon will launch the <code>%(cli)s</code> command-line agent, "
        "which connects to <b>%(provider)s</b>'s servers. %(login)s</p>"
        "<p>Every prompt you send may transmit the following data to "
        "%(provider)s, including data sent automatically as tool results:</p>"
        "<ul>%(items)s</ul>"
        "<p>If your designs are confidential, do not continue. You will be "
        "asked again if you switch to another service or if the kinds of "
        "data sent change.</p>"
        "<p>Do you consent to sending this data to %(provider)s?</p>"
    ) % {
        "provider": html.escape(provider),
        "cli": html.escape(agent["cli"]),
        "login": html.escape(agent.get("login_note", "")),
        "items": items,
    }

    box = QtWidgets.QMessageBox(parent or _main_window())
    box.setWindowTitle("Data consent — Claude Code Connector")
    box.setIcon(QtWidgets.QMessageBox.Warning)
    box.setTextFormat(QtCore.Qt.RichText)
    box.setText(text)
    agree = box.addButton("I consent", QtWidgets.QMessageBox.AcceptRole)
    box.addButton("Cancel", QtWidgets.QMessageBox.RejectRole)
    box.setDefaultButton(QtWidgets.QMessageBox.Cancel)
    box.exec_()
    if box.clickedButton() is not agree:
        return False
    params().SetBool(_consent_key(provider), True)
    App.Console.PrintMessage(
        "[Claude Code Connector] consent recorded for %s\n" % provider
    )
    return True


def confirm_dangerous_option(title, text, parent=None):
    """Yes/No confirmation shown when an opt-in dangerous mode is enabled."""
    answer = QtWidgets.QMessageBox.warning(
        parent or _main_window(),
        title,
        text,
        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        QtWidgets.QMessageBox.No,
    )
    return answer == QtWidgets.QMessageBox.Yes
