# Bootstrap de buckets de estado

Este root declara los dos buckets R2 privados que usan los roots de DNS y
Netcup. No crea credenciales: un Secret Access Key dentro de Terraform acabaría
persistido en el propio state.

## Por qué usa backend local

Un root no puede depender de un bucket que todavía no existe. Su backend
`local` recibe una ruta absoluta sobre almacenamiento cifrado y fuera del
checkout. Ese pequeño state de bootstrap se cifra además con `age` y se copia
offsite después de cada cambio.

## Flujo inicial

1. Crear un token Cloudflare temporal, limitado a administrar R2 en la cuenta.
2. Copiar los dos ejemplos a ficheros ignorados y sustituir los placeholders.
3. Confirmar que la ruta del backend está cifrada, fuera del repositorio y
   respaldada.
4. Importar buckets preexistentes antes de planificar cualquier cambio.
5. Revisar y aplicar el plan; ambos buckets tienen `prevent_destroy`.
6. Revocar el token temporal si no se usará para mantenimiento.
7. Crear manualmente una credencial R2 distinta y limitada por bucket.
8. Inicializar los dos roots consumidores y probar locking concurrente.
9. Crear un snapshot `age` del state de este root y de los dos remotos.

```bash
cp backend.local.tfbackend.example backend.local.tfbackend
cp terraform.tfvars.example terraform.tfvars

../../../../.tools/terraform init \
  -backend-config=backend.local.tfbackend \
  -reconfigure
../../../../.tools/terraform plan \
  -var-file=terraform.tfvars \
  -out=state-bootstrap.tfplan
../../../../.tools/terraform show state-bootstrap.tfplan
../../../../.tools/terraform apply state-bootstrap.tfplan
```

Los nombres de bucket no son secretos, pero no se inventan ni se publican hasta
confirmar la cuenta, jurisdicción, retención y responsables. El root desactiva
explícitamente el dominio administrado `r2.dev`; no se añaden custom domains.
