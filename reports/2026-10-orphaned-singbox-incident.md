# Incident report: orphaned sing-box instances held live provider tunnels that the anyhop API could not see

| | |
| --- | --- |
| Status | Analysis complete. The defect is **not yet reproduced in isolation**; fixes are proposed, none implemented. |
| Events | 2026-10-05 .. 2026-10-09 (UTC). Census taken 2026-10-09 05:10 UTC. |
| anyhop under observation | Container image `ghcr.io/anyhop/anyhop:0.1.20` (Docker Desktop on macOS, state volume mounted at `/var/lib/anyhop`). |
| Source read | Checkout of `main` at 0.1.22. `git diff v0.1.20 v0.1.22 -- src/anyhop/singbox.py src/anyhop/proc.py` changes documentation text only, so the code paths below apply to both versions. |
| Reporter | Operator of a downstream client that drives the REST API. Analysis assisted by an AI agent. |
| Conventions | Every claim is tagged **Established** (directly observed), **Strong** (follows from observation plus the source), **Hypothesis**, or **Unknown**. Addresses, hostnames, account data and keys are scrubbed. |

## 摘要 (Chinese summary)

anyhop 容器里同时存在 4 个 sing-box 进程（正常应为 1 个）：3 个陈旧实例已运行 83h / 32h / 25h，合计持有约 154 个 socket，而 anyhop 的 REST API 只认最新实例，对旧实例完全不可见。旧实例持续保活 WireGuard 隧道，占用 VPN 服务商的每账号连接数，造成 NordVPN 客户端"Too many connections"持续数天，而 API 同时报告 "0 个启用、0 个活跃"。

日志显示，每次孤儿产生前都有同一个签名：reconnect 触发的 sing-box 重启刚排队，**同一秒**监督者就报 `exited unexpectedly (crash 1)`，随后出现 `bind: address already in use`，anyhop 把自己上一个实例占着的端口误判为 "taken by another process" 并**永久改端口**（20115 -> 20124 ...）。保留的约 53 小时日志中，anyhop 自己重启 sing-box 共 106 次，其中只有 3 次出现该爆发，约 3%。最可能的机制是"重启路径"与"崩溃监督者"并发拉起两个实例，输家崩溃，pidfile 被写成死 pid，存活的赢家变成无人认领的孤儿；`_stop_local()` 在 pidfile 校验失败时只删文件、不杀进程，且代码里没有按进程名扫描的清扫逻辑，所以孤儿永远不会被回收。这一机制尚属推断，未经复现。

## 1. Summary

1. The anyhop container ran **four** sing-box processes. Three were stale (ages 83h38m, 31h53m, 24h56m at census) and together held 154 sockets. The REST API reported zero enabled and zero live channels for the affected provider at the same time. **Established.**
2. The provider (NordVPN) rejected new client connections with "Too many connections / device limit reached" for days. **Established** (operator-reported); the causal link to the stale instances is **Strong**, not yet confirmed by a successful connection after cleanup.
3. Each stale instance's birth time (derived from process age) coincides within about three minutes with a log burst whose signature is: reconnect-initiated restart, immediate `exited unexpectedly (crash 1)`, `bind: address already in use`, `taken by another process -- moved to :N`. **Established** for two of three births; the oldest birth predates the retained logs.
4. anyhop's treatment of the in-use port is itself a defect: it declares the port stolen and permanently renumbers the channel. After the incident every live listen port (9 of 9) lies outside the compose-published range 20040-20079. **Established.**
5. The leading mechanism (two concurrent spawners; pidfile overwritten by the loser; fail-open stop; no orphan reconciliation) is **Hypothesis**, supported by the logs and the source, but no pid-level trace exists because the instances were killed before they could be inspected further.

## 2. Environment and workload

- Deployment: Docker Compose service `anyhop` (image pinned to 0.1.20, `restart: unless-stopped`, no `init`, PID 1 is `anyhop run`), state volume at `/var/lib/anyhop`, `ANYHOP_PORT_BASE=20040`, channel ports published to loopback for 20040-20079 only, API on 8080.
- Channels: 14 NordVPN, 38 ProtonVPN, 3 custom WireGuard (`customized/*`) -- 55 configured. At any time only a few were enabled.
- Client workload (the downstream client): a sweep that, per server, did `POST /channels/nordvpn/<name>/server`, `POST .../enabled {true}`, a short browser probe through the channel port, then `POST .../enabled {false}`. Roughly 2,300 pins plus as many disables over about seven days, with up to 8 concurrent client threads and 8 enabled Nord channels at a time. The client was hard-killed (SIGKILL) three times while channels were enabled, which left channels enabled (documented separately; disabling them via the API worked correctly and tunnels dropped within about two minutes).
- A separate host-native anyhop instance on the same machine belongs to the operator, has no NordVPN channels, and is **not involved** in this incident.

