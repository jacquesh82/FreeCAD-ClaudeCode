"""Volet ancrable « Claude Code » : chat avec l'agent qui pilote FreeCAD."""

import html

import FreeCADGui as Gui

from PySide import QtCore, QtGui, QtWidgets

from . import claude_runner, rpc_server

_dock = None


class _PromptEdit(QtWidgets.QPlainTextEdit):
    """Zone de saisie : Entrée envoie, Maj+Entrée insère un retour à la ligne."""

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
        super().__init__("Claude Code", parent)
        self.setObjectName("ClaudeCodePanel")

        server = rpc_server.get_server()
        self.runner = claude_runner.ClaudeRunner(server.port, parent=self)
        self.runner.text_received.connect(self._on_assistant_text)
        self.runner.tool_used.connect(self._on_tool_used)
        self.runner.turn_finished.connect(self._on_turn_finished)
        self.runner.error.connect(self._on_error)

        body = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(body)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # Barre du haut : modèle + nouvelle conversation
        top = QtWidgets.QHBoxLayout()
        self.model_combo = QtWidgets.QComboBox()
        self.model_combo.addItems(["défaut", "sonnet", "opus", "haiku"])
        self.model_combo.setToolTip("Modèle Claude à utiliser")
        top.addWidget(QtWidgets.QLabel("Modèle :"))
        top.addWidget(self.model_combo)
        top.addStretch()
        self.new_btn = QtWidgets.QPushButton("Nouvelle conversation")
        self.new_btn.clicked.connect(self._on_new_conversation)
        top.addWidget(self.new_btn)
        layout.addLayout(top)

        # Historique
        self.history = QtWidgets.QTextBrowser()
        self.history.setOpenExternalLinks(True)
        self.history.setMinimumHeight(200)
        layout.addWidget(self.history, stretch=1)

        # Statut
        self.status = QtWidgets.QLabel("Prêt — port RPC FreeCAD : %d" % server.port)
        self.status.setStyleSheet("color: gray;")
        layout.addWidget(self.status)

        # Option permissions
        self.skip_perms = QtWidgets.QCheckBox(
            "Autoriser tous les outils (fichiers, bash…) — à tes risques"
        )
        self.skip_perms.setToolTip(
            "Par défaut, seuls les outils FreeCAD (exec_python, get_scene, "
            "screenshot) sont autorisés. Cette case active "
            "--dangerously-skip-permissions."
        )
        layout.addWidget(self.skip_perms)

        # Saisie + boutons
        self.prompt_edit = _PromptEdit()
        self.prompt_edit.setPlaceholderText(
            "Demande à l'agent de modéliser quelque chose… (Entrée pour envoyer, "
            "Maj+Entrée pour un retour à la ligne)"
        )
        self.prompt_edit.setFixedHeight(70)
        self.prompt_edit.submitted.connect(self._on_send)
        layout.addWidget(self.prompt_edit)

        buttons = QtWidgets.QHBoxLayout()
        self.send_btn = QtWidgets.QPushButton("Envoyer")
        self.send_btn.clicked.connect(self._on_send)
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.clicked.connect(self.runner.stop)
        self.stop_btn.setEnabled(False)
        buttons.addWidget(self.send_btn)
        buttons.addWidget(self.stop_btn)
        layout.addLayout(buttons)

        self.setWidget(body)
        self._append_html(
            "<i>Connecté via ton abonnement Claude Code (CLI <code>claude</code>). "
            "L'agent dispose des outils FreeCAD : exec_python, get_scene, "
            "screenshot.</i>"
        )

    # --- affichage --------------------------------------------------------

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
            "<p><b style='color:#2266cc;'>Toi :</b> %s</p>" % html.escape(prompt)
        )
        model = self.model_combo.currentText()
        self.runner.send_prompt(
            prompt,
            model="" if model == "défaut" else model,
            skip_permissions=self.skip_perms.isChecked(),
        )
        self.send_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status.setText("Agent en cours d'exécution…")

    def _on_new_conversation(self):
        self.runner.stop()
        self.runner.reset_session()
        self.history.clear()
        self._append_html("<i>Nouvelle conversation.</i>")
        self.status.setText("Prêt")

    # --- signaux du runner ------------------------------------------------

    def _on_assistant_text(self, text):
        self._append_html(
            "<p><b style='color:#cc6a1d;'>Claude :</b> %s</p>"
            % html.escape(text).replace("\n", "<br>")
        )

    def _on_tool_used(self, name, preview):
        self._append_html(
            "<p style='color:gray;'>&#128295; <code>%s</code> %s</p>"
            % (html.escape(name), html.escape(preview))
        )

    def _on_error(self, message):
        self._append_html(
            "<p style='color:#bb2222;'><b>Erreur :</b> %s</p>"
            % html.escape(message).replace("\n", "<br>")
        )

    def _on_turn_finished(self, ok, session_id):
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status.setText(
            "Prêt — session %s" % (session_id[:8] + "…" if session_id else "(aucune)")
        )


def show_panel():
    """Crée (au besoin) et affiche le volet dans la fenêtre principale."""
    global _dock
    main_window = Gui.getMainWindow()
    if _dock is None:
        try:
            _dock = ClaudeCodePanel(main_window)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                main_window, "Claude Code", "Échec du démarrage : %s" % exc
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
