"""Commandes GUI de l'addon Claude Code."""

import os

import FreeCADGui as Gui

_ICON = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "Resources", "icons", "claudecode.svg"
)


class ToggleClaudePanelCommand:
    def GetResources(self):
        return {
            "Pixmap": _ICON,
            "MenuText": "Volet Claude Code",
            "ToolTip": "Afficher/masquer le volet de l'agent Claude Code",
        }

    def Activated(self):
        from . import panel

        panel.toggle_panel()

    def IsActive(self):
        return True


Gui.addCommand("ClaudeCode_TogglePanel", ToggleClaudePanelCommand())
