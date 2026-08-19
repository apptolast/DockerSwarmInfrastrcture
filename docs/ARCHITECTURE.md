# Arquitectura y límites de propiedad

## Alcance

Este repositorio gobierna toda la infraestructura del servidor: proveedor,
host, Docker Swarm, edge, stacks, datos de migración, observabilidad y backup.
El código fuente interno de una app puede residir fuera; su ejecución y estado
en este VPS no.

`MigracionNetCup` se auditó como origen histórico. Sus Compose, Terraform y
Ansible solapados no conservan propiedad ni deben ejecutarse en paralelo.

## Contrato compartido

[`config/platform.yml`](../config/platform.yml) declara:

| Campo | Valor |
| --- | --- |
| Entorno/release | `production`, `0.1.0` no aceptada |
| IPv4 nueva/legacy | `159.195.156.57`, `138.199.157.58` |
| Interfaz pública | `eth0` |
| Swarm | `10.0.0.0/8`, `/24`, data path `4789/UDP` |
| Instalación/estado | `/opt/dockerswarm`, `/srv/dockerswarm` |
| TCP contractual | `80`, `443`, `25565` |
| Gate Minecraft | `false` |
| Zona | `apptolast.com` |

`25565` está en la allowlist coherente de las tres capas, pero permanece
efectivamente cerrado porque el gate explícito es falso. Cambiarlo exige una
decisión revisada; un `tfvars` no puede ampliar exposición por sí solo.

Invariantes:

1. ningún secreto entra en Git;
2. un recurso tiene un único writer;
3. import/adopción precede a cualquier cambio de recurso existente;
4. servicio, datos, DNS y rollback se aceptan como una unidad;
5. estado declarado y estado aplicado se informan por separado.

## Topología

Existe un único nodo manager/worker. Un manager tolera cero fallos y no ofrece
alta disponibilidad. Traefik tiene una réplica, restricción al manager
etiquetado y publica `80/443` en modo host; su update `stop-first` puede causar
una interrupción breve.

Antes de añadir nodos se necesitan, como mínimo, red privada autenticada,
managers impares en dominios de fallo, capacidad/almacenamiento compatibles y
pruebas de quorum.

Los puertos Swarm `2377/TCP`, `7946/TCP+UDP` y `4789/UDP` no se publican en
Internet. Netcup, UFW y `DOCKER-USER` forman la frontera pública.

## Aislamiento de red y edge

Traefik no monta el socket Docker ni activa el provider Swarm. Usa el file
provider con Configs inmutables, de modo que una app no queda publicada por
añadir labels.

En lugar de una overlay compartida, el contrato crea:

- una overlay cifrada/no attachable por workload HTTP;
- `apptolast-edge-monitoring` para observabilidad;
- ninguna conexión lateral entre workloads salvo dependencias declaradas.

La configuración dinámica declara exactamente:

- `/ping` de `edge.apptolast.com`;
- Kropia;
- Minecraft Stats;
- n8n;
- OpenClaw limpio;
- Passbolt;
- portfolio Pablo;
- portfolio Alberto;
- Shlink.

Minecraft no pasa por Traefik y su publicación TCP sigue desactivada.

## Workloads

[`config/services.yml`](../config/services.yml) es la allowlist. Fija imágenes
por digest, datasets, puertos, origen de evidencia y método de migración. La
política operativa separada
[`config/workload-image-updates.yml`](../config/workload-image-updates.yml)
autoriza una única excepción mutable para `personal-website-alberto/app`: la
referencia exacta de Docker Hub con `update_policy: tracked-tag`. El preflight
la compara con `approved_runtime_reference`, un `latest@sha256:...` versionado
y revisado, antes de renderizar un stack de producción. El servicio desplegado
conserva así una identidad de contenido exacta. Separar ambos contratos
preserva sin cambios el hash del catálogo ligado al marcador de restauración.
La denylist completa se valida en CI y no puede aparecer en stacks.

El render offline conserva `:latest` como representación declarativa. El modo
`--check` sí consulta el registro de forma no mutante y exige el digest
versionado, por lo que anticipa exactamente la identidad de un apply posterior.

`stacks/workloads` contiene bases, caches, runners y aplicaciones. Los
instaladores de secrets comparan nombres e identidades HMAC contra manifests
versionados; el contenido permanece fuera de Git. El despliegue exige un marker
de restore ligado a catálogo, runtime manifest y checksums.

OpenClaw se inicializa vacío y rechaza legado. n8n mantiene workflows sin
publicar hasta completar aceptación OAuth. Minecraft mantiene
`online-mode=false` y su gate público cerrado.

## Observabilidad

El stack interno reúne Prometheus, Alertmanager, Loki, Grafana, Alloy,
exporters y blackbox. No publica puertos en Internet; acceso administrativo y
forwarding se realizan mediante túneles versionados. La ausencia de receptor
real de alertas sigue siendo una compuerta externa.

## Estado y secretos

Estado durable:

- `/srv/dockerswarm/services`;
- `/srv/dockerswarm/traefik/acme.json`;
- `/var/lib/docker/swarm`;
- states Terraform en R2;
- backups restic fuera del VPS.

Docker Secrets protege material runtime dentro de Raft, pero no sustituye al
gestor externo ni permite recuperar valores. El secret ACME actual existe,
aunque debe rotarse por exposición fuera del canal previsto.

Autolock sigue desactivado hasta custodiar/probar la unlock key externamente.
Backup está codificado pero no activado por falta de R2/restic/escrow.

## Fronteras de ejecución

- Bootstrap y Ansible comparten `/run/lock/dockerswarm-iac.lock`.
- Un marker abandonado bloquea ambos scopes hasta recuperación con evidencia.
- Terraform exige backend/identidad/locking proof, plan firmado, commit limpio y
  snapshots cifrados.
- Los helpers directos se ejecutan solo en ventana exclusiva porque no todos
  comparten el mutex Ansible.

Metadatos aplicados registran commit, perfil y hash contractual en
`/opt/dockerswarm`. Un fichero de metadatos no sustituye la validación runtime.

## Estado aplicado actual

A 26 de julio de 2026:

- Swarm activo, un manager, autolock falso;
- cero stacks y cero servicios persistentes;
- solo red `ingress`;
- datos restaurados en la raíz protegida;
- secret ACME v1 presente;
- DNS aún en la IPv4 legacy;
- Terraform remoto, offsite backup, edge, workloads y observabilidad no
  aplicados.

## Referencias oficiales

- [Docker Swarm](https://docs.docker.com/engine/swarm/)
- [Managers, quorum y recuperación](https://docs.docker.com/engine/swarm/admin_guide/)
- [Redes overlay](https://docs.docker.com/engine/network/drivers/overlay/)
- [Firewall de Docker](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
- [Docker secrets](https://docs.docker.com/engine/swarm/secrets/)
- [Docker Configs](https://docs.docker.com/engine/swarm/configs/)
- [Traefik file provider](https://doc.traefik.io/traefik/v3.7/reference/install-configuration/providers/others/file/)
