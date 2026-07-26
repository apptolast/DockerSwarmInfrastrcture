# DNS de `apptolast.com`

Este root empieza administrando únicamente `edge.apptolast.com`. Los registros
de las aplicaciones siguen apuntando al servidor anterior y no se adoptarán ni
modificarán hasta que sus datos, servicios y rollback hayan sido verificados.

## Credencial

Usar un token de automatización diferente al token ACME:

- `Zone / Zone / Read`;
- `Zone / DNS / Edit`;
- recurso limitado exclusivamente a `apptolast.com`.

Exportarlo como `CLOUDFLARE_API_TOKEN`. El zone ID se suministra mediante
`TF_VAR_cloudflare_zone_id`; ninguno debe escribirse en comandos compartidos.

## Inicialización

```bash
cp backend.r2.tfbackend.example backend.r2.tfbackend
# Sustituir el account ID y el bucket exclusivo de este root.
../../../../.tools/terraform init \
  -backend-config=backend.r2.tfbackend \
  -reconfigure
../../../../.tools/terraform plan -out=production.tfplan
../../../../.tools/terraform show production.tfplan
../../../../.tools/terraform apply production.tfplan
```

El plan se trata como sensible y se elimina después de aplicarlo. Un cambio
manual en Cloudflare crea drift; hay que importarlo o revertirlo mediante un plan
revisado, nunca mantener dos propietarios del mismo registro.

No se aplica este root hasta que Traefik responda localmente y el registro se
haya comprobado como ausente o importado. `prevent_destroy` protege el objeto,
pero no sustituye la revisión humana del plan.
