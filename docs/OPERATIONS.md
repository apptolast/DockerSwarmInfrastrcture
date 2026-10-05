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

### SSH por NetBird

Desde el 2026-10-04 el propietario entra por SSH a través de NetBird Cloud
(plan gratuito), una red WireGuard gestionada: la dirección privada del host
no cambia aunque cambie la IP pública del propietario. El SSH público del
puerto 22 sigue igual (solo clave, `AllowUsers admin`, `22/tcp LIMIT`) y es,
con la consola de Netcup, la vía de reserva. El cliente NetBird del host se
instaló a mano y este repositorio todavía no lo gestiona (ver
[DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md), «Deriva fuera del
repositorio»).

Efecto sobre la frontera del host:

- NetBird inserta `-A INPUT -i wt0 -j ACCEPT` en la primera posición de
  `INPUT` al arrancar. El 2026-10-04 quedaba antes de `CROWDSEC_CHAIN`,
  `PSAD_BLOCK_INPUT` y las cadenas de UFW, así que lo que entraba por `wt0`
  no pasaba por UFW, por el límite de `22/tcp`, por Fail2ban (que banea con
  UFW) ni por CrowdSec. Ese orden depende de quién se insertó el último: el
  bouncer de CrowdSec también se coloca el primero cada vez que arranca y
  nada ordena las unidades entre sí. Tras un reinicio del host, del bouncer o
  de UFW, otra cadena puede quedar delante, y entonces un baneo de CrowdSec o
  de Fail2ban sí corta `wt0`. La red NetBird no cabe en
  `/etc/dockerswarm/crowdsec/trusted-ips`, que solo admite direcciones
  públicas de `/24` o más estrechas; ese baneo se retira como en «Si el
  propietario queda baneado», por SSH público o por la consola de Netcup.
  `sudo -- iptables -S INPUT | head -n 4` muestra el orden vigente.
- El único filtro propio de `wt0` son las tablas nftables `ip netbird` e
  `ip6 netbird`, con el mismo esquema en IPv4 e IPv6: en el hook `input`
  aceptan lo establecido y lo que permiten las políticas del panel de
  NetBird, y descartan el resto. Desde el 2026-10-05 sólo está habilitada
  `owner-ssh-to-apptolast-vps`: TCP/22, no bidireccional, del único PC en
  `owner-ssh-clients` al único VPS en `apptolast-vps-ssh`. `Default` está
  deshabilitada; no hay otra política habilitada en la lectura posterior.
  Se verificaron la API y las reglas efectivas IPv4/IPv6, pero falta una
  conexión SSH nueva desde el PC. Configuración, reconstrucción y rollback
  en [NETBIRD_ACCESS.md](NETBIRD_ACCESS.md).
- NetBird también añade `-A FORWARD -i wt0 -j ACCEPT` antes de `DOCKER-USER`.
  Lo que llegue por `wt0` a un puerto publicado por Docker no pasaría por
  `CROWDSEC_CHAIN` ni por `DOCKERSWARM-INGRESS`; el hook `forward` de
  NetBird descarta las conexiones nuevas sin permiso. Acepta lo establecido
  y una marca que sólo se asigna al TCP/22 autorizado con destino local;
  esa marca no habilita puertos publicados de aplicación. Una ruta de red
  o un exit node en el panel requiere revisar esa frontera de nuevo.
- El servidor SSH propio de NetBird está apagado (`SSH Server: Disabled` en
  `sudo -- netbird status`): `netbird up` nunca lleva `--allow-server-ssh`.
- `DOCKER-USER` no cambia: NetBird no la toca y conserva el orden
  `CROWDSEC_CHAIN`, `DOCKERSWARM-INGRESS`.
- La salida sigue siendo la del contrato `host_security_required_host_egress`.
  NetBird usa TCP/443 para gestión, señalización y relay, y UDP/443 para
  STUN. Sin UDP de salida para WireGuard la conexión va por relay, que basta
  para SSH; una conexión directa exige ampliar ese contrato con un cambio
  revisado.
