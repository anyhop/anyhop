#!/bin/sh
# anyhop container entrypoint: converge on the declared setup, then hand PID 1
# to the daemon loop (exec, so `docker stop`'s SIGTERM reaches it directly).
#
# Started as root (the image default), it fixes state-volume ownership and
# drops to the unprivileged `anyhop` user — unless ANYHOP_RUN_AS_ROOT=1, which
# TUN/gateway mode needs in v1 (sing-box must be able to create the tun
# device; the capability comes from --cap-add NET_ADMIN + /dev/net/tun).
# Started with --user, it runs as that user and touches no ownership.
set -eu

BUNDLE="${ANYHOP_BUNDLE-/etc/anyhop/bundle.yaml}"
STATE="${ANYHOP_HOME:-/var/lib/anyhop}"

fail() {
	echo "anyhop-entrypoint: ERROR: $*" >&2
	exit 1
}

run_as() {
	# Root path: run everything as the anyhop user via setpriv (in util-linux,
	# present in slim). Non-root path: run in place.
	if [ "$(id -u)" = "0" ] && [ "${ANYHOP_RUN_AS_ROOT:-}" != "1" ]; then
		setpriv --reuid anyhop --regid anyhop --init-groups "$@"
	else
		"$@"
	fi
}

# Ownership commands below take $STATE from the environment — refuse values
# whose repair sweep could touch the system: relative paths, the root
# directory, and system prefixes are configuration errors, not state dirs.
case "$STATE" in
/) fail "ANYHOP_HOME=/ is not a state directory" ;;
/bin | /bin/* | /dev | /dev/* | /etc | /etc/* | /lib | /lib/* | /proc | /proc/* | /sbin | /sbin/* | /sys | /sys/* | /usr | /usr/*)
	fail "ANYHOP_HOME=$STATE points into a system prefix — mount state elsewhere (default /var/lib/anyhop)"
	;;
/*) ;;
*) fail "ANYHOP_HOME=$STATE must be an absolute path" ;;
esac

# Started as root (the explicit override profile — the image USER is 1000):
# initialize the state dir once and repair ONLY wrong-owned entries, instead
# of an unconditional recursive chown on every restart. A named volume
# already inherits the image's anyhop-owned /var/lib/anyhop, so on a healthy
# steady state this is a read-only scan.
if [ "$(id -u)" = "0" ] && [ "${ANYHOP_RUN_AS_ROOT:-}" != "1" ]; then
	mkdir -p -- "$STATE"
	find "$STATE" ! -user anyhop -exec chown -h -- anyhop:anyhop {} +
fi

# The bundle is the desired state: `anyhop sync` converges the managed setup on
# it every start (repeat boots are idempotent; edits/removals touch only what
# sync itself created), and any problem fails the start loudly in
# `docker logs` — *before* anything is imported — instead of running a
# half-configured VPN. The mount contract fails loud too: quickstarts use
# long `--mount` syntax so a missing host file is an engine error, and a
# directory at the bundle path (what short `-v` creates from a missing host
# file) is refused here rather than silently skipped. Only the explicit
# no-bundle profile (ANYHOP_BUNDLE set empty or "none") and the interactive
# default profile (nothing mounted, ANYHOP_BUNDLE unset) skip the sync.
if [ -z "$BUNDLE" ] || [ "$BUNDLE" = "none" ]; then
	echo "anyhop-entrypoint: no-bundle profile (ANYHOP_BUNDLE=${BUNDLE:-}) — starting unconfigured" >&2
elif [ -d "$BUNDLE" ]; then
	fail "$BUNDLE is a directory, not a bundle file — a short '-v host.yaml:$BUNDLE' \
mount with a missing host file creates a directory; use \
'--mount type=bind,src=/abs/path/bundle.yaml,dst=$BUNDLE,readonly' so a missing \
source fails at docker run instead"
elif [ -e "$BUNDLE" ] && [ ! -f "$BUNDLE" ]; then
	fail "$BUNDLE exists but is not a regular file — mount a bundle file there, \
or set ANYHOP_BUNDLE=none for the explicit no-bundle profile"
elif [ ! -e "$BUNDLE" ]; then
	if [ -n "${ANYHOP_BUNDLE+set}" ]; then
		fail "ANYHOP_BUNDLE=$BUNDLE does not exist — mount the bundle file, or set \
ANYHOP_BUNDLE=none for the explicit no-bundle profile"
	fi
	echo "anyhop-entrypoint: no bundle at $BUNDLE — starting unconfigured (interactive profile)" >&2
else
	echo "anyhop-entrypoint: syncing $BUNDLE" >&2
	run_as anyhop sync "$BUNDLE"
fi

# Gateway profile (ANYHOP_GATEWAY=1): privilege-check and declare the
# fail-closed TUN + kill-switch data plane BEFORE the daemon starts, so
# readiness (the HEALTHCHECK) can gate Compose dependants on the declared
# contract instead of a manual post-start toggle. Missing root mode,
# /dev/net/tun, or NET_ADMIN fails the start loudly right here.
if [ "${ANYHOP_GATEWAY:-}" = "1" ]; then
	echo "anyhop-entrypoint: gateway profile — declaring TUN + kill switch" >&2
	run_as anyhop gateway init
fi

# exec (not a function call) so "$@" becomes PID 1 and receives docker stop's
# SIGTERM directly.
if [ "$(id -u)" = "0" ] && [ "${ANYHOP_RUN_AS_ROOT:-}" != "1" ]; then
	exec setpriv --reuid anyhop --regid anyhop --init-groups "$@"
fi
exec "$@"
