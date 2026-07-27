# Estado Terraform y recuperación

## Estado conocido y estado desconocido

Existen tres roots declarados:

- `infra/terraform/cloudflare/state-bootstrap` declara dos buckets de state y
  el bucket dedicado de backups. Su state local vive en almacenamiento cifrado
  fuera del checkout.
- `infra/terraform/cloudflare/apptolast-dns` administra el DNS de
  `apptolast.com`. Su key es
  `cloudflare/apptolast-dns/terraform.tfstate`.
- `infra/terraform/netcup/perimeter` administra firewall, asignación y claves
  SCP. Su key es `netcup/perimeter/terraform.tfstate`.

El account ID de Cloudflare está versionado porque no es una credencial. No se
dispone en esta revisión de nombres definitivos de buckets, Access Key
IDs/Secret Access Keys, states remotos, states del repositorio antiguo ni
credenciales de proveedores. No se afirma que un backend esté inicializado, que
un recurso esté importado o que se haya ejecutado un `apply`.

Los dos ficheros `backend.r2.tfbackend.example` usan placeholders distintos y
rechazan así el bucket compartido del diseño anterior. No deben usarse en
producción hasta sustituir cada placeholder por un nombre real, privado y
distinto. Este documento no inventa esos nombres.

La identidad autorizada no queda implícita en esos ficheros ignorados. Después
de confirmar los valores reales se copia
`infra/terraform/backend-identities.json.example` a
`infra/terraform/backend-identities.json`, se completa cada identidad confirmada
y se commitea. El registro contiene solo ruta/bucket/key y hashes SHA-256 de Access
Key IDs, nunca Secret Access Keys. Mientras el registro definitivo no exista,
los wrappers bloquean incluso un backend técnicamente accesible; así un clon o
bucket equivocado no puede convertirse en producción por accidente.
Para romper la dependencia de bootstrap, su primera versión puede autorizar
solo la ruta local y dejar `production: null` en los dos roots remotos. Esos
roots permanecen inutilizables hasta crear los buckets/credenciales, completar
sus identidades exactas y commitear otra revisión.