- `PerSourcePenalties` de `sshd` también se aplica dentro de NetBird en
  cuanto `host-baseline` retire la exención manual del 2026-10-04. Una
  conexión que aborta antes de autenticarse, como un cliente que rechaza una
  huella de host nueva, suma penalización a su dirección NetBird.

La caducidad de sesión de los peers registrados con SSO está desactivada en
toda la cuenta (Settings, Authentication, «Peer Session Expiration»), también
para los dispositivos del propietario. Con ella activa, el host sale de la
red cada 24 horas hasta un nuevo inicio de sesión. Registrar el host con una
setup key de un solo uso, que nunca entra en este repositorio, permitiría
reactivarla para los demás peers.

En un host reconstruido: instalar `netbird=0.80.0` desde
`https://pkgs.netbird.io/debian` con la clave de huella
`EFE37DF047DF7CCDF1FC54FA83F79AD029778355`, ejecutar `sudo -- netbird up` y
completar el inicio de sesión en el navegador con la cuenta del propietario.
Ninguna setup key ni token entra en este repositorio. El host reconstruido es
un peer nuevo con otra dirección NetBird: se borra el peer anterior en el
panel, el nuevo entra en el grupo de la política `TCP 22` y los clientes
cambian su dirección. Esos clientes aceptan una vez la huella SSH del host,
que es la misma que la de la IP pública si se conservan las claves de host.

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

Todo apply de `edge` que cambie la Config dinámica, un secret o una red
reemplaza la única tarea de Traefik: unos 13 s sin conexiones nuevas en
80/443 para todos los hostnames y cortes en los WebSocket y SSE abiertos. Se
hace en una ventana, con el estado de cada ruta pública registrado antes y
comparado después. `docker service rollback edge_traefik` vuelve a los
nombres de Config anteriores solo hasta el siguiente apply de `edge`:
`docker stack deploy` actualiza el servicio aunque no cambie y guarda el spec
en uso como `PreviousSpec`. Toda comprobación que pueda pedir un rollback va
antes de repetir el apply; después, la vuelta atrás es un PR revisado y otro
apply. Si el apply falló, su marker sigue presente y bloquea el lock: el
rollback va sin lock y el marker se recupera antes de cualquier otro paso. El
procedimiento del primer apply tras codificar Satisfactory está en
[EDGE.md](EDGE.md) («Ventana de aplicación»), el de la ruta de AX, en
«Ventana de aplicación de la ruta», y el del log de acceso de Traefik, en
«Ventana del log de acceso».

## Aparcar un servicio

`platform_parked_workloads` (`config/platform.yml`) detiene servicios del
stack `workloads` sin borrar nada: se renderizan con `replicas: 0` y conservan
imagen, datos bajo `/srv/dockerswarm/services`, secretos, redes y ruta del
edge, pero liberan su presupuesto de capacidad. La lista va ordenada, sin
duplicados, se escribe `[]` cuando no queda ningún servicio aparcado (una
clave vacía es `null` y todas las capas la rechazan) y solo admite
`minecraft` y `openclaw`, los dos servicios que ningún otro necesita para
funcionar: `minecraft-stats` solo lee el mundo de Minecraft en modo lectura y
sigue sirviendo las últimas estadísticas. Aparcar una base de datos dejaría a
sus consumidores sin backend, así que los validadores lo rechazan.

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
- Capacidad: el servicio deja de contar en `config/capacity.yml` y
  `config/capacity-profiles.yml` (ver [CAPACITY.md](CAPACITY.md)), así que
  su RAM y su CPU quedan libres también en el contrato. Desaparcarlo exige
  devolver su reserva y su límite a esos dos ficheros en el mismo cambio, y
  el validador comprueba que el plan sigue cabiendo en el host. Con los
  stacks externos declarados hoy, ni Minecraft ni los dos juntos caben sin
  una decisión de capacidad previa. Mientras está aparcado, el preflight de
  cualquier playbook salvo `workloads` y `site` falla si el servicio corre.

