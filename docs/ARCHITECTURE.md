# Arquitectura y límites de propiedad

## Alcance y estado

Este repositorio define la plataforma del VPS: perímetro externo, host, Docker
Swarm y el edge HTTP/HTTPS. No es el repositorio de las aplicaciones ni de sus
datos.

Este documento describe el contrato **declarado en Git**. No demuestra que la
configuración esté aplicada en el servidor, en Netcup o en Cloudflare. El host
y el Swarm se inspeccionaron en modo read-only; no estaban disponibles
credenciales autenticadas de Netcup/Cloudflare ni states Terraform remotos.
Todo lo que requiera esos accesos permanece pendiente hasta observarlo y
guardar evidencia.

La comparación con `MigracionNetCup` se realizó sobre el commit
`79e9c79e5c9ca9f71a7f9011cde9a1bb3a1f8e42`. Ese commit es una referencia de
inventario, no una afirmación sobre los procesos que siguen activos en el VPS.

## Contrato compartido

[`config/platform.yml`](../config/platform.yml) es el contrato legible por
Ansible y los roots Terraform. Los valores actualmente declarados son:

| Campo | Valor declarado | Significado |
| --- | --- | --- |
| Entorno | `production` | Único entorno cubierto por este contrato |
| Release | `0.1.0` | Versión declarativa previa a aceptación productiva |
| IPv4 pública | `159.195.156.57` | Destino esperado del DNS inicial del edge |
| Interfaz pública | `eth0` | Interfaz sobre la que se controla la exposición |
| Puerto VXLAN | `4789/UDP` | Data path de Swarm; no es un puerto público |
| Pool Swarm | `10.0.0.0/8`, máscara `/24` | Espacio para redes overlay |
| Raíz instalada | `/opt/dockerswarm` | Artefactos renderizados; no estado |
| Raíz de estado | `/srv/dockerswarm` | Estado persistente de la plataforma |
| Red edge | `apptolast-edge` | Overlay compartida entre edge y futuras apps |
| TCP público | `80`, `443` | Allowlist pública actual y completa |
| Zona Cloudflare | `apptolast.com` | Única zona autorizada para el edge |
| Nombre inicial | `edge.apptolast.com` | Registro y route iniciales |

Un valor en este fichero no prueba que la realidad coincida. Antes de un cambio
se compara el contrato con las interfaces, rutas, redes Docker, reglas de
firewall y registros DNS observados.

Los invariantes son:

1. Ningún dato sensible entra en este fichero ni en Git.
2. Abrir otro puerto público exige modificar y revisar el contrato; un
   `tfvars` no puede ampliar por sí solo la allowlist.
3. Un recurso tiene un único propietario activo. Dos repositorios o dos estados
   Terraform no pueden escribir el mismo objeto.
4. DNS, datos y servicio se migran como una unidad con rollback probado.
5. Un fichero declarado no sustituye una verificación del estado aplicado.

## Propiedad durante y después de la migración

- **DNS autoritativo gestionado:** el root
  `infra/terraform/cloudflare/apptolast-dns`. `MigracionNetCup` solo aporta
  inventario hasta la importación y no se aplica en paralelo.
- **Firewall Netcup, asignación al VPS y claves SCP:** el root
  `infra/terraform/netcup/perimeter`. La automatización anterior queda
  congelada hasta importar y transferir la propiedad.
- **Paquetes, daemon Docker, Swarm y UFW edge:** el rol Ansible `platform`.
  El baseline anterior no vuelve a ejecutarse.
- **SSH, updates, journald, hardening y controles del host:** el rol
  `host_baseline`. Sus reglas equivalentes anteriores no vuelven a aplicarse.
- **`DOCKER-USER` y orden con CrowdSec:** los roles `platform` y
  `host_baseline`. Los scripts equivalentes anteriores quedan congelados.
- **Inicialización y contrato Swarm:** el rol `platform`; el repositorio
  anterior no conserva propiedad.
- **Red overlay `apptolast-edge`:** el rol `edge`. Su bridge homónimo se
  retira únicamente durante el corte.
- **Traefik, ACME y route `/ping`:** el stack `edge`. El Traefik anterior se
  retira únicamente durante el corte.
- **Aplicaciones, esquemas y datos:** el repositorio y runbook de cada
  aplicación siguen siendo la fuente hasta migrar cada carga.
