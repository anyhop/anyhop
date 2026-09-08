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
