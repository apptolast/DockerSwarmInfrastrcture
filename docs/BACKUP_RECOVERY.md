# Backup y recuperación

## Objetivo y estado de activación

Esta capa protege fuera del VPS los datos necesarios para reconstruir los
servicios aprobados. Usa restic 0.19.1, fijado por SHA-256 para `amd64` y
`arm64`, y un repositorio cifrado en un bucket Cloudflare R2 dedicado.

La activación está bloqueada de forma intencionada:

- `backup_activation_enabled` vale `false`;
- `backup_r2_account_id` y `backup_r2_bucket` están vacíos;
- las credenciales R2/restic no existen en Git;
- el escrow de la clave de autolock tampoco pertenece a Git.

`ansible/playbooks/backup.yml` falla antes de cambiar el host mientras falte
alguna de esas entradas. No se debe eliminar este gate para obtener un
despliegue verde.

## Cobertura exacta

| Conjunto | Mecanismo de consistencia |
| --- | --- |
| 5 bases lógicas / 3 PostgreSQL | `pg_dump` custom y `pg_restore --list` |
| Home de n8n | Writers de n8n detenidos y tar local antes de reanudarlos |
| GPG y JWT de Passbolt | Passbolt detenido y tar local |
| OpenClaw limpio | OpenClaw detenido y tar local |
| Fuentes Secrets + clave HMAC | Tar cifrado de fuentes inmutables |
| Prometheus/Alertmanager/Loki/Grafana | Writers detenidos por ventana |
| Secretos de observabilidad | Tar cifrado de fuentes inmutables |
| ACME de Traefik | Traefik detenido y tar local |
| Mundo y mods de Minecraft | Quiesce RCON, tar y recuperación RCON |
| Estado del manager Swarm | Copia fría completa con Docker detenido |

Las imágenes, nombres de servicio y rutas proceden del mismo contrato de
workloads. No se copia OpenClaw legacy ni ningún servicio de la denylist.

Los tar preservan propietarios numéricos, ACL, atributos extendidos, sparse
files y SELinux. El controlador rechaza symlinks en la raíz del dataset,
archivos especiales, rutas absolutas y cualquier miembro con `..`.

## Diseño

El ciclo diario de aplicaciones es:

1. validar configuración, permisos, checksum de restic, manager y repositorio;
2. adquirir `/run/lock/dockerswarm-backup.lock` sin esperar;
3. recorrer diez grupos de consistencia declarados y cerrados;
4. para cada grupo, comprobar y detener solo sus writers;
5. producir y validar sus dumps y datasets mientras esos writers están a cero;
6. recuperar ese grupo en orden inverso, incluso si hubo un error, antes de
   continuar con el siguiente;
7. ejecutar por separado el protocolo RCON de Minecraft y recuperar siempre
   `save-on`;
8. crear un manifiesto con grupos, ventanas, hashes, tamaños, imágenes,
   extensiones y réplicas;
9. subir el staging ya inmutable con restic, confirmar el snapshot y aplicar
   retención;
10. borrar únicamente el staging temporal verificado.

PostgreSQL documenta que `pg_dump` obtiene una copia internamente consistente
sin bloquear lectores ni escritores. Se detienen además los writers para que
el dump y los ficheros asociados representen el mismo punto lógico.

No existe una parada global: n8n y sus tres bases lógicas forman un grupo;
Passbolt y sus claves otro; Shlink, OpenClaw, Traefik y cada almacén de
observabilidad tienen ventanas independientes. Los dos árboles de fuentes de
secretos, inmutables por contrato, se archivan en un grupo sin servicios. Esto
limita el impacto y evita afirmar que datos sin relación pertenecen al mismo
instante.

La copia de Swarm es independiente. Primero verifica el repositorio y que el
escrow coincide con `docker swarm unlock-key -q`; después detiene
`docker.socket` y `docker.service`, archiva el directorio completo, reinicia,
desbloquea y comprueba el mismo ID de Swarm. La subida se realiza con Docker
ya recuperado. Un fallo intenta recuperar Docker antes de devolver error.

Docker indica expresamente que una copia caliente del estado del manager no es
recomendable: hay que detener Docker y copiar todo
`/var/lib/docker/swarm`. Esa carpeta contiene las claves de los logs Raft.

## Credenciales y bootstrap

Se necesitan credenciales S3 de R2 `Object Read & Write` limitadas únicamente
al bucket de backup. No sirven el bearer token de Cloudflare DNS ni las
credenciales del backend Terraform. El repositorio no admite credenciales en
la URL.

### STOP: activación de autolock

No existe actualmente una secuencia autorizada de activación. En particular,
no se debe ejecutar manualmente `docker swarm update --autolock=true`: la
operación emite la única unlock key y hay una ventana de crash antes de
capturarla, custodiarla y probarla. En un manager mononodo esa ventana puede
dejar el plano de control bloqueado.

