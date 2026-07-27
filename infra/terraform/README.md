# Terraform

Terraform gestiona únicamente recursos externos al sistema operativo y al
Swarm. Cada directorio raíz tiene estado, credenciales y blast radius propios:

- `cloudflare/apptolast-dns`: DNS autoritativo de `apptolast.com`.
- `cloudflare/state-bootstrap`: dos buckets de state y el bucket de backups.
- `netcup/perimeter`: firewall del proveedor y catálogo de claves públicas.

Ansible es el único propietario de la configuración del host, Docker, UFW y el
stack edge. Terraform no gestiona secretos Swarm ni `acme.json`.

Los roots de DNS y Netcup usan un backend S3 parcial preparado para Cloudflare
R2. Cada uno requiere un bucket privado y un par `Object Read & Write` limitado
a ese bucket. No comparten bucket ni credencial.

El root `state-bootstrap` declara esos tres buckets privados y desactiva su
dominio `r2.dev`. Usa backend local sobre almacenamiento cifrado y
off-repository porque no puede depender de recursos que todavía no existen.
Las credenciales R2 se crean fuera de Terraform para impedir que sus secretos
terminen dentro del state.

R2 no implementa el versionado S3. La recuperación depende de snapshots
cifrados y off-host creados después de cada apply y probados periódicamente.
R2 tampoco ofrece una garantía previa de locking para el backend S3 de
Terraform. Las plantillas declaran `use_lockfile = true`, pero eso no basta: los
wrappers rechazan el backend si no reciben además un `locking-proof.json`
vigente y firmado, ligado por hash al root, bucket, endpoint, credencial
registrada, contrato del harness y Terraform `1.15.8`. El proof
caduca como máximo a las 24 horas y acredita exclusión con dos clientes,
liberación normal, recuperación de cliente interrumpido y aislamiento de la
credencial cruzada. Si una prueba falla, no se crea plan ni se ejecuta writer.
`scripts/test-terraform-r2-locking.sh` realiza esas pruebas sobre una key
obligatoriamente situada bajo `lock-tests/` y genera un proof de 24 horas; usa
solo `terraform_data` y lo destruye al terminar. La credencial del otro root
debe demostrar primero lectura, escritura, listado y borrado en su propio
bucket y después recibir `AccessDenied` exacto para esas cuatro operaciones en
el bucket probado.

Ningún backend se selecciona solo por un `.tfbackend`. El fichero versionado
`infra/terraform/backend-identities.json` autoriza exactamente la ruta local o
el bucket, key y hash del Access Key ID de cada root. Durante una copia de state
puede declarar un único `pending_destination`, que los wrappers normales no
aceptan como producción. El repositorio incluye únicamente
`backend-identities.json.example`: hasta confirmar los valores reales, crear y
commitear el registro definitivo, todo acceso a state falla cerrado.

Los planes se generan desde un archivo del commit limpio, no desde el checkout
mutable, y su sidecar se firma con OpenSSH. El apply exige la firma adyacente
`PLAN.metadata.json.sig` y un firmante aprobado en el registro versionado
`plan.allowed-signers`. Los ejemplos de ambos registros de firmantes no
constituyen confianza: el propietario debe aprobar claves públicas dedicadas y
commitear los ficheros reales antes de poder escribir.

Las credenciales se inyectan solo desde el entorno:

```text
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
CLOUDFLARE_API_TOKEN
NETCUP_SCP_REFRESH_TOKEN
```

Los tokens DNS de Terraform y ACME son distintos. Nunca se guardan credenciales,
state, planes ni ficheros `.terraform` en Git.

La validación sin backend ni credenciales es:

```bash
./scripts/bootstrap-tooling.sh
./scripts/validate-iac.sh
```

La creación, prueba de bloqueo, importación, snapshot y recuperación se
detallan en [`docs/TERRAFORM_STATE.md`](../../docs/TERRAFORM_STATE.md).

La primera inicialización de un backend realmente vacío usa un consentimiento
ligado al root, commit y hash de backend:

```bash
./scripts/plan-terraform.sh \
  --root cloudflare/apptolast-dns \
  --backend-mode initialize \
  --backend-config /ruta/segura/backend.r2.tfbackend \
  --lock-proof /ruta/segura/locking-proof.json \
  --signing-key /ruta/segura/terraform-plan-signing-key \
  --var-file /ruta/segura/adoption.tfvars \
  --confirm-initialize \
  'INITIALIZE:cloudflare/apptolast-dns:<COMMIT>:<BACKEND_SHA256>'
```

