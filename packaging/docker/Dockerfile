# The official anyhop container image — the opt-in container profile.
#
# Design (see docs/docker.md):
# * The container boundary is the trust boundary: ANYHOP_LISTEN=0.0.0.0 makes
#   channel/router proxy ports reachable on the container network; nothing is
#   reachable beyond it unless the operator publishes a port. The Web UI stays
#   loopback-only — manage via `docker exec <name> anyhop …`.
# * No sing-box is baked into this image (GPL option A): the pinned build is
#   fetched and checksum-verified on first start, into the state volume, so a
#   container recreate never re-downloads.
# * PID 1 is `anyhop run` (the daemon loop, foreground, logs on stderr);
#   restart policies replace launchd/systemd. `docker stop` = clean SIGTERM,
#   and foreground ownership stops/reaps sing-box + tears down TUN in the
#   stop grace period.
# * The image USER is 1000 (the `anyhop` user) — proxy mode is non-root in the
#   image *metadata*, so `runAsNonRoot`-style admission checks can verify it.
#   Gateway mode is the explicit root override: user: "0" + ANYHOP_RUN_AS_ROOT=1
#   + ANYHOP_GATEWAY=1 (plus --cap-add NET_ADMIN --device /dev/net/tun).
#   A named state volume inherits the image's anyhop-owned /var/lib/anyhop; for a
#   bind-mounted state dir either chown it to uid 1000 or run the root
#   override, where the entrypoint repairs only wrong-owned entries.

# Base images pinned by immutable digest — same discipline as the SHA-pinned
# CI Actions. The tag rides in the reference, not a trailing comment: with a
# digest-only `python@sha256:…` dependabot tracked the default `python` tag
# and offered the full (non-slim) image. Resolved 2026-08-26; the digest is
# the multi-arch manifest list.
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6
ARG ANYHOP_WHEEL_SHA256
LABEL org.opencontainers.image.anyhop-wheel-sha256=$ANYHOP_WHEEL_SHA256
RUN useradd --uid 1000 --user-group --home-dir /var/lib/anyhop --create-home \
    --shell /usr/sbin/nologin anyhop \
    && mkdir -p /etc/anyhop
COPY packaging/docker/requirements.lock /tmp/requirements.lock
COPY dist/*.whl /tmp/
RUN set -eu; \
    wheel=$(find /tmp -maxdepth 1 -name 'anyhop-*.whl' -type f); \
    [ "$(printf '%s\n' $wheel | wc -l)" = 1 ]; \
    actual=$(sha256sum "$wheel" | awk '{print $1}'); \
    [ -z "$ANYHOP_WHEEL_SHA256" ] || [ "$actual" = "$ANYHOP_WHEEL_SHA256" ]; \
    pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock; \
    pip install --no-cache-dir --no-deps "$wheel"; \
    python -c 'import importlib.metadata as m; assert m.version("pycountry") == "26.2.16"'; \
    python -c 'import importlib.metadata as m; assert m.version("pyyaml") == "6.0.3"'; \
    rm /tmp/*.whl /tmp/requirements.lock
COPY packaging/docker/entrypoint.sh /usr/local/bin/anyhop-entrypoint
RUN chmod 0755 /usr/local/bin/anyhop-entrypoint

# The container profile knobs (each opt-in in core; the image opts in):
#   ANYHOP_CONTAINER  — authoritative "in a container" signal (guardrails/hints)
#   ANYHOP_SERVICE    — CLI calls via `docker exec` never self-spawn a second
#                     daemon around PID 1
#   ANYHOP_HOME       — state volume mount point
#   ANYHOP_LISTEN     — proxy inbounds bind the container network, not loopback
#   ANYHOP_PORT_BASE  — deterministic ports (publishable ahead of time); declare
#                     ports in the bundle for full control
ENV ANYHOP_CONTAINER=1 \
    ANYHOP_SERVICE=1 \
    ANYHOP_HOME=/var/lib/anyhop \
    ANYHOP_LISTEN=0.0.0.0 \
    ANYHOP_PORT_BASE=20000

VOLUME /var/lib/anyhop

# Numeric so runAsNonRoot admission checks can prove the non-root contract
# (a named user would be rejected as unverifiable). uid 1000 = the anyhop user.
USER 1000:1000

# Generous start period: the first ever start downloads sing-box into the volume.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD ["anyhop", "health"]

ENTRYPOINT ["anyhop-entrypoint"]
CMD ["anyhop", "run"]
