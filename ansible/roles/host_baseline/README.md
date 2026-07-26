# Host baseline

This role adopts the security controls already present on the reviewed
Ubuntu 26.04 Docker Swarm manager. It is deliberately separate from the
`platform` role: first reconcile Docker, Swarm, UFW edge rules, and the
`DOCKER-USER` service; then run `host-baseline.yml`.

## Safety contract

The role keeps the existing `admin` identity, SSH port 22, `sshusers` group,
authorized keys, account password, sudo policy, and Docker-group membership.
It never creates `ops`, replaces `authorized_keys`, locks the password, grants
passwordless sudo, changes the SSH port, resets UFW, or opens port 25565.

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
- A late sysctl file enforces conservative kernel settings while keeping
  `net.ipv4.ip_forward=1`. Reverse-path filtering uses loose mode (`2`) so
  asymmetric container and overlay paths are not broken.
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

Run it a second time and require `changed=0`.

Relevant primary documentation:

- [OpenSSH server configuration](https://man.openbsd.org/sshd_config)
- [Ubuntu 26.04 OpenSSH changes](https://documentation.ubuntu.com/release-notes/26.04/summary-for-lts-users/#openssh)
- [Ubuntu OpenSSH crypto configuration](https://documentation.ubuntu.com/server/explanation/crypto/openssh-crypto-configuration/)
- [Ubuntu automatic updates](https://documentation.ubuntu.com/server/how-to/software/automatic-updates/)
- [systemd journal configuration](https://www.freedesktop.org/software/systemd/man/latest/journald.conf.html)
- [Docker packet filtering and UFW](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
