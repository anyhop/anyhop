# Getting started

Install anyhop, add a provider, create channels, and learn the everyday
commands. The [CLI reference](cli-reference.md) documents every command and
flag; this page is the walkthrough.

## Install

Choose a native user-level install or the scoped Docker deployment. Native
TUN can capture the host; Docker never does.

| Choice                | Supervisor                 | Traffic captured                          | Host-wide VPN |
| --------------------- | -------------------------- | ----------------------------------------- | ------------- |
| One-command uv script | launchd / `systemd --user` | Host apps or host TUN                     | Yes           |
| Homebrew              | `brew services`            | Host apps or host TUN                     | Yes           |
| Manual uv install     | launchd / `systemd --user` | Host apps or host TUN                     | Yes           |
| Manual pipx install   | launchd / `systemd --user` | Host apps or host TUN                     | Yes           |
| Docker proxy hub      | Docker restart policy      | Proxy-aware containers/apps               | No            |
| Docker gateway        | Docker restart policy      | anyhop netns + explicitly joined containers | No            |

### One-command script (macOS + Linux)

```bash
curl -LsSf \
  https://github.com/anyhop/anyhop/releases/latest/download/install.sh | sh
```

The release-pinned bootstrap supports normal, non-root macOS and systemd Linux
hosts on arm64/aarch64 and x86_64. It verifies and installs a pinned uv when
needed, installs that release's exact `anyhop` version, registers the
user-level login service, and verifies readiness. It refuses containers, WSL,
non-systemd Linux sessions, and installs already owned by another package
manager; it never invokes `sudo` or a system package manager. On Linux, opt
into running after logout explicitly by replacing the final `sh` with
`sh -s -- --linger`.

The command works from bash, zsh, ash, and other interactive shells because
they only feed the asset to its declared POSIX `sh` interpreter; the installer
does not depend on the caller's shell syntax.

If the uv tool directory was not already on `PATH`, the installer updates the
appropriate shell profile and prints both the exact temporary `export` command
and the absolute `anyhop` path. Restart the shell before relying on bare `anyhop`,
or run that printed export to use it immediately in the current shell.

The one-liner trusts HTTPS and GitHub for the first downloaded byte, and its
`latest` URL moves to each new stable release. To inspect an immutable,
explicitly tagged asset and verify that release's published digest before
executing it:

```bash
version=v0.1.18
base="https://github.com/anyhop/anyhop/releases/download/$version"
curl -LsSf -O "$base/install.sh"
curl -LsSf -O "$base/install.sh.sha256"
sha256sum -c install.sh.sha256             # Linux
# or: shasum -a 256 -c install.sh.sha256   # macOS
less install.sh
sh install.sh                              # add --linger on Linux if desired
```

### Manual install with [`uv`](https://docs.astral.sh/uv/)

```bash
# Install uv first using its official instructions, then install anyhop.
uv tool install anyhop
# If uv reports that its tool directory is not on PATH:
uv tool update-shell
```

Restart the shell after `uv tool update-shell` before using bare `anyhop`. To run
the background daemon at login, optionally register it in that new shell:

```bash
anyhop daemon install
```

Without the service step, the runtime auto-starts on first use and runs for the
session.

### Install with [Homebrew](https://brew.sh) (macOS + Linux)

```bash
# 1. add the tap and install the headless CLI + Web UI
brew install anyhop/tap/anyhop
# 2. run the background daemon at login, supervised by brew services
brew services start anyhop
```

The Homebrew channel is deliberately headless — CLI, daemon, control API, and
bundled Web UI. On this channel let `brew services`
own the daemon rather than `anyhop daemon install` (they would register competing
launchd/`systemd --user` units for the same user). `anyhop upgrade` recognizes a
brew-owned install and delegates to `brew upgrade`.

### Manual install with [`pipx`](https://pipx.pypa.io/stable/)

```bash
# 1. install pipx (e.g. `brew install pipx` or your distro's package)
# 2. install the anyhop CLI
pipx install anyhop
# 3. (optional) run the background daemon at login
anyhop daemon install
```

