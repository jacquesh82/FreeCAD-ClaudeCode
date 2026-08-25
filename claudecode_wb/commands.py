"""Commandes GUI de l'addon Claude Code."""

import os

import FreeCADGui as Gui

_ICONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "Resources", "icons"
)
_ICON = os.path.join(_ICONS_DIR, "claudecode.svg")
_SKETCH_ICON = os.path.join(_ICONS_DIR, "claudecode_sketch.svg")


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


class ToggleSketchPanelCommand:
    def GetResources(self):
        return {
            "Pixmap": _SKETCH_ICON,
            "MenuText": "Volet Croquis",
            "ToolTip": (
                "Afficher/masquer le volet Croquis : esquisses 2D multi-vues "
                "que l'agent lit via l'outil get_sketch"
            ),
        }

    def Activated(self):
        from . import sketch

        sketch.toggle_panel()

    def IsActive(self):
        return True


Gui.addCommand("ClaudeCode_TogglePanel", ToggleClaudePanelCommand())
Gui.addCommand("ClaudeCode_ToggleSketch", ToggleSketchPanelCommand())