Ese modo acepta únicamente ausencia real del objeto de state; un objeto
existente, aunque esté vacío, se rechaza. El
plan debe ser no destructivo, contener el inventario exacto del root y crear
`terraform_data.root_identity`; los `import` declarativos de DNS se revisan en
ese mismo plan. No sirve para adoptar a ciegas un state ya poblado.

Los planes posteriores se crean desde un commit limpio:

```bash
./scripts/plan-terraform.sh \
  --root cloudflare/apptolast-dns \
  --backend-mode established \
  --backend-config /ruta/segura/backend.r2.tfbackend \
  --lock-proof /ruta/segura/locking-proof.json \
  --signing-key /ruta/segura/terraform-plan-signing-key
```

El exit code `2` significa que existe drift o un cambio propuesto y obliga a
revisar el plan y su sidecar `PLAN.metadata.json`; no es un error del wrapper.
El sidecar firmado liga plan binario/JSON, commit, backend, lock proof, workspace,
lineage, serial, variables, inventario previsto y una política que prohíbe
`delete` y reemplazos. El apply también rechaza un plan futuro o con más de una
hora: un state sin cambios no demuestra que la realidad del proveedor siga
igual meses después de su último refresh.

`init`, `plan` y `apply` se consumen mediante la salida NDJSON oficial de
Terraform; `validate` usa su JSON estructurado. Cualquier diagnóstico, nivel
`warn`/`error`, `stderr` no vacío o stream truncado invalida la operación,
incluso cuando Terraform devuelve `0` o `2`. El plan y el state deben contener
el inventario exacto de validations, preconditions y bloques `check` del root,
con todos los padres e instancias en `pass`. La proyección y su SHA-256 quedan
en el sidecar firmado y se vuelven a verificar después del apply antes de
liberar el lease. No se interpreta un exit code correcto como ausencia de
warnings: los bloques `check` de Terraform solo avisan y continúan, por lo que
este gate adicional es obligatorio.

Solo [`scripts/apply-terraform.sh`](../../scripts/apply-terraform.sh) aplica ese
plan. Revalida todos los vínculos, exige confirmación
`APPLY:<ROOT>:<COMMIT>:<PLAN_SHA256>:<RECIPIENT_SHA256>`, obtiene snapshot
`age` previo cuando
existe state, ejecuta el plan guardado, comprueba state e inventario final y
crea el snapshot posterior. La migración entre backends del mismo tipo usa
`scripts/migrate-terraform-state.sh`. El segundo pull se bufferiza en memoria,
se atesta y se compara con el state que recibió la confirmación antes de emitir
un solo byte hacia `state push`; nunca usa `-force`. Después vuelve a comprobar
lineage, serial, hash completo, inventario y checks en origen y destino. Ese
comando solo crea una copia verificada hacia `pending_destination`: producción
sigue siendo el origen hasta congelar/revocar externamente sus writers y
commitear una promoción explícita en `backend-identities.json`.

Los writers remotos mantienen un lease CAS persistente durante state previo,
writer, snapshot y verificación final. Un fallo posterior a `writer_started`
queda en cuarentena sin TTL; recuperarlo exige aprobación OpenSSH ligada al
documento/ETag exactos y a una clave pública presente en el commit origen.
`snapshot-recipients.json` fija por SHA-256 el fichero `age` permitido.

STOP explícitos actuales:

- `cloudflare/state-bootstrap` puede validarse y snapshottarse, pero sus
  planes de producción, `apply` y migraciones locales están bloqueados. Además
  de faltar una cuarentena persistente equivalente al lease remoto, el provider
  Cloudflare fijado emite dos warnings reales
  `Resource Destruction Considerations` al planificar los buckets R2. No se
  permiten ni se silencian; este root seguirá en STOP hasta disponer de un flujo
  oficial, codificado y sin warnings;
- ningún create/cambio DNS desde ausencia o IP legacy hacia la IP de plataforma
  se acepta —incluido `edge`— hasta implementar el proof firmado de readiness y
  un coordinador que comparta el lock de mutación con Ansible.

No existe flag de override para ninguno de esos STOP.

Antes de copiar hacia un destino R2 pendiente, su prueba se genera con
`test-terraform-r2-locking.sh --backend-role pending_destination`; el rol forma
parte del alcance firmado. Las pruebas ordinarias de un backend ya activo usan
`--backend-role production`.
