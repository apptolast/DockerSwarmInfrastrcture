# Bootstrap de buckets R2

Este root declara los dos buckets R2 privados que usan los roots de DNS y
Netcup y un tercer bucket privado dedicado a los backups cifrados de Docker
Swarm. No crea credenciales: un Secret Access Key dentro de Terraform acabaría
persistido en el propio state.

## Por qué usa backend local

Un root no puede depender de un bucket que todavía no existe. Su backend
`local` recibe una ruta absoluta sobre almacenamiento cifrado y fuera del
checkout. Ese pequeño state de bootstrap se cifra además con `age` y se copia
offsite después de cada cambio.

## Flujo inicial

1. Crear un token Cloudflare temporal, limitado a administrar R2 en la cuenta.
2. Copiar los ejemplos a ficheros ignorados y sustituir los placeholders.
3. Confirmar que la ruta del backend está cifrada, fuera del repositorio y
   respaldada; registrar y commitear esa ruta como producción en
   `infra/terraform/backend-identities.json`. Los roots remotos pueden seguir
   en `production: null` durante este bootstrap.
4. Inventariar los buckets preexistentes y preparar sus importaciones oficiales
   antes de planificar cualquier cambio.
5. Detenerse en el STOP descrito abajo; hoy no existe un plan/apply de
   producción autorizado para este root.
6. Revocar el token temporal si no se usará para mantenimiento.
7. Crear manualmente una credencial R2 distinta y limitada por bucket.
8. Inicializar los dos roots de state y probar locking concurrente.
9. Configurar el rol de backup con la credencial exclusiva del tercer bucket.
10. Crear un snapshot `age` del state de este root y de los dos remotos.

Antes del primer plan también debe existir el registro versionado
`infra/terraform/plan.allowed-signers`, con una clave pública aprobada por el
propietario; el fichero `.example` no concede confianza.

```bash
cp backend.local.tfbackend.example backend.local.tfbackend
cp terraform.tfvars.example terraform.tfvars

git commit
../../../../scripts/plan-terraform.sh --help
```

Este root está deliberadamente bloqueado por dos condiciones independientes:

- el backend local todavía no tiene journal persistente y cuarentena
  post-writer equivalentes al lease CAS de los roots remotos;
- Cloudflare provider `5.22.0` emite dos diagnósticos reales
  `Resource Destruction Considerations` al planificar los buckets R2. El
  contrato exige cero warnings y no contiene allowlist ni bypass.

Por tanto, el ejemplo solo prepara ficheros y muestra la interfaz; no autoriza
un `plan` directo ni un `apply`. El STOP se retirará mediante un cambio
versionado únicamente cuando exista un flujo oficial sin warnings y una
recuperación local persistente probada frente a señales y `SIGKILL`.

Los nombres de bucket no son secretos, pero no se inventan ni se publican hasta
confirmar la cuenta, jurisdicción, retención y responsables. El root desactiva
explícitamente el dominio administrado `r2.dev`; no se añaden custom domains.
El modo `initialize` exige un backend vacío. Si algún bucket ya existe, no se
aplica esperando que Terraform lo descubra: se obtiene su ID oficial y se
diseña una adopción/importación revisada antes de habilitar este flujo.
