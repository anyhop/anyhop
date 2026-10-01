"""On-demand speed test for a channel, run through its local proxy.

Where the heartbeat :mod:`probe` only asks "is this tunnel alive and what's its
exit IP", this drives a real bulk transfer through the channel to estimate
download/upload throughput plus a latency floor. It is strictly on-demand (the
``anyhop test --speed`` command) and never runs on the daemon's hot path.

Method — everything goes through the channel's ``mixed`` proxy via a urllib
``ProxyHandler`` (same technique as the probe):

* **latency** — the min round trip of a few tiny GETs (min, not mean, to shed
  scheduler/one-off jitter). It bundles TCP+TLS setup so it reads higher than raw
  ICMP ping, but is consistent across channels, so the *ordering* is meaningful.
* **download** — read a large object for up to ``DOWNLOAD_SECONDS``, then stop;
  throughput is bytes read / elapsed. Time-bounding keeps a fast link honest and a
  slow one quick.
* **upload** — POST zeros to an endpoint that discards them, in rounds that
  grow from ``UPLOAD_FIRST_BYTES`` (doubling, but each sized from the rate
  measured so far to finish inside what is left of ``UPLOAD_SECONDS``) up to
  ``UPLOAD_MAX_BYTES`` in all; throughput is bytes / summed round time. Each
  round is timed to the server's answer, so it counts only bytes that actually
  arrived — a single streamed body would be timed by its writes into the
  *local* proxy, which buffers far more than a slow uplink carries. A normal
  link sends the whole 8 MB; a slow or congested one stops near the time
  budget and still reports a (low) number, where one fixed 8 MB payload
  outlasted the run's deadline and reported nothing.

Each metric has an ordered list of endpoints and the first one that works wins —
the same multi-source approach as the heartbeat probe, so one retired or flaky
endpoint degrades to a backup instead of reporting "-". Primaries: download uses
Cloudflare; upload uses a plain upload sink because Cloudflare's ``__up`` throttles
proxied POSTs heavily (it is kept only as the last-resort backup). The former
primary, librespeed.org's ``backend/empty.php``, was retired: it answers 404 —
after swallowing the whole body, so it cost a full upload before falling back.
Upload is also naturally capped by the machine's own uplink (shared by every
channel), so all channels tend to converge there.
"""

from __future__ import annotations

import time
import urllib.request
from collections.abc import Callable

from anyhop.probe import proxy_opener

# 50 MB per fetch — Cloudflare rejects much larger single requests, so the
# download test loops fetches until DOWNLOAD_SECONDS is up rather than asking
# for one huge object. Backups are large static objects on independent
# infrastructure. HTTPS ONLY (decided 2026-07-17): advisory speed numbers are
# not worth plain-HTTP egress from a VPN product, so the former tele2/Firefox
# plain-HTTP fallbacks are gone rather than noqa'd.
DOWNLOAD_URLS = [
    "https://speed.cloudflare.com/__down?bytes=50000000",
    "https://proof.ovh.net/files/100Mb.dat",
]
# Endpoints that accept and discard a POST body.
UPLOAD_URLS = [
    "https://dlptest.com/api/http-post/",  # "nothing stored, logged, or forwarded"
    "https://speed.cloudflare.com/__up",  # throttles proxied POSTs; last resort
]
# Tiny objects on independent infrastructure (Cloudflare, Google).
LATENCY_URLS = [
    "https://speed.cloudflare.com/__down?bytes=1",
    "https://www.gstatic.com/generate_204",
]

DOWNLOAD_SECONDS = 5.0  # read the download stream for at most this long
UPLOAD_SECONDS = 5.0  # time budget the upload rounds are sized to fit
UPLOAD_FIRST_BYTES = 256 * 1024  # first round; later ones at most double
UPLOAD_MIN_ROUND = 64 * 1024  # a smaller round than this is all overhead: stop
UPLOAD_MAX_BYTES = 8 * 1024 * 1024  # total payload, as the fixed upload sent
LATENCY_SAMPLES = 5  # tiny requests; the fastest one wins
# The whole run (all phases, all fallbacks) is bounded by one monotonic
# deadline — a hung endpoint can stall one phase, never the entire command.
OVERALL_SECONDS = 60.0
_CHUNK = 65536
_SMALL_READ_CAP = 1 * 1024 * 1024  # latency/upload replies are tiny; cap reads
_USER_AGENT = "anyhop-speedtest/1"


class Cancelled(Exception):
    """Raised to abort a throughput run mid-transfer (e.g. the streaming client
    disconnected). Caught by :func:`run`, which returns what it has so far."""


def _latency_ms(
    opener, timeout: int, cancel: Callable[[], bool] | None = None
) -> float | None:
    """Min round trip against the first latency endpoint that answers.

    A failed sample abandons that endpoint for the next one (rather than
    retrying a dead host ``LATENCY_SAMPLES`` times), so a broken primary costs
    one timeout, not five.
    """
    for url in LATENCY_URLS:
        if cancel and cancel():
            raise Cancelled
        best: float | None = None
        for _ in range(LATENCY_SAMPLES):
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})  # noqa: S310
            start = time.monotonic()
            try:
                with opener.open(req, timeout=timeout) as r:  # noqa: S310 (loopback proxy)
                    r.read(_SMALL_READ_CAP)
            except Exception:  # noqa: BLE001 — endpoint not usable; try the next source
                break
            ms = (time.monotonic() - start) * 1000
            best = ms if best is None else min(best, ms)
        if best is not None:
            return round(best, 1)
    return None


