"""Install/uninstall the privileged TUN helper as a macOS system LaunchDaemon.

The companion to :mod:`anyhop.daemonctl`: ``daemonctl`` installs the **user-level**
applier (``~/Library/LaunchAgents``); this installs the **root** helper that owns
sing-box while tun mode is on (``/Library/LaunchDaemons``). The helper itself is
:mod:`anyhop.helper`; this module is just the service-management plumbing behind
``sudo anyhop helper install`` / ``uninstall`` / ``status``.

macOS-only: Linux uses ``setcap`` (no root process, no helper). The plist execs
the stable ``anyhop`` console-script shim with the hidden ``helper-run`` verb
(stable across ``uv tool upgrade``, exactly like ``anyhop applier``), and carries
``ANYHOP_HELPER_ALLOWED_UID`` (the real user behind ``sudo``) plus ``ANYHOP_HOME``
as environment — so at boot the root helper knows which user it serves and
where that user's state lives. ``install``/``uninstall`` require root
(``/Library/LaunchDaemons`` is root-owned); ``status`` does not.
"""

from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
from pathlib import Path

from anyhop import applog, daemonctl, paths, proc
from anyhop.helper import HELPER_LABEL, HELPER_SOCKET_DEFAULT

LAUNCHD_PLIST = f"/Library/LaunchDaemons/{HELPER_LABEL}.plist"


class HelperCtlError(RuntimeError):
    """A user-correctable problem installing/removing the privileged helper."""


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _supported() -> bool:
    return platform.system() == "Darwin"


def _require_darwin() -> None:
    if not _supported():
        raise HelperCtlError(
            f"the privileged helper is macOS-only (Linux uses setcap); "
            f"this is {platform.system()}."
        )


def _require_root(action: str) -> None:
    if os.geteuid() != 0:
        raise HelperCtlError(
            f"anyhop helper {action} needs root (it writes {LAUNCHD_PLIST}): "
            "run it under sudo."
        )


def _real_uid() -> int:
    """The user behind ``sudo anyhop helper install`` — the one the helper serves.

    ``sudo`` sets ``SUDO_UID``; without it (already-root shell, or a future root
    LaunchDaemon doing the install) the helper serves root only, which is
    useless for a user-level daemon — refuse rather than install silently
    broken.
    """
    sudo_uid = os.environ.get("SUDO_UID")
    if not sudo_uid:
        raise HelperCtlError(
            "could not determine the real user (SUDO_UID is unset). "
            "Run via:  sudo anyhop helper install"
        )
    try:
        return int(sudo_uid)
    except ValueError:
        raise HelperCtlError(
            f"bad SUDO_UID {sudo_uid!r}; run via: sudo anyhop helper install"
        )


def _service_exec() -> list[str]:
    """The stable command the plist execs: the ``anyhop`` shim + ``helper-run``.

    An app-bundled runtime must win over PATH. Helper install runs under
    ``sudo``, which replaces PATH with ``secure_path``, so ``which("anyhop")``
    from inside the macOS app would resolve a *brew/uv* shim and the root
    helper would then exec CLI code against the app's ``ANYHOP_HOME`` — exactly
    the cross-contamination the hermetic bundle exists to prevent. Mirror
    :func:`anyhop.daemon._self_command` and take ``ANYHOP_EXECUTABLE`` first.

    Otherwise prefer the console-script shim (stable across upgrades) and fall
    back to ``<python> -m anyhop helper-run`` when no shim is on PATH, mirroring
    daemonctl so an upgrade never orphans the installed unit.
    """
    exe = os.environ.get("ANYHOP_EXECUTABLE")
    if exe and os.path.isabs(exe):
        return [exe, "helper-run"]
    shim = shutil.which("anyhop")
    if shim:
        return [shim, "helper-run"]
    return [shutil.which("python3") or "python3", "-m", "anyhop", "helper-run"]