## 3. Symptom

- The provider's own desktop client refused to connect for days: "Too many connections or device limit reached. If you're connected on 10 devices, disconnect one of them."
- `GET /api/v1/channels` and `/api/v1/status` showed, for that provider: 14 channels, 0 enabled, 0 with an IP. Other providers' channels (5 enabled, 5 with an IP across Proton and custom) were as expected.
- Fresh pins of that provider's channels produced `no IP within 90 s`, and anyhop's own probe reported every IP source timing out ("all IP sources failed (cloudflare-trace, icanhazip); last: channel deadline (15s) exhausted") for those channels while the other providers' channels probed healthy.
- Disabling every channel of the provider and waiting did not clear the condition (tried after 13 h and after more than 24 h of zero handshakes).

## 4. Evidence

### E1. Process census (taken 2026-10-09 05:10 UTC, read-only, inside the container)

| pid | age | sockets (fds of type socket) | note |
| --- | --- | --- | --- |
| 38861 | 83 h 38 m | 54 | stale |
| 243171 | 31 h 53 m | 62 | stale |
| 272461 | 24 h 56 m | 38 | stale |
| 378494 | 0 h 04 m | 48 | current (started by a normal reconnect restart at about 05:06) |

All four command lines were `.../sing-box@1.14.0 run -c /var/lib/anyhop/singbox.json`. PID 1 was `anyhop run`. Network namespace totals: 47 UDP4 sockets and 42 UDP6 sockets (89), where a handful of live tunnels needs single digits. The API saw only the newest instance. **Established.**

Ages were computed from `/proc/<pid>/stat` start time against `/proc/uptime` (CLK_TCK 100); the error is about a minute.

### E2. anyhop restarts sing-box on its own, constantly

From `anyhop.log.1` (2026-10-07 00:20 .. 2026-10-08 03:09 UTC) and `anyhop.log` (2026-10-08 03:09 .. 2026-10-09 05:13 UTC):

| log | `reconnect: restarted sing-box once` | `exited unexpectedly (crash 1)` bursts | `start hit an in-use port` | `was taken by another process` | `address already in use` (FATAL) |
| --- | --- | --- | --- | --- | --- |
| `anyhop.log.1` | 9 | 2 | 32 | 16 | 15 |
| `anyhop.log` | 97 | 1 | 20 | 10 | 10 |
| total | **106** | **3** | 52 | 26 | 25 |

The restarts are driven by the reconnect state machine (attempt 1/5 at +30 s, 2/5 at +60 s, 3/5 at +120 s, 4/5 at +300 s) for persistently unhealthy channels -- almost all for two Proton channels, a few for custom channels, none for the provider whose cap was exhausted. Busiest hours (UTC): 12 restarts in `2026-10-09 03:00`, 11 in `00:00`, 8 in `02:00` and in `2026-10-08 17:00`. **Established.** About 3 % of restarts (3 of about 109 attempts) entered the burst signature below.

Each restart tears down and re-establishes every enabled channel's tunnel, for every provider, not only the unhealthy one (inferred from a single sing-box process owning all endpoints; **Strong**).

### E3. The three crash bursts (UTC, scrubbed)

The only three occurrences of `exited unexpectedly (crash 1)` in the retained logs:

| burst | channel that triggered the reconnect | derived orphan birth (from E1, +-3 min) |
| --- | --- | --- |
| 2026-10-07 21:19:12 | custom channel `a1` | 31 h 53 m old instance -> 2026-10-07 21:17 |
| 2026-10-07 21:27:13 | not examined in detail | no matching instance found |
| 2026-10-08 04:16:39 | custom channel `b1` | 24 h 56 m old instance -> 2026-10-08 04:14 |
| (outside retained logs) | -- | 83 h 38 m old instance -> 2026-10-05 17:32 |

Excerpt, burst on 2026-10-08 (hostnames and addresses scrubbed, long error tails truncated):

