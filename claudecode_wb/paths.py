"""Per-user private storage for the addon's runtime files.

The Addon Index rules for AI agents require that tokens, agent configuration
files, working directories and screenshots are readable only by the current
user and are never placed in shared temporary directories. Everything the
addon writes goes under FreeCAD's per-user cache directory (for example
``~/.cache/FreeCAD/ClaudeCodeConnector/`` on Linux) with owner-only
permissions.
"""

import os
import stat
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


def trusted_path(path):
    """Reject files/directories another local user can replace or modify.

    Check both the lexical path and symlink targets. Root-owned sticky system
    directories (e.g. /tmp) are allowed as ancestors, never as executable files.
    This is an ownership check, not a vendor signature check.
    """
    if not hasattr(os, "getuid"):
        return False  # ZCode discovery currently supports Linux only.
    # A container/user namespace may map the filesystem root's owner to a
    # different UID. Trust that system owner as well as the current user.
    system_owner = os.stat(os.path.sep).st_uid
    owners = {0, system_owner, os.getuid()}
    try:
        for candidate in (os.path.abspath(path), os.path.realpath(path)):
            while True:
                info = os.lstat(candidate)
                if info.st_uid not in owners:
                    return False
                if not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022:
                    sticky_root = (
                        stat.S_ISDIR(info.st_mode) and info.st_uid in {0, system_owner}
                        and info.st_mode & stat.S_ISVTX
                    )
                    if not sticky_root:
                        return False
                parent = os.path.dirname(candidate)
                if parent == candidate:
                    break
                candidate = parent
        return True
    except OSError:
        return False


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