Para aparcar o desaparcar se edita la lista y se sigue la secuencia de
cambio. Desde que la lista declara un servicio aparcado, el preflight de
capacidad rechaza cualquier playbook salvo `workloads` y `site` mientras ese
servicio siga en marcha, así que el orden es:

- Para aparcar: `workloads` primero (lleva el servicio a `0/0`), después
  `edge` (retira la sonda de OpenClaw) y después `observability` si está
  desplegado (retira sus sondas). Hasta el apply de `edge`, la sonda de salud
  del Traefik vivo marca OpenClaw caído, su ruta responde igualmente `503` y
  Traefik registra un WARN cada 15 s. Si `observability` está desplegado,
  `MinecraftEndpointDown` y `PublicEndpointDown` pueden disparar durante
  esos minutos.
- Para desaparcar: `edge` (repone el backend y la sonda), después
  `workloads` (arranca la tarea; su smoke exige el `200` que ya sirve el
  edge) y después `observability` si está desplegado. Traefik registra el
  WARN de la sonda entre los dos primeros applies.

Si `backup` está desplegado, se aplica también para renderizar la lista
nueva. Cada apply de `edge` que cambia la configuración dinámica reemplaza la
tarea de Traefik (`stop-first`), con un corte breve de todas las rutas
públicas.

La compuerta del 25565 solo se comprueba durante un apply de `workloads`.
Entre dos applies, un proceso local sin privilegios podría escuchar en ese
puerto y quedar expuesto a Internet. El propietario acepta ese hueco hasta que
el firewall cierre el puerto mientras Minecraft está aparcado, y tampoco hay
alerta si alguien arranca a mano un servicio aparcado. Los dos seguimientos
figuran en [DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md).

Mientras no exista el backup externo (ver «Backup y autolock»), al aparcar
se archiva el estado en frío en el propio host, bajo el lock host-global, en
cuanto el servicio está en `0/0` (datos en reposo), y se registra su SHA-256.
Las rutas son `minecraft/data` y `minecraft/mods` para Minecraft y
`openclaw-clean/home` para OpenClaw. El nombre lleva la hora UTC, fijada una
sola vez, y la orden se niega a sobrescribir un archivo existente:

```bash
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
sudo -- install -d -o root -g root -m 0700 /var/backups/dockerswarm/parked
sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
  --operation parked-cold-archive -- \
  /bin/sh -c 'umask 077 && out="$1" && shift &&
    test ! -e "$out" && exec /usr/bin/tar --create --zstd \
    --numeric-owner --acls --xattrs --file "$out" \
    -C /srv/dockerswarm/services "$@"' sh \
  "/var/backups/dockerswarm/parked/minecraft-cold-${stamp}.tar.zst" \
  minecraft/data minecraft/mods
sudo -- tar --list --zstd \
  --file "/var/backups/dockerswarm/parked/minecraft-cold-${stamp}.tar.zst" \
  >/dev/null
sudo -- sha256sum \
  "/var/backups/dockerswarm/parked/minecraft-cold-${stamp}.tar.zst"
```

Para OpenClaw se repite con `openclaw-cold-${stamp}.tar.zst` y la ruta
`openclaw-clean/home`.

El archivo es `0600 root:root` dentro de un directorio `0700` y contiene el
estado en claro, incluido el de OpenClaw. Convive con los datos en el mismo
disco: protege frente a un error al desaparcar, no frente a la pérdida del
servidor. Se conserva hasta que el servicio vuelve a estar en marcha y sano y
existe un backup externo verificado que lo cubra; entonces se borra. Para
restaurar, con el servicio aún aparcado, se extrae en un directorio vacío de
staging, se compara con el dataset y solo después se sustituye el dataset
completo, sin extraer nunca sobre datos vivos.

## CrowdSec y los 401 de Traefik

