# Repositorios, carpetas y ciclo de mantenimiento

## Decisión

No se necesita crear otro repositorio de infraestructura ahora. Este repositorio
debe ser la única fuente de verdad de plataforma:

- perímetro Cloudflare y Netcup mediante Terraform;
- host, Docker, Swarm y edge mediante Ansible;
- scripts de comprobación, CI y runbooks de recuperación.

El remoto actual se llama `DockerSwarmInfrastrcture`. Conviene renombrarlo a
`dockerswarm-platform` para corregir el typo y expresar su responsabilidad,
pero el cambio se hará después de confirmar enlaces, protecciones y
automatizaciones. GitHub suele mantener redirecciones, pero cada clon, secret,
badge y pipeline debe verificarse; este repositorio no ejecuta el renombrado.

`MigracionNetCup` conserva temporalmente aplicaciones, Compose, datos y
procedimientos de migración. Tras el inventario se elige una de estas opciones:

1. Renombrarlo a `apptolast-workloads` y convertirlo en el monorepo de stacks.
2. Crear `apptolast-workloads` limpio y archivar `MigracionNetCup` como
   evidencia de la transición.

La segunda opción es preferible si el historial antiguo contiene estructura
obsoleta, secretos ya revocados o automatización peligrosa. No se crea un
repositorio por aplicación hasta que exista una diferencia real de equipo,
permisos, release o ciclo de vida.

Los secretos, states y backups no constituyen repositorios Git. Residen en sus
backends y gestores cifrados con acceso, rotación y recuperación propios.

## Propiedad final

| Objeto | Repositorio propietario |
| --- | --- |
| R2 de state, DNS y firewall de proveedor | `dockerswarm-platform` |
| Host, Docker, Swarm, Traefik y red edge | `dockerswarm-platform` |
| Stacks, routers y configuración no sensible de apps | `apptolast-workloads` |
| Esquemas, migraciones y pruebas de cada app | Repo de workloads o de la app |
| Datos, credenciales y material de backup | Backend/gestor externo |
| Expediente de corte y evidencias sensibles | Almacén operativo restringido |

Un objeto nunca tiene dos repositorios, states o pipelines con permiso de
escritura simultáneo.

## Organización de este repositorio

```text
.
├── .github/               CI, Dependabot, CODEOWNERS y plantilla de PR
├── ansible/
│   ├── inventory/         Destinos sin secretos
│   ├── playbooks/         Entradas operativas pequeñas
│   └── roles/             Estado del host y del Swarm por responsabilidad
├── config/                Contratos compartidos y configuración base
├── docs/                  Arquitectura, decisiones y runbooks
├── infra/terraform/
│   ├── cloudflare/
│   │   ├── state-bootstrap/
│   │   └── apptolast-dns/
│   └── netcup/perimeter/
├── scripts/               Wrappers y verificaciones sin lógica duplicada
├── stacks/                Templates de stacks propiedad de plataforma
└── tests/                 Fixtures sin datos de producción
```

Reglas de organización:

- cada root Terraform tiene backend, lock, variables, outputs, tests y README;
- cada rol Ansible posee una sola capa y no gestiona recursos del proveedor;
- `config/platform.yml` es el contrato común, no un almacén de secretos;
- `requirements-dev.in` declara Python directo y `requirements-dev.txt`
  bloquea todo el grafo con hashes; se regenera con
  `scripts/update-python-lock.sh`;
- un fichero generado va a `.build`, `.terraform.d` o un destino ignorado;
- toda excepción de seguridad tiene motivo, propietario y prueba;
- los inventarios sensibles y `host_vars` reales no entran en Git.

## Versionado y releases

`platform_release_version` usa SemVer y empieza en `0.1.0` mientras el edge y
la recuperación no estén aceptados en producción.

- **patch:** corrección compatible, test o documentación sin cambiar contrato;
- **minor:** capacidad nueva compatible, recurso o control operativo;
- **major:** cambio de propietario, contrato incompatible o reconstrucción.

Todo cambio se registra en `CHANGELOG.md`. Una release de producción:

1. tiene worktree limpio, PR revisada y CI verde;
2. actualiza versión y changelog en el mismo commit;
3. usa un tag firmado `vMAJOR.MINOR.PATCH`;
4. conserva planes, aprobación y evidencia fuera de Git;
5. se despliega con los wrappers versionados;
6. deja `/opt/dockerswarm/DEPLOYED_VERSION.yml` con commit, perfil y contrato;
7. registra validación y rollback en el expediente operativo.

No se etiqueta `0.1.0` por el simple hecho de que el código compile.

## GitHub

Configuración recomendada para `main`:

- PR obligatoria y conversación resuelta;
- CI `Terraform, Ansible, Traefik and security` obligatoria;
- revisión de CODEOWNERS;
- force-push y borrado de rama protegida deshabilitados;
- commits y tags firmados cuando todas las identidades estén preparadas;
- secret scanning, push protection y Dependabot habilitados;
- entorno `production` con aprobación manual y sin ejecución desde forks.

La CI de este repositorio es deliberadamente read-only. No se habilita
`terraform apply` ni Ansible de producción en GitHub hasta disponer de
credenciales separadas, environment protegido, backup probado y responsable de
rollback.

## Cadencia mínima

### Semanal

- revisar PR de dependencias y advisories;
- ejecutar CI y comprobar unidades/servicios fallidos;
- revisar espacio, inodos, logs y expiración próxima de certificados.

### Mensual

- ejecutar planes read-only de los tres roots y clasificar drift;
- comprobar uso y próxima rotación de tokens, incluido el refresh token Netcup;
- verificar snapshots cifrados, checksums y copia offsite;
- revisar usuarios, claves SSH, grupo Docker y permisos GitHub.

### Trimestral

- restaurar states, ACME y una copia coherente de Swarm en entorno aislado;
- ensayar rollback y medir RTO/RPO;
- revisar firewall Netcup, UFW, `DOCKER-USER` y superficie publicada;
- retirar Configs, secrets y accesos obsoletos tras su retención.

### Antes de cada cambio

- plan, backup y rollback objetivos;
- commit/tag exacto y worktree limpio;
- ventana y consola de recuperación;
- aplicación serial;
- segunda ejecución Ansible con `changed=0`;
- validación externa de DNS, TLS, datos y observabilidad.