- **Minecraft y sus datos:** su repositorio y runbook. El servicio sigue
  cerrado al público hasta completar su migración.

En el commit auditado de `MigracionNetCup` existen tres solapamientos que deben
eliminarse, pero solo en el orden definido por
[`MIGRATION.md`](MIGRATION.md):

- `compose/compose.yml` declara otro Traefik que publica `80/443`, una red bridge
  llamada `apptolast-edge` y Minecraft en `25565/TCP`;
- `infra/terraform` declara firewall, asignación de firewall y claves SSH de
  Netcup;
- `provisioning/ansible/roles/host_baseline` declara Docker, daemon, firewall y
  parámetros del host.

Hasta terminar el traspaso, el repositorio antiguo puede servir para restaurar
las aplicaciones, pero sus automatizaciones solapadas quedan congeladas. Tras
el traspaso se retiran esas piezas de su ejecución y CI; conservarlas en el
historial no les devuelve propiedad.

## Topología de ejecución

La topología objetivo actual tiene un solo nodo, que actúa a la vez como manager
y worker. Docker permite ejecutar tareas en un manager, pero un Swarm de un
manager tolera cero fallos de manager. No hay alta disponibilidad, evacuación
automática a otro host ni mantenimiento sin interrupción.

El servicio Traefik tiene una réplica, se fija al manager etiquetado para edge y
publica `80/443` en modo `host`. Sus actualizaciones son `stop-first`; por tanto,
incluso una actualización correcta puede causar una interrupción breve. Añadir
réplicas en el mismo nodo no elimina el punto único de fallo ni permite que dos
tareas publiquen los mismos puertos en modo `host`.