El gate solo se abrirá cuando exista un destino externo real y una operación
revisada que, sin escribir la clave en logs o shell history:

1. active autolock y capture la clave en memoria;
2. la escriba en el gestor externo y verifique una lectura independiente;
3. instale el escrow local root-only;
4. demuestre que ambos valores coinciden con Swarm;
5. ensaye bloqueo/desbloqueo y recuperación fuera de banda;
6. archive evidencia no secreta o revierta de forma segura.

`backup-provision-secrets.sh` es un aprovisionador posterior: se niega a
operar mientras autolock no esté ya activo y probado. Cuando la compuerta
anterior exista, solicitará sin eco:

- access-key ID R2;
- secret-access key R2;
- contraseña nueva de restic, con confirmación.

El script no sobrescribe ficheros existentes y crea exclusivamente:

```text
/etc/dockerswarm/backup/restic-password
/etc/dockerswarm/backup/r2-access-key-id
/etc/dockerswarm/backup/r2-secret-access-key
/etc/dockerswarm/backup/swarm-unlock-key
```

Los cuatro serán ficheros regulares `root:root 0600`, dentro de un directorio
`0700`. No deben copiarse al checkout, a variables Ansible, a la shell, a CI
ni a un Docker Config. La contraseña restic debe tener una segunda copia en el
gestor de secretos: perderla hace irrecuperable el repositorio cifrado.

El escrow local de autolock permitirá que el job frío y los reinicios del host
recuperen el mononodo sin intervención. La capa platform instala
`dockerswarm-swarm-unlock.service`: queda enlazada a cada arranque/reinicio de
`docker.service`, acepta únicamente el estado `locked`, valida fichero,
propietario, modo y formato, entrega la clave por stdin y espera
`active true`. Un estado inesperado falla cerrado. El role backup se niega a
activarse si ese lifecycle no está instalado y habilitado.

Esto reduce la protección ante robo completo del disco: quien obtenga a la vez
Raft y el escrow local puede desbloquearlo. La copia independiente de la clave
en un gestor de secretos sigue siendo obligatoria. Cuando exista un gestor
externo con identidad de máquina, debe sustituirse el fichero local por entrega
en cada ejecución. Hasta entonces, el escrow local y su copia independiente
deben rotarse juntos; una rotación no está completa hasta que
`docker swarm unlock-key -q`, el fichero root y el gestor externo coinciden.

Solo después de completar esa operación se declaran los valores no secretos en
IaC:

```yaml
backup_activation_enabled: true
backup_r2_account_id: ID_REAL_DE_32_HEX
backup_r2_bucket: nombre-real-del-bucket
backup_initialize_repository: true
```

Entonces se revisa y aplica una sola vez:

```bash
./scripts/deploy-ansible.sh \
  --playbook backup \
  --check \
  --ask-become-pass
./scripts/deploy-ansible.sh \
  --playbook backup \
  --confirm-production \
  --ask-become-pass
```

Tras confirmar el primer snapshot y un ensayo, se versiona
`backup_initialize_repository: false`. Las siguientes aplicaciones exigen que
el repositorio ya exista y pueda abrirse; nunca lo recrean silenciosamente.

## Planificación, retención y no solapamiento

Los calendarios iniciales son una política técnica y no un SLA de negocio:

| Trabajo | Calendario | Resultado |
| --- | --- | --- |
| Aplicaciones | diario 02:15 | snapshot lógico y de ficheros |
| Integridad completa | sábado 03:15 | `restic check --read-data` |
| Estado frío de Swarm | domingo 04:15 | breve parada completa de Docker |
| Ensayo de aplicación | día 1, 05:15 | restore probado de cinco bases |

Cada timer añade hasta 15 minutos aleatorios. Todos comparten un `flock`
no bloqueante; si otro trabajo está activo, el segundo falla y deja estado y
métrica en vez de solaparse.

La retención, separada por host y tags, conserva 24 horarios, 14 diarios,
8 semanales, 12 mensuales y 5 anuales. `forget --prune` elimina solo snapshots
expirados del repositorio; nunca toca datos vivos. Debe revisarse cuando se
acuerden RPO, RTO, capacidad y coste.

RPO provisional de aplicaciones: hasta 24 horas si el último job terminó bien.
El estado Swarm puede tener hasta una semana, pero los stacks y configs siguen
versionados en Git y los datos de aplicación tienen su ciclo propio. No se
declara un RTO hasta medir el ensayo con el volumen real.

## Verificación, estado y alertas

Cada artefacto lleva SHA-256 en `manifest.json`. Además:

