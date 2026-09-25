# Host baseline

This role adopts the security controls already present on the reviewed
Ubuntu 26.04 Docker Swarm manager. It is deliberately separate from the
`platform` role: first reconcile Docker, Swarm, UFW edge rules, and the
`DOCKER-USER` service; then run `host-baseline.yml`.

## Safety contract

The role itself keeps the existing `admin` identity, SSH port 22, `sshusers`
group, authorized keys, account password, sudo policy, and Docker-group
membership. It never creates `ops`, replaces `authorized_keys`, locks the
password, grants passwordless sudo, changes the SSH port, resets UFW, or
opens port 25565.

`host-baseline.yml` (the playbook) also runs the separate `host_security`
role first, which intentionally reconciles `admin`'s supplementary groups to
exactly `sudo` and `sshusers` — removing Docker-group membership if present,
per the final contract in the top-level README ("El grupo `docker` equivale
a root... el contrato final elimina a todos los usuarios humanos, incluido
`admin`, de ese grupo"). That group change comes from `host_security`, not
from this role.

Before writing the SSH drop-in it proves that:

- Ansible is connected as `admin` with no configured SSH password.
- `admin` already belongs to `sshusers` and `sudo`.
- the existing 0600 `authorized_keys` file is owned by `admin` and contains at
  least one cryptographically parseable key;
- the complete existing OpenSSH configuration is valid and already exposes
  port 22 with public-key support.

The managed drop-in disables password and keyboard-interactive
authentication, requires `publickey`, and preserves the existing forwarding
ban. The complete configuration is checked with `sshd -t` before reload. A
remote run resets the Ansible control connection and proves it can
reauthenticate. Keep the Netcup rescue console available during the first
production run; no automated check can replace an out-of-band recovery path.

The two existing 2048-bit RSA keys are not silently removed and
`RequiredRSASize` is not increased. Retire those keys only after their owners
and last required clients have been reviewed.

### Post-quantum key exchange

Both the installed client and server are OpenSSH 10.2p1. Each implements
`mlkem768x25519-sha256`, `sntrup761x25519-sha512`, and the OpenSSH-namespaced
Sntrup variant. The inherited main `sshd_config` replaces the complete KEX
list with:

```text
curve25519-sha256@libssh.org,ecdh-sha2-nistp521,
ecdh-sha2-nistp384,ecdh-sha2-nistp256,
diffie-hellman-group-exchange-sha256
```

That replacement excludes all mutually supported hybrid post-quantum
algorithms and directly causes the OpenSSH 10.2 client warning. The managed
early drop-in replaces it with the three hybrid algorithms first, followed by
the existing reviewed classical algorithms plus the standard Curve25519 name.
No SHA-1 KEX is enabled. The client chooses the first mutual algorithm; the
OpenSSH 10.2 client default already puts ML-KEM first.

The role queries implemented algorithms with `ssh -Q KexAlgorithms`, renders
the candidate in memory, parses it with `sshd -T -f /dev/stdin`, and requires
the exact KEX list again from the post-reload `sshd -T` output. No private or
public key material is read for this check.

## Managed controls

- UFW is read-only in this role. IPv4 and IPv6 must already use DROP for
  INPUT, FORWARD, and OUTPUT. The existing host-egress allowlist and edge
  ports are asserted; `ufw reset` and `default allow outgoing` are forbidden.
- Unattended upgrades are limited to Ubuntu security and ESM security
  origins. Every legacy override is copied once to the root-only
  `/var/backups/dockerswarm` directory before removal, inherited origin lists
  are cleared, and automatic reboot is disabled because this is a
  single-manager Swarm.
- Chrony must already be installed, enabled, active, and synchronized.
  `systemd-timesyncd` must remain absent.
- Journald is persistent, compressed, bounded to 1 GiB and 14 days, while
  retaining 5 GiB free. `Seal=yes` is intentionally omitted: forward-secure
  sealing without provisioned keys and an off-host verification-key workflow
  would be a false control.
- A late sysctl file persists conservative kernel settings while keeping
  `net.ipv4.ip_forward=1`. Reverse-path filtering uses loose mode (`2`) so
  asymmetric container and overlay paths are not broken. At runtime the role
  reads every managed key, writes with `sysctl -w` only the keys whose live
  value differs, and then asserts all of them again. It never runs
  `sysctl --system`: that would also reload inherited files this contract
  does not manage, and on the production host `99-hardening.conf` would set
  `net.ipv6.conf.all.forwarding=0`.
- AppArmor, Fail2ban, PSAD, CrowdSec, its firewall bouncer, and rsyslog are
  validated as existing active controls. Their credential-bearing
  configuration is not copied into Git.
- CrowdSec is registered on `DOCKER-USER`. A systemd post-start helper
  guarantees that `CROWDSEC_CHAIN` is first and `DOCKERSWARM-INGRESS` second,
  so the public 80/443 allowlist cannot bypass CrowdSec decisions after a
  Docker restart.

Host OUTPUT policy does not govern container egress because Docker forwards
published and bridged traffic before UFW's host INPUT/OUTPUT chains. Container
egress requires a separate reviewed `DOCKER-USER` policy and is not
misrepresented as covered here.

### Kernel core dumps and Apport

The contract keeps `fs.suid_dumpable=0`, the kernel's traditional mode: a
process that has changed privilege levels, such as a setuid binary, `sudo` or
`sshd`, is never dumped, so memory that can hold keys and password hashes
never reaches a core file. Ubuntu's `apport.service`, from the package
`apport-core-dump-handler`, sets `fs.suid_dumpable=2` every time it starts,
after `systemd-sysctl`, without reading `/etc/default/apport`
(`start_apport()` in `/usr/share/apport/apport`, apport 2.34.1). The
persisted `0` was lost on every boot, and every later apply failed its
runtime assertion.

The role stops and disables only that unit. Stopping it runs
`apport --stop`, which itself restores `fs.suid_dumpable=0` and
`kernel.core_pattern=core`, the package default from
`/usr/lib/sysctl.d/10-coredump-debian.conf`. Package upgrades do not start
it again, because `deb-systemd-invoke` skips a disabled unit that is not
running. The unit is not masked, so an operator can still start it by hand
to debug a crash; the next apply then converges the key back to `0`. A
missing unit is accepted, a masked one is left as it is, and any other
`LoadState` stops the apply.

`/etc/default/apport` is deliberately left unchanged. It does not control
this unit, and `enabled=0` would only also switch off Apport's Python and
package-failure reports, which never touch kernel settings. On 2026-09-25
`whoopsie` was not installed and `/var/crash` was empty, so no crash report
from this host was being collected.

With Apport stopped, `core_pattern=core` would let any crashing process that
has not changed privilege write a full core file into its working directory.
`docker.service` and `containerd.service` run with `LimitCORE=infinity`, so
that includes every container, and a core file holds whatever secrets the
process had in memory. The contract therefore also manages
`kernel.core_pattern=|/bin/false`, which discards every dump. A piped
handler always runs in the initial mount namespace and `RLIMIT_CORE` does not
apply to it (core(5)), so a container cannot redirect or keep its dump. The
`99-z` file sorts after `10-coredump-debian.conf`, so the managed value also
wins at boot. To debug a crash, set another pattern at runtime; the next
apply converges it back.

### No firewall restart on a converged host

`host-baseline.yml` runs the `host_security` role first. That role owns the
UFW default policies and the PSAD logging block in `/etc/ufw/before*.rules`.
With UFW active, `ufw default` always stops and starts the firewall, even
when the policy does not change (`set_default_policy()` in `ufw/frontend.py`),
and `ufw reload` does the same. Each stop sets the INPUT, OUTPUT and FORWARD
policies to ACCEPT and flushes UFW's chains (`ufw_stop` in
`/lib/ufw/ufw-init-functions`, with `MANAGE_BUILTINS=no`). Each start appends
the PSAD `LOG` rules to INPUT and FORWARD again. Before this was fixed, every
apply briefly exposed the Swarm ports 2377, 7946 and 4789 and an unthrottled
port 22 to the Internet.

