# DNS de `apptolast.com`

Este root tiene una allowlist cerrada de diez registros `A`, todos DNS-only y
con TTL 300. El destino se decide por registro mediante
`platform_dns_cutover` en `config/platform.yml`:

- `edge.apptolast.com`, que se crea como registro nuevo;
- `kropia.apptolast.com`;
- `minecraft-stats.apptolast.com`;
- `minecraft.apptolast.com`;
- `n8n.apptolast.com`;
- `openclaw.apptolast.com`;
- `passbolt.apptolast.com`;
- `generadorcodigosqr.apptolast.com`;
- `pablohurtadohg.apptolast.com`;
- `albertohidalgo.apptolast.com`.

Los nueve registros de aplicaciones ya existen y tienen bloques `import`
individuales en `imports.tf`. `edge` no tiene import. Este root no descubre,
importa ni gestiona ningún otro registro de la zona, incluidos los
demás registros legacy.

El valor transitorio `adoption_only = true` conserva los nueve registros
existentes en el servidor legacy `138.199.157.58`; únicamente el registro nuevo
`edge` apunta a `159.195.156.57`. Debe declararse explícitamente usando
`adoption.tfvars.example`. El valor predeterminado también conserva todos esos
servicios en legacy mientras `platform_dns_cutover` siga en `false`; omitir el
fichero no autoriza un corte.
Tras completar y verificar la adopción, un commit de cutover separado podrá
cambiar gates `platform_dns_cutover`, pero el validador actual bloquea todo
create/cambio hacia la IP nueva hasta implementar el proof firmado de readiness
y la coordinación con el lock de mutación del host. Minecraft permanece en
legacy. Su gate está acoplado a
`platform_minecraft_public_enabled` y exige además `online_mode: true`; por
tanto, DNS y puerto 25565 no pueden adelantarse a la decisión de autenticación.
Un rollback HTTP se expresa cambiando únicamente el booleano del registro
afectado y aplicando un plan completo revisado, nunca con `-target`.

El account ID `becc1c00820a0f6779e9b278ab150abf` y el zone ID
`b69e21ff397bd00c35b989fe10068d0e` no son credenciales. Están fijados en este
root; el account ID también forma parte del endpoint R2 de la plantilla de
backend.

## Credenciales

Usar un token de automatización diferente al token ACME:

- `Zone / Zone / Read`;
- `Zone / DNS / Edit`;
- recurso limitado exclusivamente a `apptolast.com`.

Se exporta como `CLOUDFLARE_API_TOKEN`. Las credenciales R2 se inyectan mediante
`AWS_ACCESS_KEY_ID` y `AWS_SECRET_ACCESS_KEY`. Ninguna credencial se guarda en
HCL, YAML, `.tfbackend`, comandos compartidos ni planes.

## Backend R2

R2 no implementa S3 Versioning, por lo que no proporciona historial recuperable
del state. Tampoco se presupone que su implementación S3 proporcione locking
correcto a Terraform. La plantilla declara `use_lockfile = true`, pero ningún
wrapper escritor la acepta sin el proof vigente de la prueba siguiente.

Antes de usar R2 como backend escritor se prueba `use_lockfile = true` con dos
clientes, la versión fijada de Terraform y una key desechable. Solo después de
superar creación exclusiva, espera, liberación normal y recuperación de un lock
interrumpido se registra el proof ligado al backend real. Si la prueba falla o
el proof caduca, no se ejecuta un writer de producción contra R2. Los snapshots
cifrados offsite y los restore rehearsals siguen siendo obligatorios aunque el
locking funcione.

Para migrar un state existente al backend R2:

```bash
cp backend.r2.tfbackend.example /ruta/segura/backend.r2.tfbackend
# Sustituir únicamente el bucket; nunca introducir credenciales.
../../../../scripts/migrate-terraform-state.sh --help
```

Antes se congela cualquier writer y se crea un snapshot cifrado del state de
origen. `-reconfigure` solo inicializa cada extremo; el wrapper copia mediante
`state pull | state push` sin `-force`.

## Adopción sin mover tráfico

No se ejecuta `terraform import` directamente: omitiría las atestaciones,
snapshots y controles de locking, y dejaría un state sin la identidad raíz que
los wrappers rechazan.

La adopción usa exclusivamente los bloques declarativos de `imports.tf` y dos
planes guardados y firmados independientes. Ambos se generan con
`plan-terraform.sh --signing-key ...`; el apply exige el `.sig` adyacente:

1. Con el backend vacío se copia `adoption.tfvars.example` fuera del repositorio
   y se usa `plan-terraform.sh --backend-mode initialize --var-file ...`. El
   contrato de seguridad exige `adoption_only = true`: el plan importa los nueve
   objetos existentes, crea `terraform_data.root_identity` y crea `edge`, pero
   conserva todos los servicios existentes en `138.199.157.58`. El apply se
   realiza únicamente con `apply-terraform.sh`, que captura el snapshot
   posterior incluso si Terraform falla.
2. Después de verificar el state, el snapshot y `edge`, se genera un nuevo plan
   en modo `established`, ya sin el fichero transitorio. El valor permanente
   `adoption_only = false` hace que ese plan separado proponga solo
   actualizaciones in-place de los registros cuyos gates estén activos.

Los bloques `import` son idempotentes y no vuelven a importar las instancias ya
presentes en state. En la segunda fase Minecraft debe permanecer sin cambios en
`138.199.157.58`; cualquier destroy, reemplazo, otro nombre o cambio prematuro
de Minecraft bloquea la ventana.

No se importa `edge`, no se usa importación masiva y no se añade el resto del
inventario legacy «para evitar drift»: esos objetos conservan su propietario
actual. Los nueve IDs deben contrastarse de nuevo con la API/UI de Cloudflare
antes de tocar state.

`prevent_destroy` protege las diez instancias frente a destrucción, pero no
impide una actualización in-place de `content`. El apply del cutover solo se
autoriza cuando los servicios, la resolución forzada hacia la IP nueva y el
rollback estén verificados. El plan se trata como sensible y se elimina tras la
ventana.
