"""Private data-plane backend contract for anyhop's orchestration layer.

The CLI, REST API, Web UI, and application services speak only in anyhop
domain objects.  Runtime-specific process control and telemetry sit behind
``RuntimeBackend``; the default implementation is the pinned sing-box runner.

This is an internal substitution seam, not a public plugin API.  In
particular, backend endpoints, credentials, configuration, and native response
shapes must not cross into service results or the public REST contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ApplyResult(Protocol):
    """Result shape returned when a backend applies generated configuration."""

    @property
    def outcome(self) -> object: ...

    @property
    def detail(self) -> str: ...


class RuntimeBackend(Protocol):
    """Process and telemetry operations required by orchestration."""

    def running_pid(self) -> int | None: ...

    def is_running(self) -> bool: ...

    def control_alive(self) -> bool: ...

    def apply(self, config: dict) -> ApplyResult: ...

    def start(self, binary: Path | None = None) -> None: ...

    def stop(self) -> None: ...

    def restart(self, binary: Path | None = None) -> None: ...

    def logs(self, tail: int = 80) -> str: ...

    def connections(self) -> list[dict] | None: ...

    def generation(self) -> str | None: ...


def runtime_backend() -> RuntimeBackend:
    """Return the configured private data-plane backend.

    The import stays lazy so read-only CLI operations do not load the process
    management stack.  Keeping construction here gives orchestration one seam
    to replace in tests or if another core is supported in the future.
    """
    from anyhop.singbox import Runner

    return Runner()
