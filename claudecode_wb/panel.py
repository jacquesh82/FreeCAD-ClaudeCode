"""Dockable "Claude Code Connector" panel: chat with the agent that drives FreeCAD.

Compliance with the FreeCAD Addon Index rules for AI agents lives here and in
rpc_server.py / approval.py:
  * no agent is selected by default; nothing starts until the user picks one;
  * consent to send data is asked before the first prompt to each service;
  * the RPC server starts at the first prompt, not when FreeCAD starts;
  * dangerous modes (auto-run of generated code, all CLI tools) are opt-in,
    off by default, confirmed by a dialog and shown as a visible warning.
"""

import html
import os

import FreeCADGui as Gui

from PySide import QtCore, QtGui, QtWidgets

from . import approval, claude_runner, rpc_server, zcode_runner

_dock = None

AGENT_PLACEHOLDER = "— choose an agent —"

# UI-facing description of each supported agent CLI. `models` feeds the model
# combo; ZCode has no --model flag (0.16.x) so it only offers "default".
AGENTS = [
    {
        "name": "Claude Code",
        "factory": claude_runner.ClaudeRunner,
        "models": ["default", "sonnet", "opus", "haiku"],
        "fixed_permissions": False,
        "provider": "Anthropic",
        "cli": "claude",
        "login_note": "It uses the Claude Code login already present on this "
                      "machine (your Anthropic subscription).",
    },
    {
        "name": "ZCode",
        "factory": zcode_runner.ZCodeRunner,
        "models": ["default"],
        "fixed_permissions": True,  # headless prompts always run in yolo mode
        "provider": "Z.AI",
        "cli": "zcode",
        "login_note": "It uses the Z.AI login of ZCode Desktop: the addon copies "
                      "the model provider selected in ZCode Desktop (including "
                      "its API key) into ~/.zcode/cli/config.json so the CLI "
                      "can run headless.",
    },
]

AUTO_RUN_WARNING = (
    "With this option the Python code written by the agent runs inside "
    "FreeCAD immediately, with your full user permissions, without being "
    "shown to you first.\n\nFiles you open may contain instructions planted "
    "by someone else that the agent will follow. Reviewing each run is your "
    "main protection.\n\nEnable automatic execution anyway?"
)

SKIP_PERMS_WARNING = (
    "With this option the agent's own tools (reading and writing any file, "
    "running shell commands, network access…) run without asking you, with "
    "your full user permissions. For Claude Code this is "
    "--dangerously-skip-permissions.\n\nThis is not a sandbox. Enable it anyway?"
)


def _human_size(size):
    for unit in ("B", "kB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.0f %s" % (size, unit) if unit == "B" else "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%d B" % size


class _PromptEdit(QtWidgets.QPlainTextEdit):
    """Input area: Enter sends, Shift+Enter inserts a newline. Pasted images
    and dropped/pasted files become attachments."""

    submitted = QtCore.Signal()
    image_pasted = QtCore.Signal(QtGui.QImage)
    files_dropped = QtCore.Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def keyPressEvent(self, event):
        if event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter) and not (
            event.modifiers() & QtCore.Qt.ShiftModifier
        ):
            self.submitted.emit()
            return
        super().keyPressEvent(event)

    def canInsertFromMimeData(self, source):
        return (
            source.hasImage()
            or source.hasUrls()
            or super().canInsertFromMimeData(source)
        )

    def insertFromMimeData(self, source):
        if source.hasImage():
            image = QtGui.QImage(source.imageData())
            if not image.isNull():
                self.image_pasted.emit(image)
                return
        if source.hasUrls():
            local = [u.toLocalFile() for u in source.urls() if u.isLocalFile()]
            local = [p for p in local if os.path.isfile(p)]
            if local:
                self.files_dropped.emit(local)
                return
        super().insertFromMimeData(source)


