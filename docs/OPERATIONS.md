# Operación y seguridad

## Topología

El clúster tiene un único manager/worker y una interfaz pública. Permite
stacks, secrets, configs y overlays, pero tolera cero fallos del manager.

Los puertos internos de Swarm son:

| Tráfico | Uso |
| --- | --- |
| `2377/TCP` | plano de control |
| `7946/TCP+UDP` | descubrimiento |
| `4789/UDP` | VXLAN |

No se autorizan desde Internet. Un segundo nodo exige red privada/túnel,
direcciones estables y quorum revisado.

Docker puede saltarse el procesamiento normal de UFW. La política combina
Netcup, UFW y `DOCKER-USER`; CrowdSec ocupa el primer salto y
`DOCKERSWARM-INGRESS` el segundo.

## Modelo de acceso

El grupo `docker` equivale a root. El contrato final elimina a todos los
usuarios humanos, incluido `admin`, de ese grupo.

- validación de repo/CI: usuario sin privilegios;
- Ansible remoto: `admin` con `--ask-become-pass`;
- Ansible local: `--local`, que eleva supervisor y Ansible juntos;
- diagnóstico Docker en el host: `sudo -- docker ...`;
- helpers productivos que administran Docker: `sudo -- ./scripts/...`.

No se guardan contraseñas sudo en inventario, variables, shell history ni Git.

Excepción aceptada por el owner: el servicio `autoupdater_shepherd` monta
`/var/run/docker.sock` en el único manager. El bind es de solo lectura, pero
eso no limita la API Docker: el servicio equivale a root. Es el único stack
al que los validadores permiten el socket, su contrato está fijado en
`config/autoupdater.yml` y solo actúa sobre servicios con la etiqueta
`apptolast.autoupdate=true`. Se detiene con `enabled: false` y
`--playbook autoupdater`; `docker service scale` a mano solo en emergencia y
codificado el mismo día. Ver [AUTOUPDATE.md](AUTOUPDATE.md).

## Bloqueo de cambios Ansible

El bootstrap fresco y todos los targets de `deploy-ansible.sh` usan el mismo
inode:

```text
/run/lock/dockerswarm-iac.lock
```

El inode es `1001:1001 0600`. Bootstrap lo crea como root y lo transfiere al UID
revisado. Cada scope usa un marker distinto:

```text
/run/lock/dockerswarm-bootstrap.marker
/run/lock/dockerswarm-ansible.marker
```

El marker liga nonce de 256 bits, commit, contrato, perfil, modo, controlador y
PID holder. Un crash, EOF, pérdida del holder, fallo Ansible, cambio de `HEAD`
o worktree sucio deja el marker fail-closed. No se borra por edad ni al volver
a ejecutar.

El supervisor:

- conserva una PTY real para prompts;
- comprueba continuamente el holder;
- termina el grupo completo al perderlo;
- actúa como subreaper y rechaza descendientes que intenten escapar con
  `setsid`;
- en modo local se eleva antes de lanzar Ansible, por lo que también puede
  terminar descendientes root.

Los instaladores directos de secretos, GC, scripts de migración y
`backupctl` no están serializados por este mutex común. Deben ejecutarse en una
ventana exclusiva, con ambos markers ausentes y sin Ansible activo. Cada helper
con lock propio conserva además su exclusión específica.

## Recuperar un marker abandonado

Solo después de demostrar que el controlador original está detenido y que no
hay otra mutación:

```bash
sudo -- install -d -o root -g root -m 0700 \
  /var/backups/dockerswarm

sudo -- /usr/bin/python3 scripts/ansible-operation-lock.py \
  recover \
  --operation-id ID_64_HEX
```

El dry-run inspecciona inode/marker, intenta adquirir el lock común, busca
mutadores visibles y muestra una confirmación exacta ligada al SHA-256. Para
aplicar se repite con:

