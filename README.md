# Infraestructura Docker Swarm de ApptoLast

Este repositorio es la fuente de verdad única de toda la infraestructura del
servidor `apptolast`: proveedor, DNS, host, seguridad, Docker Swarm, Traefik,
stacks, migración, observabilidad, backup y recuperación.

La regla de oro es que un servidor perdido se reconstruye desde un commit
revisado más los secretos y backups externos. Ninguna configuración manual del
host se considera estado válido si no queda codificada o documentada aquí.

## Estado observado

Estado comprobado el 26 de julio de 2026:

- Docker Swarm está activo en `159.195.156.57` con un único manager/worker,
  `Ready`, `Active` y `Leader`.
- El Swarm usa `10.0.0.0/8`, subredes `/24` y data path
  `159.195.156.57:4789`; autolock sigue desactivado.
- No hay stacks ni servicios persistentes desplegados. Solo existe la red
  `ingress` y el nodo aún no tiene la label `platform.edge`.
- Los datos de migración están materializados bajo `/srv/dockerswarm`, con
  acceso root, pero ningún workload los está usando todavía.
- Existe el Docker Secret `cloudflare_dns_api_token_v1`. Su valor no está en
  Git y no puede recuperarse desde Docker. Como la credencial se expuso fuera
  del gestor previsto, debe rotarse antes del edge de producción.
- Los registros públicos continúan apuntando al servidor anterior
  `138.199.157.58`; no se ha ejecutado el cutover.
- n8n fue restaurado con sus workflows sin publicar. La verificación de
  OAuth/negocio sigue siendo una compuerta funcional externa.
- Minecraft conserva `online-mode=false`; por ello DNS, firewall y publicación
  de `25565/TCP` permanecen bloqueados mediante
  `platform_minecraft_public_enabled: false`.

Todo lo anterior distingue código preparado de estado aplicado. Aún no se ha
ejecutado el despliegue productivo de este árbol.

## Cobertura IaC

| Capa | Implementación |
| --- | --- |
| R2 de state, DNS Cloudflare y perímetro Netcup | Terraform |
| Bootstrap, identidad, SSH y seguridad del host | Ansible y Jinja |
| Docker, Swarm, firewall y ciclo de desbloqueo | Ansible |
| Traefik, rutas y redes | Stacks/Jinja desplegados por Ansible |
| Servicios aprobados y restauración | Catálogo, stacks, scripts y Ansible |
| Métricas, logs y alertas | Stack/Ansible de observabilidad |
| Copias y ensayos de restore | restic, scripts, systemd y Ansible |
| Pruebas, políticas y trazabilidad | CI, tests y metadatos de despliegue |

`MigracionNetCup` es evidencia histórica de origen, no propietario activo de
infraestructura. El código fuente de una aplicación puede seguir en su repo,
pero toda definición que gobierna este servidor pertenece aquí.

## Organización

```text
.
├── ansible/               inventarios, playbooks, roles y templates
├── backup/                controlador reproducible de backup/restore
├── config/                contratos compartidos y allowlists
├── docs/                  arquitectura, operación y recuperación
├── images/                imágenes auxiliares reproducibles
├── infra/terraform/       roots aislados de proveedor y backends
├── migration/             restauración y evidencia de la migración
├── scripts/               wrappers fail-closed y validadores
├── stacks/                edge, workloads y observabilidad
└── tests/                 pruebas de contrato y regresión
```

Los datos, states, claves privadas, tokens, contraseñas y unlock keys nunca se
guardan en Git. El repo solo contiene sus catálogos, identidades esperadas,
checksums y procedimientos.

## Validación

Las versiones de Terraform, providers, Ansible, colecciones, Python, acciones
CI e imágenes se fijan y verifican. La validación completa es:

```bash
./scripts/bootstrap-tooling.sh
./scripts/validate-iac.sh
./scripts/lint.sh
```

Incluye `terraform fmt/init/validate/test`, Ansible lint/syntax, render Jinja,
validación real de Traefik y OpenSSH, ShellCheck, tests adversariales,
markdownlint y escaneo de secretos.

Los writers productivos exigen un commit limpio. Bootstrap y todos los
playbooks Ansible comparten el mismo mutex host-global
`/run/lock/dockerswarm-iac.lock`; una caída conserva un marker que requiere
recuperación explícita. Los instaladores directos de secretos y controladores
de backup no quedan cubiertos por ese mutex y solo se ejecutan en una ventana
exclusiva sin Ansible activo.

## Orden productivo

