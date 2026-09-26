# Host security

The `host_security` role pins the host's security packages, the CrowdSec
repository key and Hub content, UFW, Fail2ban, PSAD, Chrony, `/proc` and the
lockout-safe SSH bootstrap. Its inputs live in `config/host-security.yml`,
which `scripts/validate-host-security.py` checks offline. The playbooks
`host-baseline`, `platform`, `site` and the fresh-host bootstrap run it.

This README documents the CrowdSec bans for repeated HTTP 401 answers on the
Traefik routers behind `basicAuth`. The other controls are described in the
role's task comments, in
[`host_baseline/README.md`](../host_baseline/README.md) and in
[`docs/OPERATIONS.md`](../../../docs/OPERATIONS.md).

## Why

Until this change CrowdSec read only syslog and `auth.log`. A password
guesser against `logs-satisfactory.apptolast.com` or `ax.apptolast.com` met
Traefik's rate limits but was never banned. Traefik cannot count only failed
logins, so the ban belongs to CrowdSec.

## What the role installs

<!-- markdownlint-disable MD013 -->

| Piece | Where | What it does |
| --- | --- | --- |
| File acquisition | `/etc/crowdsec/acquis.d/02-dockerswarm-traefik.yaml` | Tails `/var/log/dockerswarm/edge/access.log`, Traefik's JSON access log, labelled `type: traefik` |
| Watched directories | `/var/log/dockerswarm`, `/var/log/dockerswarm/edge` | `root:root 0755`, created before CrowdSec loads the source |
| Hub parser | `crowdsecurity/traefik-logs` 1.5 | Parses Traefik's JSON access log into `http_status`, `traefik_router_name` and `source_ip` |
| Local scenario | `/etc/crowdsec/scenarios/dockerswarm-traefik-basicauth-bf.yaml` | `apptolast/traefik-basicauth-bf`: a leaky bucket of 401s per source IP on `ax@file` and `satisfactory-logs@file` |
| Profile | `/etc/crowdsec/profiles.yaml.local` | A 30 minute ban for that scenario only, evaluated before the packaged 4 h profile |
| Allowlist | CrowdSec database, list `apptolast-trusted` | IPs that are never banned, taken from a host-only file |

<!-- markdownlint-enable MD013 -->

The access log file itself, its bind mount into the Traefik task and its
rotation belong to the `edge` role
([`docs/EDGE.md`](../../../docs/EDGE.md), «Log de acceso en fichero»).

### Acquisition: a file, never the Docker API

CrowdSec runs every datasource of the host in one acquisition tomb, and in
1.7.8 one datasource that returns an error stops all of them: "if one of the
acquisitions returns an error, we kill the others"
(`pkg/acquisition/acquisition.go:652-656` at tag v1.7.8). Only `journalctl`
and `syslog` are restartable streams there (`acquisition.go:531-608`).

The first draft of this change read the Traefik task through CrowdSec's
Docker datasource. That datasource retries its Docker events subscription
with an exponential backoff (`pkg/acquisition/modules/docker/run.go:28-40`,
`:410`) whose total time is capped by the library default of 15 minutes
(`cenkalti/backoff` v5.0.3, `retry.go:10`, `:73`), and no option changes it.
After a Docker outage of about 15 minutes the source returns the error
(`run.go:462-469`), every acquisition stops, `auth.log` and syslog included,
and CrowdSec logs "Acquisition is finished" (`cmd/crowdsec/crowdsec.go:242`)
while systemd still reports it active. SSH brute-force detection would then
depend on Docker, until someone restarts CrowdSec by hand. CrowdSec also asks
Docker for `info` when it configures that source
(`pkg/acquisition/modules/docker/config.go:173-175`), so a Docker that does
not answer would have stopped CrowdSec from starting and failed every
playbook that runs this role.

So Traefik now writes its access log to a host file and CrowdSec tails it
with the `file` datasource, the one that already reads `auth.log`. That
datasource cannot stop the others in normal operation:

- a file that does not exist when CrowdSec starts is only a warning
  (`pkg/acquisition/modules/file/config.go:116-118`), and with
  `force_inotify: true` the existing directory is watched, so the file is
  picked up when the edge role creates it (`config.go:96-109`,
  `run.go:146-157`; [docs](https://docs.crowdsec.net/docs/v1.7/log_processor/data_sources/file));
- a tailer that dies is dropped from the map and the goroutine returns
  `nil`, not an error (`run.go:309-325`);
- the only error it returns is a line error (`run.go:332-335`), which the
  `nxadm/tail` v1.4.11 library it pins sets only when a rate limiter is
  configured (`tail.go:311-317`), and CrowdSec configures none
  (`run.go:271-277`);
- a truncated file is reopened from its start (`tail.go:403-410`), which is
  how the edge role's `copytruncate` rotation looks to it.

`host_security` therefore needs neither Docker nor its socket. The role
creates `/var/log/dockerswarm` and `/var/log/dockerswarm/edge`
(`root:root 0755`, the same identity the edge role requires, a link refused)
before it installs the source, so the order of the `host-baseline` and
`edge` applies does not matter. The source reads exactly one path, no glob:
never a rotated sibling nor another container's log. Traefik writes only its
access log there; its application log stays on stdout, so the parser no
longer sees the application lines that the first draft had to live with.

### Parser

`crowdsecurity/traefik-logs` is pinned in
`host_security_crowdsec_standalone_hub_lock` by version and content digest,
like every other Hub item, and installed by name. The collection
`crowdsecurity/traefik` is not installed: it would also enable
`base-http-scenarios` and `http-cve` on every site. No Hub scenario fits a
`basicAuth` 401: `crowdsecurity/http-generic-bf` needs `sub_type: auth_fail`,
which this parser never sets, and `LePresidente/http-generic-401-bf` counts
only `POST`.

The parser takes the source IP from `ClientHost`. The entrypoints trust no
forwarded headers (`stacks/edge/static.yml.j2` sets no `forwardedHeaders`)
and the ports are published in host mode, so Traefik discards a client's
`X-Forwarded-For` and `ClientHost` is the TCP peer: a client cannot get
another address banned.

### Scenario

```yaml
filter: evt.Meta.log_type == 'http_access-log'
  && evt.Meta.http_status == '401'
  && evt.Meta.traefik_router_name in ['ax@file', 'satisfactory-logs@file']
groupby: evt.Meta.source_ip
capacity: 10
leakspeed: 1m
blackhole: 5m
```

A leaky bucket overflows when it holds more than `capacity` events and loses
one event every `leakspeed`
([format](https://docs.crowdsec.net/docs/v1.7/log_processor/scenarios/format/)).
Every 401 counts, with or without credentials: Traefik answers both the
same way, and the access log drops `ClientUsername`.

- **The owner:** a new browser session costs one 401 (the challenge). Ten
  failures in a burst never ban, and a full bucket drains in ten minutes. In
  the 3 h of `ax.apptolast.com` log read on 2026-09-26 (only hashed
  addresses were printed), the allowlisted owner had 2 401s for 70
  authenticated requests, and another authenticated client 1.
- **A guesser at the real rate limits of `ax`:** Traefik lets one IP send
  about 30 requests every 2-3 s and the whole host about 10 every 1-2 s
  (`ax-rl-ip` and `ax-rl-host` in `stacks/edge/dynamic.yml.j2`), so a burst
  reaches its eleventh 401 in 1-2 s (the bucket is
  `rate.NewLimiter(rate.Every(leakspeed), capacity)` and overflows when
  `Allow()` fails, `pkg/leakybucket/bucket.go:82` and `:282`). CrowdSec
  reads the line at once, but the firewall bouncer fetches new decisions only
  every 10 s (`update_frequency: 10s` on the host), so the guesser keeps its
  5 to 10 answers a second until the drop lands: about 60 to 110 guesses per
  30 minute ban, some 3 000 to 5 000 a day per IP, instead of about 430 000
  a day at 5 a second without CrowdSec
  ([`docs/EDGE.md`](../../../docs/EDGE.md), «Ruta de AX»).
  `logs-satisfactory` has no rate limit: there the ban is the only brake, and
  the burst before the drop is bounded only by Traefik's bcrypt cost. In the
  same 3 h one scanner sent 31 401s to `ax` in 26 s: it would have been
  banned at the eleventh.
- **Below one guess a minute** the bucket never overflows (about 1 440 a
  day). That residual risk is covered by the password strength and the
  per-IP rate limits, not by CrowdSec.
- `blackhole: 5m` silences repeated overflows of the same IP, for example
  an allowlisted owner who keeps mistyping.

Each IPv6 address has its own bucket, so rotating addresses inside one
`/64` is not grouped. Today 80 and 443 listen on IPv4 only.

### Profile

CrowdSec prepends `profiles.yaml.local` to `profiles.yaml` and reads them as
one multi-document stream
([docs](https://docs.crowdsec.net/docs/next/local_api/profiles/intro);
`pkg/csconfig/profiles.go`, `PrependedPatchContent`). The role never edits
the packaged `profiles.yaml`, which is a dpkg conffile. Its profile matches
only `Alert.GetScenario() == "apptolast/traefik-basicauth-bf"` and stops
with `on_success: break`; every other alert still gets the packaged 4 h
ban. The contract only accepts 15 to 30 minutes. A ban drops the IP on
`INPUT` and `DOCKER-USER`, so it also cuts SSH: 30 minutes lets a mistyping
owner back in without the Netcup console while an attacker loses most of
its rate.

### Allowlist

CrowdSec 1.6.8 and later keep allowlists in the LAPI database
([docs](https://docs.crowdsec.net/docs/v1.7/local_api/centralized_allowlists/)).
An alert whose source IP is allowlisted is dropped before any decision, and
`cscli allowlists add` also expires every active decision the new entry
covers. The role converges one list, `apptolast-trusted` (the name of the
list created by hand on 2026-09-20), to exactly the entries of a host-only
file, `/etc/dockerswarm/crowdsec/trusted-ips`. This repository is public, so
the addresses never appear in it.

`scripts/validate-crowdsec-allowlist.py` reads the file without following a
link, in a `root:root 0700` directory, and requires a single-link
`root:root 0600` regular file of at most 4 096 bytes. Each line holds one
public IPv4 or IPv6 address or network, no wider than `/24` or `/48`; blank
lines and `#` comments are ignored; at most 16 entries. Its error messages
never contain an address, and every task that sees one has `no_log`.

The read-only gates live in `tasks/crowdsec_allowlist_gates.yml`, which the
role runs twice:

1. **Preflight**, right after the CrowdSec package checks and before the
   first Hub item, acquisition or profile change, so before anything can
   restart CrowdSec with new content. It always requires a safe source file.
   When the LAPI answers, it also stops the apply if CrowdSec holds another
   allowlist (or one managed from the console), if an entry is malformed, or
   if the file is absent while the list has entries (the role does not guess
   whether to keep them; an empty file removes them all). When the LAPI does
   not answer within 30 s, it skips those checks instead of failing, so a
   stopped or hung CrowdSec can still be repaired by the role.
2. **Strict**, after the services start and just before the writes. It
   retries the LAPI for a minute and runs the same checks again.

`cscli allowlists list` talks to the LAPI through an HTTP client that has no
timeout at all (`pkg/apiclient/auth_jwt.go:247-248`,
`pkg/apiclient/client.go:153-196`), so both LAPI reads of the role run under
`/usr/bin/timeout --kill-after=5 30`: a LAPI that accepts the connection and
never answers costs 30 s per attempt instead of holding the host-global lock
forever. `create`, `add` and `remove` write to the database directly and do
not use the LAPI.

Then the role creates the list if needed, removes stale entries (and desired
ones stored with an expiry), adds the missing ones without expiry, re-reads
the list and requires it to equal the file.

On the production host the LAPI answers, so the first apply without
`trusted-ips` stops at the preflight, before any CrowdSec change. `platform`
and `site` run this role too and stop the same way.

## Check mode and apply

A `--check` run installs nothing, so the inventory gate tolerates exactly
the pinned parser and the local scenario being absent, and nothing else.
Both allowlist passes run in check mode; only the `cscli` writes are
skipped. Before the restart handler loads the new files, `crowdsec -t` tests
them, so a broken configuration stops the apply with the running engine
untouched. That task hides its output like the existing engine test; run
`sudo -- crowdsec -c /etc/crowdsec/config.yaml -t` to read the reason.

## Verification

After the apply, on the host:

```bash
sudo -- cscli metrics show acquisition parsers scenarios
sudo -- cscli allowlists list
```

The acquisition table must show `file:/var/log/dockerswarm/edge/access.log`
with lines read once the edge writes it, next to `auth.log` and syslog; the
parser table `crowdsecurity/traefik-logs` with parsed lines; and the scenario
table `apptolast/traefik-basicauth-bf` once a 401 arrives. A Docker outage no
longer needs any CrowdSec step. The safe tests are in
[`docs/OPERATIONS.md`](../../../docs/OPERATIONS.md), «CrowdSec y los 401 de
Traefik».
