# OrganizationWeb: despliegue independiente del MVP

Este procedimiento aplica el catálogo `config/organizationweb.yml`, sin
modificar `config/services.yml`, generaciones de datos legacy ni su marcador
de restauración. El release publicado es el commit MVP que declara el catálogo;
las dos imágenes deben conservar esa revisión OCI además de su digest.
No incluye la feature 19 aún en desarrollo.

## Precondiciones y alcance

- Commit revisado, CI verde y checkout limpio en el host. Ejecutar bootstrap,
  validate-iac y lint según AGENTS; conservar sus salidas y el commit.
- Un solo nodo Swarm, manager activo y elegible para workloads. El role lo
  comprueba antes de usar healthchecks locales. No hay puertos nuevos públicos.
- Perfil activo `organizationweb`: edge y workloads conservan su presupuesto;
  observability queda fuera de este perfil y su despliegue se rechaza. `site`
  también se rechaza, porque incluye observability. No es una desinstalación:
  cualquier servicio live incompatible o desconocido bloquea el preflight.
- DNS de `organizacion.apptolast.com` ya verificado por el operador. El certificado
  y la ruta TLS no quedan acreditados hasta su comprobación posterior.
- Backup externo y escrow/autolock continúan con sus límites documentados.
  Este despliegue no cierra el gate de migración legacy ni certifica recuperación
  ante pérdida del host. No ejecutar el playbook backup como atajo.

El plan app suma 6784 MiB de reservas y 11456 MiB de límites de servicios,
manteniendo 3072 MiB de reserva del sistema y 512 MiB de margen. Es un sizing
inicial probado nominalmente, no una garantía de carga sostenida.

## Bootstrap de siete secretos externos

El operador conserva las credenciales en su almacenamiento protegido, fuera
del repositorio, y prepara un script temporal root-only fuera del checkout.
No incluir valores en argumentos, historial, logs, Git, variables exportadas
ni salida de los comandos. Desactivar xtrace (`set +x`) antes de cargarlos.
Los passwords DB/Rabbit deben ser hexadecimales aleatorios de 64 caracteres,
evitando interpretaciones adicionales en `rabbitmq.conf`.

Los nombres exactos y la procedencia exigida son:

| Nombre | Contenido |
| --- | --- |
| organizationweb-db-username-v1 | `organization` |
| organizationweb-db-password-v1 | Password DB conservado por el operador |
| organizationweb-auth-username-v1 | Usuario de acceso a la aplicación |
| organizationweb-auth-password-v1 | Password de acceso conservado |
| organizationweb-rabbitmq-username-v1 | `organization` |
| organizationweb-rabbitmq-password-v1 | Password Rabbit conservado |
| organizationweb-rabbitmq-config-v1 | Configuración con esos mismos datos |

Todos llevan `com.apptolast.managed-by=manual-bootstrap` y
`com.apptolast.purpose=organizationweb`. Son objetos inmutables. Si alguno ya
existe, verificar el contexto y la operación anterior; no sustituir credenciales
ni recrear una mitad del conjunto con passwords nuevos. Un bootstrap parcial
se resuelve con las mismas credenciales conservadas, nunca inventando su valor.

El cuerpo del script protegido, una vez cargadas las variables shell locales
`ORG_DB_PASSWORD`, `ORG_AUTH_USERNAME`, `ORG_AUTH_PASSWORD` y
`ORG_RABBIT_PASSWORD`, crea mediante stdin (sin imprimir valores):

```bash
set +x
set -euo pipefail
create_secret() {
  docker secret create \
    --label com.apptolast.managed-by=manual-bootstrap \
    --label com.apptolast.purpose=organizationweb "$1" - >/dev/null
}
printf %s organization | create_secret organizationweb-db-username-v1
printf %s "$ORG_DB_PASSWORD" | create_secret organizationweb-db-password-v1
printf %s "$ORG_AUTH_USERNAME" | create_secret organizationweb-auth-username-v1
printf %s "$ORG_AUTH_PASSWORD" | create_secret organizationweb-auth-password-v1
printf %s organization | create_secret organizationweb-rabbitmq-username-v1
printf %s "$ORG_RABBIT_PASSWORD" | create_secret organizationweb-rabbitmq-password-v1
printf '%s\n' 'default_user = organization' \
  "default_pass = $ORG_RABBIT_PASSWORD" 'default_vhost = organization' \
  | create_secret organizationweb-rabbitmq-config-v1
unset ORG_DB_PASSWORD ORG_AUTH_USERNAME ORG_AUTH_PASSWORD ORG_RABBIT_PASSWORD
```

Ejecutarlo bajo el lock real, con la ruta absoluta del script root-only revisado:

```bash
sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
  --operation organizationweb-secrets -- \
  /bin/bash /root/organizationweb-secrets-v1.sh
```

El role sólo inspecciona metadatos de secrets, con `no_log`. API monta seis
archivos configtree 0400 para UID999; PostgreSQL usa sus dos `_FILE` para UID70;
RabbitMQ monta configuración 0400 para UID100/GID101. El role crea únicamente
los directorios nuevos: PostgreSQL y RabbitMQ con 0700. Rechaza enlaces y
propiedad/escritura inseguras en las cadenas de instalación y almacenamiento.

## Check, apply y aceptación

Desde el checkout limpio revisado, ejecutar secuencialmente:

```bash
./scripts/deploy-ansible.sh --local --playbook edge --check
./scripts/deploy-ansible.sh --local --playbook edge --confirm-production
./scripts/deploy-ansible.sh --local --playbook organizationweb --check
./scripts/deploy-ansible.sh --local --playbook organizationweb --confirm-production
```

Edge crea sólo una red adicional cifrada y un router file-provider para la
aplicación; conserva los ocho routers y redes legacy. Mantener los Configs
anteriores de Traefik para rollback. OrganizationWeb necesita esa red antes
del check. Cada operación está ligada a commit y contratos, bajo el mismo lock.
Si falla, conservar el marker y usar la recuperación existente descrita en
OPERATIONS; no borrarlo ni saltar su guard.

El apply exige cuatro réplicas, healthchecks y referencias exactas de red y
secrets. El operador verifica después HTTPS sin desactivar TLS, sesión con
cookie Secure, login, lectura, escritura y publicación outbox/broker. También
comprueba que los endpoints legacy siguen accesibles. Repetir el apply y exigir
convergencia; conservar salidas, hashes y límites de estas observaciones.

## Rollback sin destruir datos

En una actualización posterior, mantener el commit de catálogo anterior y sus
secrets; restablecer esos digests en un nuevo commit revisado, pasar gates y
repetir check/apply del role. Antes de bajar una versión, comprobar compatibilidad
de esquema y disponer de backup probado: Swarm rollback no revierte Flyway.

En la primera instalación, no existe versión anterior de la aplicación. Si es
necesario retirarla, el operador puede ejecutar la retirada del stack nuevo
bajo el lock, conservando datos y los siete secrets:

```bash
sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
  --operation organizationweb-initial-rollback -- \
  /usr/bin/docker stack rm organizationweb
```

La retirada es asíncrona: comprobar sólo ese namespace hasta que no tenga
servicios. No borrar `/srv/organizationweb`, redes globales, secretos ni
volúmenes. Para retirar el router nuevo, revertir sus cambios en un commit
revisado compatible con los ocho legacy y ejecutar edge check/apply por el
wrapper; no editar dinámicamente el file-provider ni degradar todo el checkout
a un snapshot de seguridad obsoleto. Una ruta de aplicación sin backend puede
devolver 503 durante esta retirada; el edge compartido debe seguir sano.

## Aceptación productiva del 7 de septiembre de 2026

El catálogo `491e2c2`, con imágenes de aplicación `4d34b9c`, convergió
con cuatro servicios saludables. La aplicación real terminó con 38 tareas
correctas, tres cambios y ningún fallo. La repetición terminó con 38 tareas
correctas, cero cambios y los mismos cuatro contenedores. Los 16 servicios
anteriores conservaron una réplica disponible y ocho rutas mantuvieron sus
códigos HTTP previos. PR29 incorporó este catálogo a main como `5607afc`.

La aceptación autenticada por HTTPS creó datos sintéticos identificados:
proyecto, tarea, reserva cancelada y sesión iniciada, pausada, reanudada y
cerrada. El cierre idempotente recuperó el mismo recibo; historial y revisión
semanal conservaron el tiempo neto. No quedó una sesión activa ni una reserva
futura de la aceptación. Nueve eventos figuraban publicados en outbox; las
colas mostraron los recuentos esperados, sin consumir sus mensajes.

Se ejecutó `pg_dump` en formato custom bajo el bloqueo de operación del host.
El archivo se creó con exclusividad, sin seguir enlaces, y se sincronizó a
disco. El directorio `/var/backups/organizationweb` es root:root 0700; el
archivo es root:root 0600. La copia del 7 de septiembre a las 17:02:34 UTC
ocupa 46.689 bytes. Su SHA256 es:

```text
c0a92ee91b5ceb2bbac738a5d8fd749cf9a9497667c9a2cdf29044fef196299a
```