```
04:16:39  reconnect: customized/<custom-b> still failing after 3/3 probes -- attempt 1/5 due
04:16:39  reconnect: customized/<custom-b> attempt 1/5 -- sing-box restart queued; next retry in 30s if still unhealthy
04:16:39  sing-box exited unexpectedly (crash 1); restarting (next attempt in 1s if it crashes again)
04:16:39  supervised restart failed: sing-box started but its control API did not become reachable
04:16:39  reconnect: sing-box restart failed -- sing-box exited immediately (code 1); last log lines: ... context canceled
04:16:41  sing-box exited unexpectedly (crash 2); restarting (next attempt in 2s if it crashes again)
04:16:42  reconcile: start hit an in-use port -- retrying the same config in 0.5s before treating it as stolen
04:16:42  reconcile: start hit an in-use port -- retrying the same config in 1.5s before treating it as stolen
04:16:44  reconcile: port 20115 of protonvpn/vpn_protonvpn_us_ca_346 was taken by another process -- moved to :20124
04:16:49  reconcile failed (retrying in 60s): sing-box exited immediately (code 1); last log lines: FATAL[0000] start service: start inbound/mixed[in-protonvpn-vpn_protonvpn_us_ca_346]: listen tcp4 <ip>:20115: bind: address already in use ...
04:16:49  sing-box exited unexpectedly (crash 3); restarting (next attempt in 4s if it crashes again)
04:16:53  reconcile: port 20117 of protonvpn/vpn_protonvpn_us_ca_642 was taken by another process -- moved to :20126
...       (crash 4 at 04:16:58 and crash 5 at 04:17:06, one more channel moved to a new port per iteration)
```

Excerpt, burst on 2026-10-07 (same shape, plus DNS errors for a custom endpoint hostname at the same moment):

```
21:19:12  reconnect: customized/<custom-a> attempt 1/5 -- sing-box restart queued; next retry in 30s if still unhealthy
21:19:12  sing-box exited unexpectedly (crash 1); restarting (next attempt in 1s if it crashes again)
21:19:13  reconnect: sing-box restart failed -- sing-box exited immediately (code 1); last log lines: ERROR dns: lookup failed for <custom-endpoint-host>: context canceled ...
21:19:13  supervised restart failed: sing-box started but its control API did not become reachable
21:19:15  reconcile: start hit an in-use port -- retrying the same config in 0.5s before treating it as stolen
21:19:18  reconcile: port 20108 of nordvpn/wg_us_albuquerque_1 was taken by another process -- moved to :20109
21:19:23  reconcile: port 20087 of nordvpn/wg_us_buffalo_1 was taken by another process -- moved to :20110
```

Two actors act in the same second: the reconnect path queues a restart, and the process supervisor reports the resulting exit as an *unexpected* crash and schedules its own respawn. **Established** (the log lines); that they are two independent spawners is **Strong**.

### E4. Port drift

- Channels start on `ANYHOP_PORT_BASE` upward (20040..). The 26 "taken by another process" lines moved channels to ports 20109..20132 (24 parseable targets).
- Current `singbox.json` (read for port numbers only): 9 listen ports, minimum 20124, maximum 20132, **9 of 9 above 20079**, the upper bound of the compose-published range.
- The renumbering is permanent: nothing returns a channel to its base port after the situation resolves. **Established.**

### E5. The in-use port was held by anyhop's own earlier instance, not by a foreign process

The ports reported as "taken by another process" are ports anyhop itself allocated in a container where nothing else listens. At the time of the second and third births there was also a surviving sing-box from an earlier generation (E1). **Strong** (consistent observations, no direct pid-to-port mapping was captured).

### E6. Source analysis (0.1.22)

- `src/anyhop/singbox.py` `SingBox.apply()`: reload by SIGHUP when `is_running()`, otherwise `restart()` -> `stop()` -> `_stop_local()`.
- `_stop_local()`:

```python
pid = proc.read_pidfile(_pid_path(), ("sing-box",))
if pid is None:
    _pid_path().unlink(missing_ok=True)
    _started_path().unlink(missing_ok=True)
    return
os.kill(pid, signal.SIGTERM) ...  # then SIGKILL after about 4 s
```

