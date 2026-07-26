# Perímetro netcup

Este root gestiona únicamente el firewall SCP del servidor existente y las
claves públicas disponibles para futuras instalaciones de imagen. No compra,
reinstala ni destruye el VPS.

netcup no publica un provider Terraform oficial. Se usa
`hornc-greedy/netcup` fijado exactamente a `1.0.0`; cualquier actualización
exige auditar código, esquema y plan. La documentación de la API y del firewall
de netcup sigue siendo la autoridad funcional.

## Compuertas de seguridad

- `manage_firewall` permanece `false` por defecto.
- Hay que obtener `server_id` y `server_mac` desde SCP; no se infieren.
- SSH exige al menos un CIDR administrativo restringido.
- `preserved_policy_ids` debe enumerar todas las policies asignadas, incluidas
  las administradas por el proveedor.
- La consola SCP y una sesión SSH ya probada deben permanecer abiertas.
- Todos los recursos usan `prevent_destroy`.
- Los objetos preexistentes se importan antes de permitir cambios.

La credencial SCP se inyecta mediante `NETCUP_SCP_REFRESH_TOKEN`. Los valores
reales se guardan en un `.tfvars` ignorado y el state reside en R2.

`manage_firewall=true` no se usa para descubrir el estado. Primero se
inventariaría el estado en SCP, después se importan policy, asignación y claves
existentes, y por último se revisa un plan sin reemplazos. El root no compra,
reinstala ni elimina el VPS.

La documentación del provider indica que el refresh token SCP debe usarse al
menos una vez cada 30 días para conservar su validez. La custodia debe incluir
un control de expiración y una prueba read-only periódica; nunca se programa un
`apply` artificial para mantener vivo un token.

Import IDs publicados para la versión fijada:

```text
netcup_firewall_policy: <policy_id>
netcup_server_firewall: <server_id>/<mac>
netcup_ssh_key: <scp_key_id>
```

- [Provider 1.0.0 y refresh token](https://github.com/hornc-greedy/terraform-provider-netcup/tree/v1.0.0)
- [Import de la asignación](https://registry.terraform.io/providers/hornc-greedy/netcup/latest/docs/resources/server_firewall)
- [Import de claves SCP](https://registry.terraform.io/providers/hornc-greedy/netcup/latest/docs/resources/ssh_key)