La mención a un bucket R2 “versionado” en documentación previa tampoco se toma
como capacidad real. Cloudflare publica `GetBucketVersioning` y
`PutBucketVersioning` como operaciones S3 no implementadas:
[compatibilidad S3 de R2](https://developers.cloudflare.com/r2/api/s3/api/).

## Bootstrap sin dependencia circular

`cloudflare/state-bootstrap` usa el backend `local` con una ruta absoluta
aportada mediante `backend.local.tfbackend`. La ruta debe estar en
almacenamiento cifrado, fuera del checkout y con copia offsite.

Ese root declara los tres buckets con `prevent_destroy` y mantiene `r2.dev`
deshabilitado. El tercero, `dockerswarm_backups`, está aislado de los dos
states. No crea Access Keys: introducir un Secret Access Key en Terraform lo
persistiría en el state que se intenta proteger. El token temporal que
administra buckets se inyecta mediante `CLOUDFLARE_API_TOKEN`, se limita a esa
cuenta y se revoca cuando deja de ser necesario.

Si los buckets ya existen, se importan antes del primer plan. El state local de
bootstrap se incluye en la misma política de snapshots `age` y restore
rehearsal que los states remotos.

## Diseño obligatorio de backends

Cada root remoto usa:

- un bucket R2 privado distinto;
- una credencial R2 distinta, `Object Read & Write`, limitada a ese bucket;
- una key estable y exclusiva;
- backend S3 parcial, sin credenciales en HCL o en el fichero `.tfbackend`;
- locking S3 explícito: las plantillas usan `use_lockfile = true`, pero todos
  los writers siguen bloqueados hasta aportar un proof vigente, ligado por hash
  al bucket, endpoint y Terraform fijado, de la prueba con dos clientes;
- ninguna URL pública, custom domain ni `r2.dev`;
- snapshots cifrados en otro proveedor/cuenta y fuera del VPS.

Los placeholders de diseño son:

- El root **DNS** usa el bucket `<R2_BUCKET_CLOUDFLARE_DNS>`, la key
  `cloudflare/apptolast-dns/terraform.tfstate` y la credencial
  `<R2_CREDENTIAL_DNS>`.
- El root **Netcup** usa el bucket `<R2_BUCKET_NETCUP_PERIMETER>`, la key
  `netcup/perimeter/terraform.tfstate` y la credencial
  `<R2_CREDENTIAL_NETCUP>`.
- El rol **backup** usa el bucket `<R2_BUCKET_DOCKERSWARM_BACKUPS>` y una
  credencial `<R2_CREDENTIAL_DOCKERSWARM_BACKUPS>` exclusiva. Restic administra
  las keys internas del repositorio; este bucket nunca almacena state vivo.

Los placeholders nunca se copian literalmente a producción. Los nombres se
comprueban y se escriben en los ficheros `.tfbackend` ignorados y, al no ser
secretos, en el registro de identidades versionado. El Secret Access Key
permanece únicamente en el almacén seguro.

Cloudflare documenta el backend remoto R2 y la creación de credenciales limitadas
al bucket:
[backend R2 para Terraform](https://developers.cloudflare.com/terraform/advanced-topics/remote-backend/)
y [tokens R2](https://developers.cloudflare.com/r2/api/tokens/).

HashiCorp recomienda configuración parcial y credenciales por entorno. Poner
secretos en `-backend-config` o hardcodearlos puede persistirlos en `.terraform`
y en planes:
[backend S3](https://developer.hashicorp.com/terraform/language/backend/s3).

## Por qué separar roots, buckets y credenciales

La separación evita que:

- un plan DNS bloquee o corrompa el state de Netcup;
- una credencial comprometida pueda reescribir ambos states;
- un error de lifecycle, limpieza o bucket lock afecte ambos dominios;
- la recuperación de un root obligue a sobrescribir el otro;
- el blast radius de una automatización exceda su responsabilidad.

Una key diferente dentro del mismo bucket no proporciona el mismo aislamiento
administrativo que bucket y credencial distintos.

Los buckets pueden residir en la misma cuenta Cloudflare, pero los permisos se
limitan por bucket. Si el modelo de amenaza exige independencia del proveedor o
de la cuenta, la copia offsite debe cubrirla; dos buckets de la misma cuenta no
protegen frente a pérdida o compromiso de esa cuenta.

## R2 no sustituye el historial de state

Terraform recomienda S3 Bucket Versioning para recuperarse de borrados y errores
humanos. R2 es S3-compatible, pero no implementa el versionado S3 que proporciona
ese historial. Además, HashiCorp solo prueba el backend `s3` contra Amazon S3 y
califica otros proveedores compatibles como soporte “best effort”:
[limitaciones del backend S3](https://developer.hashicorp.com/terraform/language/backend/s3#support-for-s3-compatible-storage-providers).

R2 cifra automáticamente objetos y metadatos en reposo y usa TLS en tránsito:
[seguridad de datos R2](https://developers.cloudflare.com/r2/reference/data-security/).
Ese cifrado, gestionado por Cloudflare, no recupera una versión sobrescrita y no
equivale a un backup cliente cifrado.

Cloudflare ofrece bucket locks propios que impiden sobrescribir o eliminar
objetos:
[bucket locks R2](https://developers.cloudflare.com/r2/buckets/bucket-locks/).
No se aplica un lock de retención al objeto vivo `terraform.tfstate`, porque
Terraform necesita sobrescribirlo, ni al `.tflock`, porque necesita eliminarlo
al liberar el lock. Puede estudiarse para snapshots con keys inmutables, pero no
reemplaza la copia offsite ni el versionado ausente.

## Prueba obligatoria de locking

Declarar `use_lockfile = true` no acredita que R2 implemente correctamente la
exclusión. Antes de usar el backend se prueba la combinación exacta de:

- versión Terraform fijada;
- endpoint y jurisdicción R2 reales;
- permisos reales de la credencial;
- bucket real;
- dos clientes independientes.

La prueba se hace con un root y keys **desechables** dentro de los dos buckets
reales registrados, sin recursos de proveedor:

1. Inicializar el backend y realizar una escritura/lectura de state controlada.
2. Hacer que el cliente A mantenga un lock de escritura mediante un harness
   controlado.
3. Intentar desde B una operación que requiera lock con un `-lock-timeout`
   corto.
4. Confirmar que B espera o falla sin escribir state.
5. Liberar normalmente A y confirmar que B puede adquirir el lock.
6. Repetir terminando A de forma abrupta; documentar detección y recuperación
   del `.tflock` sin usar producción.
7. Demostrar primero que la credencial del otro root puede listar, leer,
   escribir y borrar un objeto desechable en su propio bucket; después exigir
   `403 AccessDenied` XML exacto para listar, leer, escribir y borrar en el
   bucket objetivo.
8. Repetir después de actualizar Terraform o ante un cambio relevante de R2.

La prueba reproducible se ejecuta con
[`scripts/test-terraform-r2-locking.sh`](../scripts/test-terraform-r2-locking.sh)
y el root desechable
`infra/terraform/testing/r2-lock`. Se preparan dos `.tfbackend`, uno por root,
con sus buckets registrados pero keys exclusivas `lock-tests/...tfstate`; las
credenciales primarias entran por entorno y las del otro root por dos ficheros
`0600`. Se declaran también `--root` y `--other-root`; no se admite un tercer
bucket o una credencial distinta de las registradas. El harness:

1. hace que A mantenga el lock durante un `terraform_data`;
2. exige que B falle específicamente al adquirirlo;
3. prueba liberación normal;
4. mata A con `SIGKILL`, exige el lock huérfano y recupera solo ese ID;
5. reconcilia y destruye el `terraform_data`;
6. exige el control positivo completo de la credencial cruzada en su bucket;
7. exige que esa misma credencial no pueda inicializar el backend objetivo y
   reciba `AccessDenied` exacto en las cuatro operaciones S3;
8. solo entonces publica el proof de 24 horas y su firma OpenSSH adyacente.

Ejemplo sin valores reales:

```bash
AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \
scripts/test-terraform-r2-locking.sh \
  --root cloudflare/apptolast-dns \
  --backend-role production \
  --backend-config /ruta/segura/dns-lock-test.tfbackend \
  --other-root netcup/perimeter \
  --other-backend-config /ruta/segura/netcup-lock-test.tfbackend \
  --other-access-key-file /ruta/segura/netcup-access-key-id \
  --other-secret-key-file /ruta/segura/netcup-secret-access-key \
  --operator OPERADOR_RESPONSABLE \
  --proof-output /ruta/fuera/del/repo/dns-locking-proof.json \
  --signing-key /ruta/segura/locking-proof-signing-key
```

El resultado se registra con el esquema
[`infra/terraform/locking-proof.json.example`](../infra/terraform/locking-proof.json.example).
`scripts/terraform-safety.py locking-scope` calcula desde la metadata de un
`terraform init` la representación canónica, el rol versionado
(`production` o `pending_destination`) y su SHA-256. Para probar un destino R2
antes de copiar el state se usa `--backend-role pending_destination`; el bucket
de control positivo del otro root siempre debe seguir siendo su `production`.
El proof:

- usa Terraform `1.15.8`;
- coincide con ese `locking_scope_sha256`;
- identifica los dos roots, buckets y hashes de Access Key IDs registrados;
- liga por hash el commit limpio y cada fichero del harness ejecutado;
- identifica un operador responsable;
- marca los catorce resultados obligatorios como verdaderos, incluido el lease
  distribuido CAS;
- usa timestamps UTC y una vigencia máxima de 24 horas.

No contiene Access Keys en claro, solo sus hashes SHA-256. El state vacío de la
key desechable puede conservarse
como evidencia o retirarse posteriormente con la API R2 tras la retención
acordada; nunca se reutiliza como state productivo. `plan-terraform.sh`,
`apply-terraform.sh` y
`migrate-terraform-state.sh` vuelven a comprobar proof, backend y vigencia; un
JSON copiado, caducado o ligado a otro bucket falla cerrado. El proof es un
registro de una prueba ejecutada, no un sustituto de ejecutarla.

El repositorio solo aporta
`infra/terraform/lock-proof.allowed-signers.example`. Antes de probar, el
propietario aprueba una clave pública dedicada y commitea
`lock-proof.allowed-signers`; el fichero de ejemplo no autoriza a nadie. El
proof y su `.sig` se guardan fuera de Git. Sin ese registro real, todos los
writers R2 permanecen bloqueados.

Además del lock `.tflock` de Terraform, cada `apply` remoto toma un coordinador
persistente y fijo por root bajo `operation-leases/<ROOT>.json` en el bucket
`production` autoritativo. Es una máquina de estados CAS ligada al commit,
registro de backends, identidad del backend y hash del plan:
`acquired → prestate_verified → writer_started → writer_finished →
snapshot_verified → poststate_verified → released`. Cada transición usa
`If-Match`, incrementa generación, cambia nonce y encadena el SHA-256 del
documento anterior. Un timeout se reconcilia con `GET`; nunca se reintenta una
escritura ambigua a ciegas. El token propietario local es exclusivo, `0600` y
no se guarda en Git.

Tras `writer_started`, un error, `INT`, `TERM` o `HUP` intenta el snapshot
estructural y deja el coordinador `quarantined`; nunca expira ni se roba por
TTL. `r2-operation-lease.py recover` solo libera mediante CAS el ETag exacto
después de verificar una aprobación OpenSSH vigente, el hash íntegro del
documento, operation ID y generación. La lista de firmantes se lee del
`source_commit` del propio lease, no del worktree. Deben commitearse
`lease-recovery.allowed-signers` y `snapshot-recipients.json`; sus `.example`
no autorizan nada. Antes de firmar recovery hay que demostrar que el controlador
antiguo está parado o su credencial revocada.

Terraform bloquea automáticamente las operaciones escritoras cuando el backend
lo soporta y desaconseja `-lock=false`:
[state locking](https://developer.hashicorp.com/terraform/language/state/locking).

Si la exclusión mutua, creación condicional o liberación falla una sola vez, R2
no se aprueba como backend escritor hasta rediseñar el locking. No se rebaja la
prueba ni se usa `-lock=false` para continuar.

## Credenciales y sesiones

Para cada root se abre una sesión de entorno separada con:

```text
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
```

Los wrappers eliminan cualquier `TF_*` heredado antes de fijar sus propias
variables y usan `TF_CLI_CONFIG_FILE=/dev/null`; así `TF_CLI_ARGS`,
`TF_REATTACH_PROVIDERS`, trazas y `~/.terraformrc` no pueden alterar la
ejecución. También rechazan perfiles/roles AWS alternativos, endpoints, CA
personalizadas y proxies heredados. Una red que necesite proxy o CA privada
requiere un rediseño y revisión explícitos, no una variable ambiental oculta.

La sesión de bootstrap no usa credenciales S3. Solo recibe el token Cloudflare,
el account ID como variable y los nombres de bucket mediante un `.tfvars`
ignorado.

La sesión DNS añade únicamente:

```text
CLOUDFLARE_API_TOKEN
```

El account ID y el zone ID no son secretos ni variables de sesión: están
fijados en el root DNS.

La sesión Netcup añade únicamente:

```text
NETCUP_SCP_REFRESH_TOKEN
```

No se guardan en:

- Git, HCL, tfvars versionados o `.tfbackend`;
- historial del shell, logs, tickets o output de CI;
- planes compartidos;
- secretos Swarm: esos backends no se consumen desde servicios.

Los tokens R2 `Object Read & Write` pueden limitarse a buckets específicos y
sus Secret Access Keys solo se muestran al crearlos:
[autenticación R2](https://developers.cloudflare.com/r2/api/tokens/). La
custodia y rotación deben estar resueltas antes de inicializar los backends.

El state y los planes pueden contener datos sensibles aunque los outputs estén
marcados `sensitive`. HashiCorp explica que el state conserva esos valores:
[datos sensibles en Terraform](https://developer.hashicorp.com/terraform/language/manage-sensitive-data).

## Inicialización y primera adopción

Cada root incluye `terraform_data.root_identity`, con root, backend esperado,
workspace y los IDs inmutables de su dominio. Los wrappers no aceptan un state
poblado sin esa identidad ni una identidad de otro root.
Todos los recursos externos declaran dependencia explícita de esa identidad.
Terraform debe completar primero el sentinel local, incluidas sus lecturas,
antes de importar o cambiar objetos del proveedor; esto evita que un fallo
parcial normal deje recursos gestionados en state sin identidad recuperable.

Primero se inicializa o adopta `cloudflare/state-bootstrap` desde su state local
cifrado. Después, para cada root remoto y de uno en uno:

1. Confirmar y commitear la identidad de producción exacta —incluido el hash
   del Access Key ID— en `backend-identities.json`.
2. Confirmar que la credencial del otro root no tiene acceso.
3. Ejecutar la prueba de locking.
4. Buscar state anterior local/remoto y automatizaciones que puedan escribirlo.
5. Crear snapshot cifrado de cualquier state encontrado.
6. Migrar el state con el wrapper fail-closed y las configuraciones parciales
   revisadas, siguiendo el
   [contrato oficial de backends](https://developer.hashicorp.com/terraform/language/backend).
7. Registrar lineage, serial, Terraform version, root, commit, bucket y key.
8. Inventariar los recursos reales del proveedor.
9. Importar los preexistentes según [`MIGRATION.md`](MIGRATION.md).
10. Revisar un plan completo y no destructivo antes de autorizar cambios.

No se inicializa un state vacío y se aplica sobre infraestructura existente
esperando que Terraform “la detecte”. Si el state antiguo no se localiza, la API
del proveedor y el import son la fuente para reconstruir el mapeo.

`-reconfigure` no migra state. El wrapper inicializa ambos extremos por
separado y transmite `state pull | state push` sin `-force`.

El wrapper de planes obliga a declarar `--backend-mode established` o
`--backend-mode initialize`:

- `established` exige la identidad exacta y permite que falten recursos
  modelados para que Terraform pueda crearlos, pero rechaza toda dirección de
  state ajena al allowlist del root;
- `initialize` exige ausencia verificable del objeto de state; un objeto
  existente se rechaza aunque no tenga recursos/outputs. También exige una
  confirmación
  `INITIALIZE:<ROOT>:<COMMIT>:<BACKEND_SHA256>`. El plan debe ser no destructivo,
  contener el inventario deseado exacto y crear la identidad. Esto permite los
  imports declarativos revisados de DNS, sin convertir un state poblado
  desconocido en uno “establecido”.

Todo plan se ejecuta contra una copia temporal obtenida con `git archive` del
commit limpio, no contra HCL mutable del checkout. Requiere una clave privada
de firma mediante `--signing-key`. Todo plan con cambios produce
`PLAN.metadata.json` y `PLAN.metadata.json.sig`: liga los hashes del plan
binario y JSON, commit limpio, root, backend, proof, workspace, variables,
lineage/serial previos, inventario previsto y acciones. Cualquier `delete`,
reemplazo, provider action/action trigger, deferred change o campo de ejecución
desconocido se rechaza; no existe override. El apply verifica la firma contra
el registro versionado `plan.allowed-signers`, vuelve a materializar el mismo
commit y rechaza además timestamps futuros y planes con más de una hora para
no ejecutar una
vista congelada antes de drift externo reciente.

Los wrappers no confían únicamente en el exit code. `init`, `plan` y `apply`
usan la salida NDJSON oficial; `validate` usa JSON. Cualquier evento
`warn`/`error`, diagnóstico, `stderr` no vacío, JSON duplicado o stream
truncado falla cerrado aunque Terraform termine con `0` o `2`. Esto es
necesario porque un bloque `check` fallido es solo un warning y Terraform
continúa. Cada root tiene un inventario exacto de validations de variables,
preconditions y bloques `check`; todos sus padres e instancias deben estar en
`pass` tanto en el plan como en el state crudo y en `terraform show -json`
posterior al apply. El sidecar firmado liga esa proyección, su hash y el hash
del stream limpio.

El repositorio aporta solo `plan.allowed-signers.example`; el propietario debe
aprobar una clave pública dedicada y commitear el fichero real. Ni el ejemplo
ni una clave generada automáticamente establecen confianza.

`apply-terraform.sh` es el único apply documentado. Recalcula ese sidecar contra
la realidad, exige
`APPLY:<ROOT>:<COMMIT>:<PLAN_SHA256>:<RECIPIENT_SHA256>`, crea snapshot cifrado
previo (salvo que `initialize` haya demostrado que no existe state), aplica el
plan guardado, exige que el inventario final coincida con el previsto y crea el
snapshot posterior. Incluso si Terraform devuelve error tras un cambio parcial,
el wrapper intenta primero un snapshot post-intento, conserva el código de
fallo y exige reconciliación; no confunde el snapshot con un apply correcto.

La migración se separa en dos commits y un control externo. En el primero,
`backend-identities.json` conserva el origen como `production` y declara el
destino como `pending_destination`. Solo
`migrate-terraform-state.sh` acepta ese destino pendiente; plan/apply normales
siguen autorizando exclusivamente el origen. El wrapper exige destino sin
ningún objeto de state, locking/proofs, snapshot origen, confirmación ligada a
ambos backends y lineage/serial, verificación bit a bit de la atestación lógica
y snapshot destino. Después de la confirmación hace un segundo pull: lo
bufferiza en memoria, recalcula contrato/hash/checks, exige que coincida con la
atestación confirmada y comprueba `stderr` antes de emitir un solo byte al
`state push`. Nunca usa `-force`; el push adquiere el lock de destino y
conserva las protecciones de lineage/serial de Terraform.
Si otro writer puebla el destino después del preflight, el push falla en vez de
sobrescribirlo. Una transición local→S3 requiere el commit/configuración que
declare ese cambio de
tipo y un procedimiento específico; un fichero `-backend-config` no cambia el
tipo de backend HCL. Un error durante la copia también dispara un intento de
snapshot de emergencia del destino antes de devolver el fallo original.

La migración R2 toma un único lease en el coordinador del origen autoritativo
antes de leer state y lo liga simultáneamente a las identidades SHA-256 de
origen y destino. No toma primero un lock del destino pendiente. El digest
SHA-256 canónico del state completo forma parte de la confirmación y se compara
contra destino y contra una nueva lectura del origen antes de liberar el lease.

STOP de seguridad actual: `apply-terraform.sh` y
`migrate-terraform-state.sh` rechazan `cloudflare/state-bootstrap`. Un `flock`
local no conserva una cuarentena después de `SIGKILL`, por lo que permitir ese
writer daría menos garantías que R2. La ruta futura segura es mover su state a
un backend remoto con el mismo lease persistente o implementar antes una
máquina de estados local duradera, firmada y ensayada; no se autoriza un
override manual en estos wrappers.

El `plan` de `cloudflare/state-bootstrap` emitía dos warnings reales e
inevitables del provider Cloudflare `5.22.0`,
`Resource Destruction Considerations`, uno por cada
`cloudflare_r2_managed_domain` declarado (los tres buckets R2 en sí no
emiten el warning; sus dominios gestionados sí, porque esa API no tiene
`delete`). La política del repo es cero warnings sin allowlist genérica ni
silenciamiento por flag. `scripts/terraform-safety.py` reconoce ahora, de
forma estrecha y verificada contra un `terraform plan -json` real capturado
el 2026-07-27, exactamente esa combinación
(`severity=warning`, el `summary`/`detail` exactos del provider, dirección
con prefijo `cloudflare_r2_managed_domain.`) y solo para `plan` en este
root exacto — cualquier otro diagnóstico, en cualquier otro root, o durante
`apply`, sigue rechazándose sin excepción. El STOP de `apply`/`import`
descrito arriba (falta de cuarentena persistente del backend local) sigue
vigente sin cambios: un plan limpio no autoriza escribir state.

STOP de cutover actual: todo create o cambio DNS hacia la IP de plataforma
desde ausencia/legacy se rechaza, incluido `edge` durante initialize. El
coordinador futuro deberá aceptar un proof JSON canónico firmado y de corta
vigencia, ligado como mínimo a `schema`, contrato/commit del repo, IP origen y
destino, root/backend, hash del catálogo de servicios, hashes de metadata
desplegada e imágenes, restore markers, resultados de smoke HTTPS/TCP por
servicio, operation ID/lock del host, operador y `tested_at`/`valid_until`.
El wrapper deberá mantener el mismo lock de mutación host desde esas pruebas
hasta adquirir el lease Terraform y aplicar el plan. Ese esquema todavía no
está implementado ni aceptado; por tanto no existe proof que pueda saltar el
STOP.

El éxito significa solo «copia verificada». Después se congelan y revocan de
forma comprobable todos los writers/credenciales del origen. Solo entonces un
segundo commit promueve el destino a `production` y elimina
`pending_destination`. El wrapper no afirma ni automatiza esa revocación
externa.

No se copia el state monolítico antiguo completo a ambos roots. Se divide por
recursos mediante un procedimiento ensayado, con el state antiguo congelado y
snapshots antes y después. Durante el traspaso puede existir temporalmente un
mapeo duplicado, pero solo el root nuevo puede escribir y la duplicidad se
elimina en cuanto se verifica el import.

## Política de snapshots

### Cuándo

Crear snapshots:

- antes y después de cada import, state move/remove, backend migration o apply;
- antes y después de actualizar Terraform/provider;
- de forma periódica aunque no haya cambios;
- inmediatamente ante sospecha de corrupción o credencial comprometida.

### Cómo

1. Bloquear otras automatizaciones/escritores.
2. Ejecutar
   [`scripts/snapshot-terraform-state.sh`](../scripts/snapshot-terraform-state.sh)
   con el root, `.tfbackend` ignorado, fichero de recipients `age` y directorio
   de salida explícitos. El helper usa un `TF_DATA_DIR` temporal, valida en
   memoria que `terraform state pull` sea JSON no vacío y que cumpla el
   contrato exacto del root —incluido Terraform fijado y todos los
   `check_results` en `pass`—, comprueba que `stderr` esté vacío, lo canaliza a
   `age` y no escribe state en claro.
   Tras un writer fallido se usa automáticamente validación estructural de
   emergencia (formato, lineage, serial, outputs y resources), para poder
   preservar un state parcial que todavía no cumpla el inventario final sin
   confundirlo con un snapshot normal válido.
   El fichero de recipients debe coincidir byte a byte con el SHA-256 aprobado
   en el registro versionado `snapshot-recipients.json`.
3. Confirmar que el artefacto `.tfstate.age` y su checksum no están dentro del
   repositorio y tienen permisos restrictivos. El helper usa nanosegundos y un
   nonce exclusivo, rechaza symlinks y comprueba que `mv --no-clobber` publicó
   realmente ambos ficheros. Antes de declarar éxito hace `fsync` del
   ciphertext temporal, del publicado, del checksum y del directorio tras cada
   rename; así ni una colisión ni un corte de energía posterior a la liberación
   del lease puede convertir un éxito reportado en un snapshot perdido.
4. Enviar la copia ya cifrada a almacenamiento offsite de otro
   proveedor/cuenta/dominio de fallo.
5. Registrar root, timestamp UTC, commit, Terraform/provider versions, bucket,
   key, lineage, serial, checksum y custodio.
6. Probar periódicamente descifrado y lectura de state en un entorno aislado.

Ejemplo estructural, con rutas reales elegidas por el operador:

```bash
scripts/snapshot-terraform-state.sh \
  --root cloudflare/apptolast-dns \
  --backend-config /ruta/ignorada/backend.r2.tfbackend \
  --recipient-file /ruta/segura/recipients.txt \
  --output-dir /ruta/fuera/del/repositorio
```

El helper protege la copia local, no acredita que `--output-dir` sea offsite. El
paso 4 sigue siendo obligatorio y debe verificarse desde otro sistema.

El destino, herramienta de cifrado, clave, retención, periodicidad y responsable
todavía no están disponibles en este repositorio. Hasta decidirlos y completar
un restore rehearsal, cualquier `apply` de producción queda bloqueado.

R2 declara alta durabilidad, pero su propia documentación aclara que la
durabilidad no evita borrados intencionados o accidentales:
[durabilidad R2](https://developers.cloudflare.com/r2/reference/durability/).

## Recuperación

### Diagnóstico

Ante state ausente, corrupto o sobrescrito:

1. Parar todos los pipelines y prohibir writes manuales.
2. No ejecutar `apply`, `refresh`, `import`, `state rm`, `push` ni
   `force-unlock` hasta delimitar el incidente.
3. Capturar de forma cifrada el state remoto actual, `.tflock`, logs y metadata
   de R2.
4. Confirmar root, bucket, key, workspace, lineage y serial.
5. Inventariar la infraestructura real con APIs read-only.
6. Seleccionar el último snapshot cuyo checksum y restore hayan sido
   verificados.

### Restauración

1. Descifrar el snapshot en un entorno aislado y de acceso restringido.
2. Comparar lineage/serial y recursos con el state remoto y la realidad.
3. Confirmar el commit y versiones capaces de leerlo.
4. Obtener aprobación de dos personas para cualquier sobrescritura remota.
5. Preferir la migración soportada por `terraform init` cuando se cambia de
   backend.
6. Usar `terraform state push` solo como acción break-glass: HashiCorp la
   califica de extremadamente peligrosa porque sobrescribe el state.
7. No usar `-force` para saltar protecciones de lineage/serial salvo incidente
   documentado, backup adicional y aprobación explícita.
8. Ejecutar un plan completo contra la realidad antes de cualquier apply.
9. Guardar snapshots cifrados del estado anterior y restaurado.
10. Rehabilitar un solo escritor y vigilar el primer ciclo completo.

Las protecciones y riesgos de pull/push están documentados en
[almacenamiento y locking de state](https://developer.hashicorp.com/terraform/language/state/backends#manual-state-pullpush).

Restaurar state no restaura un recurso borrado ni los datos de una aplicación:
solo recupera el mapeo conocido por Terraform. La recuperación de recursos y
datos tiene sus propios backups/runbooks.

## Rotación y revocación R2

Por cada root:

1. Crear una credencial nueva limitada al mismo bucket.
2. Actualizar y commitear el hash del nuevo Access Key ID en
   `backend-identities.json`; desde ese commit la credencial anterior deja de
   ser aceptada por los wrappers.
3. Probar lectura, locking, control positivo cruzado y escritura controlada;
   producir un proof nuevo porque el scope de credencial cambió.
4. Actualizar el almacén seguro del único pipeline autorizado.
5. Ejecutar plan sin cambios inesperados.
6. Revocar la credencial anterior.
7. Confirmar que el otro root y consumidores no se afectaron.
8. Registrar IDs y fechas, nunca Secret Access Keys.

Si se sospecha exposición, se detienen escritores, se revoca primero la
credencial afectada, se inspecciona R2 y se compara con snapshots offsite antes
de volver a aplicar.

## Compuertas para autorizar producción

- [ ] Dos nombres de bucket reales, distintos y privados.
- [x] Root Terraform de bootstrap con `prevent_destroy` y `r2.dev` desactivado.
- [ ] State de bootstrap en ruta cifrada, off-repository y respaldada.
- [x] Ejemplos de backend separados mediante placeholders no utilizables.
- [ ] Placeholders sustituidos por dos nombres reales en ficheros ignorados.
- [ ] Dos credenciales bucket-scoped distintas y custodiadas.
- [ ] `backend-identities.json` real, revisado y commiteado con hashes de IDs.
- [ ] Claves públicas dedicadas aprobadas en ambos registros de firmantes.
- [ ] Endpoint/account/jurisdicción verificados.
- [ ] Locking, recuperación, controles positivos y `AccessDenied` cruzado probados.
- [ ] State antiguo localizado o búsqueda negativa documentada.
- [ ] Inventario/import completo sin dual writer.
- [ ] Plan no destructivo revisado.
- [ ] Snapshots cliente cifrados antes y después.
- [ ] Copia offsite fuera del VPS y de la cuenta/proveedor primario.
- [ ] Restore rehearsal con checksum, lineage y serial verificados.
- [ ] Retención, responsables, rotación e incidente aprobados.

Ninguna casilla se marca por la mera existencia de un fichero en Git.

## Referencias oficiales

- [Cloudflare: backend remoto R2 para Terraform](https://developers.cloudflare.com/terraform/advanced-topics/remote-backend/)
- [Cloudflare: autenticación y tokens R2](https://developers.cloudflare.com/r2/api/tokens/)
- [Cloudflare: compatibilidad S3 de R2](https://developers.cloudflare.com/r2/api/s3/api/)
- [Cloudflare: seguridad de datos R2](https://developers.cloudflare.com/r2/reference/data-security/)
- [Cloudflare: bucket locks R2](https://developers.cloudflare.com/r2/buckets/bucket-locks/)
- [HashiCorp: backend S3](https://developer.hashicorp.com/terraform/language/backend/s3)
- [HashiCorp: configuración de backend](https://developer.hashicorp.com/terraform/language/backend)
- [HashiCorp: state locking](https://developer.hashicorp.com/terraform/language/state/locking)
- [HashiCorp: almacenamiento y recuperación manual](https://developer.hashicorp.com/terraform/language/state/backends)
- [HashiCorp: datos sensibles](https://developer.hashicorp.com/terraform/language/manage-sensitive-data)