1. Validar, revisar, hacer commit y push del estado exacto.
2. Aprovisionar fuera de Git identidades firmantes, recipients `age`,
   buckets/credenciales R2 independientes y locking remoto probado.
3. Importar recursos existentes antes de cualquier `terraform apply`.
4. Aplicar y repetir `platform`, `host-baseline` y `preflight-images`.
5. Rotar el token ACME expuesto e instalar su nueva versión como Docker
   Secret, sin reutilizarlo para Terraform.
6. Desplegar Traefik primero contra Let’s Encrypt staging y después
   producción.
7. Aplicar `workloads` y comprobar datos/funciones servicio a servicio.
8. Aplicar `observability`; activar `backup` solo cuando R2, restic y la
   custodia externa de autolock estén probados.
9. Adoptar en Terraform los nueve A existentes sin cambiar su contenido.
10. Crear el A nuevo de `edge`; cambiar solo los ocho A HTTP aprobados.
    Minecraft queda en la IP legacy hasta abrir su gate específico.
11. Verificar DNS, TLS, firewall, datos, métricas y rollback durante al menos
    los TTL efectivos.

Ejemplo Ansible:

```bash
./scripts/deploy-ansible.sh \
  --playbook platform \
  --check \
  --ask-become-pass

./scripts/deploy-ansible.sh \
  --playbook platform \
  --confirm-production \
  --ask-become-pass
```

La ejecución remota es el modo normal. `--local` eleva el supervisor completo
y Ansible; el usuario `admin` no se conserva en el grupo `docker`.

## Compuertas externas abiertas

La infraestructura permanece fail-closed hasta disponer de:

- dos backends R2, credenciales separadas, prueba de locking live y snapshots
  cifrados;
- identidades/firmas reales de planes y pruebas de locking, fuera de Git;
- token Cloudflare Terraform separado y credencial ACME rotada;
- credenciales Netcup SCP/imports reales si se activa ese root;
- destino y custodio externo de la unlock key antes de activar autolock;
- aceptación OAuth/negocio de n8n;
- decisión explícita sobre `online-mode=false` antes de publicar Minecraft.
- prueba de que el snapshot staged es final o un refresh/promoción versionado
  antes de arrancar workloads.

No se debe sustituir ninguna de estas entradas por valores inventados ni
desactivar los gates para obtener una ejecución verde.

A diferencia de las anteriores, el cutover DNS (paso 10 del orden productivo)
no se desbloquea aportando ningún credential o evidencia: `terraform-safety.py`
rechaza de forma incondicional cualquier create/cambio de un registro Cloudflare
hacia la IP de plataforma hasta que exista un coordinador de "host-readiness"
firmado — código que todavía no se ha escrito. Ver el STOP de cutover en
[`docs/TERRAFORM_STATE.md`](docs/TERRAFORM_STATE.md).

## Límites

Un manager único no ofrece alta disponibilidad. Un reinicio o pérdida del VPS
interrumpe el plano de control y sus cargas. No se abren públicamente
`2377/TCP`, `7946/TCP+UDP` ni `4789/UDP`; añadir nodos exige red privada,
quorum impar y un nuevo diseño probado.

## Documentación

- [Arquitectura y propiedad](docs/ARCHITECTURE.md)
- [Catálogo exacto de servicios](docs/SERVICE_CATALOG.md)
- [Migración y cutover](docs/MIGRATION.md)
- [Reconstrucción completa](docs/REBUILD.md)
- [Operación y bloqueo](docs/OPERATIONS.md)
- [Edge, Cloudflare y ACME](docs/EDGE.md)
- [Estado Terraform](docs/TERRAFORM_STATE.md)
- [Backup y recuperación](docs/BACKUP_RECOVERY.md)
- [Observabilidad](docs/OBSERVABILITY.md)
- [Repositorios y mantenimiento](docs/REPOSITORIES.md)
- [Capacidad](docs/CAPACITY.md)
- [Changelog](CHANGELOG.md)

## Referencias oficiales

- [Administrar un Swarm](https://docs.docker.com/engine/swarm/admin_guide/)
- [Firewall de Docker](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
- [Traefik en Swarm](https://doc.traefik.io/traefik/v3.7/reference/install-configuration/providers/swarm/)
- [Traefik ACME](https://doc.traefik.io/traefik/v3.7/reference/install-configuration/tls/certificate-resolvers/acme/)
- [Backend R2 de Terraform](https://developers.cloudflare.com/terraform/advanced-topics/remote-backend/)
- [Backend S3 de Terraform](https://developer.hashicorp.com/terraform/language/backend/s3)
