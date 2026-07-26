# Estado Terraform y recuperación

## Estado conocido y estado desconocido

Existen tres roots declarados:

- `infra/terraform/cloudflare/state-bootstrap` declara los dos buckets R2. Su
  state local vive en almacenamiento cifrado fuera del checkout.
- `infra/terraform/cloudflare/apptolast-dns` administra el DNS de
  `apptolast.com`. Su key es
  `cloudflare/apptolast-dns/terraform.tfstate`.
- `infra/terraform/netcup/perimeter` administra firewall, asignación y claves
  SCP. Su key es `netcup/perimeter/terraform.tfstate`.

No se dispone en esta revisión de account ID R2, nombres definitivos de buckets,
Access Key IDs/Secret Access Keys, states remotos, states del repositorio
antiguo ni credenciales de proveedores. No se afirma que un backend esté
inicializado, que un recurso esté importado o que se haya ejecutado un `apply`.

Los dos ficheros `backend.r2.tfbackend.example` usan placeholders distintos y
rechazan así el bucket compartido del diseño anterior. No deben usarse en
producción hasta sustituir cada placeholder por un nombre real, privado y
distinto. Este documento no inventa esos nombres.

La mención a un bucket R2 “versionado” en documentación previa tampoco se toma
como capacidad real. Cloudflare publica `GetBucketVersioning` y
`PutBucketVersioning` como operaciones S3 no implementadas:
[compatibilidad S3 de R2](https://developers.cloudflare.com/r2/api/s3/api/).

## Bootstrap sin dependencia circular

`cloudflare/state-bootstrap` usa el backend `local` con una ruta absoluta
aportada mediante `backend.local.tfbackend`. La ruta debe estar en
almacenamiento cifrado, fuera del checkout y con copia offsite.

Ese root declara ambos buckets con `prevent_destroy` y mantiene `r2.dev`
deshabilitado. No crea Access Keys: introducir un Secret Access Key en Terraform
lo persistiría en el state que se intenta proteger. El token temporal que
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
- `use_lockfile = true`, solo después de superar la prueba de locking;
- ninguna URL pública, custom domain ni `r2.dev`;
- snapshots cifrados en otro proveedor/cuenta y fuera del VPS.

Los placeholders de diseño son:

- El root **DNS** usa el bucket `<R2_BUCKET_CLOUDFLARE_DNS>`, la key
  `cloudflare/apptolast-dns/terraform.tfstate` y la credencial
  `<R2_CREDENTIAL_DNS>`.
- El root **Netcup** usa el bucket `<R2_BUCKET_NETCUP_PERIMETER>`, la key
  `netcup/perimeter/terraform.tfstate` y la credencial
  `<R2_CREDENTIAL_NETCUP>`.

Los placeholders nunca se copian literalmente a producción. Los nombres se
eligen y registran fuera de Git, se comprueba que son distintos y se escriben
únicamente en los ficheros `.tfbackend` ignorados.

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

`use_lockfile = true` está declarado, pero no se confía en él hasta probar la
combinación exacta de:

- versión Terraform fijada;
- endpoint y jurisdicción R2 reales;
- permisos reales de la credencial;
- bucket real;
- dos clientes independientes.

La prueba se hace en un root y bucket **desechables**, sin recursos de
producción:

1. Inicializar el backend y realizar una escritura/lectura de state controlada.
2. Hacer que el cliente A mantenga un lock de escritura mediante un harness
   controlado.
3. Intentar desde B una operación que requiera lock con un `-lock-timeout`
   corto.
4. Confirmar que B espera o falla sin escribir state.
5. Liberar normalmente A y confirmar que B puede adquirir el lock.
6. Repetir terminando A de forma abrupta; documentar detección y recuperación
   del `.tflock` sin usar producción.
7. Verificar que una credencial de DNS no puede listar, leer o escribir el
   bucket Netcup y viceversa.
8. Repetir después de actualizar Terraform o ante un cambio relevante de R2.

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

La sesión de bootstrap no usa credenciales S3. Solo recibe el token Cloudflare,
el account ID como variable y los nombres de bucket mediante un `.tfvars`
ignorado.

La sesión DNS añade únicamente:

```text
CLOUDFLARE_API_TOKEN
TF_VAR_cloudflare_zone_id
```

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

Primero se adopta o aplica `cloudflare/state-bootstrap` desde su state local
cifrado. Después, para cada root remoto y de uno en uno:

1. Confirmar bucket, key, endpoint y credencial correctos.
2. Confirmar que la credencial del otro root no tiene acceso.
3. Ejecutar la prueba de locking.
4. Buscar state anterior local/remoto y automatizaciones que puedan escribirlo.
5. Crear snapshot cifrado de cualquier state encontrado.
6. Inicializar/reconfigurar el backend siguiendo el
   [contrato oficial de backends](https://developer.hashicorp.com/terraform/language/backend).
7. Registrar lineage, serial, Terraform version, root, commit, bucket y key.
8. Inventariar los recursos reales del proveedor.
9. Importar los preexistentes según [`MIGRATION.md`](MIGRATION.md).
10. Revisar un plan completo y no destructivo antes de autorizar cambios.

No se inicializa un state vacío y se aplica sobre infraestructura existente
esperando que Terraform “la detecte”. Si el state antiguo no se localiza, la API
del proveedor y el import son la fuente para reconstruir el mapeo.

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
   de salida explícitos. El helper usa un `TF_DATA_DIR` temporal, canaliza
   `terraform state pull` directamente a `age` y no escribe state en claro.
3. Confirmar que el artefacto `.tfstate.age` y su checksum no están dentro del
   repositorio y tienen permisos restrictivos.
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
2. Probar lectura, locking y escritura controlada.
3. Actualizar el almacén seguro del único pipeline autorizado.
4. Ejecutar plan sin cambios inesperados.
5. Revocar la credencial anterior.
6. Confirmar que el otro root y consumidores no se afectaron.
7. Registrar IDs y fechas, nunca Secret Access Keys.

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
- [ ] Endpoint/account/jurisdicción verificados.
- [ ] Locking concurrente y recuperación de lock probados.
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