def _plist_bytes(uid: int, anyhop_home: str) -> bytes:
    log = str(Path(anyhop_home) / "helper.log")
    env = {
        "ANYHOP_HELPER_ALLOWED_UID": str(uid),
        "ANYHOP_HELPER_SOCKET": HELPER_SOCKET_DEFAULT,
        "ANYHOP_HOME": anyhop_home,
        # The helper runs as root out of the installing user's uv-tool
        # venv. Without this, root would write root-owned .pyc into that
        # venv and later `uv tool upgrade` would fail to remove them. A
        # root daemon does not benefit from bytecode caching anyway.
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update(daemonctl._app_env())
    plist = {
        "Label": HELPER_LABEL,
        "ProgramArguments": _service_exec(),
        "EnvironmentVariables": env,
        "UserName": "root",
        "RunAtLoad": True,
        "KeepAlive": True,  # supervisor: respawn on crash
        "ProcessType": "Background",
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    return plistlib.dumps(plist)


def _serving_singbox_pid(served_home: str) -> int | None:
    """The live sing-box PID the installed helper owns, or ``None``.

    Read straight from the served home's pidfile rather than over the socket:
    a helper bound to a *different* home refuses ``status`` (that is the whole
    point of home scoping), so the protocol cannot answer this question. We run
    as root here, so the other home's pidfile is readable.
    """
    from anyhop import singbox

    try:
        return proc.read_pidfile(singbox.pid_path_for(served_home), ("sing-box",))
    except OSError:
        return None


def install(takeover: bool = False) -> dict:
    """Write + load the system LaunchDaemon. Must run as root (via sudo).

    There is one helper per machine (one label, one plist, one socket) bound to
    one ``ANYHOP_HOME``, so installing from a second install — e.g. the macOS app
    alongside a CLI install — *rebinds* the shared helper and leaves the first
    install unable to use tun. Worse, the unload below stops a helper that may
    currently own a running sing-box, dropping a live VPN as a side effect.
    Both are refused unless ``takeover`` says that is intended.
    """
    _require_darwin()
    _require_root("install")
    uid = _real_uid()
    anyhop_home = str(paths.state_dir())
    already = is_installed()
    if already and not takeover:
        current_home = _installed_home()
        if current_home and current_home != anyhop_home:
            raise HelperCtlError(
                f"the installed helper is bound to ANYHOP_HOME {current_home}, "
                f"not {anyhop_home}. There is one helper per machine, so "
                f"installing from here would take tun away from that install. "
                f"To move it, re-run with --takeover."
            )
        held = _serving_singbox_pid(current_home or anyhop_home)
        if held is not None:
            raise HelperCtlError(
                f"the installed helper is currently running sing-box "
                f"(pid {held}) for {current_home or anyhop_home} — reinstalling "
                f"would stop it and drop tun. Turn tun off first, or re-run "
                f"with --takeover to accept the interruption."
            )
    rebound_from = _installed_home() if already else None
    if rebound_from == anyhop_home:
        rebound_from = None
    p = Path(LAUNCHD_PLIST)
    # Unload any prior generation first so the new plist's env takes effect
    # cleanly (launchctl keeps a stale job definition otherwise).
    if already:
        _run(["launchctl", "unload", "-w", LAUNCHD_PLIST])
    p.write_bytes(_plist_bytes(uid, anyhop_home))
    r = _run(["launchctl", "load", "-w", LAUNCHD_PLIST])
    if r.returncode != 0:
        # Don't leave a plist that failed to load — it would shadow nothing
        # but read as "installed" to status().
        try:
            p.unlink()
        except OSError:
            pass
        raise HelperCtlError(f"launchctl load failed: {r.stderr.strip()}")
    applog.log(
        f"privileged helper {'reinstalled' if already else 'installed'} "
        f"(serves uid {uid})"
        + (f", rebound from {rebound_from}" if rebound_from else "")
    )
    return {
        "installed": True,
        "reinstalled": already,
        "rebound_from": rebound_from,
        "plist": LAUNCHD_PLIST,
        "serves_uid": uid,
        "serves_home": anyhop_home,
        "socket": HELPER_SOCKET_DEFAULT,
    }


def uninstall() -> dict:
    """Unload + remove the system LaunchDaemon. Must run as root."""
    _require_darwin()
    _require_root("uninstall")
    if not is_installed():
        return {"removed": False}
    # Stop the helper first so it releases the socket and any tun it holds.
    _run(["launchctl", "unload", "-w", LAUNCHD_PLIST])
    try:
        Path(LAUNCHD_PLIST).unlink()
    except OSError as e:
        raise HelperCtlError(f"could not remove {LAUNCHD_PLIST}: {e}") from e
    applog.log("privileged helper removed")
    return {"removed": True, "plist": LAUNCHD_PLIST}


def is_installed() -> bool:
    return _supported() and Path(LAUNCHD_PLIST).exists()


def _installed_home() -> str | None:
    """The ANYHOP_HOME the installed plist binds the helper to, or None."""
    try:
        pl = plistlib.loads(Path(LAUNCHD_PLIST).read_bytes())
        home = pl.get("EnvironmentVariables", {}).get("ANYHOP_HOME")
        return str(home) if home else None
    except (OSError, plistlib.InvalidFileException):
        return None


def status() -> dict:
    if not _supported():
        return {"supported": False, "platform": platform.system()}
    installed = is_installed()
    return {
        "supported": True,
        "installed": installed,
        # "loaded"/liveness is NOT reported here: a non-root user cannot
        # reliably query a system daemon's load state (`launchctl list` of a
        # system LaunchDaemon needs root). The service layer adds `reachable`
        # — a root-free ping — as the authoritative liveness signal.
        "plist": LAUNCHD_PLIST if installed else None,
        "socket": HELPER_SOCKET_DEFAULT if installed else None,
        # Which home the installed helper serves (from the plist, so it works
        # even when the helper is not answering). One helper, one home.
        "serves_home": _installed_home() if installed else None,
    }