class ClaudeCodePanel(QtWidgets.QDockWidget):
    def __init__(self, parent=None):
        super().__init__("Claude Code Connector", parent)
        self.setObjectName("ClaudeCodePanel")

        self.runners = {}          # agent name -> runner (created on first use)
        self._agent_name = None    # None until the user picks an agent
        self._attachments = []     # pending attachments for the next prompt
        self._image_counter = 0

        body = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(body)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # Top bar: agent + model + new conversation
        top = QtWidgets.QHBoxLayout()
        self.agent_combo = QtWidgets.QComboBox()
        self.agent_combo.addItem(AGENT_PLACEHOLDER)
        self.agent_combo.addItems([agent["name"] for agent in AGENTS])
        self.agent_combo.setToolTip(
            "Agent CLI driving FreeCAD. Nothing is sent anywhere until you "
            "choose one and consent to the data it receives."
        )
        self.agent_combo.currentTextChanged.connect(self._on_agent_changed)
        top.addWidget(QtWidgets.QLabel("Agent:"))
        top.addWidget(self.agent_combo)
        self.model_combo = QtWidgets.QComboBox()
        top.addWidget(QtWidgets.QLabel("Model:"))
        top.addWidget(self.model_combo)
        top.addStretch()
        self.new_btn = QtWidgets.QPushButton("New conversation")
        self.new_btn.clicked.connect(self._on_new_conversation)
        top.addWidget(self.new_btn)
        self.menu_btn = QtWidgets.QToolButton()
        self.menu_btn.setText("⋯")
        self.menu_btn.setToolTip("More")
        self.menu_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        menu = QtWidgets.QMenu(self.menu_btn)
        menu.addAction("Revoke data consents…", self._on_revoke_consents)
        self.menu_btn.setMenu(menu)
        top.addWidget(self.menu_btn)
        layout.addLayout(top)

        # History
        self.history = QtWidgets.QTextBrowser()
        self.history.setOpenExternalLinks(True)
        self.history.setMinimumHeight(200)
        layout.addWidget(self.history, stretch=1)

        # Status
        self.status = QtWidgets.QLabel("Ready — choose an agent")
        self.status.setStyleSheet("color: gray;")
        layout.addWidget(self.status)

        # Opt-in dangerous modes (off by default, confirmed, visibly flagged)
        self.auto_run = QtWidgets.QCheckBox(
            "Run the agent's code without showing it to me first (dangerous)"
        )
        self.auto_run.setToolTip(
            "By default every exec_python run is displayed in FreeCAD and "
            "runs only once you approve it. This opt-in skips that review."
        )
        self.auto_run.clicked.connect(self._on_auto_run_clicked)
        layout.addWidget(self.auto_run)

        self.skip_perms = QtWidgets.QCheckBox(
            "Allow all agent tools (files, shell…) without approval (dangerous)"
        )
        self._skip_perms_tooltip = (
            "By default only the FreeCAD tools (exec_python, get_scene, "
            "screenshot) and reading your attachments are allowed to Claude "
            "Code. This checkbox enables --dangerously-skip-permissions, which "
            "also grants unrestricted file and shell access."
        )
        self.skip_perms.setToolTip(self._skip_perms_tooltip)
        self.skip_perms.clicked.connect(self._on_skip_perms_clicked)
        layout.addWidget(self.skip_perms)

        self.warning = QtWidgets.QLabel()
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet(
            "color: #a30000; background: #ffe5e5; border: 1px solid #a30000; "
            "padding: 4px;"
        )
        self.warning.hide()
        layout.addWidget(self.warning)

        # Data / execution disclosure (always visible, inside the interface)
        self.disclosure = QtWidgets.QLabel()
        self.disclosure.setWordWrap(True)
        self.disclosure.setStyleSheet("color: #8a6d3b;")
        layout.addWidget(self.disclosure)

        # Attachments
        self.attach_list = QtWidgets.QListWidget()
        self.attach_list.setFlow(QtWidgets.QListView.LeftToRight)
        self.attach_list.setWrapping(True)
        self.attach_list.setIconSize(QtCore.QSize(40, 40))
        self.attach_list.setMaximumHeight(70)
        self.attach_list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.attach_list.setToolTip("Files sent with the next prompt (Delete removes)")
        self.attach_list.hide()
        layout.addWidget(self.attach_list)
        shortcut_cls = getattr(QtGui, "QShortcut", None) or QtWidgets.QShortcut  # Qt6 / Qt5
        shortcut_cls(
            QtGui.QKeySequence.Delete, self.attach_list, self._on_remove_attachments
        )

        # Input + buttons
        self.prompt_edit = _PromptEdit()
        self.prompt_edit.setPlaceholderText(
            "Ask the agent to model something… (Enter to send, Shift+Enter "
            "for a newline; paste an image or drop files to attach them)"
        )
        self.prompt_edit.setFixedHeight(70)
        self.prompt_edit.submitted.connect(self._on_send)
        self.prompt_edit.image_pasted.connect(self._on_image_pasted)
        self.prompt_edit.files_dropped.connect(self._add_files)
        layout.addWidget(self.prompt_edit)

        buttons = QtWidgets.QHBoxLayout()
        self.send_btn = QtWidgets.QPushButton("Send")
        self.send_btn.clicked.connect(self._on_send)
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.clicked.connect(self._on_stop)
        self.stop_btn.setEnabled(False)
        self.attach_btn = QtWidgets.QPushButton("Attach file…")
        self.attach_btn.setToolTip(
            "Attach images, documents or CAD files to the next prompt. "
            "Their full contents are sent to the AI service."
        )
        self.attach_btn.clicked.connect(self._on_attach_clicked)
        self.clear_attach_btn = QtWidgets.QPushButton("Clear attachments")
        self.clear_attach_btn.clicked.connect(self._clear_attachments)
        self.clear_attach_btn.hide()
        buttons.addWidget(self.send_btn)
        buttons.addWidget(self.stop_btn)
        buttons.addStretch()
        buttons.addWidget(self.attach_btn)
        buttons.addWidget(self.clear_attach_btn)
        layout.addLayout(buttons)

        self.setWidget(body)
        self._refresh_agent_widgets()
        self._append_html(self._welcome_html())

    # --- agent selection ---------------------------------------------------

    @property
    def _agent(self):
        if self._agent_name is None:
            return None
        return next(a for a in AGENTS if a["name"] == self._agent_name)

    @property
    def runner(self):
        """Runner of the selected agent, created on first use (or None)."""
        agent = self._agent
        if agent is None:
            return None
        runner = self.runners.get(agent["name"])
        if runner is None:
            runner = agent["factory"](parent=self)
            runner.text_received.connect(self._on_assistant_text)
            runner.tool_used.connect(self._on_tool_used)
            runner.turn_finished.connect(self._on_turn_finished)
            runner.error.connect(self._on_error)
            self.runners[agent["name"]] = runner
        return runner

    def _on_agent_changed(self, name):
        previous = self._agent_name
        self._agent_name = name if name != AGENT_PLACEHOLDER else None
        if previous is not None and previous in self.runners and previous != self._agent_name:
            self.runners[previous].stop()
        self._refresh_agent_widgets()
        if self._agent_name is not None and previous != self._agent_name:
            self._append_html("<i>Switched to %s.</i>" % html.escape(self._agent_name))
            self.status.setText("Ready — session (none)")

    def _refresh_agent_widgets(self):
        """Update model combo, opt-in checkboxes and disclosures for the
        selected agent. Dangerous modes are reset to off on every switch."""
        agent = self._agent
        self.model_combo.clear()
        for box in (self.auto_run, self.skip_perms):
            box.blockSignals(True)
            box.setChecked(False)
            box.blockSignals(False)
        if rpc_server.is_running():
            rpc_server.get_server().auto_approve = False
        self.stop_btn.setEnabled(False)
        enabled = agent is not None
        for widget in (self.send_btn, self.model_combo, self.auto_run, self.skip_perms):
            widget.setEnabled(enabled)
        if agent is None:
            self.disclosure.setText(
                "Choose an agent to start. Nothing is sent anywhere until you "
                "do and consent to the data it receives."
            )
            self._refresh_warning()
            return

        self.model_combo.addItems(agent["models"])
        if agent["fixed_permissions"]:
            self.model_combo.setToolTip(
                "The ZCode CLI (0.16.x) has no --model flag; the account's "
                "default model is used."
            )
            self.skip_perms.setToolTip(
                "ZCode headless prompts always run with all tools allowed "
                "(--mode yolo); the CLI offers no per-tool allowlist. You must "
                "opt in here before prompts can be sent to ZCode."
            )
        else:
            self.model_combo.setToolTip("Claude model to use")
            self.skip_perms.setToolTip(self._skip_perms_tooltip)
        self.disclosure.setText(
            "⚠ Each prompt may upload your design data to %s: scene contents "
            "(object names, file paths), 3D-view screenshots, the agent's code "
            "and its output, and any attachments. The agent runs Python inside "
            "FreeCAD with your user permissions; every run is shown to you for "
            "approval first unless you opt in to automatic execution above."
            % agent["provider"]
        )
        self._refresh_warning()

    def _welcome_html(self):
        return (
            "<i>Choose an agent (Claude Code or ZCode) in the combo above. "
            "Before your first prompt to a service you will be asked to "
            "consent to the data it receives. The agent gets the FreeCAD "
            "tools exec_python, get_scene and screenshot; every exec_python "
            "run is displayed here in FreeCAD for your approval before it "
            "executes, and is undoable in one step.</i>"
        )

    # --- dangerous opt-ins -------------------------------------------------

    def _on_auto_run_clicked(self, checked):
        if checked and not approval.confirm_dangerous_option(
            "Automatic code execution", AUTO_RUN_WARNING, self
        ):
            self.auto_run.setChecked(False)
        if rpc_server.is_running():
            rpc_server.get_server().auto_approve = self.auto_run.isChecked()
        self._refresh_warning()

    def _on_skip_perms_clicked(self, checked):
        if checked and not approval.confirm_dangerous_option(
            "All agent tools without approval", SKIP_PERMS_WARNING, self
        ):
            self.skip_perms.setChecked(False)
        runner = self.runners.get(self._agent_name)
        if (not self.skip_perms.isChecked() and runner is not None
                and runner.running and runner.unrestricted):
            runner.stop()
            self.status.setText("Stopping agent — unrestricted permissions revoked")
        self._refresh_warning()

    def _refresh_warning(self):
        active = []
        if self.auto_run.isChecked():
            active.append(
                "the agent's Python code runs in FreeCAD WITHOUT being shown to you"
            )
        if self.skip_perms.isChecked() or any(
            runner.running and runner.unrestricted for runner in self.runners.values()
        ):
            active.append(
                "the agent's file, shell and network tools run WITHOUT approval"
            )
        if not active:
            self.warning.hide()
            return
        self.warning.setText(
            "⚠ DANGEROUS MODE: " + "; ".join(active)
            + ". Everything runs with your full user permissions."
        )
        self.warning.show()

    def _on_revoke_consents(self):
        approval.revoke_all_consents()
        self._append_html(
            "<i>Data consents revoked: you will be asked again before the "
            "next prompt.</i>"
        )

    # --- attachments -------------------------------------------------------

    def _on_attach_clicked(self):
        files, _filter = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Attach files to the next prompt"
        )
        self._add_files(files)

    def _add_files(self, file_paths):
        for path in file_paths:
            if not os.path.isfile(path):
                continue
            size = os.path.getsize(path)
            item = QtWidgets.QListWidgetItem(
                "%s (%s)" % (os.path.basename(path), _human_size(size))
            )
            image = QtGui.QImage(path)
            if not image.isNull():
                item.setIcon(QtGui.QIcon(QtGui.QPixmap.fromImage(image)))
            else:
                item.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_FileIcon))
            item.setToolTip(path)
            self._attachments.append(
                {"name": os.path.basename(path), "path": path, "size": size,
                 "image": image if not image.isNull() else None}
            )
            self.attach_list.addItem(item)
        self._refresh_attachments()

    def _on_image_pasted(self, image):
        self._image_counter += 1
        name = "pasted-image-%d.png" % self._image_counter
        item = QtWidgets.QListWidgetItem(
            "%s (%dx%d)" % (name, image.width(), image.height())
        )
        item.setIcon(QtGui.QIcon(QtGui.QPixmap.fromImage(image)))
        item.setToolTip("Image pasted from the clipboard")
        self._attachments.append({"name": name, "path": None, "image": image, "size": 0})
        self.attach_list.addItem(item)
        self._refresh_attachments()

    def _on_remove_attachments(self):
        rows = sorted((i.row() for i in self.attach_list.selectedIndexes()), reverse=True)
        for row in rows:
            self.attach_list.takeItem(row)
            del self._attachments[row]
        self._refresh_attachments()

    def _clear_attachments(self):
        self._attachments = []
        self.attach_list.clear()
        self._refresh_attachments()

    def _refresh_attachments(self):
        visible = bool(self._attachments)
        self.attach_list.setVisible(visible)
        self.clear_attach_btn.setVisible(visible)

    def _stage_attachments(self, runner):
        """Copy pending attachments into the runner's private directory and
        return their paths."""
        staged = []
        for att in self._attachments:
            if att["path"] is None:
                buffer = QtCore.QBuffer()
                buffer.open(QtCore.QIODevice.WriteOnly)
                att["image"].save(buffer, "PNG")
                staged.append(runner.stage_attachment(att["name"], data=bytes(buffer.data())))
            else:
                staged.append(runner.stage_attachment(att["name"], source_path=att["path"]))
        return staged

    def _attachments_html(self):
        parts = []
        for att in self._attachments:
            label = html.escape(att["name"])
            if att["image"] is not None:
                self._image_counter += 1
                url = QtCore.QUrl("attach:///%d.png" % self._image_counter)
                image = att["image"]
                if image.width() > 240:
                    image = image.scaledToWidth(240, QtCore.Qt.SmoothTransformation)
                self.history.document().addResource(
                    QtGui.QTextDocument.ImageResource, url, image
                )
                parts.append(
                    "<div>&#128206; %s<br><img src='%s'></div>" % (label, url.toString())
                )
            else:
                parts.append(
                    "<div>&#128206; %s (%s)</div>" % (label, _human_size(att["size"]))
                )
        return "".join(parts)

    # --- display ----------------------------------------------------------

    def _append_html(self, fragment):
        self.history.append(fragment)
        self.history.verticalScrollBar().setValue(
            self.history.verticalScrollBar().maximum()
        )

    # --- actions ----------------------------------------------------------

    def _on_send(self):
        agent = self._agent
        if agent is None:
            self.status.setText("Choose an agent first")
            return
        prompt = self.prompt_edit.toPlainText().strip()
        if (not prompt and not self._attachments) or self.runner.running:
            return
        if agent["fixed_permissions"] and not self.skip_perms.isChecked():
            self._append_html(
                "<p style='color:#bb2222;'>ZCode's headless mode runs all of "
                "its tools (files, shell…) without asking. Tick “Allow all "
                "agent tools” above to use it, or choose Claude Code.</p>"
            )
            return
        # Consent to send data to this service (asked once per service).
        if not approval.ensure_consent(agent, parent=self):
            self.status.setText("Cancelled — no data was sent")
            return

        # Start the local RPC server on first use and warn inside FreeCAD.
        first_start = not rpc_server.is_running()
        try:
            server = rpc_server.get_server()
            token = server.begin_turn()
        except Exception as exc:
            self._on_error("Cannot start FreeCAD RPC server: %s" % exc)
            self.status.setText("Unable to send — RPC server unavailable")
            return
        server.auto_approve = self.auto_run.isChecked()
        server.agent_name = agent["name"]
        server.decision_callback = self._on_exec_decision
        if first_start:
            self._append_html(
                "<p style='color:#8a6d3b;'>&#9888; Local RPC server started "
                "on 127.0.0.1:%d. Connected agents can run Python inside "
                "FreeCAD with your user permissions; each run is shown here "
                "for approval unless automatic execution is enabled.</p>"
                % server.port
            )
        runner = self.runner
        runner.set_rpc(server.port, token, revoke=lambda: server.end_turn(token))

        if not prompt:
            prompt = "See the attached files."
        try:
            attachments = self._stage_attachments(runner)
        except Exception as exc:
            server.end_turn(token)
            self._on_error("Cannot prepare attachments: %s" % exc)
            self.status.setText("Unable to send — check attachments")
            return

        model = self.model_combo.currentText()
        # Set the busy state before launch: even a synchronous failure can
        # now restore it without being overwritten after send_prompt returns.
        self.send_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status.setText("Preparing / running agent… (RPC port %d)" % server.port)
        try:
            accepted = runner.send_prompt(
                prompt,
                model="" if model == "default" else model,
                skip_permissions=self.skip_perms.isChecked(),
                attachments=attachments,
            )
        except Exception as exc:
            self._on_error("Cannot start agent: %s" % exc)
            accepted = False
        if not accepted:
            server.end_turn(token)
            self.send_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.status.setText("Unable to start agent — see error above")
            return
        self._append_html(
            "<p><b style='color:#2266cc;'>You:</b> %s</p>%s"
            % (html.escape(prompt).replace("\n", "<br>"), self._attachments_html())
        )
        self.prompt_edit.clear()
        self._clear_attachments()

    def _on_stop(self):
        if self.runner is not None:
            self.runner.stop()

    def _on_new_conversation(self):
        if self.runner is not None:
            self.runner.stop()
            self.runner.reset_session()
        self._clear_attachments()
        self.history.clear()
        self._append_html("<i>New conversation.</i>")
        self.status.setText("Ready")

    # --- runner / server signals -------------------------------------------

    def _on_exec_decision(self, approved, code):
        if self.auto_run.isChecked():
            self._append_html(
                "<p style='color:#a30000;'>&#9889; Code run automatically "
                "(%d lines, automatic execution enabled).</p>" % len(code.splitlines())
            )
        elif approved:
            self._append_html(
                "<p style='color:#2a7a2a;'>&#10004; Code approved and run "
                "(%d lines).</p>" % len(code.splitlines())
            )
        else:
            self._append_html(
                "<p style='color:#bb2222;'>&#10008; Code denied — not run.</p>"
            )

    def _on_assistant_text(self, text):
        self._append_html(
            "<p><b style='color:#cc6a1d;'>%s:</b> %s</p>"
            % (html.escape(self._agent_name or "Agent"),
               html.escape(text).replace("\n", "<br>"))
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
        self._refresh_warning()
        # An old agent may finish stopping after the user switched agents.
        if self.sender() is not self.runners.get(self._agent_name):
            return
        self.send_btn.setEnabled(self._agent is not None)
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