```bash
sudo -- /usr/bin/python3 scripts/ansible-operation-lock.py \
  recover \
  --operation-id ID_64_HEX \
  --apply \
  --confirm 'CONFIRMACION_EXACTA_MOSTRADA'
```

La búsqueda de mutadores cuenta como tal todo cliente `docker stack`,
`service`, `swarm` o `node`, también detrás de un `timeout` (coreutils o
busybox, con `-s`/`-k`) y de opciones globales de Docker (`--config`, `-c`,
`--context`, `-H`/`--host`, `-l`/`--log-level`, `--tls*`, `-D`/`--debug`,
incluida la forma `--opcion=valor`). Así detecta el
`timeout 900 docker service update ...` del vigilante de imágenes, con o sin
`--config`. Mientras el vigilante actualiza, `recover` se niega; hay que
esperar y repetir el dry-run. El marker `direct` se recupera con otro helper
que no busca mutadores.

Límites de esa búsqueda:

- Solo reconoce el cliente directo y los envoltorios `timeout` y
  `busybox timeout`. Un `docker service update` lanzado detrás de `sudo`,
  `env`, `nice`, `nohup`, `setsid` o `sh -c` no cuenta; confirma a mano con
  `ps -eo args` que no hay ninguno.
- Una opción que no sabe interpretar cuenta como mutador si detrás aparece
  `stack`, `service`, `swarm` o `node` en cualquier posición, aunque sea el
  valor de otra opción (`docker --opcion-rara ps --filter node`). Ese falso
  positivo solo obliga a esperar y repetir el dry-run.
- El proceso no basta como prueba: Swarm sigue el update en el servidor tras
  morir el cliente. Antes se revisa `UpdateStatus` de los servicios activados
  (ver «Riesgos aceptados» en [AUTOUPDATE.md](AUTOUPDATE.md)).

Para un marker de bootstrap se añaden:

```text
--marker-path /run/lock/dockerswarm-bootstrap.marker
--owner-uid 0
--owner-gid 0
```

La evidencia se archiva antes de retirar el marker. Se usa, si es posible, el
helper del mismo commit registrado. Un reboot mata procesos pero elimina
`/run`; primero debe conservarse la evidencia cuando todavía sea accesible.

### Apply rechazado por un update en curso

`operation_lock_guard` corre en todos los playbooks con lock, sin variable
que lo desactive. Un apply que empieza mientras un servicio con
`apptolast.autoupdate=true` está en `updating` o `rollback_started` espera
hasta 36 × 10 s por servicio a que termine y, si sigue, falla antes de
mutar y deja su marker. Swarm no registra quién empezó el update:
puede ser el vigilante o un apply anterior. Los roles de stack despliegan
con `detach: true`, así que un apply que cambió uno de esos servicios
termina con el update aún dentro de su ventana `monitor` (120 s en
`workloads`, 90 s en `edge`); la espera del guard cubre ese plazo. Si aun
así falla, cuando el update termine recupera el marker con el procedimiento
de arriba y repite el apply.

Un update que nunca termina bloquea todos los applies, también el PR de
hold que lo arreglaría. Se reconoce porque `UpdateStatus.StartedAt` es más
antiguo que el `delay` más la ventana `monitor` de su stack y
`sudo -- docker service ps <servicio>` muestra la tarea nueva parada en
`pending`, `preparing` o `assigned`. La salida manual, auditable y sin
`docker service rollback` (ver «Interruptor y rollback» en
[AUTOUPDATE.md](AUTOUPDATE.md)), es:

1. Detén el vigilante con la parada de emergencia documentada:
   `sudo -- docker service scale autoupdater_shepherd=0`.
2. Recupera el marker retenido (dry-run y `--apply` de arriba).
3. Bajo el lock directo, fija en ese servicio el último digest bueno (ver
   «Rollback de un servicio» en [AUTOUPDATE.md](AUTOUPDATE.md)):

   ```bash
   sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
     --operation autoupdate-stuck-update -- \
     /usr/bin/docker service update --detach \
     --image 'repo:tag@sha256:ULTIMO_BUENO' SERVICIO
   ```

