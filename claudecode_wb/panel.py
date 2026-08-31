"""Dockable "Claude Code Connector" panel: chat with the agent that drives FreeCAD."""

import html

import FreeCADGui as Gui

from PySide import QtCore, QtGui, QtWidgets

from . import claude_runner, rpc_server

_dock = None


class _PromptEdit(QtWidgets.QPlainTextEdit):
    """Input area: Enter sends, Shift+Enter inserts a newline."""

    submitted = QtCore.Signal()

    def keyPressEvent(self, event):
        if event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter) and not (
            event.modifiers() & QtCore.Qt.ShiftModifier
        ):
            self.submitted.emit()
            return
        super().keyPressEvent(event)


class ClaudeCodePanel(QtWidgets.QDockWidget):
    def __init__(self, parent=None):
        super().__init__("Claude Code Connector", parent)
        self.setObjectName("ClaudeCodePanel")

        server = rpc_server.get_server()
        self.runner = claude_runner.ClaudeRunner(server.port, server.token, parent=self)
        self.runner.text_received.connect(self._on_assistant_text)
        self.runner.tool_used.connect(self._on_tool_used)
        self.runner.turn_finished.connect(self._on_turn_finished)
        self.runner.error.connect(self._on_error)

        body = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(body)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # Top bar: model + new conversation
        top = QtWidgets.QHBoxLayout()
        self.model_combo = QtWidgets.QComboBox()
        self.model_combo.addItems(["default", "sonnet", "opus", "haiku"])
        self.model_combo.setToolTip("Claude model to use")
        top.addWidget(QtWidgets.QLabel("Model:"))
        top.addWidget(self.model_combo)
        top.addStretch()
        self.new_btn = QtWidgets.QPushButton("New conversation")
        self.new_btn.clicked.connect(self._on_new_conversation)
        top.addWidget(self.new_btn)
        layout.addLayout(top)

        # History
        self.history = QtWidgets.QTextBrowser()
        self.history.setOpenExternalLinks(True)
        self.history.setMinimumHeight(200)
        layout.addWidget(self.history, stretch=1)

        # Status
        self.status = QtWidgets.QLabel("Ready — FreeCAD RPC port: %d" % server.port)
        self.status.setStyleSheet("color: gray;")
        layout.addWidget(self.status)

        # Permissions option
        self.skip_perms = QtWidgets.QCheckBox(
            "Allow all tools (files, bash…) — at your own risk"
        )
        self.skip_perms.setToolTip(
            "By default only the FreeCAD tools (exec_python, get_scene, "
            "screenshot) are allowed. Note that exec_python already runs "
            "arbitrary Python inside FreeCAD with your permissions. This "
            "checkbox enables --dangerously-skip-permissions, which also "
            "grants file and shell access."
        )
        layout.addWidget(self.skip_perms)

        # Data disclosure warning
        disclosure = QtWidgets.QLabel(
            "⚠ Each prompt you send may upload your design data to Anthropic: "
            "scene contents (object names, file paths), screenshots of the 3D "
            "view, and exec output are sent to the Claude API as tool results. "
            "The agent can run arbitrary Python inside FreeCAD with your "
            "permissions."
        )
        disclosure.setWordWrap(True)
        disclosure.setStyleSheet("color: #8a6d3b;")
        layout.addWidget(disclosure)

        # Input + buttons
        self.prompt_edit = _PromptEdit()
        self.prompt_edit.setPlaceholderText(
            "Ask the agent to model something… (Enter to send, "
            "Shift+Enter for a newline)"
        )
        self.prompt_edit.setFixedHeight(70)
        self.prompt_edit.submitted.connect(self._on_send)
        layout.addWidget(self.prompt_edit)

        buttons = QtWidgets.QHBoxLayout()
        self.send_btn = QtWidgets.QPushButton("Send")
        self.send_btn.clicked.connect(self._on_send)
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.clicked.connect(self.runner.stop)
        self.stop_btn.setEnabled(False)
        buttons.addWidget(self.send_btn)
        buttons.addWidget(self.stop_btn)
        layout.addLayout(buttons)

        self.setWidget(body)
        self._append_html(
            "<i>Connected via your Claude Code subscription (<code>claude</code> "
            "CLI). The agent has the FreeCAD tools: exec_python, get_scene, "
            "screenshot. Nothing is sent until you type a prompt; once you do, "
            "tool results (scene data, screenshots, exec output) go to "
            "Anthropic's API.</i>"
        )

    # --- display ----------------------------------------------------------

    def _append_html(self, fragment):
        self.history.append(fragment)
        self.history.verticalScrollBar().setValue(
            self.history.verticalScrollBar().maximum()
        )

    # --- actions ----------------------------------------------------------

    def _on_send(self):
        prompt = self.prompt_edit.toPlainText().strip()
        if not prompt or self.runner.running:
            return
        self.prompt_edit.clear()
        self._append_html(
            "<p><b style='color:#2266cc;'>You:</b> %s</p>" % html.escape(prompt)
        )
        model = self.model_combo.currentText()
        self.runner.send_prompt(
            prompt,
            model="" if model == "default" else model,
            skip_permissions=self.skip_perms.isChecked(),
        )
        self.send_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status.setText("Agent running…")

    def _on_new_conversation(self):
        self.runner.stop()
        self.runner.reset_session()
        self.history.clear()
        self._append_html("<i>New conversation.</i>")
        self.status.setText("Ready")

    # --- runner signals ---------------------------------------------------

    def _on_assistant_text(self, text):
        self._append_html(
            "<p><b style='color:#cc6a1d;'>Claude:</b> %s</p>"
            % html.escape(text).replace("\n", "<br>")
        )

    def _on_tool_used(self, name, preview):
        self._append_html(
            "<p style='color:gray;'>&#128295; <code>%s</code> %s</p>"
            % (html.escape(name), html.escape(preview))
        )

    def _on_error(self, message):
        self._append_html(
            "<p style='color:#bb2222;'><b>Error:</b> %s</p>"
            % html.escape(message).replace("\n", "<br>")
        )

    def _on_turn_finished(self, ok, session_id):
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status.setText(
            "Ready — session %s" % (session_id[:8] + "…" if session_id else "(none)")
        )


def show_panel():
    """Create (if needed) and show the panel in the main window."""
    global _dock
    main_window = Gui.getMainWindow()
    if _dock is None:
        try:
            _dock = ClaudeCodePanel(main_window)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                main_window, "Claude Code Connector", "Startup failed: %s" % exc
            )
            raise
        main_window.addDockWidget(QtCore.Qt.RightDockWidgetArea, _dock)
    _dock.show()
    _dock.raise_()


def toggle_panel():
    if _dock is not None and _dock.isVisible():
        _dock.hide()
    else:
        show_panel()
