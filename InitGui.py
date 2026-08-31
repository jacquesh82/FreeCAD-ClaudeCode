# Registers the "Claude Code Connector (unofficial)" workbench and the panel command.
#
# NOTE: FreeCAD executes this file via exec() WITHOUT defining __file__.
# Any path must therefore be computed through an imported module (claudecode_wb).

import FreeCADGui as Gui


class ClaudeCodeWorkbench(Gui.Workbench):
    """Minimal workbench: activating it shows the Claude Code chat panel."""

    MenuText = "Claude Code Connector (unofficial)"
    ToolTip = "Unofficial AI agent panel driving FreeCAD via Claude Code (subscription auth)"

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

            FreeCAD.Console.PrintWarning("[Claude Code Connector] icon not found: %s\n" % exc)

    def Initialize(self):
        import claudecode_wb.commands  # noqa: F401  (enregistre les commandes)

        self.appendToolbar("Claude Code Connector", ["ClaudeCode_TogglePanel"])
        self.appendMenu("&Claude Code Connector", ["ClaudeCode_TogglePanel"])

    def Activated(self):
        from claudecode_wb import panel

        panel.show_panel()

    def Deactivated(self):
        # The panel stays visible in other workbenches: close nothing.
        pass

    def GetClassName(self):
        return "Gui::PythonWorkbench"


Gui.addWorkbench(ClaudeCodeWorkbench())
