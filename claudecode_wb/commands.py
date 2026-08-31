"""GUI commands for the Claude Code Connector addon."""

import os

import FreeCADGui as Gui

_ICON = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "Resources", "icons", "claudecode.svg"
)


class ToggleClaudePanelCommand:
    def GetResources(self):
        return {
            "Pixmap": _ICON,
            "MenuText": "Claude Code Connector panel",
            "ToolTip": "Show/hide the Claude Code Connector agent panel",
        }

    def Activated(self):
        from . import panel

        panel.toggle_panel()

    def IsActive(self):
        return True


Gui.addCommand("ClaudeCode_TogglePanel", ToggleClaudePanelCommand())
