# Repositorios, carpetas y ciclo de mantenimiento

## Decisión de propiedad

`git@github.com:apptolast/DockerSwarmInfrastrcture.git` es la única fuente de
verdad IaC de este servidor. El nombre remoto conserva por ahora su grafía
actual para no romper clones, protecciones ni automatizaciones.

Pertenecen a este repositorio:

- Terraform de Cloudflare, R2 y Netcup;
- bootstrap, identidad, seguridad y configuración del host;
- Docker Engine, Swarm, firewall y redes;
- Traefik, todos los routers y stacks de servicios;
- migración y contratos de datos;
- observabilidad, backup, restore y disaster recovery;
- validadores, CI, políticas, runbooks y evidencias no sensibles.

`MigracionNetCup` solo conserva evidencia histórica del origen. No tiene
permiso de aplicar infraestructura, firewall, DNS, Traefik ni stacks en
paralelo con este repositorio. No se crea `apptolast-workloads`: fragmentar la
infraestructura contradiría el propietario único acordado.

El código fuente de una aplicación sí puede vivir en su propio repositorio. Su
pipeline construye una imagen inmutable; este repo fija el digest y gobierna
cómo se ejecuta en el servidor.

## Fronteras de estado

| Objeto | Propietario |
| --- | --- |
| Definición completa del servidor y sus servicios | Este repositorio |
| Código fuente y tests internos de cada aplicación | Repo de la aplicación |
| Imágenes publicadas | Registry, referenciadas aquí por digest |
| State Terraform | Backend R2 dedicado y snapshots cifrados |
| Datos de producción | `/srv/dockerswarm` más backup externo probado |
| Tokens, contraseñas y claves privadas | Gestor de secretos externo |
| Planes y evidencias sensibles de cambios | Almacén operativo restringido |

Un objeto no puede tener dos repositorios, states o pipelines con permiso de
escritura simultáneo.

## Organización

```text
.
├── .github/               CI, Dependabot, CODEOWNERS y PR
├── ansible/
│   ├── inventory/         destinos sin secretos
│   ├── playbooks/         entradas operativas pequeñas
│   ├── roles/             estado por responsabilidad
│   └── templates/         políticas compartidas Jinja
├── backup/                controlador y contratos de backup/restore
├── config/                contratos y allowlists comunes
├── docs/                  decisiones y runbooks
├── images/                builds auxiliares reproducibles
├── infra/terraform/
│   ├── cloudflare/
│   │   ├── state-bootstrap/
│   │   └── apptolast-dns/
│   ├── netcup/perimeter/
│   └── testing/           harness offline sin credenciales
├── migration/             restauración exacta y evidencia
├── scripts/               wrappers y validadores fail-closed
├── stacks/
│   ├── edge/
│   ├── observability/
│   └── workloads/
└── tests/                 regresión y pruebas adversariales
```

Reglas:

- cada root Terraform tiene backend, identidad, lock, tests y README propios;
- todo recurso preexistente se importa antes de aplicar;
- cada rol Ansible tiene una responsabilidad y pre/postcondiciones explícitas;
- `config/*.yml` contiene contratos, nunca secretos;
- imágenes, providers, Python, colecciones y acciones se fijan por versión,
  hash o digest;
- los generados van a destinos ignorados, no al árbol versionado;
- las fuentes de secretos se crean fuera del repo y se identifican por
  catálogo/checksum;
- toda excepción de seguridad tiene razón, límite y prueba.

## Versionado y despliegue

`platform_release_version` usa SemVer. La versión `0.1.0` sigue pendiente hasta
que el runtime y la recuperación sean aceptados.

- **patch:** corrección compatible, test o documentación;
- **minor:** capacidad nueva compatible o control operativo;
- **major:** cambio incompatible de contrato o propiedad.

Una release:

1. parte de un worktree limpio y CI verde;
2. actualiza contrato y `CHANGELOG.md` en el mismo commit;
3. usa commit y tag firmados cuando las identidades estén aprovisionadas;
4. conserva plan, aprobación y evidencia fuera de Git;
5. aplica exclusivamente mediante wrappers versionados;
6. deja commit, perfil y hash contractual en el host;
7. repite Ansible y exige idempotencia;
8. registra aceptación y rollback.

Bootstrap y Ansible comparten un mutex host-global. Los writers Terraform usan
locking remoto y pruebas firmadas. Los helpers directos que quedan fuera de
esas fronteras solo se ejecutan en una ventana exclusiva documentada.

## Protección GitHub

Para `main`:

- PR y conversaciones resueltas obligatorias;
- CI y CODEOWNERS obligatorios;
- force-push y borrado deshabilitados;
- secret scanning y push protection habilitados;
- commits/tags firmados al completar el bootstrap de identidades;
- entorno `production` protegido y sin ejecución desde forks;
- CI read-only hasta disponer de credenciales separadas y rollback probado.

## Cadencia mínima

### Semanal

- revisar dependencias, advisories y unidades fallidas;
- comprobar espacio, inodos, logs, backups y certificados;
- confirmar que no existe drift o marker de operación abandonado.

### Mensual

- ejecutar planes read-only de cada root y clasificar drift;
- revisar usuarios, claves SSH, permisos GitHub y rotación de credenciales;
- verificar snapshots cifrados y ejecutar el ensayo de aplicaciones.

### Trimestral

- reconstruir states y restaurar datos en un entorno aislado;
- ensayar recuperación de Raft/ACME y medir RTO/RPO;
- revisar Netcup, UFW, `DOCKER-USER`, DNS y superficie publicada;
- retirar accesos, Configs y secrets obsoletos conforme a su retención.

### Antes de cada cambio

- commit exacto, plan, backup, ventana y rollback;
- consola fuera de banda si se toca acceso o red;
- aplicación serial bajo el lock correspondiente;
- segunda ejecución idempotente;
- verificación externa de DNS, TLS, datos y observabilidad.
