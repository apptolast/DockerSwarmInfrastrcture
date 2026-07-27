# Promoción transaccional de la generación runtime

Este runbook describe el único mecanismo soportado para sustituir el runtime
canónico final:

```text
/srv/dockerswarm/services
```

La implementación es
[`scripts/promote_runtime_generation.py`](scripts/promote_runtime_generation.py).
La CLI de producción no admite sobrescribir rutas, exige `root` y serializa
cualquier mutación mediante el mismo lock global del host que usa Ansible.

## Estado actual: STOP

La promoción no está autorizada hoy. Faltan, como mínimo:

- detener y demostrar el cierre de todos los writers del servidor legacy;
- generar y verificar un backup final cuyo `sourceBackup` sea estrictamente
  posterior a `apptolast-data-20260723T225340Z` y al runtime canónico;
- preparar y finalizar una generación candidata desde ese backup final;
- instalar un allowed-signers revisado fuera de Git;
- emitir y firmar una attestation vigente ligada a la generación y al commit
  exactos;
- integrar mediante Ansible los directorios privados descritos aquí.

El ejemplo versionado de allowed-signers autoriza deliberadamente a nadie.
Ni el backup del 23 de julio, ni un JSON sin firma, ni ser `root` sustituyen
estos gates.

No se ha ejecutado esta CLI contra `/srv`, no se ha promovido ninguna
generación y no se ha desplegado ningún servicio como parte de su desarrollo.

## Rutas inmutables

<!-- markdownlint-disable MD013 -->

| Finalidad | Ruta |
| --- | --- |
| Runtime canónico | `/srv/dockerswarm/services` |
| Candidata | `/srv/dockerswarm/runtime-generations/candidates/<sourceBackup>/services` |
| Historial | `/srv/dockerswarm/runtime-generations/history/<sourceBackup>/services` |
| Journal | `/srv/dockerswarm/runtime-generations/transactions/<transactionId>` |
| Attestation | `/var/lib/dockerswarm-migration/final-freeze-evidence/<sourceBackup>.json` |
| Firma | `/var/lib/dockerswarm-migration/final-freeze-evidence/<sourceBackup>.json.sig` |
| Allowed signers | `/etc/dockerswarm/migration/runtime-promotion.allowed-signers` |

<!-- markdownlint-enable MD013 -->

Cada directorio privado administrado es `root:root` y `0700`. La attestation y
su firma son `root:root` y `0600`. El allowed-signers es un fichero regular,
`root:root`, con un único enlace y nunca escribible por grupo u otros.

No se siguen symlinks. Se vuelven a comparar `device` e `inode` a través de
descriptores de directorio. Los ficheros runtime deben ser regulares y tener
un único hard link. La candidata, el canónico, su slot de origen y el historial
de rollback deben estar en el mismo filesystem.

## Qué valida una generación

Antes del plan y otra vez bajo el lock inmediatamente antes del exchange, la
CLI verifica:

- el árbol completo sin enlaces, tipos especiales ni montajes en otro
  filesystem;
- catálogo de servicios, catálogo de secrets y contrato de plataforma del
  commit limpio actual;
- `runtime-manifest.json` v4;
- `restore-state/workloads-ready-v2.json`;
- SHA-256 exactos de catálogo, manifiesto runtime, manifiesto recovery, gate y
  clave de identidad;
- cobertura y contenido de `recovery/SHA256SUMS`;
- catálogo y SHA-256 de todas las fuentes Secret, sin imprimir sus valores;
- markers de las fases PostgreSQL/vector;
- datasets, ficheros críticos, ownership numérico y exclusión del OpenClaw
  legacy;
- una huella SHA-256 determinista de todo el runtime, incluyendo contenido,
  ruta, modo, UID y GID.

La attestation firma todos esos hashes, los IP legacy/destino declarados en
`config/platform.yml`, el `sourceBackup`, el commit exacto y las marcas de
tiempo de stop y backup.

## Gate externo firmado

El principal fijo es:

```text
apptolast-runtime-promotion
```

El namespace fijo de firma es:

```text
runtime-promotion@apptolast.com
```

El esquema es
[`config/runtime-promotion-attestation.schema.json`](config/runtime-promotion-attestation.schema.json).
El fichero
[`config/runtime-promotion-attestation.json.example`](config/runtime-promotion-attestation.json.example)
solo muestra el formato. La attestation real debe ser JSON ASCII canónico:
claves ordenadas, representación compacta y un único salto de línea final.

Los tiempos deben cumplir:

```text
legacyStoppedAt
  <= backupStartedAt
  <= timestamp de sourceBackup
  <= backupCompletedAt
  <= issuedAt
  < expiresAt
```

La vigencia máxima es de seis horas, se toleran como máximo cinco minutos de
desfase futuro y `expiresAt` debe seguir vigente al aplicar.

