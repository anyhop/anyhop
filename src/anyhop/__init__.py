"""anyhop — multi-location VPN gateways via one sing-box process (WireGuard)."""

import os as _os
import warnings as _warnings
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("anyhop")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"


def _adopt_legacy_env() -> None:
    """Mirror pre-rename ``ALLE_*`` / ``_ALLE_*`` vars onto their ``ANYHOP_*``
    equivalents (the project was renamed from ``alle`` in 2026-09).

    The new name wins when both are set; otherwise the legacy value is copied
    across and one ``DeprecationWarning`` is emitted. Scheduled for removal two
    releases after the rename.
    """
    adopted = []
    for key in [k for k in _os.environ if k.startswith(("ALLE_", "_ALLE_"))]:
        new_key = key.replace("ALLE_", "ANYHOP_", 1)
        if new_key not in _os.environ:
            _os.environ[new_key] = _os.environ[key]
            adopted.append(f"{key}->{new_key}")
    if adopted:
        _warnings.warn(
            "anyhop: ALLE_* environment variables are deprecated after the "
            f"rename; use ANYHOP_* ({', '.join(sorted(adopted))})",
            DeprecationWarning,
            stacklevel=2,
        )


_adopt_legacy_env()