The pipx service step is optional: without it the runtime auto-starts on first
use (`anyhop start` or the first channel you add) and runs for the session.
`anyhop daemon install` registers it as a user-level login service (macOS
LaunchAgent / `systemd --user`) so it starts at login and is supervised — see
the [CLI reference](cli-reference.md#anyhop-daemon).

Also works: `python -m pip install anyhop` into an environment you manage,
or one-off runs with `uvx --from anyhop anyhop --help`.

**With Docker (proxy hub):**

```bash
docker pull ghcr.io/anyhop/anyhop:latest
docker run -d --name anyhop --restart unless-stopped \
  --mount type=volume,src=anyhop-state,dst=/var/lib/anyhop \
  --mount type=bind,src="$PWD/bundle.yaml",dst=/etc/anyhop/bundle.yaml,readonly \
  ghcr.io/anyhop/anyhop:latest
docker exec anyhop anyhop health
docker exec anyhop anyhop status
```

The bundle mount is optional, but when present use the long syntax so a
missing host file fails instead of becoming a directory. Docker uses
`anyhop run` plus its restart policy, not `anyhop daemon install`. Gateway mode
adds an explicit root override, `NET_ADMIN`, and `/dev/net/tun` in place of
the native helper/setcap/sudo ladder. It captures only anyhop's network
namespace and containers explicitly joined to it—not the Linux or macOS host.
See [docker.md](docker.md) and the [Compose walkthrough](docker-compose.md).

After installation:

```bash
anyhop version
anyhop --help
```

**Uninstall** according to the channel that owns anyhop:

```bash
# macOS only: do this first if you installed the optional root helper
sudo anyhop helper uninstall

# One-command script: remove service, uv-owned anyhop tool, and all anyhop state
curl -LsSf \
  https://github.com/anyhop/anyhop/releases/latest/download/install.sh | \
  sh -s -- --uninstall

# Manual uv
anyhop stop
anyhop daemon uninstall
uv tool uninstall anyhop

# Manual pipx
anyhop stop
anyhop daemon uninstall
pipx uninstall anyhop

# pip, from the same managed Python environment used to install anyhop
anyhop stop
anyhop daemon uninstall
python -m pip uninstall anyhop

# Homebrew
brew services stop anyhop
anyhop stop
brew uninstall anyhop
```

The script uninstaller acts only when its bootstrap receipt proves ownership.
It removes only the uv-owned anyhop tool recorded or adopted by the bootstrap,
never uv itself or a pipx, pip, or Homebrew installation. It refuses while the
optional macOS root helper is still installed because a user-level script
cannot safely remove its root LaunchDaemon. It deletes the dedicated state
directory recorded during bootstrap, including provider credentials and
WireGuard keys, and restores Linux login lingering only when the bootstrap
enabled it. It retains uv itself because uv may now be used independently.
If uninstall is interrupted after teardown starts, rerun the same command; the
receipt-backed cleanup resumes without claiming an unrelated installation.
Manual package-manager uninstalls leave `~/.anyhop` behind; remove it separately
if you want their state gone. If you set `ANYHOP_HOME` for the bootstrap, make it
a dedicated anyhop state directory: successful script uninstall removes that
recorded directory in full. The explicit `anyhop stop` in the manual recipes
also covers a session runtime that was started without a login service.

Upgrades track stable releases by default. A uv, pipx, or pip installation can
explicitly inspect or install a future prerelease with `anyhop upgrade --check
--prerelease` or `anyhop upgrade --prerelease`. Homebrew, the one-command
bootstrap's `latest` asset, Docker `latest`, and GitHub's stable `latest` stay
on numeric stable releases. Version ordering follows
[PEP 440](https://packaging.python.org/en/latest/specifications/version-specifiers/)
(`0.1.8` < `0.1.9rc1` < `0.1.9`) while remaining fully compatible with
numeric-only versions.

## Quick start

Add a provider, create a channel, start the runtime, then use the channel's
local proxy port.

```bash
anyhop providers add nordvpn
anyhop channels add nordvpn --country "United States"
anyhop start
anyhop channels ls
```

`anyhop channels ls` prints the local proxy port for each channel:

```text
LABEL            ID                       PORT    COUNTRY        CITY           IPV6  STATUS
---------------  -----------------------  ------  -------------  -------------  ----  -------
wg_us_1          nordvpn/wg_us_1          :53124  United States  (Any City)     no    enabled
```

Use that port from any tool or app that supports an HTTP or SOCKS proxy:

```bash
curl -x http://127.0.0.1:53124 https://api.ipify.org
```

Check health and traffic (`status` is the system summary; `test` is the
per-channel table — fresh IP/latency plus cumulative sent/received):

```bash
anyhop status
anyhop test
```

## Provider setup

`anyhop` supports two provider setup styles today:

**NordVPN** uses an access token:

```bash
anyhop providers add nordvpn
anyhop locations nordvpn
anyhop locations nordvpn --country "United States"
anyhop channels add nordvpn --country "United States" --city "Seattle"
```

To rotate a bad or expired token later, run `anyhop providers add nordvpn` again
(or use the gear on the provider in the Web UI): it confirms, validates the new
token, and re-resolves the provider's channels — no need to remove and re-add.
The stored token is never displayed back, only a masked preview. This is distinct
from a bundle import (which changes your whole setup from a file); a token update
changes one live credential. See
[`anyhop providers add`](cli-reference.md#anyhop-providers-add-provider).

**Proton VPN** uses WireGuard config files downloaded from Proton:

```bash
anyhop providers add protonvpn
anyhop channels add protonvpn --config ~/Downloads/wg-US-CA-842.conf
```

Re-importing the same `.conf` file updates that channel in place, keeping the
same channel id and local port; re-importing a byte-identical file changes
nothing and tells you the channel already exists.

## Friendly names

Channels are identified by a globally-unique, provider-qualified id
(`nordvpn/wg_us_1`) — the handle every command takes, shown in the `ID`
column. You can also give one a display label for readability (the `LABEL`
column in `channels ls` and `test`). The id never changes,
so relabelling is always safe:

```bash
anyhop channels add nordvpn --country "United States" --label "Streaming - US"
anyhop channels setlabel wg_us_1 "Streaming - US"   # or set it later
anyhop channels setlabel wg_us_1                    # omit text to clear
```

## Common commands

Useful commands after setup:

```bash
anyhop providers ls
anyhop channels ls
anyhop channels ls --refs
anyhop status
anyhop test
anyhop logs
anyhop stop
```

Most read commands support `--json` for scripts:

```bash
anyhop status --json
anyhop channels ls --json
anyhop test --json
```

Channel and provider removals accept multiple targets:

```bash
anyhop channels rm wg_jp_1 wg_us_seattle_1
anyhop channels rm protonvpn/wg_us_ca_842
anyhop channels rm 'wg_us_*' --dry-run
anyhop providers rm nordvpn protonvpn -y
```

## Hold more channels than your plan's connection cap

Some subscriptions limit simultaneous connections (NordVPN and Proton VPN
allow ~10). A **disabled** channel stays in your config but is not
materialised at all — no WireGuard handshake or keepalive toward the
provider, so it uses **no connection slot**. Keep a stable of servers on hand
and flip which ones are live:

```bash
anyhop channels disable wg_jp_1            # free the slot; config + rules stay
anyhop channels enable wg_jp_1             # dial it again
anyhop channels disable 'wg_us_*'          # same ref grammar as rm
```

Disabled channels stay visible everywhere (`channels ls` grows a STATUS
column; `test` shows a skipped `Disabled` row) and can't be targeted by
routing rules while disabled. This is local intent only — it doesn't
deregister the device from your provider account.

## Where to next

- [Rule-based routing](routing.md) — one router entrypoint, first-match rules.
- [Web UI](web-ui.md) — the browser dashboard (`anyhop ui`).
- [Backup and declarative setup](declarative-config.md) — the bundle file.
- [CLI reference](cli-reference.md) — every command, flag, and env var.
