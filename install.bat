@echo off
rem Windows installer wrapper for the Claude Code Connector (unofficial).
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0install.py" %*
) else (
  python "%~dp0install.py" %*
)
