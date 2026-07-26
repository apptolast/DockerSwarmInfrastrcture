# Terraform

Terraform gestiona únicamente recursos externos al sistema operativo y al
Swarm. Cada directorio raíz tiene estado, credenciales y blast radius propios:

- `cloudflare/apptolast-dns`: DNS autoritativo de `apptolast.com`.
- `cloudflare/state-bootstrap`: buckets privados para los states remotos.
- `netcup/perimeter`: firewall del proveedor y catálogo de claves públicas.

Ansible es el único propietario de la configuración del host, Docker, UFW y el
stack edge. Terraform no gestiona secretos Swarm ni `acme.json`.

Los roots de DNS y Netcup usan un backend S3 parcial preparado para Cloudflare
R2. Cada uno requiere un bucket privado y un par `Object Read & Write` limitado
a ese bucket. No comparten bucket ni credencial.

El root `state-bootstrap` declara esos buckets y desactiva su dominio `r2.dev`.
Usa backend local sobre almacenamiento cifrado y off-repository porque no puede
depender de recursos que todavía no existen. Las credenciales R2 se crean fuera
de Terraform para impedir que sus secretos terminen dentro del state.

R2 no implementa el versionado S3. La recuperación depende de snapshots
cifrados y off-host creados después de cada apply y probados periódicamente.
`use_lockfile` se mantiene habilitado, pero el comportamiento de locking debe
probarse con dos procesos reales antes del primer apply: HashiCorp solo ofrece
compatibilidad de mejor esfuerzo con implementaciones S3 de terceros.

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

Un plan de producción se crea desde un commit limpio:

```bash
./scripts/plan-terraform.sh \
  --root cloudflare/apptolast-dns \
  --backend-config \
  infra/terraform/cloudflare/apptolast-dns/backend.r2.tfbackend
```

El exit code `2` significa que existe drift o un cambio propuesto y obliga a
revisar el plan; no es un error del wrapper.