Traefik escribe su log de acceso en `/var/log/dockerswarm/edge/access.log`
y CrowdSec lo lee como fichero, igual que `auth.log`, nunca por la API de
Docker ([EDGE.md](EDGE.md), «Log de acceso en fichero»). Con el parser fijado
`crowdsecurity/traefik-logs`, el escenario local
`apptolast/traefik-basicauth-bf` cuenta por IP de origen las respuestas
`401` de los routers `ax@file` y `satisfactory-logs@file`: el undécimo `401`
en ráfaga (se vacía uno por minuto) banea esa IP 30 minutos, no las 4 h del
perfil por defecto. El baneo corta también SSH, porque se aplica en `INPUT`
y `DOCKER-USER`. El diseño y sus fuentes están en
[`host_security/README.md`](../ansible/roles/host_security/README.md).

Una caída de Docker ya no afecta a CrowdSec: ni a su arranque ni a la
lectura de `auth.log`, y no pide ningún paso después.

### Direcciones que nunca se banean

Viven solo en el host, en `/etc/dockerswarm/crowdsec/trusted-ips`
(`root:root 0600`, directorio `0700`), nunca en este repositorio público. Una
IP o red pública por línea, no más ancha que `/24` en IPv4 ni `/48` en IPv6;
las líneas vacías y las que empiezan por `#` se ignoran. Todo apply que
ejecuta `host_security` (`host-baseline`, `platform`, `site`) deja la
allowlist `apptolast-trusted` de CrowdSec exactamente igual que el fichero:

- si el fichero no existe y la allowlist tiene entradas, el apply se detiene
  antes de cambiar nada de CrowdSec (la comprobación previa corre antes del
  primer cambio del Hub, de las fuentes o del perfil);
- un fichero vacío o solo con comentarios vacía la allowlist;
- una entrada añadida a mano con `cscli allowlists add` se retira en el
  siguiente apply, y cualquier otra allowlist detiene el apply.

Crear el fichero la primera vez, en un terminal SSH propio:

```bash
sudo -- install -d -o root -g root -m 0700 /etc/dockerswarm/crowdsec
sudo -- install -o root -g root -m 0600 /dev/null \
  /etc/dockerswarm/crowdsec/trusted-ips
sudoedit /etc/dockerswarm/crowdsec/trusted-ips
```

`install ... /dev/null` vacía un fichero existente: solo para crearlo.
`sudoedit` conserva el dueño y el modo. Para adoptar la allowlist que se creó
a mano el 2026-09-20 sin mostrar sus direcciones en pantalla, en vez de
`sudoedit`, con el fichero ya creado. `sudo -v` pide la contraseña una sola
vez, antes de una tubería con dos `sudo`, y `pipefail` hace visible un fallo
de `cscli` o de `jq`, que si no dejaría el fichero vacío sin ningún error:

```bash
sudo -v
if (
  set -o pipefail
  sudo -- cscli allowlists inspect apptolast-trusted -o json --error |
    jq -r '(.items // [])[].value' |
    sudo -- tee -a /etc/dockerswarm/crowdsec/trusted-ips >/dev/null
); then echo 'Copia hecha.'; else echo 'FALLO: repetir la copia.' >&2; fi
sudo -- grep --count --invert-match --extended-regexp '^[[:space:]]*(#|$)' \
  /etc/dockerswarm/crowdsec/trusted-ips
sudo -- cscli allowlists list
```

El `grep` cuenta las entradas del fichero y `cscli allowlists list` muestra
las de `apptolast-trusted` en su columna `Size`, sin ninguna dirección. Las
dos cifras deben coincidir antes de cualquier apply que ejecute
`host_security`: ese apply deja la allowlist igual que el fichero, y un
fichero vacío la vacía. Los cambios del fichero llegan a CrowdSec con el
siguiente apply de `host-baseline`.

### Si el propietario queda baneado

Desde otra IP o desde la consola de Netcup:

```bash
sudo -- cscli decisions list --scenario apptolast/traefik-basicauth-bf
sudo -- cscli decisions delete --ip IP_BANEADA
```