Se restauró esa copia real, transferida por SSH, en PostgreSQL local aislado,
sin red ni puertos publicados. `pg_restore` terminó correctamente sobre un
esquema inicialmente vacío: 18 migraciones, proyecto, tarea, sesión y nueve
eventos recuperados. Las huellas de sesión, cambios e intervalos coincidieron
con el origen. El contenedor y su volumen de prueba se retiraron; el archivo
original protegido se conserva en el servidor.

Esta prueba acredita restauración PostgreSQL de esa copia. No acredita
respaldo externo programado, restauración RabbitMQ, custodia de la clave de
Swarm ni RPO/RTO. Esas obligaciones conservan sus puertas operativas propias.
La mejora posterior de DNS de Nginx se valida y despliega por separado; este
apartado registra el corte aceptado, no un despliegue futuro del catálogo.

## Apariencia 20 desplegada y aceptada

El 7 de septiembre de 2026 se desplegó el catálogo `8aec158`, incorporado a
main mediante PR31 (`f89a014`), con la revisión OCI de producto
`ed00ad426842b4a85f2a0f849de014a8615ba76d`. Los índices API/web publicados
y verificados para linux/amd64, con attestation, coinciden con el catálogo:

```text
API sha256:fae45cecc45c8a3feed715524dd0cbfba6ecfac9eabd1ef50be740f84332ceb6
web sha256:3b939af19b1d66b05c8adef5649b9e5ecd3d8778aea0a3905c86e0c206be4d23
```

La CI inicial de aplicación 34152171279 dejó 141 casos correctos y dos
fallos de fixtures E2E; esa evidencia se conserva. Tras corregir los fixtures,
la CI de aplicación 34154520811 sobre `1f36315` y la CI de infraestructura
34154628641 terminaron SUCCESS. El replay dirigido de frontend terminó con
72 Killed de 79, separado del original y con sus siete residuos documentados.

El wrapper oficial terminó check con 27 tareas correctas, dos cambios y cero
fallos; apply con 38 correctas, cinco cambios y cero fallos, ambos EXIT 0.
Después, `UpdateStatus.State` de backend y web mostró `completed` y
`update completed`; no se dedujo convergencia sólo del retorno del wrapper.

La aceptación autenticada por HTTPS verificó defaults sin configurar,
PUT 200 y preferencia exacta tras recarga y nueva sesión. Los datos de trabajo
anteriores siguieron disponibles y las ocho rutas legacy conservaron sus
respuestas. PostgreSQL y RabbitMQ mantuvieron sus contenedores. No se
modificaron DNS, edge, usuarios, tmpfs, secrets, redes ni recursos para esta
actualización; el cambio operativo fue release y los dos digests.

Evidencia operativa fuera de Git:
`deployment-preparation/organizationweb-appearance-acceptance.json`, junto
a los logs check/apply del operador. SHA256 del JSON:

```text
581fbd7a32b5ef04d5020ab6fb29ae84926a762d4e234f1bc63ec2be2a458621
```

Las capturas de aceptación usan viewports emulados, no dispositivos físicos.
El primer cierre de sesión de UI no quedó acreditado por timeout de la
herramienta; se verificaron después un nuevo login y logout HTTP explícito
204 con sesión anónima. No se presenta el primer intento como éxito.

El backup fresco del 7 de septiembre de 2026 a las 18:40:34 UTC tiene
49.534 bytes y SHA256:

```text
6f1a141d7ed70667ac1f3271ab0ad939aacfcff774ecdc7fb945c9a0884a639e
```

El operador registro modo 0600, transferencia con hash identico y restore
completo EXIT 0 sobre destino vacio: 18 migraciones, proyecto, tarea, sesion
y nueve eventos; la tabla de apariencia aun no existia. El ensayo aislado
retiro sus recursos. Evidencia externa conservada:
`deployment-preparation/organizationweb-appearance-backup-restore.json`.
No incluye escrituras posteriores al instante de la copia.

Ademas, un ensayo local real arranco API20, migro V19 y guardo una preferencia;
luego arranco la API anterior `4d9469a` sobre esa misma base, con login y
lectura de proyectos correctos; finalmente API20 recupero la preferencia
exacta. Fila, tabla y toda la historia Flyway permanecieron identicas.
No se borraron migraciones ni se relajaron sus validaciones. Informe externo:
`deployment-preparation/organizationweb-v19-rollback-review.md` (EXIT 0).

Ese ensayo acredita compatibilidad API/Flyway local con V19, no rollback
Swarm/TLS/web ni recuperacion de escrituras concurrentes. Para retroceder,
restablecer los dos digests anteriores mediante nuevo catalogo revisado,
conservando datos, V19 y secrets; no restaurar automaticamente una copia
vieja encima de escrituras posteriores. RabbitMQ, respaldo externo
programado, escrow y RPO/RTO conservan sus limites y gates independientes.

## Personalización21 desplegada y aceptada