- cada dump se lista antes de subir;
- se registra el hash y número de entradas de cada catálogo, además del
  inventario exacto de relaciones de usuario;
- restic confirma que el ID recién creado existe;
- el sábado se descargan y verifican todos los packs;
- el ensayo mensual restaura con `--verify`;
- cada dump se carga realmente en un PostgreSQL efímero con la imagen fijada
  del servicio y termina con `SELECT 1`;
- el catálogo del dump y el inventario de tablas, vistas, secuencias y
  particiones deben coincidir después del restore;
- `vectors` conserva `pgvector` 0.8.1 y `rag` conserva 0.8.2; el ensayo crea
  y verifica esas versiones antes y después de cargar cada dump;
- cada base lleva una imagen de ensayo fijada por digest: `vectors` usa la
  imagen histórica que puede crear directamente 0.8.1, mientras `rag` y la
  base principal usan la imagen 0.8.2;
- los tar se validan y materializan en staging vacío.

Los contenedores de ensayo no tienen red y usan un `tmpfs`; se destruyen por
nombre aleatorio en el bloque de cleanup. Ningún ensayo monta datos vivos.

Estado JSON:

```text
/var/lib/dockerswarm-backup/status/application.json
/var/lib/dockerswarm-backup/status/swarm-state.json
/var/lib/dockerswarm-backup/status/verify.json
/var/lib/dockerswarm-backup/status/rehearse.json
```

Métricas para node-exporter:

```text
/var/lib/node_exporter/textfile_collector/dockerswarm_backup.prom
```

Las series son:

- `dockerswarm_backup_last_run_success`;
- `dockerswarm_backup_last_finished_timestamp_seconds`;
- `dockerswarm_backup_last_duration_seconds`.

La monitorización debe alertar si un trabajo falla o supera dos veces su
intervalo. Hasta que exista un receptor real de Alertmanager, `systemctl` y
los JSON son el gate operativo:

```bash
systemctl list-timers 'dockerswarm-*backup*'
systemctl status dockerswarm-application-backup.service
journalctl -u dockerswarm-application-backup.service --since today
```

## Restore seguro de aplicaciones

Un restore nunca acepta `latest`, nunca borra datos y nunca activa servicios.
Se selecciona un ID después de listar snapshots:

```bash
sudo /usr/local/libexec/dockerswarm-backupctl \
  --config /etc/dockerswarm/backup/config.json \
  snapshots \
  --tag application

sudo /usr/local/libexec/dockerswarm-backupctl \
  --config /etc/dockerswarm/backup/config.json \
  restore \
  --snapshot ID_EXPLICITO \
  --target /srv/dockerswarm/restore/ID_EXPLICITO \
  --extract-filesystems-to \
  /srv/dockerswarm/restore/ID_EXPLICITO-filesystems
```

Ambos comandos cargan las credenciales desde los ficheros root y nunca piden
pegarlas ni exportarlas en la shell.

Después del staging:

1. comparar el manifiesto y el commit desplegado;
2. ejecutar `backupctl rehearse` si aún no existe un ensayo del snapshot;
3. mantener apagados writers y edge;
4. crear raíces de destino vacías;
5. mover cualquier raíz anterior a una cuarentena fechada, nunca borrarla;
6. mover los datasets materializados preservando propietario y permisos;
7. iniciar bases vacías y cargar los dumps con `pg_restore --exit-on-error`;
8. ejecutar las pruebas funcionales y el gate de workloads;
9. activar writers y edge;
10. conservar la cuarentena hasta aprobar un nuevo backup y rollback.

El restore inicial de la migración sigue perteneciendo al tooling de
migración. Este componente no genera su marker de readiness: hacerlo desde un
staging de backup afirmaría falsamente que el cutover fue validado.

## Ensayo manual

El ensayo soportado selecciona el snapshot de aplicación más reciente:

```bash
sudo systemctl start dockerswarm-backup-rehearsal.service
sudo systemctl status dockerswarm-backup-rehearsal.service
```

Debe ejecutarse también antes de cualquier purga extraordinaria o rotación de
credenciales. El ensayo estructural de Swarm valida hashes y tar al hacer
restore a staging, pero no prueba Raft. Una vez por trimestre se necesita un
VPS aislado para recorrer el runbook completo siguiente.

## Recuperación completa del estado Swarm

La copia Raft no sustituye las copias de aplicaciones. Solo se usa cuando hay
que recuperar identidad, servicios, configs y secretos de Swarm. El host de
recuperación debe usar una versión Docker compatible y permanecer aislado de
los nodos antiguos.

1. reconstruir host, red y Docker desde Terraform/Ansible;
2. restaurar un snapshot `swarm-state` a un staging vacío;
3. validar manifiesto, SHA-256 y miembros del tar;
4. disponer de la clave autolock externa y verificar que coincide con el
   artefacto cifrado;