- `proc.read_pidfile()` returns `None` for a missing or unparseable file, a dead pid, **or a live pid whose identity does not match what was recorded at spawn**. In all three cases `_stop_local()` deletes the pidfile and returns **without killing anything**. A live sing-box that the pidfile no longer vouches for survives and is never examined again.
- A text search of `singbox.py`, `proc.py` and `daemon.py` finds no logic that scans the process table for stray instances of the managed binary. The only orphan handling found concerns a TUN trial (`daemon.py`, about line 485). **Established** (search scope: those three files).
- The pidfile has a single writer slot; two near-simultaneous spawns can leave it naming the loser, which exits with a bind error. **Hypothesis** (not traced).
- The container runs without `init`; PID 1 is the Python daemon. This did not cause the orphans (they were live, not zombies) but removes a safety net for reparented processes.

### E7. Environment facts that were ruled out

- A second anyhop controller sharing this state volume: the container's `applier.lock` and a single `anyhop run` PID 1 were present; no second controller was seen. The separate host-native anyhop uses a different home and has no NordVPN channels.
- Operator mistakes on the provider side: the operator's own client was disconnected throughout; its VPN services showed "Disconnected".

## 5. Causal analysis

| Question | Answer | Confidence |
| --- | --- | --- |
| Why was the API blind? | It tracks one sing-box through the pidfile; stale instances are outside any bookkeeping. | Established |
| Why did the provider stay over capacity? | Stale instances kept WireGuard keepalives alive for channels that were enabled when each instance was born (provider channels were enabled and failing in the Oct 7 burst's log lines). | Strong; not confirmed by a post-cleanup connection |
| How does an orphan arise? | A restart cycle in which two spawners race; the winner of the bind becomes untracked when the pidfile is overwritten or invalidated; `_stop_local()` then fails open. | Hypothesis |
| Why are orphans permanent? | No reconciliation by process scan; `_stop_local()` returns early when the pidfile cannot vouch for a live process. | Established (code), Strong (effect) |
| What triggers restarts? | The reconnect state machine for unhealthy channels, including channels unrelated to the provider whose cap was exhausted. | Established |
| Did the client's behaviour cause it? | It enabled the provider's channels and was hard-killed, which exposed the consequences (cap exhaustion), but every observed orphan birth followed an anyhop-initiated restart of unrelated custom or Proton channels. The defect is in anyhop. | Strong |
| Where did the oldest orphan come from? | Unknown: the log that covers its birth (about 2026-10-05 17:32 UTC) has rotated away. | Unknown |

## 6. Impact

1. **Invisible provider-slot leak.** Provider connection caps (here about 10 per account) are consumed by tunnels the API reports as absent. `disable` is documented to free the slot, which is true only for the newest instance.
2. **Silent port drift.** Channels drift out of any statically published port range (compose pattern in the docs: "the range must cover as many channels as anyhop allocates"), so host-side clients on loopback lose access with no error.
3. **Restart amplification.** About 106 sing-box restarts in 53 hours for two persistently unhealthy Proton channels; each restart re-handshakes every enabled channel, for every provider, and each carries the 3 % chance of entering the burst.
4. **Misleading diagnostics.** The log says "taken by another process" for what is probably anyhop's own previous instance.

## 7. Recommendations

Priority P0 (stops the leak):

1. **Serialize sing-box lifecycle.** One owner (lock or actor) for spawn/stop/reload. The supervisor must know about *intentional* stops (set a flag or generation counter before stopping) so a deliberate stop is never reported as a crash and respawned in parallel.
   *Test:* inject a restart while the supervisor is active; assert exactly one live instance afterwards, 1000 iterations with randomized timing.
2. **Reconcile by process scan before every spawn and at start-up.** Find every process whose executable is the managed binary and whose config path is this home's `singbox.json`; terminate any that is not the pidfile's verified pid (or adopt exactly one).
   *Test:* start two instances by hand against the same config, call `apply()`, assert one remains.
3. **Fail closed in `_stop_local()`.** If the pidfile is missing, dead, or mismatched, **do not** return silently: log the reason (WARN) and apply the scan from item 2; refuse to spawn while an unvouched instance exists.
   *Test:* delete the pidfile of a running instance, call `apply()`, assert the old instance is gone and one new instance runs.
4. **Do not treat an in-use port as stolen without identifying the holder.** On `EADDRINUSE` for a port anyhop allocated, resolve the holder (scan `/proc/net/tcp*` and process fds); if it is a sing-box of this home, kill it instead of renumbering. Renumber only for a genuinely foreign holder, and return the channel to its base port when the conflict clears.
   *Test:* occupy a channel's port with a foreign listener and with a stray sing-box; assert different outcomes.

Priority P1 (visibility and blast radius):

5. Include a process census in the status API and health check: list of sing-box pids, ages, socket counts; **fail the healthcheck when more than one instance exists**. Add `anyhop doctor` that prints the census and offers to kill extras.
6. Log pid, parent pid, and start time on every spawn, stop, reload, and pidfile write/removal, so a future incident is traceable at pid level.
7. Reduce restart blast radius: prefer per-endpoint reconnect or SIGHUP reload over a full process restart for a single unhealthy channel, and add a circuit breaker per channel (after attempt 5/5, back off for hours, not minutes); the observed pattern restarts the whole process for the same two channels every 5 to 20 minutes indefinitely.
8. Ship the compose example with `init: true` and document `PR_SET_PDEATHSIG`/process-group kill for the sing-box child so a dying daemon cannot leave a running child.

Priority P2:

9. Until renumbering is removed (item 4), document that a statically published port range must be wider than the channel count, because channels can drift beyond it.
10. Chaos suite: SIGKILL the daemon while sing-box runs; SIGSTOP sing-box so stop times out; two concurrent API-driven enable/disable cycles; assert the census invariant (one instance) after each.

## 8. Proposed reproduction (not yet executed)

1. Compose stack with a few channels whose health can be forced to fail (for example block the endpoint with a firewall rule inside the container).
2. Let the reconnect state machine run at the 30 s / 60 s cadence while a second client toggles other channels through the API, to widen the restart window.
3. After each restart, count sing-box processes (`for p in /proc/[0-9]*` over `cmdline`) and grep the log for `exited unexpectedly (crash 1)` within one second of `restart queued`.
4. Expect roughly one occurrence per 30 restarts; adding artificial latency to `SingBox.stop()` should raise the rate and confirm the race.

## 9. Open questions and data to capture next time

- Which actor wrote the pidfile last at each birth, and what did it contain? (needs item 6)
- Was the surviving process the restart path's spawn or the supervisor's?
- Does the 83-hour-old instance's birth (2026-10-05 17:32 UTC) show the same signature? (log rotated; keep more history)
- Does the provider clear its connection count after the stale instances die, and how fast? (operator to report)
- Is there a cap on `anyhop.log` rotation that should be raised for incidents like this? Two 5 MB files covered only 53 hours.

## 10. Remediation performed on the affected machine

All with explicit operator instruction, none destructive to state:

- Stopped the compose `core` and `anyhop` containers (`docker compose stop`), which ended all four sing-box processes. Containers, volumes, configuration and results were left intact.
- Removed the client-side scratch profiles and stopped the client's own scripts.
- Confirmed no sing-box, anyhop or WireGuard process remained, and that 30 residual UDP/51820 flows seen through Docker Desktop's backend timed out within 62 seconds.
- Provider-side session expiry is outside anyhop's control; whether the provider's cap has cleared is pending the operator's next connection attempt.

## 11. Appendix: collection commands

Process census (inside the container, no dependencies):

```sh
UP=$(cut -d" " -f1 /proc/uptime | cut -d. -f1)
for p in $(ls /proc | grep -E '^[0-9]+$'); do
  tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | grep -q 'sing-box' || continue
  ST=$(awk '{print $22}' /proc/$p/stat); AGE=$((UP - ST/100))
  echo "pid=$p age=$((AGE/3600))h$(((AGE%3600)/60))m sockets=$(ls -l /proc/$p/fd 2>/dev/null | grep -c socket:)"
done
echo "udp4=$(( $(wc -l < /proc/net/udp) - 1 )) udp6=$(( $(wc -l < /proc/net/udp6) - 1 ))"
```

Log statistics (state volume, read-only mount):

```sh
for f in anyhop.log.1 anyhop.log; do
  grep -c 'restarted sing-box' $f
  grep -c 'exited unexpectedly (crash 1)' $f
  grep -c 'was taken by another process' $f
  grep -c 'address already in use' $f
done
grep 'exited unexpectedly (crash 1)' anyhop.log.1 anyhop.log | cut -c1-19
```

Port inventory (numbers only; `singbox.json` contains key material and must not be printed):

```sh
grep -oE '"listen_port": *[0-9]+' singbox.json | grep -oE '[0-9]+$' | sort -n | uniq -c
```
