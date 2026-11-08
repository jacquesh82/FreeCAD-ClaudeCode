#!/usr/bin/env python3
"""Installer for the Claude Code Connector (unofficial) FreeCAD addon.

Cross-platform (Linux, macOS, Windows). Copies or symlinks this addon
directory into FreeCAD's user Mod directory after sanity-checking the
environment:

  - the FreeCAD user Mod directory (auto-detected, overridable with --mod-dir)
  - the required FreeCAD version (from package.xml, <freecadmin>)
  - an agent CLI: `claude` and/or ZCode Desktop (warning only)
  - a Python interpreter usable by the MCP bridge (warning only)

A missing agent CLI can be installed from this script: the installer offers
it interactively (when run in a terminal), or unconditionally with
--install-claude / --install-zcode.

Usage:
  python3 install.py             # Linux/macOS
  py -3 install.py               # Windows
  python3 install.py --copy      # force a copy instead of a symlink
  python3 install.py --mod-dir /path/to/Mod
  python3 install.py --install-claude   # install the claude CLI (official installer)
  python3 install.py --install-zcode    # download the ZCode Desktop AppImage
  python3 install.py --uninstall
"""

import argparse
import contextlib
import glob
import os
import platform
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
ADDON_NAME = "ClaudeCodeWB"
REQUIRED_FREECAD_MINOR = 1  # FreeCAD >= 1.1 (read below from package.xml)

CLAUDE_INSTALL_SH = "curl -fsSL https://claude.ai/install.sh | bash"
CLAUDE_INSTALL_PS = "irm https://claude.ai/install.ps1 | iex"
ZCODE_DOWNLOAD_PAGE = "https://zcode.z.ai/en/docs/install"


def _fail(message):
    print("ERROR: %s" % message)
    sys.exit(1)


def _warn(message):
    print("WARNING: %s" % message)


def _ok(message):
    print("  [ok] %s" % message)


def required_freecadmin():
    """Read the minimum FreeCAD version from package.xml."""
    global REQUIRED_FREECAD_MINOR
    try:
        tree = ET.parse(os.path.join(ADDON_DIR, "package.xml"))
        ns = {"p": "https://wiki.freecad.org/Package_Metadata"}
        node = tree.getroot().find("p:freecadmin", ns)
        if node is None:
            node = tree.getroot().find("freecadmin")
        version = (node.text or "").strip() if node is not None else ""
        parts = version.split(".")
        REQUIRED_FREECAD_MINOR = int(parts[1]) if len(parts) > 1 else 0
        return version
    except Exception as exc:
        _warn("could not read package.xml (%s); assuming FreeCAD >= 1.1" % exc)
        return "1.1"


def candidate_mod_dirs():
    """Known FreeCAD user Mod directory locations, best guess first."""
    home = os.path.expanduser("~")
    appdata = os.environ.get("APPDATA", "")
    if platform.system() == "Windows":
        return [
            os.path.join(appdata, "FreeCAD", "1.1", "Mod"),
            os.path.join(appdata, "FreeCAD", "v1-1", "Mod"),
            os.path.join(appdata, "FreeCAD", "Mod"),
        ]
    if platform.system() == "Darwin":
        return [
            os.path.join(home, "Library", "Application Support", "FreeCAD", "v1-1", "Mod"),
            os.path.join(home, "Library", "Application Support", "FreeCAD", "Mod"),
        ]
    data_home = os.environ.get("XDG_DATA_HOME", os.path.join(home, ".local", "share"))
    return [
        os.path.join(data_home, "FreeCAD", "v1-1", "Mod"),
        os.path.join(data_home, "FreeCAD", "Mod"),
    ]


def detect_mod_dir():
    env_dir = os.environ.get("FREECAD_USER_MOD_DIR")
    if env_dir:
        return env_dir, "FREECAD_USER_MOD_DIR"
    for candidate in candidate_mod_dirs():
        if os.path.isdir(candidate):
            return candidate, "auto-detected"
    return None, None


def check_freecad(mod_dir):
    """Best-effort check that FreeCAD >= required version is in use.

    We cannot import FreeCAD from an external Python, so we look at the
    version-tagged user directory (v1-1 / 1.1) or FreeCAD's own marker files.
    """
    parent = os.path.dirname(mod_dir.rstrip(os.sep))
    # FreeCAD uses dash-separated version dirs on Linux/macOS ("v1-1")
    # and dot-separated on Windows ("1.1").
    name = os.path.basename(os.path.normpath(parent)).lower().lstrip("v").replace("-", ".")
    parts = name.split(".")
    versioned = len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit()
    if not versioned:
        _warn(
            "Mod directory %s is not version-tagged, so the installed FreeCAD "
            "version cannot be verified. Make sure you run FreeCAD >= 1.%d "
            "(check with Help > About in FreeCAD)."
            % (parent, REQUIRED_FREECAD_MINOR)
        )
        return
    major, minor = parts[0], parts[1]
    if (int(major), int(minor)) < (1, REQUIRED_FREECAD_MINOR):
        _fail(
            "FreeCAD %s.%s found (from %s), but this addon requires >= 1.%d. "
            "Upgrade FreeCAD, or point --mod-dir at the Mod directory of a "
            "newer FreeCAD." % (major, minor, parent, REQUIRED_FREECAD_MINOR)
        )
    _ok("FreeCAD version directory %s.%s (>= 1.%d required)"
        % (major, minor, REQUIRED_FREECAD_MINOR))


