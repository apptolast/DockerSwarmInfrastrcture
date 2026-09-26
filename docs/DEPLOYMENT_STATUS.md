# Estado del despliegue productivo

Instantánea comprobada el 28 de julio de 2026 sobre `159.195.156.57`. Sustituye
a la sección «Estado observado» de [`README.md`](../README.md), que describe el
host antes del primer despliegue real de este árbol.

## Servicios aparcados

Por decisión del propietario (2026-09-25), `config/platform.yml` declara
Minecraft y OpenClaw aparcados (`platform_parked_workloads`) para liberar RAM
y CPU del host. Para aparcar se aplica `workloads`, luego `edge` y luego
`observability` si está desplegado. Sus datos siguen en
`/srv/dockerswarm/services`. Procedimiento en
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
- `edge` no se aplicó (compuerta STOP 10). Lo aplicó la ventana del
  2026-09-26 (ver «Edge: ventana de las rutas de Satisfactory
  (2026-09-26)»), que retiró la sonda de OpenClaw.

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
  aplicar `platform` y `host-baseline`. El snapshot de paquetes 20260924 ya
  está aplicado (ver «`host-baseline` (2026-09-25)»), pero el apply de
  `platform` cortaría SFTP y Satisfactory (ver abajo). Hasta entonces el
  apply de `workloads` exige que ningún proceso del host escuche en ese
  puerto, pero solo en el momento del apply.
- Ninguna alerta avisa si alguien arranca a mano un servicio aparcado. El
  preflight de capacidad de cualquier otro playbook lo rechaza (quedaría fuera
  de presupuesto), y el siguiente apply de `workloads` lo vuelve a dejar en
  `0/0` sin preguntar.
- Desaparcar ya no es solo devolver el presupuesto: con los stacks externos
  declarados, Minecraft no cabe en el plan `observability` (13 037 MiB de
  límite frente a 12 397) y, con el laboratorio AX declarado en el activo,
  ninguno de los dos cabe en él (12 653 MiB solo OpenClaw).
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
- Traefik (`edge_traefik`) se modificó a mano el 2026-09-22 con la Docker
  Config dinámica `edge-traefik-dynamic-companions-a0952eace071`, que añadía
  las rutas de `satisfactory.apptolast.com` (web, websocket y
  `/companions`) y `logs-satisfactory.apptolast.com`, esta última con un
  middleware `basicAuth` cuyo hash no debe publicarse en este repositorio, y
  la red `apptolast-edge-satisfactory`. Desde el 2026-09-26 ya no es deriva:
  esas rutas, su backend y la red están codificados en
  `stacks/edge/dynamic.yml.j2`, el `basicAuth` lee sus usuarios del Docker
  Secret `edge-basicauth-satisfactory-logs-v1`, creado a mano desde el hash
  vivo (ver [EDGE.md](EDGE.md), «Rutas de Satisfactory»), y el servicio
  corre la Config renderizada (ver «Edge: ventana de las rutas de
  Satisfactory (2026-09-26)»). Siguen en el host, sin uso y con el hash en
  línea, las dos Configs hechas a mano (`…companions-a0952eace071` y
  `…satisfactory-2af1d9e1a890`), hasta que el propietario decida retirarlas
  (ver [EDGE.md](EDGE.md), «Rollback»). La ruta de AX (EDGE.md, «Ruta de
  AX») se aplicó en su ventana del mismo día (ver «Panel web de AX: ventana 2
  (2026-09-26)»); desde entonces `edge` y `site` solo se aplican siguiendo
  «Ventana de aplicación de la ruta», con sus tres secrets presentes.
- El registro DNS `ax.apptolast.com` (A a `159.195.156.57`, DNS-only) lo creó
  el propietario a mano en Cloudflare para el panel web de AX, igual que los
  de OrganizationWeb y RacingGame. Resolvía a esa IP el 2026-09-26. No está
  en Terraform y no debe crearse desde él: Cloudflare admite varios A con el
  mismo nombre. Se adoptará con un bloque `import` en la adopción general del
  DNS (ver [EDGE.md](EDGE.md), «Registro DNS»). Hasta que se aplique la ruta
  de AX (EDGE.md, «Ruta de AX»), Traefik no tiene certificado para ese nombre
  y, con `sniStrict`, rechaza su TLS.
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

