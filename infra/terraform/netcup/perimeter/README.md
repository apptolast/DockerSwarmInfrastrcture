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
- `server_id` y `server_mac` son hechos no secretos ya contrastados entre el
  inventario de SCP y la NIC activa; su única fuente es `config/platform.yml`.
- SSH exige al menos un CIDR administrativo restringido.
- `preserved_policy_ids` debe ser exactamente `[]`: una policy conservada solo
  por ID oculta reglas y prioridad fuera de IaC.
- `firewall_policy_inventory_confirmed=true` solo se registra después de
  exportar policies/reglas/orden y confirmar que la policy modelada sustituye
  la asignación completa.
- 80/443/25565 se autorizan solo sobre IPv4. El TCP de aplicaciones permanece
  cerrado en IPv6 hasta diseñar, publicar y probar el contrato AAAA completo.
- La consola SCP y una sesión SSH ya probada deben permanecer abiertas.
- Todos los recursos usan `prevent_destroy`.
- Los objetos preexistentes se importan antes de permitir cambios.

La credencial SCP se inyecta mediante `NETCUP_SCP_REFRESH_TOKEN`. Los valores
reales se guardan en un `.tfvars` ignorado y el state reside en R2.

`manage_firewall=true` no se usa para descubrir el estado. Primero se
inventaría el estado en SCP. Una policy necesaria debe modelarse con todas sus
reglas; no puede pasar a `preserved_policy_ids`. Después se importan los objetos
que coincidan con las direcciones modeladas y se revisa un plan sin reemplazos.
El root no compra,
reinstala ni elimina el VPS.

La documentación del provider indica que el refresh token SCP debe usarse al
menos una vez cada 30 días para conservar su validez. La custodia debe incluir
un control de expiración y una prueba read-only periódica; nunca se programa un
`apply` artificial para mantener vivo un token.

netcup documenta que las reglas se evalúan por prioridad y que, al coincidir
una, se ignoran las posteriores. Por eso una policy opaca no puede conservarse
delante de la policy IaC:
[firewall oficial de netcup](https://www.netcup.com/en/helpcenter/documentation/server/firewall).

Import IDs publicados para la versión fijada:

```text
netcup_firewall_policy: <policy_id>
netcup_server_firewall: <server_id>/<mac>
netcup_ssh_key: <scp_key_id>
```

- [Provider 1.0.0 y refresh token](https://github.com/hornc-greedy/terraform-provider-netcup/tree/v1.0.0)
- [Import de la asignación](https://registry.terraform.io/providers/hornc-greedy/netcup/latest/docs/resources/server_firewall)
- [Import de claves SCP](https://registry.terraform.io/providers/hornc-greedy/netcup/latest/docs/resources/ssh_key)
