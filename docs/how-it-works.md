# How anyhop works

The runtime model, in one page. The security counterpart is
[security.md](security.md); the REST API contract is [api.md](api.md).

- `anyhop` keeps its local state under `~/.anyhop/`, or under `$ANYHOP_HOME` when that
  environment variable is set. This includes providers, channels, credentials,
  metrics, generated config, logs, and runtime files.

- `anyhop` manages one [`sing-box`](https://github.com/SagerNet/sing-box) process
  instead of starting one VPN process per channel. The generated config contains
  one local HTTP+SOCKS inbound per channel, plus the router entrypoint inbound
  whose sing-box route rules are compiled from `anyhop routes`.

- Each channel routes to one WireGuard peer. NordVPN channels are created from
  the provider API; Proton VPN channels are created by importing a WireGuard
  `.conf` file. After creation, both behave the same way.

- WireGuard is connectionless, so `anyhop` does not model channels as connected or
  disconnected. A channel exists in config; its health comes from the latest
  probe.

- Local proxy ports are assigned by the OS and stored in state. Use
  `anyhop channels ls` to see the current ports. When something outside anyhop
  must know a port ahead of time (a firewall rule, a compose file), declare
  it instead — `anyhop channels add … --port 20010`, or `port:` in a bundle;
  declared ports are honored as written and clash loudly rather than being
  silently moved.

- The background runtime (`anyhop applier`, or foreground `anyhop run`) applies state changes, keeps the `sing-box`
  process in sync, probes channel health, and records per-channel traffic
  totals. It also serves the control API — the Web UI and the REST API — as a
  thread in the same process.

- `anyhop` uses a pinned upstream `sing-box` release and verifies its checksum
  before running it.

## Architectural boundary

`sing-box` is a private data-plane backend, not part of anyhop's public API.
The CLI and daemon-hosted REST API call the application operations in
`anyhop.service`; the Web UI calls that REST API. Those operations expose
anyhop concepts—providers, channels, rulesets, health, traffic, and
lifecycle—and do not return generated sing-box configuration, native tags,
control endpoints, credentials, or raw control responses.

Process control and telemetry needed by orchestration go through the internal
`RuntimeBackend` contract in `anyhop.backend`. Its default implementation is
the pinned sing-box runner. The engine is the implementation adapter: it
compiles anyhop state into backend configuration and owns the translation of
backend outcomes. This boundary is deliberately an internal substitution and
testing seam, not a public plugin API or a promise that arbitrary data-plane
cores are interchangeable.

The sing-box Clash API exists only for backend readiness and traffic sampling.
It binds to a random loopback port and requires a per-installation secret; its
address and response shapes are implementation details and must never be
proxied through the anyhop REST API. Clients integrate with `/api/v1`, whose
versioning is owned by anyhop and can remain stable across sing-box changes.
