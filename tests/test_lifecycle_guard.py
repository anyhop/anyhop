"""The sing-box lifecycle lock and stray reclaim.

These tests use a localhost listener whose argv looks like
``sing-box run -c <this home>/singbox.json``. They never dial a provider.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time

import pytest

from anyhop import paths, singbox
from anyhop.singbox import Runner

_FAKE = """#!/usr/bin/env python3
import os, signal, socket, sys, time
if len(sys.argv) > 1 and sys.argv[1] == "check":
    raise SystemExit(0)
port = int(os.environ["FAKE_BIND_PORT"])
sock = socket.socket()
try:
    sock.bind(("127.0.0.1", port))
    sock.listen(1)
except OSError:
    print(
        "FATAL[0000] start service: listen tcp "
        f"127.0.0.1:{port}: bind: address already in use",
        file=sys.stderr,
    )
    raise SystemExit(1)

def _die(*_args):
    raise SystemExit(0)

signal.signal(signal.SIGTERM, _die)
signal.signal(signal.SIGINT, _die)
while True:
    time.sleep(3600)
"""


@pytest.fixture
def fake_binary(monkeypatch):
    home = paths.state_dir()
    binary = home / "bin" / "sing-box"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(_FAKE)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    monkeypatch.setenv("FAKE_BIND_PORT", str(port))
    runner = Runner(local_only=True)
    try:
        yield binary, runner
    finally:
        try:
            runner.stop()
        except Exception:  # noqa: BLE001 — cleanup must still reap
            pass
        for pid in singbox.managed_pids():
            try:
                os.kill(pid, 9)
            except OSError:
                pass


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_overlapping_starts_leave_one_vouched_process(fake_binary):
    binary, runner = fake_binary
    errors: list[BaseException] = []

    def go():
        try:
            runner.start(binary)
        except Exception as exc:  # noqa: BLE001 — the assertion is the census
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    pids = singbox.managed_pids()
    assert errors == []
    assert pids == [runner.running_pid()]
    assert len(pids) == 1


def test_restart_overlapping_a_second_start_does_not_orphan(fake_binary):
    """The incident: reconnect restarts while the supervisor also starts."""
    binary, runner = fake_binary
    runner.start(binary)
    errors: list[BaseException] = []

    def bounce():
        try:
            runner.restart(binary)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def sneak():
        # Enter start() as soon as the pidfile stops vouching for a live
        # process — the supervisor's old crash path.
        if not _wait_until(lambda: not runner.is_running(), timeout=2):
            return
        try:
            runner.start(binary)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=bounce), threading.Thread(target=sneak)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=8)

    pids = singbox.managed_pids()
    assert errors == []
    assert len(pids) == 1
    assert pids[0] == runner.running_pid()


def test_a_deleted_pidfile_is_reaped_and_replaced(fake_binary):
    binary, runner = fake_binary
    runner.start(binary)
    old = runner.running_pid()
    assert old is not None
    (paths.state_dir() / "singbox.pid").unlink()
    assert runner.is_running() is False

    runner.start(binary)

    pids = singbox.managed_pids()
    assert old not in pids
    assert pids == [runner.running_pid()]


def test_two_hand_started_copies_collapse_to_one(fake_binary):
    import subprocess

    binary, runner = fake_binary
    config = paths.state_dir() / "singbox.json"
    config.write_text("{}\n")
    log = paths.state_dir() / "singbox.log"
    children = []

    def free_port() -> str:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        chosen = probe.getsockname()[1]
        probe.close()
        return str(chosen)

    with log.open("ab") as handle:
        for _ in range(2):
            # Different ports so both stay up. Reap matches the config path,
            # not the port, which is what an orphan from an older generation is.
            env = dict(os.environ)
            env["FAKE_BIND_PORT"] = free_port()
            children.append(
                subprocess.Popen(
                    [str(binary), "run", "-c", str(config)],
                    stdout=handle,
                    stderr=handle,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    env=env,
                )
            )
    assert _wait_until(lambda: len(singbox.managed_pids()) == 2)
    try:
        runner.start(binary)
        pids = singbox.managed_pids()
        assert pids == [runner.running_pid()]
        assert children[0].pid not in pids
        assert children[1].pid not in pids
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()


def test_pidfile_unlink_ignores_a_replaced_record():
    path = singbox._pid_path()
    path.write_text(json.dumps({"pid": 1, "start": "x"}))
    assert singbox._unlink_pidfile_if(2) is False
    assert path.exists()
    assert singbox._unlink_pidfile_if(1) is True
    assert not path.exists()
