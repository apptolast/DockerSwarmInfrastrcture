# Catálogo versionado de servicios

`config/services.yml` es el contrato de alcance para la migración. Define qué
cargas pueden llegar a esta plataforma, cuáles quedan fuera y qué datos,
hostnames, puertos e imágenes se conocen. No autoriza por sí solo un despliegue,
una restauración, la apertura del firewall ni un cambio DNS.

La evidencia se extrajo de la auditoría de `MigracionNetCup` fechada el
2026-07-23, disponible durante esta implementación en
`/tmp/dockerswarm-migration-audit.MnPzsw`. El catálogo conserva esa información
en un formato estable y la separa del estado objetivo de este repositorio.

## Semántica del contrato

- `images[].reference`, `ports[].target` y `datasets[].target_path` describen
  siempre el objetivo Docker Swarm.
- `source_reference`, `source_target` y `source_path`, cuando aparecen,
  describen exclusivamente la evidencia de origen.
- `published: null` significa que el puerto no se publica en el host.
- `exposure: edge` significa que Traefik alcanza el puerto en una red interna.
- `exposure: internal` impide su publicación; `loopback` exige bind local.
- `migration: restore-state` restaura datos heredados.
- `migration: redeploy` recupera la imagen o el código, sin estado runtime
  propio.
- `migration: clean-install` crea estado vacío. Es el único modo permitido para
  `openclaw-clean`.

Los consumidores de IaC deben cargar YAML y usar esos campos, no copiar sus
valores a defaults o plantillas. Las referencias opcionales de origen nunca
deben alimentar el stack objetivo.

## Servicios aprobados

<!-- markdownlint-disable MD013 -->

| ID | Estrategia | Hostname | Puerto objetivo | Datos confirmados |
| --- | --- | --- | --- | --- |
| `kropia` | Redeploy | `kropia.apptolast.com` | Edge `80/TCP` | Imagen y snapshot Git |
| `traefik-edge` | Restore | `edge.apptolast.com` | `8000→80`, `8443→443`; `8080/8082` internos | ACME; rutas reconstruidas |
| `minecraft-stats` | Redeploy | `minecraft-stats.apptolast.com` | Edge `8080/TCP` | Mundo Minecraft en solo lectura |
| `minecraft` | Restore | Ninguno confirmado | Público `25565/TCP` | Unos 3,3 GB, mods y tres mundos |
| `n8n` | Restore | `n8n.apptolast.com` | Edge `5678/TCP` | PostgreSQL, home y clave runtime |
| `openclaw-clean` | Instalación limpia | `openclaw.apptolast.com` | Edge `18789/TCP` | Home vacío; estado legado prohibido |
| `passbolt` | Restore | `passbolt.apptolast.com` | Edge `80/TCP` | PostgreSQL y claves GPG/JWT |
| `personal-website-alberto` | Redeploy | `albertohidalgo.apptolast.com` | Edge `3000/TCP` | Imagen y snapshot Git |
| `personal-website-pablo` | Redeploy | `pablohurtadohg.apptolast.com` | Edge `3000/TCP` | Imagen y working tree capturado |
| `shlink` | Restore | `generadorcodigosqr.apptolast.com` | Edge `8080/TCP` | PostgreSQL |

<!-- markdownlint-enable MD013 -->

La auditoría restauró 46 workflows y 35 credenciales de n8n, y confirmó unos
3,76 GB de PostgreSQL y unos 72 MB en su home. También verificó dos URLs de
Shlink. Esas cifras son criterios de reconciliación, no límites del esquema.

`25565/TCP` forma parte del alcance aprobado, pero sigue sujeto a restauración,
prueba y a una decisión explícita sobre `online-mode=false` antes de cambiar
`platform_minecraft_public_enabled` y abrir firewall/DNS.
El hostname de Traefik expone únicamente el health endpoint versionado; no
autoriza un dashboard público.

## Rutas de datos

Las rutas de origen reflejan el layout del artefacto auditado. Las rutas de
destino pertenecen a `platform_state_root`.

<!-- markdownlint-disable MD013 -->