4. Espera a que su `UpdateStatus.State` salga de `updating` y aplica ese
   mismo día el PR de hold de la entrada y el PR con `enabled: false` del
   vigilante, cada uno con su playbook.

### Swarm parado o bloqueado

Esa misma comprobación falla cerrado si Docker no responde (`docker info`
devuelve el error de Docker) o si `LocalNodeState` no es `inactive` ni
`active` (`pending`, `locked`, `error`). Pasa también con
`--playbook platform`, que ya no puede arrancar ni desbloquear Docker desde
Git. La salida es manual:

1. Arranca Docker con `sudo -- systemctl start docker.service`. Con el nodo
   en `locked`, desbloquéalo con
   `sudo -- systemctl start dockerswarm-swarm-unlock.service` si existe el
   escrow local o, si no, con `sudo -- docker swarm unlock` y la clave del
   gestor de secretos (ver [BACKUP_RECOVERY.md](BACKUP_RECOVERY.md)). Nunca
   ejecutes `docker swarm update --autolock=true`.
2. Comprueba que
   `sudo -- docker info --format '{{.Swarm.LocalNodeState}}'` dice `active`.
3. Recupera el marker retenido y repite el apply.

## Secuencia de cambio

1. Actualizar contratos en una rama. Lo que ejecuta cada servicio se declara
   en `config/image-channels.yml` como canal revisado (`repo:tag`) o como
   hold (`repo:tag@sha256:...`); `config/services.yml` conserva el digest
   ligado al marcador de restauración y no se edita para actualizar
   imágenes. El preflight de una ejecución real resuelve cada canal del
   stack a su digest actual, exige `linux/amd64` y lo descarga antes de mutar
   el stack. El CLI vuelve a resolver el canal durante el deploy, así que el
   apply exige después que cada servicio ejecute ese digest verificado o
   conserve el que ya tenía. OrganizationWeb, fuera del preflight, verifica la
   imagen desplegada. Ver [AUTOUPDATE.md](AUTOUPDATE.md).
2. Ejecutar validación, lint y escaneo de secretos.
3. Revisar y hacer commit; ningún writer acepta worktree sucio.
4. Crear el plan Terraform firmado con locking/state proof válidos.
5. Aplicar solo mediante `apply-terraform.sh`; conservar snapshots y evidencia
   mientras el lock remoto sigue ligado a la operación.
6. Aplicar Ansible mediante `deploy-ansible.sh`.
7. Repetir Ansible y exigir `changed=0` (si un servicio activado sigue en
   `updating`, el guard espera; ver «Apply rechazado por un update en
   curso»).
8. Validar firewall, servicios, TLS, DNS, logs, backups y unidades fallidas.
9. Registrar aceptación y rollback.

Por decisión del owner (2026-09-11), el objeto revisado es el canal, no el
digest. Los stacks se despliegan con `resolve_image: changed`: un servicio
cuya imagen renderizada no cambió conserva su digest vivo, incluido el que
haya aplicado el vigilante de canales, de modo que el paso 7 (`changed=0`)
sigue valiendo para un segundo apply consecutivo desde el mismo commit. Tras
una actualización del vigilante, el apply siguiente reescribe
`observed-images.yml` (`changed` en esa tarea, no en `docker_stack`). El
primer apply tras introducir los canales informa `changed` en `docker_stack`
por la etiqueta nueva `apptolast.autoupdate`, sin reiniciar tareas. Solo un
servicio con `autoupdate: true` puede cambiar de
digest entre applies, y solo mediante ese vigilante revisado en este
repositorio (stack `autoupdater`, playbook `autoupdater`). En la primera
adopción, `autoupdater` se aplica antes que `edge`, `workloads` y
`organizationweb`, todo desde el mismo checkout; el orden completo está en
[`AUTOUPDATE.md`](AUTOUPDATE.md) («Aplicar el registro»). En `site` el rol
sigue ejecutándose tras `edge`, `workloads` y `observability`.
`ansible-playbook` contra el clúster real sigue siendo una acción humana y
ningún push a Docker Hub puede iniciarlo por sí solo.