La entrada de Traefik era la parte de `edge` de la compuerta STOP 10 de
`CLAUDE.md`. Esa parte se levantó el 2026-09-26, cuando la ventana de
aplicación se verificó y un apply repetido informó `changed=0`; el resto de
la compuerta (los drop-ins manuales del firewall y los stacks externos)
sigue abierta. Mientras esa parte siguió abierta, la memoria de Traefik de
`stacks/edge/stack.yml.j2` se aplicó en vivo el 2026-09-25 con dos `docker
service update` sobre `edge_traefik`:
`--limit-memory 256M` a las 09:43 UTC y `--reserve-memory 128M` a las
10:03 UTC. Cada uno cambió un único campo del spec (`Limits.MemoryBytes` de
134217728 a 268435456 y `Reservations.MemoryBytes` de 67108864 a
134217728) y reemplazó la única tarea del edge, con un corte breve de 80/443
durante el relevo. La reserva solo cuenta para la planificación de Swarm.
Desde el apply de `edge` del 2026-09-26 el servicio vivo es el del
repositorio.

## Estado temporal fuera del repositorio

- Laboratorio AX (Google Agent Executor sobre Kubernetes kind y Agent
  Substrate). El ensayo manual de `/opt/ax-lab` se retiró el 2026-09-26, y
  el clúster, el registro, Substrate y AX los crea y gestiona ya el playbook
  `ax-lab`, con los límites del contrato de capacidad (ver «Aplicado y
  verificado»). Siguen fuera del estado reconstruible la semilla de las
  imágenes de AX y de su CLI, que solo está en el host y fuera del backup
  (las del runner y de agentes no se pueden reconstruir byte a byte), y las
  credenciales de `/etc/dockerswarm/ax` (ver [AX.md](AX.md), «Qué codifica
  este repositorio y qué sigue siendo manual»). Sale de esta lista con el
  cambio 5 de ese documento: la prueba de reinicio del nodo y su versión
  final.
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

### `ax-lab` (2026-09-25)

