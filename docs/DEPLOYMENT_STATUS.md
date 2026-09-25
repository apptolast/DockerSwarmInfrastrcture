# Estado del despliegue productivo

Instantánea comprobada el 28 de julio de 2026 sobre `159.195.156.57`. Sustituye
a la sección «Estado observado» de [`README.md`](../README.md), que describe el
host antes del primer despliegue real de este árbol.

## Servicios aparcados

Por decisión del propietario (2026-09-25), `config/platform.yml` declara
Minecraft y OpenClaw aparcados (`platform_parked_workloads`) para liberar RAM
y CPU del host. Para aparcar se aplica `workloads`, luego `edge` y luego
`observability` si está desplegado; mientras `edge` no pueda aplicarse (ver
«Deriva fuera del repositorio») se aplica solo `workloads`. Sus datos siguen
en `/srv/dockerswarm/services`. Procedimiento en
[OPERATIONS.md](OPERATIONS.md), «Aparcar un servicio».

Aplicado el 2026-09-25 con `--playbook workloads --local` desde `main` en
`7361203` (#59, #60 y #61):

- `--check`: `ok=74 changed=2 failed=0`.
- `--confirm-production` (09:47-09:49 UTC): `ok=164 changed=5 failed=0`;
  repetido: `ok=164 changed=0 failed=0`. Las dos operaciones liberaron el
  lock sin dejar marker.
- `workloads_minecraft=0/0` y `workloads_openclaw=0/0`, sin contenedores;
  los otros trece servicios en `1/1`. Ningún proceso del host escucha en el
  25565.
- `openclaw.apptolast.com` responde `503`; el resto de rutas públicas
  responde igual que antes del apply.
- Memoria disponible: 3 002 MiB antes del apply y 6 745 MiB después, sin
  swap; la presión de memoria (PSI) bajó a casi cero.
- `edge` no se aplicó (compuerta STOP 10).

Archivos en frío tomados en `0/0` bajo el lock host-global
(`parked-cold-archive`), `0600 root:root`:

<!-- markdownlint-disable MD013 -->

| Archivo | Bytes | Entradas | SHA-256 |
| --- | ---: | ---: | --- |
| `minecraft-cold-20260925T095312Z.tar.zst` | 2 352 288 645 | 2 601 | `14954f6f96c3f05bdd7a40664282645217f377e7abf360a1a4cfbd801c06bef1` |
| `openclaw-cold-20260925T095312Z.tar.zst` | 42 191 531 | 8 230 | `a6ac4fee4d86af91181d9242f8a9c5231cb1983e6b755e81b25507e3ede4d80d` |

<!-- markdownlint-enable MD013 -->

Antes de aparcar se tomó además un archivo en caliente de Minecraft con el
protocolo RCON del backup (`save-off`, `save-all flush`, `save-on`), sin
jugadores conectados:
`/var/backups/dockerswarm/parked/minecraft-hot-20260925T070501Z.tar.zst`,
2 352 303 106 bytes, 2 601 entradas, SHA-256
`fef4bf8b4675c63ee6445d718ce8967b4ad2d65511d6d2337dc724f3fee9e1c7`.

Seguimientos abiertos del aparcado:

- Cerrar el 25565 en el firewall mientras Minecraft está aparcado. Exige
  aplicar `platform` y `host-baseline`, que aplicarían también el snapshot de
  paquetes 20260924 pendiente (`docs/SNAPSHOT_20260924.md`), y el de
  `platform` cortaría SFTP y Satisfactory (ver abajo). Hasta entonces el
  apply de `workloads` exige que ningún proceso del host escuche en ese
  puerto, pero solo en el momento del apply.
- Ninguna alerta avisa si alguien arranca a mano un servicio aparcado. El
  preflight de capacidad de cualquier otro playbook lo rechaza (quedaría fuera
  de presupuesto), y el siguiente apply de `workloads` lo vuelve a dejar en
  `0/0` sin preguntar.
- Desaparcar ya no es solo devolver el presupuesto: con los stacks externos
  declarados, Minecraft no cabe en el plan `observability` (13 037 MiB de
  límite frente a 12 397) y los dos juntos no caben en el activo (12 909 MiB).
  Volver a arrancarlos exige una decisión de capacidad del propietario (ver
  [CAPACITY.md](CAPACITY.md)).

## Deriva fuera del repositorio

Inventario del 2026-09-25 de lo que corre en el host sin estar codificado
aquí:

- Stacks Swarm `satisfactory-companions` y `satisfactory-events` (creados el
  2026-09-22) y `sftp` (2026-09-23), posteriores al último apply desde este
  repositorio (2026-09-19). Sus réplicas y recursos están declarados y se
  verifican en `config/capacity-profiles.yml` (ver [CAPACITY.md](CAPACITY.md),
  «Stacks externos»), pero sus ficheros de stack, imágenes locales y datos no
  están aquí. Ninguno monta el socket de Docker ni corre privilegiado; `sftp`
  añade `CAP_SETGID`, `CAP_SETUID` y `CAP_SYS_CHROOT`.
- Proyectos Compose `satisfactory` (servidor del juego y sus servicios) y
  `monitor-production` (observatorio de `monitor.apptolast.com`), fuera de
  Swarm y del contrato de capacidad. Ninguno monta el socket de Docker ni es
  privilegiado; el colector de procesos de `monitor-production` comparte el
  espacio de PID del host (`pid: host`, sin red), y su `node-exporter` monta
  la raíz del host en solo lectura en `/host`. Como puede leer los datos de
  PostgreSQL restaurados, la compuerta de contenedores del apply de
  `workloads` lo rechazaba. Desde el 2026-09-25 lo admite como observador
  externo revisado, solo con esa forma exacta:
  - proyecto Compose `monitor-production` y servicio `node-exporter`;
  - no es una ejecución `oneoff`;
  - `/` montado en `/host` como bind de solo lectura;
  - usuario `nobody`, escrito exactamente `nobody`, `65534` o
    `65534:65534`;
  - sin etiquetas de servicio, tarea ni stack de Swarm.

  El contenedor vivo usa `prom/node-exporter:v1.12.1`, corre como `nobody`
  con el sistema de ficheros raíz en solo lectura y sin capacidades añadidas.
  Si el Observatorio renombra el proyecto o el servicio, la compuerta vuelve
  a bloquear el apply de `workloads`.
- Reglas manuales en la cadena `DOCKERSWARM-INGRESS`: el 2222/tcp de `sftp`
  pasa por una cadena propia `SFTP-SWARM` (jail manual de Fail2ban
  `/etc/fail2ban/jail.d/95-sftp-swarm.local`), y el 7777/tcp+udp y el
  8888/tcp del servidor de Satisfactory se admiten solo desde una IP de
  origen. No están en el render revisado de esa cadena (80, 443 y 25565).
  Dos drop-ins manuales de `dockerswarm-docker-firewall.service` las vuelven a
  añadir cada vez que esa unidad se ejecuta:
  - `90-satisfactory.conf` ejecuta `/srv/satisfactory/ops/game_firewall.py`;
  - `95-sftp.conf` ejecuta `/usr/local/sbin/apptolast-sftp-firewall`.

  Ambos scripts son `root:root` y no son escribibles por grupo ni por otros
  (`0644` y `0755`), igual que sus directorios (`/srv/satisfactory` `0700`,
  `/srv/satisfactory/ops` `0750`, `/usr/local/sbin` `0755`), y ningún
  contenedor monta esas rutas. La unidad estaba `enabled` el 2026-09-25, así que
  sobreviven a un reinicio de Docker o del host. Un apply de `host-baseline`
  solo reinicia y vuelve a habilitar la unidad cuando se dispara su handler
  «Restart Docker ingress ordering»
  (`ansible/roles/host_baseline/handlers/main.yml`), es decir, cuando cambia
  el gancho `DOCKER-USER` del bouncer, uno de sus dos scripts auxiliares o el
  drop-in `20-crowdsec-order.conf`
  (`ansible/roles/host_baseline/tasks/crowdsec-docker.yml`). En un host
  convergido no cambia nada de eso: `host-baseline` ni ejecuta la unidad ni la
  vuelve a habilitar, tampoco después de un apply de `platform`. Un apply de
  `platform` **no** las conserva: ejecuta el script base fuera de systemd
  (`ansible/roles/platform/tasks/main.yml`, «Reconcile the Docker
  published-port policy after Swarm changes») y deja la unidad deshabilitada
  al arranque. La unidad es `oneshot` con `RemainAfterExit=yes` y sigue
  activa, así que `enable --now` no la vuelve a ejecutar. Tras el apply, SFTP
  y Satisfactory quedan cerrados hasta
  `sudo -- systemctl enable dockerswarm-docker-firewall.service` seguido de
  `sudo -- systemctl restart dockerswarm-docker-firewall.service`, y
  `sudo -- iptables -S DOCKERSWARM-INGRESS` debe volver a mostrar sus
  reglas. Los roles de este repositorio no borran esos drop-ins, pero un
  servidor reconstruido desde aquí no los tendría.
- Traefik (`edge_traefik`) se modificó a mano el 2026-09-22. Usa la Docker
  Config dinámica `edge-traefik-dynamic-companions-a0952eace071`, que añade
  las rutas de `satisfactory.apptolast.com` (web, websocket y
  `/companions`) y `logs-satisfactory.apptolast.com`, esta última con un
  middleware `basicAuth` cuyo hash no debe publicarse en este repositorio, y
  está conectado a la red `apptolast-edge-satisfactory`. El secret
  `cloudflare_dns_api_token_v3` que usa sí coincide con este repositorio. Un
  apply de `edge` retiraría esas rutas y esa red y dejaría Satisfactory sin
  ruta, así que no se aplica `edge` hasta codificarlas, con el `basicAuth`
  como Docker Secret. Mientras tanto, con OpenClaw aparcado, la sonda de salud
  del Traefik vivo lo marca caído (su ruta responde `503`) y registra un WARN
  `Health check failed.` cada 15 s. El backend sin servidores de este
  repositorio lo elimina en cuanto `edge` pueda aplicarse.
- `fs.suid_dumpable` vale `2` en vivo (leído el 2026-09-25), frente al `0`
  que declaran `ansible/roles/host_baseline/defaults/main.yml` y
  `/etc/sysctl.d/99-z-dockerswarm-host-hardening.conf`. Los otros 24 valores
  gestionados coinciden. No es un cambio manual: lo escribe `apport.service`
  (paquete `apport-core-dump-handler`, `enabled`) cada vez que arranca,
  después de `systemd-sysctl`. Hasta el cambio que lo corrige, todo apply de
  `host-baseline` fallaba en «Verify every managed kernel setting» después de
  haber movido el pin de APT y actualizado paquetes. El siguiente apply
  detiene y deshabilita esa unidad, cuya parada ya devuelve la clave a `0`, y
  converge solo las claves gestionadas que difieran, sin `sysctl --system`.
  `kernel.core_pattern`, que pasa a gestionarse como `|/bin/false`, vale hoy
  el `core` del paquete (`/usr/lib/sysctl.d/10-coredump-debian.conf`); ese
  mismo apply lo converge
  (ver [`host_baseline/README.md`](../ansible/roles/host_baseline/README.md)).

Unidades systemd del host que tampoco gestiona este repositorio:
`satisfactory-backup.timer`, `satisfactory-backup-check.timer`,
`satisfactory-stable-update.timer` (con `satisfactory-maintenance@.service`),
`satisfactory-log-collector.service`, `monitor-endpoints.timer`,
`monitor-swarm.timer` y `apptolast-sftp-chain.service`, que prepara la
cadena IPv4 de la jail de Fail2ban de SFTP antes de `fail2ban.service`.

El proyecto Compose `satisfactory` (`/srv/satisfactory`) se despliega desde
otro repositorio. El 2026-09-25 su Redis estaba lleno (`maxmemory 80mb` con
`noeviction`) y la web devolvía `500` al guardar la sesión. En
`/srv/satisfactory/redis.conf` la política pasó a `volatile-lru`, que solo
expulsa claves con caducidad y deja intacta la cola de Horizon. Se editó en
el mismo inodo que monta el contenedor y se aplicó en vivo con
`CONFIG SET`; la copia anterior quedó en
`redis.conf.pre-volatile-lru-20260925`. Ese cambio tiene que llegar a su
repositorio de origen.

La entrada de Traefik es la compuerta STOP 10 de `CLAUDE.md`. Mientras
siga cerrada, la memoria de Traefik de `stacks/edge/stack.yml.j2` se aplicó
en vivo el 2026-09-25 con dos `docker service update` sobre `edge_traefik`:
`--limit-memory 256M` a las 09:43 UTC y `--reserve-memory 128M` a las
10:03 UTC. Cada uno cambió un único campo del spec (`Limits.MemoryBytes` de
134217728 a 268435456 y `Reservations.MemoryBytes` de 67108864 a
134217728) y reemplazó la única tarea del edge, con un corte breve de 80/443
durante el relevo. La reserva solo cuenta para la planificación de Swarm. El
servicio vivo sigue difiriendo del repositorio solo en la deriva descrita
arriba.

## Estado temporal fuera del repositorio

- Laboratorio AX (Google Agent Executor sobre Kubernetes kind y Agent
  Substrate) en `/opt/ax-lab`: nodo `kind-control-plane` (privilegiado, como
  exige kind) limitado con `docker update` a 3 584 MiB y registro local
  `kind-registry`, fuera de Swarm y del contrato de capacidad. Esos
  3 584 MiB equivalen a toda la reserva del contrato para el host, así que
  con el laboratorio en marcha un preflight de capacidad en verde no
  garantiza margen real. El nodo se paró el 2026-09-25 a las 07:36 UTC para
  retirar el swap antes de aparcar, y volvió a arrancarse a las 10:07 UTC,
  con los 28 pods listos. Es un ensayo manual que se codifica por partes
  (ver [AX.md](AX.md), «Qué codifica este repositorio y qué sigue siendo
  manual»); hasta entonces no forma parte del estado reconstruible. Sale de
  esta lista cuando el último de esos cambios se haya aplicado y verificado
  o, si se descarta, cuando se borren el clúster, el registro y
  `/opt/ax-lab`.
- Límites `fs.inotify.max_user_watches=524288` y
  `fs.inotify.max_user_instances=512`, aplicados en caliente para kind. El
  playbook `ax-lab` ya los codifica (ver [AX.md](AX.md)), pero hasta que se
  aplique se pierden al reiniciar. El apply los deja persistentes en
  `/etc/sysctl.d/99-z-dockerswarm-ax-lab.conf`, y entonces salen de esta
  lista.
- El swap temporal `/swap-ax-build` (4 GiB, fuera de `fstab`) que se creó
  para compilar el laboratorio se desactivó y se borró el 2026-09-25, antes
  de cualquier apply. El host vuelve a cumplir `required_swap_mib: 0`.

## Aplicado y verificado

| Capa | Resultado |
| --- | --- |
| `platform` | `ok=156 changed=18 failed=0` |
| `edge` con perfil `acme-staging` | `ok=71 failed=0` |
| `edge` con perfil `production` | `ok=72 changed=6 failed=0` |
| Certificados | 9 de 9 emitidos por Let's Encrypt producción |
| Servicios Swarm | 11 de 16 en `1/1` |

Los nueve nombres del catálogo resuelven por HTTPS con cadena verificada desde
Internet. `n8n`, `pablohurtadohg` y `albertohidalgo` sirven tráfico real.

El nodo declara las etiquetas `platform.edge` y `platform.workloads`, existen
las nueve redes overlay aisladas, UFW mantiene exactamente trece reglas de
egress más `80/tcp` y `443/tcp` de ingress, y `22/tcp` conservó su límite de
tasa durante todo el proceso.

### Runners de n8n y memoria de `portfolio-alberto` (2026-09-25)

`--playbook workloads --local` desde `main` en `1fa9c10` (#62 y #63):

- `--check`: `ok=74 changed=2 failed=0`.
- `--confirm-production` (10:40-10:43 UTC): `ok=164 changed=8 failed=0`;
  repetido: `ok=164 changed=0 failed=0`. Ninguna operación dejó marker.
- `workloads_n8n-runners` corre sobre `n8nio/runners:2.31.5`, la misma
  versión que n8n (etiqueta `org.opencontainers.image.version`), y n8n
  registró sus dos lanzadores, `launcher-javascript` y `launcher-python`.
- `workloads_portfolio-alberto` tiene 256 MiB de límite y 128 MiB de reserva.
- Todos los servicios siguen en `1/1`, salvo los aparcados en `0/0`, y todas
  las rutas públicas responden igual que antes.

## Runtime regenerado

El árbol anterior quedó apartado como
`/srv/dockerswarm/services.pre-runtime-v4-20260728T002105Z`; revertir es un
`mv`. El actual se reconstruyó desde el backup cifrado y volvió a verificarse:

- `runtime-manifest.json` en `schemaVersion 4`, con clave HMAC de identidad de
  32 bytes y 35 ficheros fuente de secrets.
- Cinco bases restauradas y comprobadas por SQL; `rag` conserva pgvector 0.8.2
  con tres tablas y `vectors` solo la extensión 0.8.1, que es su contenido real.
- `workloads-ready-v2.json` ligado al `catalogSha256` de `config/services.yml`
  en HEAD, resolviendo el desajuste que impedía abrir el gate.
- 35 Docker Secrets de workloads instalados.

## Pendiente

### Servicios que no convergen

`kropia` necesitaba recuperar `CHOWN`, `SETGID`, `SETUID` y `NET_BIND_SERVICE`
tras `cap_drop: ALL`, porque el entrypoint de nginx prepara `/var/cache/nginx`
antes de bajar de privilegios. El arreglo está commiteado pero **no llegó a
aplicarse**: ver el bloqueo descrito más abajo.

`minecraft-stats` y `passbolt` arrancan correctamente y Swarm los detiene; sus
procesos paran con `exit status 0`, señal de terminación limpia por healthcheck.
Necesitan más margen de arranque, no una corrección de código.

`shlink` pierde sus workers de RoadRunner (`WorkerAllocate: EOF`). Causa sin
confirmar.

`openclaw` responde `Missing config. Run 'openclaw setup'`. Es una instalación
limpia esperando su alta inicial, tal y como la declara el catálogo.

### Bloqueo circular al redesplegar

`workloads` falla en «Inspect every running or stopped Docker container»
(`deploy.yml:205`) porque los servicios en bucle de reinicio destruyen
contenedores entre el listado y la inspección. El propio crash-loop impide
desplegar el cambio que lo detendría.

Para romperlo, retirar del stack los servicios que reinician antes de volver a
aplicar, o aplicar el cambio de capacidades directamente sobre el servicio y
reconciliar después.

### Compuertas externas que siguen cerradas

El backup permanece bloqueado: exige un custodio externo para la unlock key del
Swarm y un bucket R2 con credencial propia.
[`docs/BACKUP_RECOVERY.md`](BACKUP_RECOVERY.md) prohíbe reutilizar el token DNS
de Cloudflare o las credenciales del backend Terraform. Mientras siga así, este
host no tiene copias fuera de sí mismo.

Minecraft espera un flag explícito que registre la aceptación de publicar con
`online-mode=false`, en lugar de eliminar el assert que hoy acopla ambas cosas.

## Advertencia sobre Terraform y DNS

**No debe ejecutarse Terraform contra el root `cloudflare/apptolast-dns`.** En
modo `initialize` fuerza `adoption_only=true`, lo que devolvería los nueve
registros A a `138.199.157.58`, un servidor que ya no existe. Los registros
apuntan hoy a la plataforma porque se cambiaron a mano en Cloudflare; adoptar
ese estado en Terraform requiere trabajo previo sobre `imports.tf`, que además
no contempla el registro `edge`.
