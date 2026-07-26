# Plataforma Docker Swarm de ApptoLast

Fuente de verdad reproducible para el VPS netcup, su Docker Swarm mononodo,
el perímetro externo y el edge Traefik de `apptolast.com`.

## Estado real

A 26 de julio de 2026:

- Docker Engine 29.6.2 y Compose 5.3.1 están instalados.
- El Swarm ya está inicializado en `159.195.156.57`; el único nodo está
  `Ready`, `Active` y es `Leader`.
- El pool inmutable es `10.0.0.0/8`, con subredes `/24`, y el data path usa
  `159.195.156.57:4789`.
- El smoke test efímero de scheduler y logging pasa, pero no existen stacks ni
  servicios persistentes y el nodo todavía no tiene la label `platform.edge`.
- `/etc/docker/daemon.json` aún solo declara `log-driver: local`; la rotación
  explícita, `no-new-privileges` y `userland-proxy: false` están declarados en
  Git, pero pendientes de aplicar en una ventana que reiniciará Docker.
- Todavía no se ha desplegado Traefik ni se ha creado
  `edge.apptolast.com`.
- La credencial mostrada anteriormente no era un token API de Cloudflare
  válido y no se ha guardado ni reutilizado.
- Los registros de aplicaciones siguen bajo el servidor anterior. Este
  repositorio no los cambia antes de migrar y verificar servicios, datos y
  rollback.

El código puede validarse sin credenciales. La validación privilegiada de
firewall/journal/logrotate sigue pendiente de una sesión `sudo` del operador.
La aplicación final permanece
deliberadamente bloqueada hasta disponer de credenciales nuevas y limitadas,
identificadores reales de los proveedores y destinos externos de backup.

## Propiedad

| Capa | Propietario |
| --- | --- |
| Contrato común, host, Docker, Swarm, UFW y `DOCKER-USER` | Este repositorio |
| Traefik, certificados ACME, rutas públicas y red edge | Este repositorio |
| DNS Cloudflare y perímetro netcup | Terraform de este repositorio |
| Aplicaciones, volúmenes y migración de datos | `MigracionNetCup` |
| Secretos y copias cifradas | Gestores externos; nunca Git |

No hace falta crear otro repositorio de infraestructura ahora. La estrategia se
detalla en [`docs/REPOSITORIES.md`](docs/REPOSITORIES.md). Antes del corte se
retirará de
`MigracionNetCup` la infraestructura duplicada para evitar que dos
automatizaciones gestionen Docker, firewall, sysctl, Traefik o recursos
externos a la vez.

## Organización

- `config/platform.yml`: contrato compartido entre todas las capas.
- `infra/terraform/`: bootstrap R2 y roots aislados para Cloudflare y Netcup.
- `ansible/roles/host_baseline`: adopción segura del baseline del host.
- `ansible/roles/platform`: Docker, Swarm, UFW y política `DOCKER-USER`.
- `ansible/roles/edge`: red overlay cifrada y stack Traefik.
- `ansible/roles/deployment_metadata`: release, commit y contrato aplicados.
- `stacks/edge`: configuración y stack renderizados mediante Ansible.
- `scripts/`: bootstrap, tests, secretos, validación y recuperación.
- `docs/`: arquitectura, migración, estado remoto y runbooks.

Traefik usa el provider de ficheros y no monta el socket Docker. Las rutas se
versionan centralmente; el proceso edge no obtiene acceso root indirecto al
host.

## Herramientas bloqueadas

- Terraform 1.15.8.
- ansible-core 2.21.2 y ansible-lint 26.6.0.
- `community.docker` 5.2.1.
- Traefik 3.7.9 fijado por digest.
- Provider Cloudflare 5.22.0.
- Provider comunitario netcup 1.0.0, desactivado por defecto.

El bootstrap descarga Terraform desde HashiCorp y valida su SHA-256. Python,
colecciones Ansible, providers, acciones de CI e imágenes auxiliares están
versionados o fijados. El binario Terraform instalado se vuelve a contrastar
en cada bootstrap, no solo el archivo descargado.

```bash
./scripts/bootstrap-tooling.sh
./scripts/validate-iac.sh
./scripts/lint.sh
```

La validación ejecuta tests Terraform con providers simulados, lint de
producción Ansible, render del stack, análisis ShellCheck, contrato entre
capas y un Traefik real sin privilegios, con filesystem de solo lectura.

## Orden de implantación

1. Aplicar el root `cloudflare/state-bootstrap` desde state local cifrado para
   declarar dos buckets R2 privados.
2. Crear credenciales independientes limitadas a cada bucket, inicializar los
   backends, probar locking y tomar una primera copia cifrada de cada state.