def _download_bps(
    opener, timeout: int, cancel: Callable[[], bool] | None = None
) -> float | None:
    for url in DOWNLOAD_URLS:
        if cancel and cancel():
            raise Cancelled
        total = 0
        start = time.monotonic()
        try:
            while time.monotonic() - start < DOWNLOAD_SECONDS:
                req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})  # noqa: S310
                with opener.open(req, timeout=timeout) as r:  # noqa: S310 (loopback proxy)
                    while time.monotonic() - start < DOWNLOAD_SECONDS:
                        if cancel and cancel():
                            raise Cancelled
                        chunk = r.read(_CHUNK)
                        if not chunk:
                            break  # object exhausted; loop fetches another
                        total += len(chunk)
        except Cancelled:
            raise
        except Exception:  # noqa: BLE001
            if total == 0:
                continue  # nothing flowed from this endpoint — try the next
            # died mid-stream: measure what we got rather than discarding it
        elapsed = time.monotonic() - start
        if total and elapsed > 0:
            return total * 8 / elapsed
    return None


def _upload_bps(
    opener, timeout: int, cancel: Callable[[], bool] | None = None
) -> float | None:
    for url in UPLOAD_URLS:
        sent = 0
        busy = 0.0  # summed round time — each timed to the server's answer
        size = UPLOAD_FIRST_BYTES
        try:
            while busy < UPLOAD_SECONDS and sent < UPLOAD_MAX_BYTES:
                if cancel and cancel():
                    raise Cancelled
                req = urllib.request.Request(  # noqa: S310
                    url,
                    data=b"\0" * size,
                    headers={
                        "User-Agent": _USER_AGENT,
                        "Content-Type": "application/octet-stream",
                    },
                )
                start = time.monotonic()
                with opener.open(req, timeout=timeout) as r:  # noqa: S310 (loopback proxy)
                    r.read(_SMALL_READ_CAP)
                busy += time.monotonic() - start
                sent += size
                # the next round: double, but only as much as the rate so far
                # says fits the time left — so the last round can't overshoot
                fits = int(sent / busy * (UPLOAD_SECONDS - busy)) if busy else size
                size = min(size * 2, fits, UPLOAD_MAX_BYTES - sent)
                if size < UPLOAD_MIN_ROUND:
                    break
        except Cancelled:
            raise
        except Exception:  # noqa: BLE001
            if not sent:
                continue  # nothing arrived at this sink — try the next one
            # a later round failed: measure the rounds that completed
        if sent and busy > 0:
            return sent * 8 / busy
    return None


def run(
    port: int,
    timeout: int = 60,
    progress=None,
    measure_latency: bool = True,
    cancel: Callable[[], bool] | None = None,
) -> dict:
    """Measure latency + download + upload through the proxy on ``port``.

    Returns ``{"latency_ms", "download_bps", "upload_bps"}``; any metric whose
    transfer failed (proxy down, endpoint unreachable) comes back ``None``.

    ``progress`` is an optional callback invoked with the phase name
    (``"latency"`` → ``"download"`` → ``"upload"``) just before each phase starts,
    so a caller can drive a live indicator during the (several-second) run.

    ``measure_latency`` skips the latency phase when the caller already has a
    fresher probe latency (``anyhop test --speed`` probes first) — the returned
    ``latency_ms`` is then ``None`` for the caller to fill in, avoiding a redundant
    round of tiny requests.

    ``cancel`` is an optional predicate polled between and within phases; when it
    returns true the run aborts (a streaming client that disconnected should not
    keep driving transfers). A cancelled phase yields ``None`` for that metric.

    The whole run is additionally bounded by one monotonic deadline
    (``OVERALL_SECONDS``): per-request timeouts bound a single connection, but
    endpoint fallbacks multiply them — the deadline caps the sum, and phases it
    cuts off return ``None`` while completed ones keep their numbers.
    """
    opener = proxy_opener(port)
    deadline = time.monotonic() + OVERALL_SECONDS

    def _expired() -> bool:
        return (cancel is not None and cancel()) or time.monotonic() >= deadline

    def _phase(name: str) -> None:
        if progress is not None:
            progress(name)

    latency = download = upload = None
    try:
        if measure_latency:
            _phase("latency")
            latency = _latency_ms(opener, timeout, _expired)
        _phase("download")
        download = _download_bps(opener, timeout, _expired)
        _phase("upload")
        upload = _upload_bps(opener, timeout, _expired)
    except Cancelled:
        pass  # return whatever phases completed before the cancel/deadline
    return {"latency_ms": latency, "download_bps": download, "upload_bps": upload}