`legacyWritersStopped` y `finalBackupVerified` deben ser booleanos `true`.
Son afirmaciones del proceso externo que controla el servidor legacy. La CLI
no contacta ese servidor y no inventa esas pruebas: solo confía en ellas si
la firma corresponde a un allowed-signer revisado.

El fichero versionado
[`config/runtime-promotion.allowed-signers.example`](config/runtime-promotion.allowed-signers.example)
no contiene ninguna clave. El registro real sigue el formato oficial de
OpenSSH y se instala fuera del repositorio.

## Preparación de candidata

Todo el bloque permanece prohibido hasta obtener el freeze y el backup final.
Los valores siguientes son nombres ilustrativos posteriores al baseline, no
una autorización:

```bash
sudo -- install -d -o root -g root -m 0700 \
  /srv/dockerswarm/runtime-generations \
  /srv/dockerswarm/runtime-generations/candidates \
  /srv/dockerswarm/runtime-generations/history \
  /srv/dockerswarm/runtime-generations/transactions \
  /var/lib/dockerswarm-migration/final-freeze-evidence

sudo -- install -d -o root -g root -m 0700 \
  /srv/dockerswarm/runtime-generations/candidates/\
apptolast-data-20260727T000500Z

sudo -- python3 migration/scripts/prepare_runtime.py \
  /var/lib/dockerswarm-migration/staging/\
apptolast-data-20260727T000500Z \
  --root /srv/dockerswarm/runtime-generations/candidates/\
apptolast-data-20260727T000500Z/services
```

Después se ejecutan restore, validaciones live y finalización contra esa
candidata. Antes de continuar deben haberse eliminado todos los contenedores y
redes de los dos Compose de restore. La promoción exige cero stacks, cero
servicios Swarm y cero contenedores Docker, incluidos los parados.

El comando siguiente entrega únicamente bindings verificables. No rellena ni
afirma los hechos externos:

```bash
sudo -- python3 migration/scripts/promote_runtime_generation.py \
  attestation-bindings \
  --candidate apptolast-data-20260727T000500Z
```

El sistema de firma autorizado completa los siete campos externos, emite el
JSON canónico y lo firma fuera del repositorio:

```bash
ssh-keygen -Y sign \
  -f /ruta/privada/clave-de-firma \
  -n runtime-promotion@apptolast.com \
  /var/lib/dockerswarm-migration/final-freeze-evidence/\
apptolast-data-20260727T000500Z.json
```

La clave privada nunca reside en Git ni en el servidor destino. El fichero
`.sig` resultante se instala `root:root` y `0600`. El allowed-signers solo
contiene la clave pública autorizada y el principal exacto.

## Plan y promoción

El plan vuelve a verificar generación, firma, commit y ausencia de writers:

```bash
sudo -- python3 migration/scripts/promote_runtime_generation.py \
  plan \
  --candidate apptolast-data-20260727T000500Z
```

La salida termina con un confirmation hash-bound. Debe copiarse literalmente:

```bash
sudo -- python3 migration/scripts/promote_runtime_generation.py \
  apply \
  --candidate apptolast-data-20260727T000500Z \
  --confirm '<confirmation exacta del plan>'
```

`apply` reconstruye el plan dentro del mutex. Un cambio en fichero, inode,
hash, firma, commit, expiry o evidencia de quiescencia invalida la
confirmación.

## Atomicidad y journal

Cada transacción tiene una identidad inmutable y eventos `O_EXCL`, `0600`,
consecutivos y ligados mediante `previousHash`/`recordHash`. Cada evento se
escribe en un inode temporal, se sincroniza, se publica sin reemplazo y se
sincroniza el directorio.

La publicación usa `renameat2(RENAME_EXCHANGE)` mediante `dirfd`. El canónico
y la candidata existen durante toda la operación: no hay un instante en que
`services` desaparezca. Se verifican los inodes antes y después.

Después del exchange:

1. se ejecuta `fsync` de ambos directorios padre;
2. el runtime anterior se mueve sin reemplazo al historial;
3. se sincronizan origen e historial;
4. se registra la fase terminal.

No se borra ninguna generación, journal o evidencia. El directorio candidato
vacío también se conserva. La limpieza y retención futuras deben ser una
operación diferente, explícita y auditada.

## Estado y recuperación

Estado:

```bash
sudo -- python3 migration/scripts/promote_runtime_generation.py status
```

Un fallo, señal o `SIGKILL` deja el journal no terminal. La CLI infiere el
estado únicamente por los inodes originales:

- `pre-exchange`: canónico antiguo y candidata nueva;
- `exchanged`: canónico nuevo y runtime antiguo aún en el slot candidato;
- `preserved`: canónico nuevo y runtime antiguo en historial;
- `unknown`: ninguna recuperación automática se autoriza.

Un evento temporal completo se adopta explícitamente en recovery. Uno parcial
se mueve a `orphaned-events` ligado a su hash; nunca se elimina.