| Dataset | Origen auditado | Destino Swarm | Política |
| --- | --- | --- | --- |
| `traefik-acme` | `/srv/apptolast/traefik` | `/srv/dockerswarm/traefik` | Restore |
| `minecraft-worlds` | `/srv/apptolast/minecraft/data` | `/srv/dockerswarm/services/minecraft/data` | Restore; stats solo lectura |
| `minecraft-mods` | `/srv/apptolast/minecraft/mods` | `/srv/dockerswarm/services/minecraft/mods` | Restore; montaje solo lectura |
| `n8n-home` | `/srv/apptolast/n8n/data` | `/srv/dockerswarm/services/n8n/home` | Restore |
| `n8n-postgres` | `/srv/apptolast/n8n/postgres` | `/srv/dockerswarm/services/n8n/postgres` | Restore |
| `openclaw-clean-home` | Ninguno | `/srv/dockerswarm/services/openclaw-clean/home` | Crear vacío |
| `passbolt-gpg` | `/srv/apptolast/passbolt/gpg` | `/srv/dockerswarm/services/passbolt/gpg` | Restore |
| `passbolt-jwt` | `/srv/apptolast/passbolt/jwt` | `/srv/dockerswarm/services/passbolt/jwt` | Restore |
| `passbolt-postgres` | `/srv/apptolast/passbolt/postgres` | `/srv/dockerswarm/services/passbolt/postgres` | Restore |
| `shlink-postgres` | `/srv/apptolast/shlink/postgres` | `/srv/dockerswarm/services/shlink/postgres` | Restore |

<!-- markdownlint-enable MD013 -->

Los secretos se rotan o se materializan en runtime y no se almacenan en este
contrato. Una ruta indica propiedad y consumo; no demuestra que el restore ya
se haya ejecutado.

## Exclusiones explícitas

Cada regla denegada tiene un ID canónico y, cuando procede, aliases observados.
Las agrupaciones abreviadas de la auditoría quedan representadas así:

- `inern-seller`, alias `inemsellar`;
- `menus-admin`, alias `menus-dev`;
- `cattle`, alias `rancher`;
- `mcp-fullstack`, alias compuesto `cyberlab/platform`;
- `uptime-kuma`, alias `kuma`.

El alias compuesto no deniega globalmente componentes llamados `cyberlab` o
`platform`. En particular, no colisiona con la clave `internal_platform` de
este contrato.

`uptime-kuma` no procede de la auditoría original de `MigracionNetCup`: es
una exclusión añadida el 2026-07-27 tras confirmar con el propietario que
`kuma.apptolast.com` (un Uptime Kuma legacy de monitorización) no se migra
ni se recrea en la plataforma nueva.

Las demás exclusiones canónicas son `greenhouse`, `hermes`,
`invernaderos-api`, `whoop`, `vpn`, `cluster-ops`, `redisinsight`,
`ficsit-monitor`, `gibbon`, `health-dashboard`, `keel`, `kube-system`,
`langflow`, `longhorn-system`, `metal`, `monitoring-dozzle` y
`openclaw-legacy`.

Añadir una carga denegada a un stack, aunque su imagen exista o su namespace
aparezca en un backup, es un cambio de alcance y requiere modificar este
contrato de forma explícita.

## Observabilidad interna

Prometheus, Alertmanager, Blackbox Exporter, Loki, Alloy, Grafana,
Node Exporter, cAdvisor, PostgreSQL Exporter y Redis Exporter son componentes
nuevos de plataforma. No son servicios heredados ni datos a migrar.

El catálogo fija sus imágenes por digest y sus puertos conocidos. Ningún
puerto de observabilidad se publica en el host. Docker Swarm no ofrece un
contrato fiable para limitar un puerto publicado a `127.0.0.1`, por lo que
declarar una publicación «loopback» daría una falsa garantía. No existe un
hostname público para Grafana ni para otro componente de observabilidad.

Sus rutas persistentes se crean vacías bajo
`/srv/dockerswarm/observability`. El campo `legacy_migration: false` y la
ausencia de cualquier `source_path` son parte del contrato.

## Validación

La validación normal y sus casos negativos directos se ejecutan con:

```bash
.venv/bin/python scripts/validate-services.py
.venv/bin/python scripts/validate-services.py --self-test
```

El validador rechaza:

- claves de esquema inesperadas, campos ausentes y claves YAML duplicadas;
- cambios en las allowlist y denylist revisadas;
- IDs, hostnames, puertos, componentes, aliases o datasets duplicados;
- solapamientos entre servicios aprobados y denegados;
- imágenes sin `@sha256:<64 hex>`;
- rutas relativas, no canónicas, fuera de su root o solapadas;
- referencias de dataset sin dueño o consumidores incoherentes;
- cualquier importación de estado legado en `openclaw-clean`;
- observabilidad clasificada como migración o con hostname público;
- divergencias con el state root, los puertos públicos, el hostname o la imagen
  target de Traefik declarados en las fuentes actuales de plataforma.

`scripts/validate-iac.sh` ejecuta el lint del catálogo y sus casos negativos
como parte de la compuerta normal del repositorio.