El baneo caduca solo a los 30 minutos. Añadir después la IP al fichero y
aplicar `host-baseline`; ese apply borra además cualquier decisión activa
sobre las IP que añade.

### Comprobar después de un apply o de un reinicio

```bash
sudo -- cscli metrics show acquisition parsers scenarios
sudo -- cscli allowlists check IP_DEL_PROPIETARIO
```

La tabla de adquisición debe mostrar `file:/var/log/dockerswarm/edge/access.log`
con líneas leídas (desde la ventana de `edge` que activa el fichero), junto
a `auth.log` y syslog, y la de parsers `crowdsecurity/traefik-logs` con
líneas parseadas.

Prueba sin tráfico y sin crear ninguna decisión: `cscli explain` pasa unas
líneas por los parsers y escenarios del host sin enviar alertas a la LAPI.
`192.0.2.10` es una dirección de documentación (RFC 5737):

```bash
tmp="$(mktemp -d)"
line='{"ClientAddr":"192.0.2.10:40000","ClientHost":"192.0.2.10",'
line+='"DownstreamStatus":401,"Duration":1000000,'
line+='"RequestHost":"logs-satisfactory.apptolast.com",'
line+='"RequestMethod":"GET","RequestPath":"/","RequestProtocol":"HTTP/2.0",'
line+='"RouterName":"satisfactory-logs@file","time":"2026-09-26T10:00:00Z"}'
for _ in $(seq 1 11); do printf '%s\n' "${line}"; done >"${tmp}/traefik.log"
sudo -- cscli explain --file "${tmp}/traefik.log" --type traefik
rm -r -- "${tmp}"
```

Cada línea debe acabar en `parser success` y en
`🟢 apptolast/traefik-basicauth-bf`; `cscli decisions list` no cambia.

Prueba de extremo a extremo, opcional, desde un origen desechable: una
máquina virtual con su propia IPv4 pública, que no esté en la allowlist y
desde la que nadie necesite SSH durante 30 minutos. Nunca una red móvil: su
IPv4 suele ser compartida (CGNAT) y el baneo cortaría a todos los que salen
por ella, quizá también al propietario. La alerta y esa IP se comparten con
la API central de CrowdSec (`share_custom: true` en
`/etc/crowdsec/console.yaml`).

1. Desde el origen desechable, once peticiones sin credenciales:
   `for i in $(seq 1 11); do curl -s -o /dev/null -w '%{http_code}\n'
   https://logs-satisfactory.apptolast.com/; done`. Once `401`.
2. En el host, `sudo -- cscli decisions list --scenario
   apptolast/traefik-basicauth-bf` muestra esa IP con unos 30 minutos.
3. Desde el origen, `curl --max-time 5 https://logs-satisfactory.apptolast.com/`
   agota el tiempo: el paquete se descarta.
4. En el host, `sudo -- cscli decisions delete --ip IP_DEL_ORIGEN` en cuanto
   el paso 3 se confirma, sin esperar los 30 minutos. El paso 3 vuelve a dar
   `401`.

## Parcheo del sistema operativo

Ningún apply ni temporizador actualiza el sistema: `host-baseline` desactiva
`APT::Periodic` y los timers `apt-daily*`, y `host_security` solo instala sus
pins exactos. El resto de paquetes Ubuntu se queda en la versión instalada
aunque el snapshot promovido traiga otra más nueva. Este procedimiento
instala lo que ofrece el snapshot ya promovido y aplicado, en una ventana que
termina con un reinicio.

Antes:

1. el snapshot del host es el de `config/host-security.yml` y está dentro del
   SLO de 14 días (`apt-config dump | grep '^APT::Snapshot'`); si no, primero
   se promueve otro con su propio cambio revisado, como en
   [SNAPSHOT_20261005.md](SNAPSHOT_20261005.md);
2. no hay markers en `/run/lock/dockerswarm-*.marker` ni procesos `apt` o
   `dpkg` en curso;
3. ninguna deriva registrada detiene el siguiente apply (ver
   [DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md), «Pendiente»);