El supervisor del lock global también conserva
`/run/lock/dockerswarm-direct.marker` si el comando termina distinto de cero.
Después de demostrar que el controlador terminó, primero se archiva y recupera
ese marker con su confirmación exacta:

```bash
sudo -- python3 scripts/host_global_operation_lock.py recover

sudo -- python3 scripts/host_global_operation_lock.py recover \
  --apply \
  --confirm '<confirmación exacta del comando anterior>'
```

Luego se pide la confirmación runtime adecuada. Antes del exchange solo se
admite abortar:

```bash
sudo -- python3 migration/scripts/promote_runtime_generation.py \
  recover \
  --mode abort

sudo -- python3 migration/scripts/promote_runtime_generation.py \
  recover \
  --mode abort \
  --apply \
  --confirm '<confirmación exacta>'
```

Después del exchange solo se admite completar la preservación:

```bash
sudo -- python3 migration/scripts/promote_runtime_generation.py \
  recover \
  --mode complete

sudo -- python3 migration/scripts/promote_runtime_generation.py \
  recover \
  --mode complete \
  --apply \
  --confirm '<confirmación exacta>'
```

Recovery vuelve a exigir el lock global y cero writers. Nunca hace rollback
implícito.

## Rollback explícito

Rollback solo acepta una generación histórica cuya preservación aparezca en
una transacción terminal válida. Exige de nuevo cero stacks, servicios,
contenedores, Compose de restore y referencias de procesos.

Plan:

```bash
sudo -- python3 migration/scripts/promote_runtime_generation.py \
  rollback \
  --generation apptolast-data-20260723T225340Z
```

Aplicación:

```bash
sudo -- python3 migration/scripts/promote_runtime_generation.py \
  rollback \
  --generation apptolast-data-20260723T225340Z \
  --apply \
  --confirm '<confirmación exacta del plan>'
```

Rollback usa otro exchange atómico. La generación sustituida se conserva bajo
su propio historial; tampoco se elimina.

## Límites honestos

- La firma demuestra qué clave autorizada afirmó el freeze; no observa por sí
  misma el servidor legacy.
- La inspección local cubre objetos Docker, los Compose de restore y
  referencias visibles en `/proc`. No protege contra un kernel o `root`
  comprometidos.
- La durabilidad depende de que el filesystem y el almacenamiento implementen
  correctamente `renameat2` y `fsync`. Si no soportan
  `RENAME_EXCHANGE`/`RENAME_NOREPLACE`, la CLI falla cerrada.
- Un estado de inodes desconocido no produce confirmación de recovery.
- La CLI no rota secretos, no cambia DNS, no despliega stacks y no reactiva
  workflows n8n.
- La presencia de una generación en historial no sustituye los backups
  cifrados externos ni su restore probado.

## Pruebas reproducibles

Durante la implementación se obtuvieron, sin tocar producción:

```text
19/19  tests dirigidos de promoción
88/88  migration/tests completo
0      warnings con Python -Wd
```

Comandos:

```bash
.venv/bin/black --check \
  migration/scripts/promote_runtime_generation.py \
  migration/tests/test_runtime_generation_promotion.py

PYTHONPYCACHEPREFIX=.build/pycache \
  .venv/bin/python -Wd -m unittest \
  migration.tests.test_runtime_generation_promotion -v

PYTHONPYCACHEPREFIX=.build/pycache \
  .venv/bin/python -Wd -m unittest discover \
  -s migration/tests \
  -v

.venv/bin/python -m py_compile \
  migration/scripts/promote_runtime_generation.py \
  migration/tests/test_runtime_generation_promotion.py

git diff --check
```

## Fuentes oficiales

- [Linux `renameat2(2)`][linux-renameat2]
- [Linux `openat(2)` y estabilidad de `dirfd`][linux-openat]
- [Linux `fsync(2)` y sincronización del directorio][linux-fsync]
- [OpenBSD `ssh-keygen -Y sign/verify` y allowed-signers][openssh-sign]
- [Docker `stack ls`][docker-stack]
- [Docker `service`][docker-service]
- [Docker `container ls --all`][docker-container]
- [Docker Compose `ps --all`][docker-compose]

[linux-renameat2]: https://man7.org/linux/man-pages/man2/renameat2.2.html
[linux-openat]: https://man7.org/linux/man-pages/man2/openat.2.html
[linux-fsync]: https://man7.org/linux/man-pages/man2/fsync.2.html
[openssh-sign]: https://man.openbsd.org/ssh-keygen.1
[docker-stack]: https://docs.docker.com/reference/cli/docker/stack/ls/
[docker-service]: https://docs.docker.com/reference/cli/docker/service/
[docker-container]: https://docs.docker.com/reference/cli/docker/container/ls/
[docker-compose]: https://docs.docker.com/reference/cli/docker/compose/ps/