Antes de llamar a esta plataforma “alta disponibilidad” se necesitan, como
mínimo, tres managers en dominios de fallo distintos, workers/capacidad
adecuados, almacenamiento y aplicaciones compatibles con múltiples nodos y
pruebas de pérdida de quorum. Docker recomienda un número impar de managers y
documenta que un Swarm de un manager tiene tolerancia cero:
[administración de managers y quorum](https://docs.docker.com/engine/swarm/admin_guide/).

## Redes y puertos

`apptolast-edge` debe ser una red:

- driver `overlay`;
- scope `swarm`;
- cifrada;
- no attachable.

El rol `edge` falla si encuentra una red con ese nombre y otro contrato. Una red
bridge de Compose y una overlay de Swarm no pueden compartir el mismo nombre;
por eso el bridge antiguo se elimina durante la ventana de corte, nunca antes
de detener sus contenedores y asegurar el rollback.

Solo `80/TCP` y `443/TCP` son públicos. Los puertos internos de Swarm
(`2377/TCP`, `7946/TCP+UDP` y `4789/UDP`) no se autorizan desde Internet; en el
mononodo no necesitan una regla pública. Antes de añadir nodos deben limitarse a
una red privada o túnel autenticado entre direcciones conocidas. Docker publica
los requisitos de red de Swarm en
[el tutorial oficial](https://docs.docker.com/engine/swarm/swarm-tutorial/).

Las publicaciones Docker pueden atravesar el procesamiento habitual de UFW.
Por ello la defensa combina el firewall de Netcup, UFW y una política explícita
en `DOCKER-USER`, siguiendo la
[documentación de filtrado de Docker](https://docs.docker.com/engine/network/packet-filtering-firewalls/).

`25565/TCP` no pertenece al contrato público actual. No se abre en Netcup, UFW,
`DOCKER-USER` ni Docker hasta que los datos y el servicio Minecraft hayan sido
migrados, restaurados en una prueba y aceptados según `MIGRATION.md`.

## Edge sin acceso al API de Docker

Traefik usa únicamente el
[file provider de Traefik 3.7](https://doc.traefik.io/traefik/v3.7/reference/install-configuration/providers/others/file/)
con un fichero dinámico inmutable y `watch: false`. No se habilita
`providers.swarm`, no se monta `/var/run/docker.sock` y no se configura ningún
endpoint hacia el API de Docker.

Esto es una frontera deliberada:

- Traefik no descubre servicios ni interpreta labels de Swarm;
- una aplicación no queda publicada por añadir labels;
- routers, middlewares y backends deben aparecer explícitamente en la
  configuración dinámica revisada;
- cada cambio crea Docker Configs con nombre derivado de su SHA-256 y actualiza
  el servicio mediante el workflow Ansible.

El
[provider Swarm de Traefik](https://doc.traefik.io/traefik/v3.7/reference/install-configuration/providers/swarm/)
requiere habilitar el provider y acceder al API de Docker. Nada de eso forma
parte del diseño actual. Introducirlo sería un cambio de arquitectura y de
modelo de amenaza, no una optimización incidental.

La configuración dinámica actual contiene solo el router HTTPS
`edge-health` para `Host(edge.apptolast.com) && Path(/ping)`, apuntando a
`ping@internal`. No existen routers de aplicaciones en este repositorio. Un
Traefik sano no significa que una aplicación esté migrada.

## Estado, secretos y recuperación

Docker secrets transporta el token DNS de ACME únicamente al servicio Traefik.
Docker cifra secrets en tránsito y en el Raft log y solo los monta en servicios
autorizados:
[secrets de Swarm](https://docs.docker.com/engine/swarm/secrets/). El contenido
no se guarda en Ansible, Terraform, Docker Configs ni Git.

Los ficheros estáticos/dinámicos no sensibles usan
[Docker Configs](https://docs.docker.com/engine/swarm/configs/). Son inmutables:
se crea una versión nueva, se despliega y solo después se considera el garbage
collection seguro descrito en [`EDGE.md`](EDGE.md).

`/srv/dockerswarm/traefik/acme.json` es estado sensible y durable, no una
configuración regenerable. El estado de Swarm reside en
`/var/lib/docker/swarm`. Ambos necesitan backups cifrados fuera del VPS y
restauraciones probadas. Docker exige detener el daemon para obtener el backup
coherente recomendado del directorio completo de Swarm:
[backup y recuperación de Swarm](https://docs.docker.com/engine/swarm/admin_guide/#back-up-the-swarm).

## Compuertas no satisfechas por Git

No se autoriza afirmar “aplicado” hasta reunir, como mínimo:

- consola de rescate netcup confirmada para el primer cambio de SSH;
- inventario final de procesos antiguos, volúmenes y automatizaciones de apps;
- credenciales e inventario de Netcup y Cloudflare;
- estados Terraform anteriores y nuevos, o evidencia de que no existen;
- dos buckets R2 separados y locking probado;
- token ACME separado, creado como secret de Swarm;
- backups cifrados offsite de Swarm, ACME y datos de aplicaciones, con prueba de
  restauración;
- plan de datos y smoke tests por aplicación;
- ventana de mantenimiento y responsable autorizado para DNS/rollback.

La ausencia de cualquiera de estas evidencias es un bloqueo operativo, no una
razón para inferir valores.

La diferencia entre controles ya declarados, controles todavía adoptados y
bootstrap externo se mantiene en [`REBUILD.md`](REBUILD.md).

Los helpers locales reducen errores mecánicos, pero no marcan estas compuertas
por sí solos:

- [`scripts/install-cloudflare-secret.sh`](../scripts/install-cloudflare-secret.sh)
  verifica el token con un TXT efímero antes de crear un secret nuevo;
- [`scripts/snapshot-terraform-state.sh`](../scripts/snapshot-terraform-state.sh)
  cifra el state en streaming y exige un destino fuera del repositorio;
- [`scripts/plan-terraform.sh`](../scripts/plan-terraform.sh) liga cada plan a
  un commit limpio y conserva el exit code de drift;
- [`scripts/deploy-ansible.sh`](../scripts/deploy-ansible.sh) liga cada
  despliegue al commit y hash exactos del contrato;
- [`scripts/gc-edge-configs.sh`](../scripts/gc-edge-configs.sh) empieza en
  dry-run y su lista debe revisarse según la retención definida en `EDGE.md`.

## Referencias oficiales

- [Docker Swarm mode](https://docs.docker.com/engine/swarm/)
- [Administración, backup y recuperación de Swarm](https://docs.docker.com/engine/swarm/admin_guide/)
- [Redes overlay](https://docs.docker.com/engine/network/drivers/overlay/)
- [Docker secrets](https://docs.docker.com/engine/swarm/secrets/)
- [Docker Configs](https://docs.docker.com/engine/swarm/configs/)
- [Traefik file provider 3.7](https://doc.traefik.io/traefik/v3.7/reference/install-configuration/providers/others/file/)
- [Traefik Swarm provider 3.7](https://doc.traefik.io/traefik/v3.7/reference/install-configuration/providers/swarm/)
