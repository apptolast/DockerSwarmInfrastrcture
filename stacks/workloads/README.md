# Approved workloads

This directory defines the active Docker Swarm stack for the application scope
recorded in `config/services.yml`. It deliberately excludes Traefik, legacy
OpenClaw, Kubernetes workloads and observability.

## Safety gates

The deployment role fails closed unless all of these conditions hold:

1. Every external image and bind path is derived from
   `config/services.yml`, where every remote source remains digest-pinned.
   `config/workload-image-updates.yml` defines the sole runtime override:
   `personal-website-alberto/app` may track its exact `latest` tag. During a
   real image preflight, Ansible reads that tag's registry descriptor and
   requires an exact match with the reviewed `approved_runtime_reference`.
   It then pulls that exact `latest@sha256:...` reference and proves that the
   local image exposes the same repository digest before rendering production.
   Each deployment therefore uses an immutable, versioned content identity.
   Keeping the override separate also preserves the service-catalog hash bound
   to the restore marker. The private historical n8n runner digest is retained
   as audited provenance, while its runtime image is built locally from the
   repository context described below.
2. Every external, versioned Docker Secret in `secrets.yml` exists with the
   installer contract labels.
3. All eight dedicated external `apptolast-edge-<backend>` overlays exist as
   encrypted, non-attachable Swarm networks.
4. The target node has `platform.workloads=true`.
5. The migration workflow has atomically emitted
   `/srv/dockerswarm/services/restore-state/workloads-ready-v2.json`.
6. The marker SHA-256 values equal the current raw bytes of
   `config/services.yml`, `runtime-manifest.json`,
   `recovery/SHA256SUMS` and the private Secret identity HMAC key.
7. No running or stopped `apptolast-restore` container exists, and only the
   exact Swarm database tasks can mount the three PostgreSQL paths.

The marker verifies the three restored PostgreSQL databases, every restored
dataset and a newly initialized OpenClaw home. Legacy OpenClaw state is
explicitly rejected.

## Repository-native n8n runner

`images/n8n-runners` is an allowlisted four-file build context. Both `FROM`
images are digest-pinned, dependencies come from `npm ci` and exact lockfile
versions, and the runtime tag is
`apptolast/n8n-runners:src-<sha256-of-context>`.

Before stack deployment Ansible builds the image locally on the sole eligible
`platform.workloads=true` node when absent. It verifies architecture, runtime
user, source labels, image ID and the installed versions/loadability of every
external module. After convergence it also proves that the running task uses
that exact preflight image ID. The stack uses `resolve_image: never` and does
not transmit registry credentials. Adding workload nodes requires an explicit
image-distribution design first; the role intentionally blocks an ambiguous
multi-node placement.

## Render and validate

```bash
scripts/validate-workloads.sh
```

This renders the Jinja template without changing Swarm, asks Docker to parse
the result, validates images, networks, ports, mounts, configs and secret
consumers, then runs the migration/workload cross-contract tests.

## Install secrets

The migration materializer writes mode `0600` source files beneath
`/srv/dockerswarm/services/secrets/files` and lists their exact names in
`runtime-manifest.json`. Convert them to immutable Swarm Secrets only after the
materializer finishes:

```bash
.venv/bin/python scripts/install-workload-secrets.py
```

SwarmKit rejects zero-byte secrets. An absent optional source value is
therefore represented inside Swarm by the fixed, non-sensitive sentinel
`__APPTOLAST_OPTIONAL_UNSET_V1__`; fixed-list entrypoint wrappers translate
that sentinel back to an unset environment variable.

Secret values are never rendered into the stack or passed as command-line
arguments. To rotate a secret, increment `workloads_secret_version`, assign a
new external name in `secrets.yml`, install it and reconcile the stack. Docker
Secrets are immutable, so an existing version is verified but never
overwritten. Each Secret also carries an HMAC-SHA-256 source identity. Its
random key is the private mode-`0600`
`workloads_secret_identity_hmac_key` file retained with the source files and
covered by encrypted backups. This lets every deployment reject a stale or
wrong immutable Secret without publishing a raw hash that could be used to
guess low-entropy configuration values.