4. los pasos «Antes» de «Reinicios»;
5. anotar `update-alternatives --query sudo | grep -E '^(Status|Value):'`
   (el 2026-10-04, `auto` y `/usr/lib/cargo/bin/sudo`),
   `sudo -- apt-mark showhold` (vacío el 2026-10-04),
   `sudo -- iptables -S INPUT | head -n 4`,
   `sudo -- iptables -S DOCKER-USER`, `sudo -- ip6tables -S DOCKER-USER` y
   `sudo -- iptables -S DOCKERSWARM-INGRESS`;
6. abrir la sesión de la ventana dentro de `tmux`, preferiblemente por el
   SSH público: si la conexión se corta a mitad de `dpkg`, la actualización
   sigue y no quedan PAM, `libc` o systemd a medio configurar con la consola
   de Netcup como única entrada.

Simular y localizar lo que el snapshot no congela:

```bash
sudo -- apt-get update
sudo -- apt-get -s full-upgrade | grep -E '^(Inst|Remv) |upgraded,'
sudo -- apt-get -s full-upgrade | awk '/^Inst / && !/Ubuntu:/ {print $2}'
```

La simulación no debe retirar paquetes (`Remv`) ni mover ningún pin de
`config/host-security.yml`: dentro de un snapshot promovido sus pins Ubuntu
ya son la versión candidata. La última orden lista los paquetes de
repositorios externos, que el snapshot no cubre: CrowdSec, su bouncer y
`docker-ce-rootless-extras` el 2026-10-04. Los paquetes Docker que fija
`/etc/apt/preferences.d/99-dockerswarm-docker` no aparecen. Esos externos se
retienen mientras dura la actualización, porque nada los congela: CrowdSec y
su bouncer subirían a 1.8.1 y 0.0.36 y romperían los pins exactos que
comprueba `host_security`.

```bash
sudo -- apt-mark hold PAQUETES_EXTERNOS
sudo -- apt-get -s full-upgrade | grep -E '^(Inst|Remv) |upgraded,'
sudo -- env NEEDRESTART_SUSPEND=1 DEBIAN_FRONTEND=noninteractive \
  apt-get -o Dpkg::Options::=--force-confdef \
  -o Dpkg::Options::=--force-confold full-upgrade
sudo -- apt-mark unhold PAQUETES_EXTERNOS
```

- La segunda simulación, ya con las retenciones, es la que se instala: sin
  `Remv` y sin ningún paquete externo. `apt-get` pide confirmación y se
  compara su resumen con esa simulación; nunca se usa `-y`.
- `NEEDRESTART_SUSPEND=1` impide que `needrestart` reinicie servicios al
  terminar. Su configuración ya excluye Docker, pero no `containerd`, del
  que depende cada contenedor, ni `netbird.service`, que puede llevar la
  propia sesión. `needrestart` es un paquete legacy preservado: su
  configuración no se toca.
- `--force-confold` conserva los ficheros de configuración locales, muchos de
  ellos gestionados por Ansible.
  `sudo -- find /etc \( -name '*.dpkg-dist' -o -name '*.ucf-dist' \)`
  muestra después las versiones que propone cada paquete, para revisarlas en
  un cambio aparte.
- `apt-mark unhold` es obligatorio, también si la actualización falla: el
  módulo `apt` de Ansible no cambia un paquete retenido, y la siguiente
  subida de un pin fallaría. `sudo -- apt-mark showhold` vuelve a quedar como
  se anotó.

Antes de reiniciar:

- `sudo -- apt-get -s full-upgrade` solo ofrece los externos;
- la alternativa `sudo` es la anotada y `/usr/bin/sudo.ws` sigue existiendo
  (ver [KNOWN_ISSUES.md](KNOWN_ISSUES.md), «`Timeout waiting for privilege
  escalation prompt` con sudo-rs»);
- `sudo -- needrestart -b -r l` lista los servicios que siguen con librerías
  antiguas, y existe `/run/reboot-required` si cambió el kernel;
