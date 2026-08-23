# Enregistre le workbench "Claude Code" et la commande d'affichage du volet.
#
# ATTENTION : FreeCAD exécute ce fichier via exec() SANS définir __file__.
# Tout chemin doit donc être calculé via un module importé (claudecode_wb).

import FreeCADGui as Gui


class ClaudeCodeWorkbench(Gui.Workbench):
    """Workbench minimal : son activation affiche le volet de chat Claude Code."""

    MenuText = "Claude Code"
    ToolTip = "Agent IA Claude Code pilotant FreeCAD (abonnement Claude)"

    def __init__(self):
        try:
            import os
            import claudecode_wb

            self.__class__.Icon = os.path.join(
                os.path.dirname(os.path.dirname(claudecode_wb.__file__)),
                "Resources", "icons", "claudecode.svg",
            )
        except Exception as exc:
            import FreeCAD

            FreeCAD.Console.PrintWarning("[Claude Code] icône introuvable : %s\n" % exc)

    def Initialize(self):
        import claudecode_wb.commands  # noqa: F401  (enregistre les commandes)

        self.appendToolbar("Claude Code", ["ClaudeCode_TogglePanel"])
        self.appendMenu("&Claude Code", ["ClaudeCode_TogglePanel"])

    def Activated(self):
        from claudecode_wb import panel

        panel.show_panel()

    def Deactivated(self):
        # Le volet reste visible dans les autres workbenches : ne rien fermer.
        pass

    def GetClassName(self):
        return "Gui::PythonWorkbench"


Gui.addWorkbench(ClaudeCodeWorkbench())