5. detener `docker.socket` y `docker.service`;
6. mover el directorio actual a cuarentena, sin borrarlo;
7. extraer el tar completo bajo `/var/lib/docker`;
8. iniciar Docker y ejecutar `docker swarm unlock`;
9. aisladamente, ejecutar `docker swarm init --force-new-cluster` con la
   dirección anunciada correcta;
10. comprobar nodo, servicios, configs, secretos, redes y aplicaciones;
11. rotar la clave autolock y actualizar sus dos escrows;
12. ejecutar inmediatamente copias nuevas de aplicaciones y Swarm.

Esqueleto para la ventana aprobada, no para ejecución ciega:

```bash
sudo systemctl stop docker.socket docker.service
sudo mv /var/lib/docker/swarm \
  /var/lib/docker/swarm.pre-restore-FECHA
sudo tar \
  --extract \
  --file /RUTA_STAGING/swarm-state.tar \
  --directory /var/lib/docker \
  --acls \
  --xattrs \
  --selinux \
  --numeric-owner \
  --same-permissions
sudo systemctl start docker.service
sudo docker swarm unlock
sudo docker swarm init \
  --force-new-cluster \
  --advertise-addr IP_REAL:2377
sudo docker swarm unlock-key --rotate
```

No se ejecuta `--force-new-cluster` mientras el servidor antiguo pueda seguir
activo. No se reutiliza un directorio Raft de otro node ID y no se elimina la
cuarentena hasta terminar el rollback window.

## Fallos deliberadamente bloqueantes

El job falla sin subir un snapshot si:

- falta un servicio, dataset, dump o credencial;
- un writer no tiene todas sus réplicas;
- hay más o menos de un contenedor para una base o Minecraft;
- el checksum/version de restic difiere;
- el repositorio no existe o R2 no responde;
- el dump o tar no pasa validación;
- no se puede recuperar una réplica o `save-on`;
- autolock está desactivado o su escrow no coincide;
- no se puede recuperar Docker con el mismo ID de Swarm;
- el restore apunta a un destino no vacío.

Un snapshot incompleto nunca recibe manifiesto final ni retención. Los errores
quedan en status, journal y métrica. Un fallo de recuperación de writers,
Minecraft o Docker requiere intervención inmediata antes de repetir el job.

## Riesgos residuales y siguiente nivel

- Un único manager tiene RTO con downtime y ninguna tolerancia a fallo.
- R2 en una sola cuenta es off-host, pero no es una estrategia 3-2-1 completa.
- La credencial de escritura que necesita restic puede alterar el repositorio.
- Los bucket locks deben probarse en un repositorio desechable: impedir
  borrados puede romper locks, retención o prune de restic.
- El ensayo Raft completo necesita infraestructura aislada y no cabe en el
  propio manager de producción.
- El backup diario causa una interrupción breve de HTTP mientras se obtiene el
  punto consistente; el job frío interrumpe todo Docker.

El siguiente endurecimiento es replicar snapshots verificados a otro proveedor
y cuenta, entregar credenciales desde un gestor externo y crear
automáticamente un VPS efímero trimestral para el ensayo Raft. Ninguno de esos
controles se declara implantado antes de disponer de proveedor, credenciales y
presupuesto reales.

## Referencias primarias

- [Docker: backup y restore de Swarm](https://docs.docker.com/engine/swarm/admin_guide/#back-up-the-swarm)
- [Docker: autolock del manager](https://docs.docker.com/engine/swarm/swarm_manager_locking/)
- [Docker: `--force-new-cluster`](https://docs.docker.com/reference/cli/docker/swarm/init/#force-restart-node-as-a-single-mode-manager---force-new-cluster)
- [PostgreSQL: `pg_dump`](https://www.postgresql.org/docs/current/app-pgdump.html)
- [PostgreSQL: SQL dumps](https://www.postgresql.org/docs/current/backup-dump.html)
- [restic: repositorios S3 compatibles](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html#s3-compatible-storage)
- [restic: backup](https://restic.readthedocs.io/en/stable/040_backup.html)
- [restic: retención](https://restic.readthedocs.io/en/stable/060_forget.html)
- [restic: comprobación completa](https://restic.readthedocs.io/en/stable/045_working_with_repos.html#checking-integrity-and-consistency)
- [Cloudflare R2: credenciales S3](https://developers.cloudflare.com/r2/api/s3/tokens/)
- [Cloudflare R2: compatibilidad S3](https://developers.cloudflare.com/r2/api/s3/api/)
- [itzg: RCON y Docker Secrets](https://github.com/itzg/docker-minecraft-server/blob/master/docs/configuration/server-properties.md#rcon)