- la entrada por defecto de `/boot/grub/grub.cfg` arranca con los mismos
  parámetros que `/proc/cmdline`, y con el kernel nuevo.

Un kernel nuevo ejecuta `update-grub`, que regenera `grub.cfg` desde
`/etc/default/grub.d/`: en este host no existe `/etc/default/grub`, y el
2026-10-05 eso quitó `net.ifnames=0` y añadió `crashkernel=` (ver
[KNOWN_ISSUES.md](KNOWN_ISSUES.md), «`eth0` pasa a `ens3` tras actualizar el
kernel»). `host-baseline` fija ahora esos parámetros y comprueba el menú en
cada apply, pero la actualización de esta ventana no pasa por Ansible.
`sudo -v` pide la contraseña una vez, antes de las dos `sudo`:

```bash
sudo -v
prog='$1 == "linux" {for (i = 3; i <= NF; i++) print $i; exit}'
diff <(tr ' ' '\n' </proc/cmdline | grep -v -e '^BOOT_IMAGE=' -e '^$') \
  <(sudo -- awk "${prog}" /boot/grub/grub.cfg) && echo 'Mismos parámetros.'
sudo -- awk '$1 == "linux" {print $2; exit}' /boot/grub/grub.cfg
```

La primera línea `linux` de `grub.cfg` es la entrada por defecto, porque
nada fija `GRUB_DEFAULT` y vale `0`. `diff` no debe imprimir ninguna línea, y
la última orden debe nombrar el kernel nuevo (`/vmlinuz-VERSIÓN`). Una línea
`<` es un parámetro del arranque actual que falta en `grub.cfg`; una `>`, uno
que aparecería.

Una diferencia solo se acepta si la línea `linux` de la entrada por defecto
es exactamente `root=… ro` seguido de `host_security_boot_cmdline_linux` y
`host_security_boot_cmdline_linux_default` de `config/host-security.yml`, en
ese orden. Entonces es un cambio revisado que `host-baseline` ya aplicó, o un
`crashkernel=` del arranque actual que el menú ya no lleva, y este reinicio
es el que lo activa: se reinicia y se anota en el registro de la ventana qué
parámetros cambian. Con cualquier otra diferencia no se reinicia: se corrige
con un cambio revisado y `host-baseline`, que ejecuta `update-grub`; a mano
solo en emergencia, editando
`/etc/default/grub.d/zz-dockerswarm-boot-cmdline.cfg` y ejecutando
`sudo -- update-grub`, sabiendo que el siguiente apply lo reescribe. Después
se repite la comprobación.

El reinicio sigue «Reinicios» y, además de sus pasos «Después»:

1. `uname -r` muestra el kernel nuevo;
2. `ip -brief link show dev eth0` muestra `eth0` en la primera columna, en
   `UP`. Es la interfaz pública: las aserciones previas de `platform` y
   `host-baseline` y las reglas `-i eth0` del cortafuegos usan ese nombre.
   `ens3`, `enp0s3` y `enx…` son nombres alternativos de la misma interfaz,
   así que `ip link show dev ens3` también responde y no prueba nada;
3. `/proc/cmdline` lleva `net.ifnames=0` y ningún `crashkernel=`
   (`grep -o -e 'net.ifnames=0' -e 'crashkernel=[^ ]*' /proc/cmdline` solo
   imprime `net.ifnames=0`);
4. la memoria sigue cubriendo `minimum_memory_mib` de `config/capacity.yml`
   (`awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo`). El 2026-10-04
   coincidían exactamente, 15 981 MiB: si un kernel nuevo reserva más
   memoria, todo playbook con `capacity_preflight` se detiene hasta revisar
   ese contrato;
5. `sudo -- iptables -S DOCKERSWARM-INGRESS` coincide con lo anotado: las
   reglas de SFTP y Satisfactory que añaden los drop-ins manuales vuelven
   con el arranque (compuerta STOP 10 de `CLAUDE.md`);
