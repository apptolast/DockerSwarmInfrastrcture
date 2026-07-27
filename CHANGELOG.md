# Changelog

Los cambios relevantes de la plataforma se documentan aquí. El formato sigue
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y las versiones
siguen [Semantic Versioning](https://semver.org/lang/es/).

## [Unreleased]

### Added

- Roots Terraform aislados para bootstrap R2, diez A de Cloudflare y perímetro
  Netcup, con imports/adopción, identidades de backend, locking proof,
  migración y tests offline.
- Wrappers Terraform ligados a commit/state/backend, planes firmados,
  snapshots cifrados y evidencia de locking.
- Bootstrap fresco de Ubuntu 26.04 para `admin`, claves SSH externas, política
  staged/final y rollback automático de acceso.
- Gestión reproducible de paquetes, repositorios, CrowdSec Hub, Fail2ban,
  PSAD, Chrony, rsyslog, UFW, SSH y firewall Docker.
- Mutex host-global compartido por bootstrap y todos los playbooks Ansible,
  markers fail-closed, recuperación con evidencia y supervisor de procesos.
- Catálogo cerrado de servicios y denylist, stacks de workloads, restore
  markers, secrets por identidad, preflight de imágenes y capacidad.
- Kropia, Minecraft Stats, Minecraft, n8n, OpenClaw limpio, Passbolt, webs de
  Alberto/Pablo y Shlink fijados por digest.
- Traefik file-provider sin socket Docker, rutas explícitas y redes aisladas
  por workload.
- Stack interno de Prometheus, Alertmanager, Loki, Grafana, Alloy, exporters,
  blackbox y acceso mediante túneles.
- Backup restic de aplicaciones/ACME/observabilidad/Minecraft, copia fría de
  Raft, retención, métricas y ensayos de restore.
- CI, validadores reales de OpenSSH/Traefik, tests adversariales, lint,
  escaneo de secretos y documentación de reconstrucción/cutover.

### Changed

- Este repositorio pasa a ser el único propietario de toda la IaC, incluidos
  stacks, rutas, migración, observabilidad y backup.
- El daemon Docker declara `no-new-privileges`, `userland-proxy: false` y
  logging `local` acotado.
- La exposición coherente incluye `80/443` y el puerto Minecraft, pero
  `25565/TCP` permanece cerrado por gate mientras `online-mode=false`.
- DNS distingue diez A gestionados: nueve adopciones existentes, ocho cambios
  HTTP en el cutover y Minecraft conservado en legacy.
- Las herramientas de seguridad heredadas se preservan e inventarían; no se
  purgan ni se reescribe PAM de forma destructiva.
- Backup queda codificado pero fail-closed hasta R2, restic, autolock y escrow
  externo probados.
- El catálogo de servicios excluye explícitamente `uptime-kuma` (alias
  `kuma`, hostname legacy `kuma.apptolast.com`), confirmado por el
  propietario el 2026-07-27.

### Security

- Los usuarios humanos dejan de pertenecer al grupo root-equivalent `docker`.
- Los despliegues rechazan worktrees sucios, holders perdidos, descendientes
  huérfanos, markers alterados y estado externo no probado.
- La configuración SSH se valida con el parser real y solo deshabilita root
  tras reconexión independiente y rollback armado.
- Tokens, claves, passwords, states y backups permanecen fuera de Git.

### Fixed

- Eliminadas carreras entre bootstrap y despliegues Ansible mediante un único
  inode de flock para ambos scopes.
- El modo Ansible local eleva supervisor y comando juntos, permitiendo terminar
  también descendientes root.
- Los checks post-state incompatibles con `--check` quedan correctamente
  condicionados.
- El despliegue edge usa `prune: true` y verifica un inventario exacto sin
  borrar Configs históricos referenciados.
- El auto-desbloqueo del Swarm comparaba contra un estado `docker info`
  imposible (`locked true`) que nunca ocurre en la realidad
  (`ControlAvailable` es `false` mientras el manager está bloqueado); el
  desbloqueo automático era inalcanzable.
- El firewall Docker degrada de nuevo a solo IPv4 si falta la cadena
  `DOCKER-USER` de `ip6tables`, en vez de abortar `ExecStartPost` y tumbar
  `docker.service`.
- `terraform-safety.py` abre los ficheros de firma/lock con `O_NOFOLLOW` y
  verifica el descriptor ya abierto, cerrando una ventana TOCTOU de symlink
  entre la comprobación y la lectura.
- `site.yml` ejecuta `capacity_preflight` antes de `host_security`, igual que
  el resto de playbooks, para que el gate de capacidad corra antes de
  cualquier mutación real del host.
- El helper de lock host-global clasifica el estado del Swarm con
  `/usr/bin/docker` explícito en vez de resolver `docker` por `PATH`.
- El timer de rollback SSH puede limpiar su marker de confirmación bajo
  `ProtectSystem=strict` y detiene (no solo deshabilita) el timer/path ya
  armados.
- `restore_databases.sh` ya no adopta una base de datos no vacía sin marker
  propio solo porque exista el marker de fase; exige evidencia verificada.
- `terraform-safety.py` reconoce ahora, de forma estrecha y verificada
  contra un `terraform plan -json` real, el warning permanente e
  inevitable `Resource Destruction Considerations` de
  `cloudflare_r2_managed_domain` — solo para `plan` en `cloudflare/state-bootstrap`,
  nunca para `apply` ni para ningún otro root o diagnóstico.
- El `become` de Ansible fallaba con `Timeout waiting for privilege escalation
  prompt` en Ubuntu 26.04: `sudo-rs` reescribe el indicador de `-p` como
  `[sudo: <prompt>] Password:` y Ansible solo reconoce una línea que empiece
  por su indicador exacto. `ansible/ansible.cfg` fija ahora
  `become_exe = /usr/bin/sudo.ws` (sudo clásico setuid, alternativa de
  prioridad 40) y `config/host-security.yml` bloquea `sudo=1.9.17p2-1ubuntu3`
  para que la reconstrucción disponga del binario. No se altera la alternativa
  del sistema, no se concede `NOPASSWD` y no se almacena ninguna contraseña.
- El assert del conjunto exacto de acquisitions de CrowdSec no podía pasar
  bajo `--check`: `ansible.builtin.find` sí se ejecuta en check mode y lee el
  estado vivo, mientras la plantilla y los borrados solo se simulan, de modo
  que comparaba el estado real contra el deseado. Queda condicionado con
  `when: not ansible_check_mode`, igual que el par equivalente de grupos del
  administrador. El apply sigue enforzándolo sin cambios.
- El apply de `host_security` fallaba de forma reproducible en `Enforce
  versioned-only CrowdSec Hub promotion`: al enmascarar
  `crowdsec-hubupdate.service` desaparece la unidad que dispara su timer y
  systemd deja `crowdsec-hubupdate.timer` en `ActiveState=failed`
  (`Result=resources`) aunque ya esté enmascarado. La propia tarea de
  enmascarado creaba el estado que el gate siguiente rechazaba. Ahora se
  limpia ese residuo con `systemctl reset-failed` antes de leer el estado;
  el assert de promoción no se relaja.
- `docs/KNOWN_ISSUES.md` describía el prefijo de `sudo-rs` con un espacio
  final dentro de un code span, que markdownlint MD038 rechaza y hacía fallar
  `scripts/lint.sh`. El texto se reformula sin alterar el hecho descrito.

Nada de esta sección afirma que los stacks, DNS, Terraform remoto o backup ya
estén aplicados en producción.

## [0.1.0] - Pendiente de despliegue

- Primera versión declarativa de la plataforma; no se etiqueta ni se considera
  desplegada hasta completar las compuertas externas y verificar producción.

[Unreleased]: https://github.com/apptolast/DockerSwarmInfrastrcture/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/apptolast/DockerSwarmInfrastrcture/releases/tag/v0.1.0
