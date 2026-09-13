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

Para un marker de bootstrap se añaden:

```text
--marker-path /run/lock/dockerswarm-bootstrap.marker
--owner-uid 0
--owner-gid 0
```

La evidencia se archiva antes de retirar el marker. Se usa, si es posible, el
helper del mismo commit registrado. Un reboot mata procesos pero elimina
`/run`; primero debe conservarse la evidencia cuando todavía sea accesible.

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
7. Repetir Ansible y exigir `changed=0`.
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