def check_claude_cli():
    names = ("claude.exe",) if platform.system() == "Windows" else ("claude",)
    for name in names:
        path = shutil.which(name)
        if path:
            _ok("claude CLI found: %s" % path)
            return True
    # Locations used by the official installer that may not be on this
    # process's PATH yet (mirrors claude_runner.find_claude).
    if platform.system() == "Windows":
        fallbacks = (
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "claude", "claude.exe"),
            os.path.expanduser(r"~\.local\bin\claude.exe"),
        )
    else:
        fallbacks = (
            os.path.expanduser("~/.local/bin/claude"),
            os.path.expanduser("~/.claude/local/claude"),
            "/usr/local/bin/claude",
        )
    for fallback in fallbacks:
        if fallback and os.path.exists(fallback):
            _ok("claude CLI found: %s" % fallback)
            return True
    _warn(
        "claude CLI not found. Install Claude Code and log in "
        "(run `claude` then /login) to use it as the panel's agent."
    )
    return False


def check_zcode_cli():
    """Best-effort detection of the ZCode CLI (bundled with ZCode Desktop).

    Mirrors zcode_runner.find_zcode() but stays dependency-free so the
    installer can run with any Python. Linux only for now.
    """
    if platform.system() != "Linux":
        return False
    from claudecode_wb.paths import trusted_path

    explicit = os.environ.get("ZCODE_APPIMAGE")
    if explicit:
        if os.path.exists(explicit) and trusted_path(explicit):
            _ok("ZCode CLI via ZCODE_APPIMAGE: %s" % explicit)
            return True
        _warn("ZCODE_APPIMAGE is missing or not protected against other users.")
        return False
    home = os.path.expanduser("~")
    for directory in ("~/Applications", "~/Downloads", "~/.local/bin", "/opt"):
        base = os.path.expanduser(directory)
        appimages = [path for path in glob.glob(os.path.join(base, "ZCode*.AppImage"))
                     if os.path.isfile(path) and os.access(path, os.X_OK) and trusted_path(path)]
        if appimages:
            _ok("ZCode AppImage found: %s" % appimages[0])
            return True
    _warn(
        "ZCode not found (looked for protected ZCode*.AppImage files in "
        "~/Applications, ~/Downloads, ~/.local/bin, /opt, "
        "and the ZCODE_APPIMAGE variable). Install ZCode Desktop to use it "
        "as the panel's agent."
    )
    return False


# --- agent CLI installation ---------------------------------------------

def _ask(question):
    """y/N prompt; auto-declines when stdin is not an interactive terminal."""
    if not sys.stdin.isatty():
        return False
    try:
        answer = input("%s [y/N] " % question).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


def install_claude_code():
    """Install the Claude Code CLI with Anthropic's official installer."""
    if platform.system() == "Windows":
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-Command", CLAUDE_INSTALL_PS]
    elif shutil.which("curl"):
        cmd = ["bash", "-c", CLAUDE_INSTALL_SH]
    elif shutil.which("npm"):
        cmd = ["npm", "install", "-g", "@anthropic-ai/claude-code"]
    else:
        _warn(
            "neither curl nor npm found — install Claude Code manually: "
            "https://code.claude.com/docs/en/setup"
        )
        return False
    print("Installing Claude Code: %s" % " ".join(cmd))
    result = subprocess.run(cmd, cwd=os.path.expanduser("~"))
    if result.returncode != 0:
        _warn("Claude Code installer exited with %d." % result.returncode)
        return False
    print(
        "  [ok] Claude Code installed. Run `claude` once in a terminal and "
        "log in (/login) with your subscription."
    )
    return True