3. Importar antes de gestionar cualquier recurso preexistente.
4. Aplicar `platform.yml` dos veces y exigir idempotencia.
5. Aplicar `host-baseline.yml` con consola netcup y una segunda sesión SSH
   abiertas; repetir y exigir idempotencia.
6. Crear un token Cloudflare exclusivo para ACME y verificarlo con
   `install-cloudflare-secret.sh`.
7. Probar Traefik contra Let’s Encrypt staging usando
   `acme-staging.json`.
8. Desplegar de nuevo contra producción usando `acme.json`.
9. Aplicar únicamente el registro DNS `edge.apptolast.com`.
10. Ejecutar las validaciones de host, firewall, TLS, DNS y logs.

Los wrappers exigen un worktree limpio y registran el commit exacto:

```bash
./scripts/deploy-ansible.sh \
  --playbook platform \
  --check \
  --ask-become-pass
./scripts/deploy-ansible.sh \
  --playbook platform \
  --confirm-production \
  --ask-become-pass
./scripts/deploy-ansible.sh \
  --playbook host-baseline \
  --confirm-production \
  --ask-become-pass

./scripts/install-cloudflare-secret.sh

./scripts/deploy-ansible.sh \
  --playbook edge \
  --profile acme-staging \
  --confirm-production \
  --ask-become-pass
./scripts/deploy-ansible.sh \
  --playbook edge \
  --confirm-production \
  --ask-become-pass

sudo ./scripts/validate.sh
```

La primera ejecución privilegiada exige acceso sudo y una vía de recuperación
fuera de banda. No se automatiza un cambio de SSH si no puede demostrarse una
reconexión con clave pública.

## Entradas pendientes

Se necesitan valores nuevos; no deben pegarse en el chat ni guardarse en el
repositorio:

- token Cloudflare ACME con `Zone Read` y `DNS Edit` limitado a
  `apptolast.com`;
- otro token Cloudflare para Terraform DNS;
- account ID, dos nombres de bucket y dos pares R2 `Object Read & Write`;
- token temporal Cloudflare capaz de declarar los buckets R2;
- `server_id`, MAC, lista completa de policies actuales y refresh token SCP
  de netcup;
- CIDR o CIDR administrativos reales para SSH;
- destinatario `age` y destino off-host para states, Swarm y `acme.json`;
- inventario confirmado de las claves SSH actuales y de sus propietarios.

Los tokens ACME, DNS, R2 y netcup son credenciales distintas. Docker Secrets
no es un sustituto de un gestor externo ni permite recuperar el valor una vez
creado.

## Límites conscientes

Un manager único no proporciona alta disponibilidad. Una caída o el
mantenimiento del VPS interrumpe el plano de control y sus cargas. No se
añadirán managers ni se abrirán `2377/TCP`, `7946/TCP+UDP` o `4789/UDP`
hasta disponer de una red privada autenticada y un diseño de quorum.

`live-restore` no conserva el plano de control de Swarm durante un reinicio de
Docker, por lo que se omite. El puerto `25565` permanece cerrado hasta el corte
documentado de Minecraft.

## Documentación operativa

- [Arquitectura](docs/ARCHITECTURE.md)
- [Migración desde el servidor anterior](docs/MIGRATION.md)
- [Edge, Cloudflare y ACME](docs/EDGE.md)
- [Estado Terraform y recuperación](docs/TERRAFORM_STATE.md)
- [Repositorios, versionado y mantenimiento](docs/REPOSITORIES.md)
- [Reconstrucción completa y cobertura](docs/REBUILD.md)
- [Changelog](CHANGELOG.md)
- [Baseline del host](ansible/roles/host_baseline/README.md)
- [Operación de Swarm](docs/OPERATIONS.md)
- [Diagnósticos conocidos](docs/KNOWN_ISSUES.md)

## Referencias oficiales

- [Administrar un Swarm](https://docs.docker.com/engine/swarm/admin_guide/)
- [Firewall de Docker](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
- [Docker Configs](https://docs.docker.com/engine/swarm/configs/)
- [Traefik en Swarm](https://doc.traefik.io/traefik/v3.7/reference/install-configuration/providers/swarm/)
- [Traefik ACME](https://doc.traefik.io/traefik/v3.7/reference/install-configuration/tls/certificate-resolvers/acme/)
- [Tokens Cloudflare](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/)
- [Backend R2](https://developers.cloudflare.com/terraform/advanced-topics/remote-backend/)
- [Backend S3 de Terraform](https://developer.hashicorp.com/terraform/language/backend/s3)
