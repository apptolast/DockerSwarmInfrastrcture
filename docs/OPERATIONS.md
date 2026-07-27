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

1. Actualizar contratos/digests en una rama.
2. Ejecutar validación, lint y escaneo de secretos.
3. Revisar y hacer commit; ningún writer acepta worktree sucio.
4. Crear el plan Terraform firmado con locking/state proof válidos.
5. Aplicar solo mediante `apply-terraform.sh`; conservar snapshots y evidencia
   mientras el lock remoto sigue ligado a la operación.
6. Aplicar Ansible mediante `deploy-ansible.sh`.
7. Repetir Ansible y exigir `changed=0`.
8. Validar firewall, servicios, TLS, DNS, logs, backups y unidades fallidas.
9. Registrar aceptación y rollback.

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