## Reconcile

After restore finalization and secret installation:

```bash
scripts/deploy-ansible.sh --playbook workloads
```

The tracked tag does not monitor Docker Hub by itself. Docker Swarm resolves a
tag only when a service image update is requested and otherwise keeps running
the previously selected image. A separate, explicitly reviewed trigger is
therefore required before this policy could provide automatic deployment, but
the repository's current governance explicitly forbids such a production
trigger and keeps every apply manual.
Render-only runs keep the declared `:latest` reference. `--check` queries the
registry but neither pulls nor mutates; it fails unless the result matches the
versioned runtime approval. A real apply reuses that exact digest before its
first possible stack mutation.

Minecraft TCP port `25565` is absent from the rendered stack while
`platform_minecraft_public_enabled=false`. Enabling it is a single reviewed
contract transition coupled to the DNS and host/perimeter gates. Each HTTP
backend joins exactly one dedicated encrypted external edge overlay; Traefik
is its only cross-stack peer.

Minecraft never joins the shared edge network; it uses the encrypted,
single-consumer `minecraft-egress` overlay for required outbound downloads.
Blackbox reaches Minecraft through the separate encrypted internal network
`apptolast-minecraft-monitoring`, shared only by the Minecraft task and the
observability probe.
Selenium is reachable only from n8n over the dedicated encrypted internal
`workloads_n8n-browser` overlay and uses the separate encrypted,
single-consumer `selenium-egress` overlay for browser traffic. It never joins
the PostgreSQL backend. The external task runner uses only
the dedicated internal `workloads_n8n-runner-broker` shared with n8n, while
Redis remains on `n8n-coordination`; untrusted Code-node execution therefore
cannot reach PostgreSQL, Selenium or Redis. Contract tests reject
cross-backend consumers and any additional egress consumer. Prometheus reaches
the n8n metrics endpoint through the separate encrypted internal
`apptolast-n8n-monitoring` network; it never joins the database, browser or
runner-broker network.

The n8n service sets `N8N_BLOCK_ENV_ACCESS_IN_NODE=true` and
`N8N_UNVERIFIED_PACKAGES_ENABLED=false`. Before status or publication, the
cutover helper reads the restored workflow definitions directly from
PostgreSQL and rejects environment access, non-official node package types,
and legacy or unverifiable internal endpoints. It never rewrites a workflow
or invents a compatibility alias. Restored credential endpoints are audited
inside the n8n container using a private ephemeral decrypted export: only safe
aggregate counters leave the process, every endpoint must resolve and accept
its declared TCP connection, and sensitive failure output is redacted.

The Python task runner keeps the n8n `2.31.5` upstream isolation switches
exactly (`-I -B -X disable_remote_debug`). The local-image preflight executes
those switches inside the exact built image with networking disabled, a
read-only root filesystem and all capabilities dropped; deployment stops if
the interpreter does not report every isolation control as effective.
The reviewed upstream source is
[`docker/images/runners/n8n-task-runners.json`](https://github.com/n8n-io/n8n/blob/n8n%402.31.5/docker/images/runners/n8n-task-runners.json).

`ONLINE_MODE=false` is retained solely as restored application behavior while
the host/perimeter gate keeps public Minecraft disabled. Do not set
`platform_minecraft_public_enabled=true` until authentication/whitelisting and
the player identity migration have been explicitly approved and tested.

The full `site` playbook reconciles platform and host baseline, preflights all
remote images and the locally built runner, then reconciles edge, workloads
and observability. Backup is deliberately not hidden inside `site`; it remains
the separate fail-closed `backup` target until R2 credentials, the restic
password and Swarm unlock-key escrow are provisioned.

Workload reconciliation finishes only after all 15 services are `1/1` and
their task containers report `healthy`, the three application databases and
both locked pgvector versions pass live queries, all eight HTTPS backends
respond through Traefik when resolved directly to the reviewed server IP, and
Minecraft's service has no published port while its public gate is closed.
Blackbox performs the Minecraft TCP probe only across
`apptolast-minecraft-monitoring`. Full `docker service ps --no-trunc` output
is retained in Ansible failure diagnostics.
