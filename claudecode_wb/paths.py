"""Per-user private storage for the addon's runtime files.

The Addon Index rules for AI agents require that tokens, agent configuration
files, working directories and screenshots are readable only by the current
user and are never placed in shared temporary directories. Everything the
addon writes goes under FreeCAD's per-user cache directory (for example
``~/.cache/FreeCAD/ClaudeCodeConnector/`` on Linux) with owner-only
permissions.
"""

import os
import tempfile

_SUBDIR = "ClaudeCodeConnector"


def _base_dir():
    base = ""
    try:
        import FreeCAD as App

        base = App.getUserCachePath()
    except Exception:  # noqa: BLE001 — outside FreeCAD (tests, bridge)
        base = ""
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".cache", "FreeCAD")
    return os.path.join(base, _SUBDIR)


def private_dir(*parts):
    """Return (creating it if needed) a directory only the current user can read."""
    path = os.path.join(_base_dir(), *parts)
    os.makedirs(path, mode=0o700, exist_ok=True)
    if os.path.islink(path) or (hasattr(os, "getuid") and os.stat(path).st_uid != os.getuid()):
        raise OSError("Private directory is not owned by the current user: %s" % path)
    _restrict(path, 0o700)
    return path


def new_private_dir(prefix):
    """Create an independent workspace, including across FreeCAD processes."""
    return tempfile.mkdtemp(prefix=prefix, dir=private_dir("sessions"))


def write_private_file(path, text):
    """Create or overwrite ``path`` so that only the current user can read it."""
    path = os.fspath(path)
    if os.path.islink(path):
        raise OSError("Refusing to overwrite a symbolic link: %s" % path)
    fd, temporary = tempfile.mkstemp(prefix=".%s-" % os.path.basename(path), dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def restrict_file(path):
    """Make an existing file (e.g. a screenshot) readable by the current user only."""
    _restrict(path, 0o600)


def _restrict(path, mode):
    os.chmod(path, mode)