Los writers Terraform y Ansible tienen fronteras distintas. No se ejecutan en
paralelo si afectan al mismo servidor o ventana de cutover.

## Aparcar un servicio

`platform_parked_workloads` (`config/platform.yml`) detiene servicios del
stack `workloads` sin borrar nada: se renderizan con `replicas: 0` y conservan
imagen, datos bajo `/srv/dockerswarm/services`, secretos, redes, ruta del edge
y presupuesto de capacidad. La lista va ordenada, sin duplicados, se escribe
`[]` cuando no queda ningún servicio aparcado (una clave vacía es `null` y
todas las capas la rechazan) y solo admite `minecraft` y `openclaw`, los dos
servicios que ningún otro necesita para funcionar: `minecraft-stats` solo lee
el mundo de Minecraft en modo lectura y sigue sirviendo las últimas
estadísticas. Aparcar una base de datos dejaría a sus consumidores sin
backend, así que los validadores lo rechazan.

Qué cambia en cada capa mientras un servicio está aparcado:

- `workloads`: la convergencia exige `0/0` y ninguna tarea viva del servicio;
  las comprobaciones de salud recorren solo los servicios en marcha y el
  smoke de OpenClaw espera el `503` del edge. El helper de publicación de n8n
  (`migration/scripts/manage_n8n_workflows.py`) acepta `0/0` solo para los
  servicios aparcados.
- `edge` (solo OpenClaw): el router y su certificado siguen, pero el backend
  no tiene servidores ni sonda y Traefik responde `503 no available server`
  sin registrar nada. Con la sonda activa y sin tarea, Traefik registra un
  WARN `Health check failed.` en cada intervalo de 15 s (ver
  [EDGE.md](EDGE.md)).
- Minecraft conserva su compuerta pública y UFW sigue admitiendo el 25565.
  Con la tarea en marcha, dockerd reserva ese puerto (escucha en `0.0.0.0`) y
  lo redirige al contenedor. Aparcado no hay reserva ni redirección y el
  tráfico llega al propio host: el apply de `workloads` falla si algún
  proceso escucha entonces en el 25565, y sin proceso el kernel rechaza la
  conexión. Cerrar el puerto en el firewall mientras Minecraft está aparcado
  exigiría hacer la lista efectiva de puertos consciente del aparcado y
  aplicar `platform` y `host-baseline`, que hoy aplicarían además el snapshot
  de paquetes pendiente; queda como cambio aparte.
- `observability`: no se renderizan la sonda TCP de Minecraft ni la sonda
  HTTPS pública de OpenClaw; la regla `MinecraftEndpointDown` sigue cargada
  sin series.
- `backup`: copia los datos en reposo, sin detener nada ni usar RCON (ver
  [BACKUP_RECOVERY.md](BACKUP_RECOVERY.md), «Servicios aparcados»).
- Capacidad: el presupuesto sigue reservado, así que desaparcar no requiere
  revisión de capacidad; lo que se libera es la RAM y CPU reales del host.

Para aparcar o desaparcar se edita la lista y se sigue la secuencia de
cambio. En los dos sentidos se aplica primero `edge` y después `workloads`: el
smoke de `workloads` exige a OpenClaw el `503` del edge si está aparcado y el
`200` si no lo está, así que el edge ya debe servir el backend que
corresponde. Al desaparcar, Traefik sondea OpenClaw desde el apply de `edge`
y registra ese WARN hasta que el de `workloads` arranca la tarea. Si
`observability` o `backup` están desplegados, se aplican también para
renderizar la lista nueva. Cada apply de `edge` que cambia la configuración
dinámica reemplaza la tarea de Traefik (`stop-first`), con un corte breve de
todas las rutas públicas.