def install_zcode_desktop():
    """Download the ZCode Desktop AppImage from the official CDN (Linux)."""
    if platform.system() != "Linux":
        _warn(
            "automated ZCode Desktop install is Linux-only — get your "
            "platform build at https://zcode.z.ai"
        )
        return False
    url = _zcode_appimage_url()
    if not url:
        _warn(
            "could not locate the ZCode AppImage download URL — download it "
            "manually from %s" % ZCODE_DOWNLOAD_PAGE
        )
        return False
    target_dir = os.path.expanduser("~/Applications")
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, os.path.basename(url))
    print("Downloading %s" % url)
    try:
        import urllib.request

        with urllib.request.urlopen(url, timeout=120) as resp, \
                open(target + ".part", "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total:
                    print("\r  %d / %d MB" % (done >> 20, total >> 20),
                          end="", flush=True)
        os.replace(target + ".part", target)
    except Exception as exc:  # noqa: BLE001 — report any download failure
        with contextlib.suppress(OSError):
            os.remove(target + ".part")
        _warn("ZCode download failed: %s" % exc)
        return False
    os.chmod(target, 0o755)
    print("\n  [ok] %s" % target)
    print(
        "  Launch it once and log in with your Z.AI coding plan — the addon "
        "detects it automatically after that."
    )
    return True


def _zcode_appimage_url():
    """Current ZCode Linux AppImage URL, read off the official download page
    (the CDN only exposes version-pinned URLs)."""
    try:
        import re
        import urllib.request

        with urllib.request.urlopen(ZCODE_DOWNLOAD_PAGE, timeout=30) as resp:
            html = resp.read().decode("utf-8", "replace")
        match = re.search(
            r"https://cdn-zcode\.z\.ai/[^\s\"'<>]*linux-x64/"
            r"ZCode-[\d.]+-linux-x64\.AppImage",
            html,
        )
        return match.group(0) if match else None
    except Exception:
        return None


def check_bridge_python():
    if platform.system() == "Windows":
        names = ("python", "python3", "py")
    else:
        names = ("python3", "python")
    for name in names:
        path = shutil.which(name)
        if path:
            _ok("Python for the MCP bridge: %s" % path)
            return
    _warn("no Python interpreter found on PATH for the MCP bridge.")


def install(mod_dir, force_copy):
    target = os.path.join(mod_dir, ADDON_NAME)
    if os.path.islink(target) or os.path.exists(target):
        _fail(
            "%s already exists — remove it first (or run with --uninstall)." % target
        )
    use_symlink = not force_copy and platform.system() != "Windows" and os.name != "nt"
    if use_symlink:
        try:
            os.symlink(ADDON_DIR, target)
            print("  [ok] symlinked %s -> %s" % (target, ADDON_DIR))
            return
        except OSError:
            pass  # fall back to a copy
    shutil.copytree(ADDON_DIR, target, ignore=shutil.ignore_patterns("__pycache__"))
    print("  [ok] copied to %s" % target)


def uninstall(mod_dir):
    target = os.path.join(mod_dir, ADDON_NAME)
    if not (os.path.islink(target) or os.path.exists(target)):
        _fail("nothing to uninstall at %s" % target)
    if os.path.islink(target):
        os.unlink(target)
        print("  [ok] removed symlink %s" % target)
    else:
        shutil.rmtree(target)
        print("  [ok] removed %s" % target)


def main():
    parser = argparse.ArgumentParser(
        description="Install the Claude Code Connector (unofficial) addon."
    )
    parser.add_argument("--mod-dir", help="FreeCAD user Mod directory (auto-detected by default)")
    parser.add_argument("--copy", action="store_true", help="copy files instead of symlinking (default on Windows)")
    parser.add_argument("--install-claude", action="store_true",
                        help="install the claude CLI with Anthropic's official installer")
    parser.add_argument("--install-zcode", action="store_true",
                        help="download the ZCode Desktop AppImage into ~/Applications (Linux)")
    parser.add_argument("--uninstall", action="store_true", help="remove the addon from the Mod directory")
    args = parser.parse_args()

    print("Claude Code Connector (unofficial) — installer")
    print("Addon source: %s" % ADDON_DIR)

    version = required_freecadmin()
    print("Required FreeCAD version (package.xml): >= %s" % version)

    mod_dir = args.mod_dir
    if mod_dir:
        print("Using --mod-dir %s" % mod_dir)
    else:
        mod_dir, how = detect_mod_dir()
        if mod_dir:
            print("FreeCAD Mod directory (%s): %s" % (how, mod_dir))
        else:
            print("Could not auto-detect the FreeCAD user Mod directory.")
            print("Candidates were:")
            for candidate in candidate_mod_dirs():
                print("  - %s" % candidate)
            _fail(
                "Re-run with --mod-dir <path> (find it in FreeCAD's Python "
                "console with FreeCAD.getUserAppDataDir() + 'Mod')."
            )
    if args.uninstall:
        if not os.path.isdir(mod_dir):
            _fail("Mod directory does not exist: %s" % mod_dir)
        uninstall(mod_dir)
        print("\nUninstalled. Restart FreeCAD if it is running.")
        return

    print("Checking environment:")
    check_freecad(mod_dir)
    has_claude = check_claude_cli()
    has_zcode = check_zcode_cli()
    if not has_claude and not has_zcode:
        _warn(
            "no agent CLI found at all — the panel needs at least one of "
            "Claude Code or ZCode to drive FreeCAD."
        )
    if args.install_claude or (not has_claude and _ask("Install Claude Code now?")):
        if install_claude_code():
            check_claude_cli()
    if args.install_zcode or (not has_zcode and platform.system() == "Linux"
                              and _ask("Download ZCode Desktop now?")):
        if install_zcode_desktop():
            check_zcode_cli()
    check_bridge_python()

    if not os.path.isdir(mod_dir):
        try:
            os.makedirs(mod_dir)
            print("  [ok] created %s" % mod_dir)
        except OSError as exc:
            _fail("cannot create Mod directory %s: %s" % (mod_dir, exc))

    print("Installing:")
    install(mod_dir, force_copy=args.copy)

    print(
        "\nDone. Restart FreeCAD, then open the workbench "
        "'Claude Code Connector' (or its menu entry) to show the panel."
    )


if __name__ == "__main__":
    main()