`--playbook ax-lab --local` desde `main` en `6b1f10f` (#67):

- `--check`: `ok=24 changed=4 failed=0`.
- `--confirm-production`: `ok=35 changed=5 failed=0`; repetido:
  `ok=35 changed=0 failed=0`. Ninguna operación dejó marker.
- `/etc/sysctl.d/99-z-dockerswarm-ax-lab.conf` es `root:root 0644` y solo
  contiene las dos claves de inotify, que siguen en `524288` y `512`. Ya no
  se pierden al reiniciar.
- `kind` v0.33.0 y `kubectl` v1.37.0 están en `/opt/dockerswarm/ax-lab/bin`
  con el sha256 de `config/ax-lab.yml`.
- `/opt/dockerswarm/deployments/ax-lab.yml` registra `6b1f10f`.
- El laboratorio manual no se tocó: 27 pods `Running` y uno `Completed`.

### `host-baseline` (2026-09-25)

`--playbook host-baseline --local --confirm-production` desde `main` en
`fd71235` (#66 y #68), en ventana exclusiva, sin otras operaciones sobre el
lock:

- Apply (13:30-13:37 UTC): `ok=210 changed=16 failed=0`, sin handlers y sin
  marker.
- El pin de APT pasa del snapshot `20260726T000000Z` al `20260924T000000Z`,
  con 19 paquetes actualizados y ninguno instalado ni retirado:
  - `openssh-server`, `openssh-client` y `openssh-sftp-server`
    `1:10.2p1-2ubuntu3.5` a `3.6`;
  - `sudo` `1.9.17p2-1ubuntu3` a `3.1`;
  - `curl`, `libcurl4t64` y `libcurl3t64-gnutls` `8.18.0-1ubuntu2.3` a
    `2.5`;
  - `apparmor` y `libapparmor1` `5.0.0~beta1-0ubuntu7` a
    `5.0.2-0ubuntu1~26.04.1`;
  - `rsyslog` `8.2512.0-1ubuntu4.1` a `4.2`;
  - `gpg` y otros ocho paquetes de GnuPG `2.4.8-4ubuntu3` a `3.1`.

  `crowdsec` 1.7.8 y su bouncer 0.0.34 no cambian.
- `fs.suid_dumpable` pasa de `2` a `0` y `kernel.core_pattern` de `core` a
  `|/bin/false`. `apport.service` queda deshabilitado e inactivo.
- La configuración efectiva de `sshd` (puerto, `AllowUsers`,
  `PermitRootLogin`, autenticación por clave y por contraseña) es idéntica a
  la de antes, y `ssh.socket` y `ssh.service` siguen activos.
- Firewall:
  - `INPUT`, `FORWARD` y `OUTPUT` siguen en `DROP` en IPv4 y en IPv6;
  - `DOCKER-USER`, `DOCKERSWARM-INGRESS` y `SFTP-SWARM` son idénticas byte a
    byte a las de antes del apply;
  - las reglas `LOG` siguen siendo 2 por familia, sin duplicar;
  - UFW sigue activo, con `logging low` y `deny` por defecto en las tres
    direcciones.
- 0 unidades fallidas. `crowdsec`, su bouncer, `fail2ban`, `docker` y
  `containerd` activos. El nodo sigue `Ready` y `Leader`, y todos los
  servicios Swarm convergidos.
- Las seis rutas públicas comprobadas responden: `n8n`, `kropia`,
  `passbolt`, `satisfactory` y `monitor` con `200`, y `organizacion` con
  `204` en `/healthz`.
- El recolector del Observatorio conserva su PID, 0 reinicios y el perfil
  `docker-default`.
- Repetido (13:37-13:41 UTC): `ok=209 changed=6 failed=0`, sin handlers ni
  avisos. Informan `changed`:
  - `Enable bounded UFW logging`;
  - las dos restauraciones de CrowdSec tras las pruebas `-t`;
  - las tres tareas del directorio temporal de la clave de CrowdSec: crearlo,
    descargar la clave y borrarlo. La tarea que instala el keyring no
    informa cambios.

### `host-baseline` converge a `changed=0` (2026-09-25)

`--playbook host-baseline --local --confirm-production` desde `main` en
`0028bca` (#71):

- Apply (17:30-17:34 UTC): `ok=219 changed=2 failed=0`. Los dos cambios son
  los metadatos del despliegue, que registran el commit nuevo. Sin handlers
  ni avisos.
- Repetido (17:34-17:37 UTC): `ok=219 changed=0 failed=0`, sin handlers ni
  avisos.
- El bouncer de CrowdSec no se reinició en ninguno de los dos: su arranque
  sigue siendo el de las 13:41:19 UTC. Ninguna prueba `-t` corrió, así que
  no hubo hueco sin filtrado.
- `-A INPUT -j CROWDSEC_CHAIN` sigue presente en IPv4 y en IPv6, y
  `DOCKER-USER`, `DOCKERSWARM-INGRESS` y `SFTP-SWARM` son idénticas byte a
  byte a las de antes del apply. Ninguna operación dejó marker.

### Laboratorio AX: ventana de los cambios 2 y 3 (2026-09-26)

`--playbook ax-lab --local` desde `main` en `bf9f8b7` (#70, fusionado como
`2b1ead5`, y #73), en ventana exclusiva y siguiendo «Ventana del
laboratorio» de [AX.md](AX.md). Horas en UTC:

- Evidencia del laboratorio manual, sin ningún secreto, en
  `/var/backups/dockerswarm/ax-lab-retirement/20260926T003215Z`. Además de
  lo que pide «Retirar el laboratorio manual», guarda una copia de
  `/opt/ax-lab/ejemplos`.
- Semilla (00:38) en `/var/backups/dockerswarm/ax-lab/images` y
  `/var/backups/dockerswarm/ax-lab/binaries`: `image-status` da `complete`
  para las seis imágenes de Substrate y las cuatro de AX, y el sha256 de la
  CLI es el de `ax.cli_sha256`. Como el cambio 4 (#76) aún no estaba
  fusionado, la semilla de AX usó su gestor desde un clon limpio del commit
  que estaba entonces en revisión. Los arreglos posteriores del cambio 4 no
  tocaron el gestor, así que la semilla es la misma. Las copias en el host
  de `ax-agents` y `ax-task-runner` del laboratorio manual se conservaron
  como reserva hasta verificar AX y se borraron a las 01:58.
- Retirada del laboratorio manual (00:38-00:39): `kind delete cluster`,
  `docker rm --force --volumes kind-registry` y `docker network rm kind`, y
  después `/opt/ax-lab`, `/usr/local/sbin/ax`, `/usr/local/sbin/ax-tarea`, la
  imagen `ax-toolbox:spike` y el volumen `ax-lab-gomod`.
- `--check` desde `bf9f8b7`: exactamente el plan documentado.
- `--confirm-production` (00:57:07-01:05:53): `ok=180 changed=21 failed=0`.
  La compilación de reserva construyó `ate-setup` en el host, en un
  contenedor de `images.toolbox`, y reprodujo el digest fijado en
  `config/ax-lab.yml`. Después creó el clúster y el registro, restauró las
  seis imágenes sembradas e instaló Substrate.
- Repetido: `ok=174 changed=0`, sin avisos.
- `image-status` de las siete imágenes de Substrate: `pinned` en el
  registro y `complete` en la copia.
- `memory.events` del nodo con `oom 0` y `oom_kill 0`, y `memory.peak` de
  2 588 610 560 bytes (2 469 MiB).
- Límites, los de `config/capacity-profiles.yml`: el nodo con 3 584 MiB de
  memoria y 1 792 MiB de reserva, 2 000m de CPU, 4 096 PIDs y reinicio `no`;
  el registro con 256 MiB de memoria y 128 MiB de reserva, 500m de CPU,
  256 PIDs y reinicio `no`.

### Edge: ventana de las rutas de Satisfactory (2026-09-26)

`--playbook edge --local` desde `main` en `e62fd93` (#74), siguiendo
«Ventana de aplicación» de [EDGE.md](EDGE.md). Horas en UTC:

- El secret `edge-basicauth-satisfactory-logs-v1` se creó a las 01:08 bajo
  el lock, desde la línea viva y sin imprimirla, con las etiquetas
  `com.apptolast.managed-by=manual-bootstrap` y
  `com.apptolast.purpose=traefik-basicauth`.
- Paso 1: `Version.Index` 147489 con la Config hecha a mano. `--check`
  limpio.
- `--confirm-production` (01:09:23-01:14:14) falló en la puerta de salud
  posterior al deploy. Con `detach: true` y `stop-first`, la puerta comprobó
  el contenedor de la tarea anterior (`62xcouqaff2i`) mientras Swarm la
  reemplazaba; la nueva, `qskoubtchffr`, estaba sana y sirviendo. Lo
  corrige #77 (`4a02d06`), que espera a que termine el relevo antes de la
  puerta.
- No se hizo el rollback de «Rollback»: el fallo era de la puerta, no del
  servicio. Antes de decidirlo se comprobó que:
  - las 17 rutas públicas respondían igual antes y después, todas con
    `verify=0`: `200` en `edge` (`/ping`), `kropia`, `minecraft-stats`,
    `monitor`, `n8n`, `organizacion`, `pablohurtadohg`, `racinggame` y
    `satisfactory`; `302` en `passbolt` y `307` en `albertohidalgo`; `401`
    en `logs-satisfactory` y en `/companions` y `/companions/` de
    `satisfactory`; `404` en `generadorcodigosqr` y en `/app/` de
    `satisfactory`, y `503` en `openclaw`, aparcado;
  - el servicio usaba la Config `edge-traefik-dynamic-8287b871c1ab8a3b`,
    los secrets `cloudflare_dns_api_token_v3` y
    `edge-basicauth-satisfactory-logs-v1` y 13 redes, con la actualización
    `completed`;
  - los logs de la tarea nueva tenían 0 líneas con `no users found` o
    `workloads_openclaw`: la sonda de OpenClaw ya no existe;
  - el fichero de usuarios dentro de la tarea era idéntico byte a byte a la
    entrada viva en línea, comparando sus sha256 sin imprimir ninguno;
  - `logs-satisfactory` respondía con
    `WWW-Authenticate: Basic realm="Satisfactory logs"`.

  Esa comparación byte a byte sustituyó al paso 7, la entrada del
  propietario: la línea es la misma, así que la contraseña también. El
  propietario puede confirmarlo con un único inicio de sesión.
- El marker que retuvo el apply fallido se recuperó con
  `scripts/ansible-operation-lock.py recover`, primero en dry-run y después
  con `--apply`.
- Repetido: `ok=110 changed=2`, solo por `deployment_metadata`, que el apply
  fallido no llegó a registrar. Repetido otra vez: `ok=110 changed=0`, con
  la misma tarea `qskoubtchffr`.

### AX: ventana del cambio 4 (2026-09-26)

`--playbook ax-lab --local` desde `main` en `b0853c3` (#76), siguiendo
«Ventana del cambio 4» de [AX.md](AX.md). Horas en UTC:

- `--check`: restaurar en el registro las cuatro imágenes de AX, aplicar del
  lado del servidor `state`, `ax-system.yaml` y `ax-workers.yaml`, y el
  `--route-timeout=1h` del router.
- `--confirm-production` (01:54:02-01:56:01): `ok=258 changed=11 failed=0`;
  repetido: `ok=251 changed=0`, sin avisos.
- `ax-controller`, `ax-redis` y `ax-server` en `ax-system`, y el worker de
  `ax-workers`, en `Running` con `1/1`.
- `sudo ax get tasks -a default` responde y no deja ningún
  `kubectl port-forward`.
- Prueba de humo (01:57:54): `sudo TURNOS=4 RAMA=master ax-tarea
  https://github.com/octocat/Hello-World "<instrucción de solo lectura>"
  claude` salió con código 0 y la respuesta del agente. Después se borraron
  la Task y su workspace, y no quedó ningún túnel.
- Nodo tras la prueba (02:15): `memory.peak` 3 631 050 752 bytes (el 96,6 %
  de los 3 584 MiB del límite), `memory.events.local` con `oom 0` y
  `oom_kill 0`, y `cpu.stat` con `nr_throttled` 923 y `throttled_usec`
  114 169 253. Los reinicios de `kube-system` (3) y `ate-system` (5) son de
  01:44-01:47, del incidente de memoria de abajo, no de este apply.
- Aislamiento de red (02:18-02:20), con una Task `CONSERVAR=1` y
  `sudo ax ssh`, comprobado a nivel de aplicación porque gVisor acepta en
  local una conexión TCP aunque su destino no responda:
  - `ax-server` `/healthz`: sin respuesta (curl código 28);
  - `ax-redis`: un `PING` no obtiene respuesta;
  - control positivo: `getent hosts github.com` resuelve y
    `https://github.com` responde `200`.

  También responden desde el sandbox: `kind-registry:5000/v2/` (`200`),
  `https://10.96.0.1/version` (`200`, anónimo) y `rustfs.ate-system.svc:9000`
  (`403`, pide credenciales). Después se borraron la Task y su workspace.

### Panel web de AX: ventana 2 (2026-09-26)

Ruta de Traefik (#79) y despliegue del panel (#78), desde `main` en
`499feba`, siguiendo [AX_WEB.md](AX_WEB.md), «Ventana 2, parte del
laboratorio», y [EDGE.md](EDGE.md), «Ruta de AX». Horas en UTC:

- Imagen `ax-web` sembrada con `seed-layout` desde el layout OCI de la CI
  (digest fijado, cada blob verificado).
- `ax-web-bootstrap.sh init` (02:21): Docker Secrets
  `edge-ax-upstream-client-v1` y `edge-ax-upstream-ca-v1` y el material del
  panel en `/etc/dockerswarm/ax/web-tls` (`0700`/`0600`). Los tres
  certificados caducan el 2029-09-25 02:21:01 GMT.
- `edge-basicauth-ax-v1` (02:24): una línea `admin:` más el hash bcrypt de
  coste 10, creada por tubería hacia `docker secret create`; solo se vieron
  el prefijo `$2` y la longitud 60.
- Paso 1: `Version.Index` 147765 con las Configs de la ventana de E1 (sube
  2 por los dos applies repetidos de esa ventana).
- `edge --check` limpio; apply (02:55:05-02:57:03) `ok=114 changed=9
  failed=0`, sin avisos. La puerta de salud esperó al relevo (#77).
  - Las 17 rutas anteriores responden igual que antes.
  - `https://ax.apptolast.com` responde `401` con `realm="AX"` y el
    certificado de Let's Encrypt verificado.
  - Red `apptolast-edge-ax`: overlay cifrada y `attachable` con
    `10.0.250.0/24`.
  - Repetido: `changed=1` (solo el registro de invocación, porque entre
    medias corrió `ax-lab`) y después `ok=112 changed=0`, con la misma
    tarea `qqe6aa3litgu`.
- `ax-lab --check` con el plan documentado. El primer apply creó el espacio
  de nombres, aplicó el manifiesto y se detuvo pidiendo
  `ax-web-bootstrap.sh k8s`, como está previsto. El marker se recuperó con
  `ansible-operation-lock.py recover`, `k8s` creó los Secrets `ax-web-tls` y
  `ax-web-agent`, y el segundo apply (hasta 03:02:19) dio `ok=338 changed=7
  failed=0`: reenviador `ax-web-edge` en `kind` y `apptolast-edge-ax`, y
  `web.json` instalado. Repetido: `ok=331 changed=0`, sin avisos.
- Verificación contra la IP pública:
  - sin credenciales, `/` responde `401` y `/healthz` `200` (Traefik, mTLS,
    reenviador y panel de extremo a extremo);
  - con el usuario del propietario, `/` `200` y `/api/status` con el gestor
    listo;
  - ejecución real desde la API del panel (03:02): la Task
    `web-20260926-030232` corrió Claude sobre `octocat/Hello-World`, la
    salida llegó por SSE, terminó con éxito en 3 turnos (0,0358 USD) y la
    Task y su workspace se borraron solos.
- Decisiones del propietario, delegadas («Si a todo, autorizo todo, seguiré
  todas tus recomendaciones») y tomadas según la recomendación:
  - se acepta `GET /healthz` sin login (router `ax-health`);
  - el panel se publica ya con los límites de Traefik, y el bloqueo de IPs
    tras fallos de login (PR-S) llega después.

### Incidentes de carga (2026-09-26)

La carga de la orquestación de estas ventanas causó dos incidentes. Horas en
UTC:

- 00:24-00:35: las baterías de pruebas de varios agentes en paralelo
  llevaron la carga del host a unos 97 con 8 CPU. Traefik falló su
  healthcheck y se reinició dos veces. A las 00:34:03 venció el heartbeat
  del agente de Swarm (`DeadlineExceeded`): la sesión se volvió a registrar
  y Swarm reinició todas sus tareas, con 1-2 min de corte en todos los
  sitios. Todos los servicios volvieron a converger salvo
  `autoupdater_shepherd`, que esperó su retardo de reinicio de 1 h.
  Mitigación: los procesos de la orquestación corren con `nice 19` e
  `ionice` idle, hay menos flujos concurrentes y ningún agente ejecuta la
  batería completa.
- 01:45:44: presión de memoria (journald, «Under memory pressure»). `/tmp`
  es un tmpfs de 7,9 G y el espacio temporal de la orquestación ocupaba
  4,1 GB, además del nodo kind nuevo. La caché de páginas de la tarea de
  Traefik entró en thrashing: leyó 43,7 GB de disco. Swarm la reemplazó a
  las 01:47:31 por `1jp7fbzy6kqw`, sana. El espacio temporal pasó a disco y
  `MemAvailable` volvió a unos 5,3 GB. A esa hora la carga llegó a unos 168
  por las pruebas de uno de los flujos en paralelo.

A las 02:00, de solo lectura (`docker service ls` y
`docker service ps edge_traefik`), todo está convergido: cada servicio en
`1/1`, `autoupdater_shepherd` incluido, salvo Minecraft y OpenClaw, aparcados
en `0/0`. `edge_traefik` corre la tarea `1jp7fbzy6kqw`, sana y con la Config
`edge-traefik-dynamic-8287b871c1ab8a3b`, y `logs-satisfactory` sigue
respondiendo `401` con su realm.

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