Mientras no exista el backup externo (ver «Backup y autolock»), al aparcar
se archiva el estado en frío en el propio host en cuanto el servicio está en
`0/0` (datos en reposo), bajo el lock host-global, y se registra su SHA-256.
Las rutas son `minecraft/data` y `minecraft/mods` para Minecraft y
`openclaw-clean/home` para OpenClaw:

```bash
sudo -- install -d -o root -g root -m 0700 /var/backups/dockerswarm/parked
sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
  --operation parked-cold-archive -- \
  /bin/sh -c 'umask 077 && exec /usr/bin/tar --create --zstd \
    --numeric-owner --acls --xattrs \
    --file /var/backups/dockerswarm/parked/minecraft-cold-UTC.tar.zst \
    -C /srv/dockerswarm/services minecraft/data minecraft/mods'
sudo -- tar --list --zstd \
  --file /var/backups/dockerswarm/parked/minecraft-cold-UTC.tar.zst >/dev/null
sudo -- sha256sum /var/backups/dockerswarm/parked/minecraft-cold-UTC.tar.zst
```

El archivo es `0600 root:root` dentro de un directorio `0700` y contiene el
estado en claro, incluido el de OpenClaw. Convive con los datos en el mismo
disco: protege frente a un error al desaparcar, no frente a la pérdida del
servidor. Se conserva hasta que el servicio vuelve a estar en marcha y sano y
existe un backup externo verificado que lo cubra; entonces se borra. Para
restaurar, con el servicio aún aparcado, se extrae en un directorio vacío de
staging, se compara con el dataset y solo después se sustituye el dataset
completo, sin extraer nunca sobre datos vivos.

## Reinicios

Antes:

1. confirmar ventana y consola fuera de banda;
2. ejecutar
   `sudo -- dockerd --validate --config-file=/etc/docker/daemon.json`;
3. verificar un backup reciente y que la unlock key externa está disponible si
   autolock está activo.

Después:

1. `sudo -- systemctl is-active docker`;
2. `sudo -- docker node ls`;
3. comprobar manager `Ready`, `Active`, `Leader`;
4. revisar journal desde el instante del reinicio;
5. comprobar réplicas, healthchecks, rutas y alertas.

`live-restore` no conserva el plano de control de Swarm durante un reinicio de
Docker.

## Logging

El daemon usa `local` por defecto. Sus ficheros internos no se manipulan:
se consultan mediante `sudo -- docker logs`.

`docker service logs` requiere `json-file` o `journald`; cada servicio que lo
necesita declara su driver y rotación. Iptables rota mediante rsyslog y el
helper soportado de Ubuntu 26.04.

## Backup y autolock

La automatización de backup existe y está versionada, pero permanece
desactivada hasta disponer de R2, contraseña restic, escrow externo y restore
probado. No se confunde “timer codificado” con “copia productiva existente”.

Autolock sigue desactivado. No se ejecuta manualmente
`docker swarm update --autolock=true`: la salida contiene la única unlock key y
un crash antes de custodiarla puede bloquear el manager. La activación está en
`STOP` hasta integrar un destino externo, escribir/verificar la clave y ensayar
el arranque como una operación aprobada.

Cuando se active, la copia fría de Raft detendrá Docker, verificará el escrow,
restaurará el mismo Swarm ID y subirá el artefacto solo después de recuperar el
daemon. El detalle está en
[`BACKUP_RECOVERY.md`](BACKUP_RECOVERY.md).

## Mantenimiento

- semanal: disco/inodos, unidades, certificados, backups y markers;
- mensual: drift Terraform, usuarios/claves, rotación y restore de aplicación;
- trimestral: recuperación de state, ACME y Raft en un host aislado;
- antes de ampliar el Swarm: red privada, quorum impar, capacidad, backup y
  prueba de pérdida de manager.