`host_security` now:

- reads `DEFAULT_INPUT_POLICY`, `DEFAULT_OUTPUT_POLICY` and
  `DEFAULT_FORWARD_POLICY` from `/etc/default/ufw` with `slurp`, and runs
  `ufw default deny` only for a direction that is not already `DROP`. If any
  of the three keys is missing, repeated or not `ACCEPT`, `DROP` or `REJECT`,
  the apply stops before it touches UFW;
- removes legacy PSAD lines only outside the managed
  `# BEGIN DOCKERSWARM PSAD` / `# END DOCKERSWARM PSAD` block, so the block is
  no longer rewritten and `Reload UFW` is no longer notified on every run.

On a host that is already in the reviewed state, neither task changes
anything and UFW is never stopped. When a default policy or `before*.rules`
really has to change, UFW is still stopped and started, because `ufw` applies
those changes no other way.

## First production run

From the repository root, validate before any privileged execution:

```bash
./scripts/validate-iac.sh
./scripts/deploy-ansible.sh \
  --playbook host-baseline \
  --check \
  --ask-become-pass
```

Apply `platform` first through the same wrapper. Review the check-mode output,
keep an authenticated second SSH session and the Netcup console open, then run:

```bash
./scripts/deploy-ansible.sh \
  --playbook host-baseline \
  --confirm-production \
  --ask-become-pass
```

Run it a second time. On a converged host only these tasks still report
`changed`, because their tools always say so:

- `Enable bounded UFW logging`: `ufw logging` always answers "Logging
  enabled" and only rebuilds UFW's logging chains; nothing is opened;
- the CrowdSec firewall bouncer `-t` checks and restarts in `host_security`
  and `crowdsec-docker.yml`, which leave a gap of a few seconds without
  CrowdSec filtering.

Any other changed task on the second run is drift to investigate.

Relevant primary documentation:

- [OpenSSH server configuration](https://man.openbsd.org/sshd_config)
- [Ubuntu 26.04 OpenSSH changes](https://documentation.ubuntu.com/release-notes/26.04/summary-for-lts-users/#openssh)
- [Ubuntu OpenSSH crypto configuration](https://documentation.ubuntu.com/server/explanation/crypto/openssh-crypto-configuration/)
- [Ubuntu automatic updates](https://documentation.ubuntu.com/server/how-to/software/automatic-updates/)
- [systemd journal configuration](https://www.freedesktop.org/software/systemd/man/latest/journald.conf.html)
- [Linux `fs` sysctls, including `suid_dumpable`](https://docs.kernel.org/admin-guide/sysctl/fs.html)
- [Docker packet filtering and UFW](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