El catálogo aplicado usa `dfac90edcabdf04e442b906f0ab6db8894cbc4b2`.
Sólo cambian release e imágenes API/web; no cambia PostgreSQL, RabbitMQ,
edge, secrets, redes ni recursos. Publicación revisada por el operador en
`deployment-preparation/release21-dfac90e-publish-results.json`, fuera de Git.
Índices publicados para las referencias ocholoko888/organizationweb-api y
ocholoko888/organizationweb-web, respectivamente:

```text
backend sha256:83e75c196f05054fde4370c9d7f30605acd12995e63fbbc85ca118d3084472fd
web sha256:9fd69f52549c7c764fcc36d9fa8eaa658f50e73f3465d22094352a29e2a79cb9
```

Aceptación real del 8 de septiembre de 2026 a las 01:10:29 UTC. PR24 y PR33
fusionadas, CI de aplicación 34173869406 SUCCESS (151 E2E); mutación
incremental con las mismas 1824 firmas y score conservador 81,9079%.
Persisten 318 supervivientes, 7 sin cobertura y 5 errores del ejecutor;
no se presentan como eliminados.

El wrapper oficial aplicó infraestructura `0bb939b` (main `5add5b8`):
38 ok, 5 changed, 0 failed/unreachable, EXIT 0. Operación completada y lock
liberado limpiamente:

```text
c92fef1adddbebfecda4b396c9285dc699695398b023c46bc81f581efc36c60d
```

API/web ejecutan los índices anteriores, ambos healthy y 1/1. PostgreSQL y
RabbitMQ permanecen healthy, sin sustituir sus contenedores; los demás
servicios conservan 1/1. Flyway20 está aplicada correctamente. Las tres
tablas de personalización siguen vacías; se conservan un proyecto, una
tarea, una sesión, una apariencia y nueve eventos outbox.

La aceptación HTTPS autenticada comprobó GET de configuración PROJECT/TASK
y valores de las entidades existentes, DTO/defaults, ETag y no-store.
Se preservaron hashes de proyectos, tarea, estado de sesión y apariencia,
excluyendo únicamente serverNow dinámico del estado. No se crearon
campos QA ni valores en producción: las doce plazas incluyen inactivos.
Altas, cambios y rollback siguen acreditados mediante ensayos aislados.

Chromium verificó cinco superficies/anchos entre 320 y 1280 píxeles,
sin desbordamiento horizontal ni incidencias axe en esas observaciones.
Incluye recarga del detalle de tarea; no atribuye otros motores ni
sustituye la matriz UX previa. Las ocho rutas legacy conservaron su
respuesta esperada. Logout HTTP204, sesión anónima, acceso protegido401
y cierre de sesión del navegador verificados.

Evidencia externa, sin credenciales, en deployment-preparation:
`organizationweb-customization-acceptance.json` y
`organizationweb-customization-apply.log`. SHA256 respectivos:

```text
57449BC710183E77A7BC9DE04A586BDAE9968F38E15A850D0307A885295BD55A
54E7A0BEC2525CB59489E20D26AEA48596DE722B9EDD4D9D44032B898DBFBB71
```

Referencia de retroceso20: release
`ed00ad426842b4a85f2a0f849de014a8615ba76d` y ambos índices anteriores:

```text
API sha256:fae45cecc45c8a3feed715524dd0cbfba6ecfac9eabd1ef50be740f84332ceb6
web sha256:3b939af19b1d66b05c8adef5649b9e5ecd3d8778aea0a3905c86e0c206be4d23
```

Retroceder requiere un catálogo revisado y el wrapper oficial, conservando
base, historial Flyway y secrets. No borrar V20 ni las preferencias nuevas;
no restaurar automáticamente una copia antigua sobre escrituras posteriores.
El ensayo anterior con V19 no acredita compatibilidad con V20. Un ensayo
local tampoco acredita rollback Swarm/TLS ni escrituras concurrentes.
Backup fresco y restauración, RabbitMQ, copia externa programada, escrow y
RPO/RTO conservan los límites y gates documentados arriba.

Ensayo local21/20/21 EXIT 0 `9b43ce`, revisado por root: ambos ámbitos,
cuatro tipos, inactivos y vistas conservan DTO/ETag exactos. Datos, esquema
y Flyway permanecen idénticos; los recursos propios se retiraron (cero
restantes). Resultado externo en deployment-preparation:
`ow-v20-rollback-f5e857f8-785c-4c22-8975-cf8de24f16d6-result.json`.
SHA256:

```text
276E5FE54899D5B651479EBC7AF6D4A704B21863785A7A11F733F0BFED7ED80D
```

No acredita rollback del servidor, Swarm/TLS/web, RabbitMQ ni copia externa.
