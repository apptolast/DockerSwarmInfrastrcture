# Migración verificada de datos

Este directorio porta el mecanismo de recuperación auditado desde
`apptolast/MigracionNetCup` en el commit
`79e9c79e5c9ca9f71a7f9011cde9a1bb3a1f8e42`.

El tooling no levanta servicios públicos. El Compose de restauración contiene
únicamente tres servicios PostgreSQL con cinco bases lógicas y un contenedor
n8n de mantenimiento, sin `ports`,
con redes internas. Tampoco contiene Traefik ni OpenClaw. El estado legado de
OpenClaw se rechaza de forma explícita y el runtime nuevo recibe un token
generado criptográficamente.

## Contrato de seguridad

- El manifiesto de la release, el número y tamaño de partes y todos los
  SHA-256 se validan antes de recomponer el paquete.
- La extracción rechaza rutas absolutas, `..`, enlaces, dispositivos,
  duplicados, raíces inesperadas y archivos fuera de límites.
- Los checksums internos deben cubrir exactamente todos los ficheros.
- El destino debe estar ausente o vacío y se publica mediante renombrado
  atómico; nunca se mezcla con datos anteriores.
- Solo se importan claves de la lista permitida. Cada valor se escribe
  literalmente en un fichero `0600`; no se usa `source`, `eval` ni dotenv.
- Cada secreto opcional declarado por el stack se materializa como fichero
  vacío `0600` cuando no existe en el origen; el montaje es determinista.
- Los valores secretos no aparecen en logs ni en `runtime-manifest.json`.
- Ningún paso de preparación, restore o finalización activa workflows n8n. La
  restauración conserva un inventario y los deja despublicados hasta la
  operación post-cutover separada y consentida del stack Swarm.

## Secuencia

Use ubicaciones privadas fuera del clon. Los ejemplos siguientes no ejecutan
ningún paso automáticamente:

```bash
migration_release_dir=/var/lib/dockerswarm-migration/release
install -d -m 0700 "${migration_release_dir}"
migration/scripts/download_release_backup.sh \
  backup-20260723T225340Z \
  "${migration_release_dir}"
```

La descarga necesita una sesión autenticada de GitHub CLI con acceso al
repositorio privado. Después, verifique el flujo completo cifrado:

```bash
migration_package="${migration_release_dir}/apptolast-data-20260723T225340Z.tar.zst.gpg"
migration_key="${migration_release_dir}/apptolast-data-20260723T225340Z.RECOVERY-KEY.txt"
migration/scripts/verify_encrypted_backup.sh \
  "${migration_package}" \
  "${migration_key}" \
  "${migration_release_dir}/ORIGINAL_SHA256SUMS"
```

Descifre en un destino nuevo y materialice el contrato canónico:

```bash
migration/scripts/decrypt_backup.sh \
  "${migration_package}" \
  "${migration_key}" \
  /var/lib/dockerswarm-migration/staging

sudo -- python3 migration/scripts/prepare_runtime.py \
  /var/lib/dockerswarm-migration/staging/apptolast-data-20260723T225340Z \
  --root /srv/dockerswarm/services
```

El destino canónico exige root para aplicar los UID/GID numéricos comprobados
de cada imagen. El comando falla antes de escribir si no puede garantizarlos.

La restauración se divide en fases reanudables. `all` restaura las bases core,
`vectors` con pgvector 0.8.1 y `rag` con pgvector 0.8.2:

```bash
migration/scripts/restore_databases.sh \
  /srv/dockerswarm/services \
  migration/compose/restore.yml \
  all

migration/scripts/restore_databases.sh \
  /srv/dockerswarm/services \
  migration/compose/restore.yml \
  status

migration/scripts/finalize_restore.sh \
  /srv/dockerswarm/services \
  migration/compose/restore.yml \
  config/services.yml

migration/scripts/restore_databases.sh \
  /srv/dockerswarm/services \
  migration/compose/restore.yml \
  cleanup
```

`finalize_restore.sh` se ejecuta solo después de completar todas las fases.
Comprueba en vivo las cinco bases, las versiones pgvector, n8n inactivo, cada
dataset y cada fuente de secret. Solo entonces crea atómicamente el gate
`restore-state/workloads-ready-v2.json` que permite desplegar el stack. El
marker v2 también ata el manifiesto runtime v4 y la clave privada de identidad
HMAC a sus SHA-256 exactos.

Los markers de `vectors` y `rag` no son simples banderas: quedan ligados al
SHA-256 del dump verificado, al número exacto de tablas, a una huella canónica
del esquema vivo y a la versión exacta de pgvector. Una reanudación vuelve a
comparar todos esos campos y `finalize_restore.sh` rechaza markers con un dump
obsoleto antes de firmar el gate.

`cleanup` elimina contenedores y redes de restauración, pero nunca los bind
mounts. Las aplicaciones solo se despliegan después desde el stack Swarm del
repositorio y tras comprobar bases, ficheros, secretos y rutas.

El estado ACME no se instala dentro de `services`. Se conserva, cubierto por el
checksum de recovery, en:

```text
/srv/dockerswarm/services/recovery/edge/traefik-acme.json
```

Antes de desplegar Edge y solo si el destino todavía no existe, se instala de
forma atómica con el UID/GID declarados para Traefik:

```bash
migration/scripts/install_traefik_acme.sh \
  /srv/dockerswarm/services \
  /srv/dockerswarm
```

El instalador verifica de nuevo todos los checksums, valida el JSON, rechaza
enlaces y nunca sobrescribe un `acme.json` existente.

## Entrega de secretos

`prepare_runtime.py` crea las fuentes bajo:

```text
/srv/dockerswarm/services/secrets/files/
```

Genera contraseñas independientes para n8n, Passbolt y Shlink, un token de
runners n8n y un token limpio para OpenClaw. Recupera únicamente los valores
permitidos de n8n, Passbolt, Shlink y la web de Pablo. La lista exacta de
nombres, nunca sus valores, queda en `runtime-manifest.json` con
`schemaVersion: 4`.

También genera `workloads_secret_identity_hmac_key`, una clave aleatoria
privada que permite atar cada Docker Secret inmutable a su fuente exacta
mediante una etiqueta HMAC-SHA-256. No es un Docker Secret consumido por una
aplicación, no aparece su valor en el manifiesto y debe conservarse junto a
las fuentes para poder verificar o reconstruir el Swarm.

Estos ficheros son la fuente privada y recuperable de Docker Swarm secrets
inmutables y versionados. No deben copiarse al repositorio, a Terraform state,
a variables de Ansible sin cifrar ni a logs. Se conservan en esa ruta con
directorio `0700` y ficheros `0600`, porque el contrato de backup los incluye
para poder reconstruir un Swarm nuevo. Solo pueden salir del host dentro del
backup cifrado, probado y con retención; no deben eliminarse después de crear
los Docker Secrets.

### Upgrade reversible de un runtime finalizado con el contrato anterior

Un runtime que ya tenga `runtime-manifest.json` v3 y
`workloads-ready-v1.json` no se refinaliza ni se restaura otra vez. El helper
dedicado valida primero el contrato legado completo, archiva copias byte a
byte con `0600`, crea únicamente la clave aleatoria de 32 bytes, sustituye
atómicamente el manifiesto por v4 y escribe el marker v2 al final:

```bash
sudo -- migration/scripts/upgrade_secret_identity_contract.py status
sudo -- migration/scripts/upgrade_secret_identity_contract.py apply
sudo -- migration/scripts/upgrade_secret_identity_contract.py status
```

El archivo de compatibilidad queda bajo
`restore-state/compat-v3/`. La operación es idempotente, reanudable tras una
interrupción y no abre ni modifica datasets ni fuentes Secret existentes.
Después de `apply`, el instalador normal reconcilia y verifica los Docker
Secrets usando la nueva identidad HMAC.

Rollback byte a byte del contrato runtime, sin borrar la clave, marker v2 ni
evidencia de upgrade:

```bash
sudo -- migration/scripts/upgrade_secret_identity_contract.py rollback
```

El rollback deja el despliegue nuevo bloqueado de forma deliberada hasta
reaplicar el upgrade o volver también al código compatible con v3/v1.

## Reactivación n8n posterior al cutover

La restauración termina siempre con todos los workflows despublicados. La
reactivación es una operación separada que exige el stack completo `1/1`,
contenedores n8n/PostgreSQL saludables, smoke local y
`https://n8n.apptolast.com/healthz` válido. Publica individualmente solo los
pares exactos `id` + `activeVersionId` de
`restore-state/n8n-active-workflows.json`, reinicia n8n como exige su CLI y
vuelve a comparar el inventario en PostgreSQL. Antes de mostrar estado o
publicar, el helper analiza en vivo todos los workflows restaurados y bloquea
acceso a variables de entorno, tipos de nodo de paquetes no oficiales y
endpoints internos legacy o no verificables; nunca reescribe workflows ni
crea aliases especulativos. También exporta credenciales descifradas únicamente
a un directorio temporal `0700` dentro del propio contenedor n8n, valida y
prueba por DNS/TCP solo sus endpoints, elimina el fichero al salir y devuelve
exclusivamente contadores. La salida del comando sensible se redacta incluso
si el export o la auditoría fallan.

No se ejecuta hasta que el propietario haya confirmado el consentimiento de
la credencial Google OAuth implicada. Esa decisión se hace explícita al
invocar:

```bash
sudo -- python3 migration/scripts/manage_n8n_workflows.py publish \
  --confirm-google-oauth-consent \
  I_HAVE_CONFIRMED_GOOGLE_OAUTH_CONSENT
```

Estado y rollback idempotente:

```bash
python3 migration/scripts/manage_n8n_workflows.py status
sudo -- python3 migration/scripts/manage_n8n_workflows.py rollback
```

Cada transición efectiva deja evidencia privada con SHA-256 del inventario.
El rollback despublica únicamente IDs auditados, fuerza el reinicio y exige
que el inventario activo quede vacío.

## Pruebas locales

```bash
python3 -m unittest discover -s migration/tests -v
bash -n migration/scripts/*.sh
python3 -m py_compile migration/scripts/*.py
docker compose -f migration/compose/restore.yml config --quiet
```

Referencia oficial de la versión fijada:
[`publish:workflow`](https://github.com/n8n-io/n8n/blob/n8n%402.31.5/packages/cli/src/commands/publish/workflow.ts)
y
[`unpublish:workflow`](https://github.com/n8n-io/n8n/blob/n8n%402.31.5/packages/cli/src/commands/unpublish/workflow.ts).