6. se anota de nuevo `sudo -- iptables -S INPUT | head -n 4` y
   `sudo -- netbird status`: el orden de `wt0` y `CROWDSEC_CHAIN` decide si un
   baneo de CrowdSec corta NetBird (ver «SSH por NetBird»);
7. `DOCKER-USER` empieza en IPv4 y en IPv6 por `CROWDSEC_CHAIN` y
   `DOCKERSWARM-INGRESS`, como lo anotado;
8. `--playbook host-baseline --check` no propone más cambios que los
   metadatos de un commit nuevo y, mientras siga en el host, la retirada de
   la línea manual `PerSourcePenaltyExemptList` de `/etc/ssh/sshd_config`.
   Confirma pins, SSH y el inventario del Hub de CrowdSec; las comprobaciones
   del cortafuegos solo corren en un apply, por eso se miran a mano en los
   pasos 5 a 7;
9. se registran en [DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md) los paquetes
   movidos, el kernel, la hora del reinicio y las comprobaciones.

Vuelta atrás: el kernel anterior sigue instalado y se elige en el menú de
GRUB desde la consola de Netcup. Sus entradas salen del mismo `update-grub`
y llevan los mismos parámetros que las del kernel nuevo: un parámetro que
falte se añade editando la entrada con `e` y se arranca con `Ctrl-x` (ver
[KNOWN_ISSUES.md](KNOWN_ISSUES.md)). Los paquetes no se degradan; un fallo
se corrige con un cambio revisado aparte.

## Reinicios

Antes:

1. confirmar ventana y consola fuera de banda;
2. ejecutar
   `sudo -- dockerd --validate --config-file=/etc/docker/daemon.json`;
3. verificar un backup reciente y que la unlock key externa está disponible si
   autolock está activo;
4. borrar las Tasks del laboratorio AX que queden, con
   `sudo ax get tasks -a default` y `sudo ax delete task <nombre> -a default`
   (ver [AX.md](AX.md), «Redis»).

Después:

1. `sudo -- systemctl is-active docker`;
2. `sudo -- docker node ls`;
3. comprobar manager `Ready`, `Active`, `Leader`;
4. revisar journal desde el instante del reinicio;
5. comprobar réplicas, healthchecks, rutas y alertas;
6. el laboratorio AX queda parado (política de reinicio `no`) y ningún
   playbook de producción lo necesita; si se quiere de vuelta, aplicar
   `ax-lab`, que arranca el registro, comprueba las imágenes de Substrate y
   de AX, arranca el nodo, espera a que Substrate esté listo sin
   reinstalarlo y recrea los workers de AX anteriores al arranque (ver
   [AX.md](AX.md), «Workers tras un reinicio»). Hasta entonces
   `ax.apptolast.com` responde 502: el mismo apply arranca de nuevo el
   reenviador `ax-web-edge` del panel (ver [AX_WEB.md](AX_WEB.md)).

`live-restore` no conserva el plano de control de Swarm durante un reinicio de
Docker.

## Logging

El daemon usa `local` por defecto. Sus ficheros internos no se manipulan:
se consultan mediante `sudo -- docker logs`.

`docker service logs` requiere `json-file` o `journald`; cada servicio que lo
necesita declara su driver y rotación. Iptables rota mediante rsyslog y el
helper soportado de Ubuntu 26.04.

El log de acceso de Traefik no está en los logs del servicio: Traefik lo
escribe en `/var/log/dockerswarm/edge/access.log`, que lee CrowdSec, y lo
rota `dockerswarm-edge-access-log-rotate.timer` cada 15 minutos
([EDGE.md](EDGE.md), «Log de acceso en fichero»).

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
- cada 14 días como máximo: promover el snapshot Ubuntu dentro de su SLO y
  aplicar «Parcheo del sistema operativo» con su reinicio;
- mensual: drift Terraform, usuarios/claves, rotación y restore de aplicación;
- trimestral: recuperación de state, ACME y Raft en un host aislado;
- antes de ampliar el Swarm: red privada, quorum impar, capacidad, backup y
  prueba de pérdida de manager.
