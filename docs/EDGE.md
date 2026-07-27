# Operación segura del edge

## Contrato declarado

El stack `edge` declara una réplica de Traefik `3.7.9` fijada por digest, como
usuario `65532:65532`, con filesystem raíz de solo lectura, todas las
capabilities eliminadas y `no-new-privileges`.

Publica únicamente `80/TCP` y `443/TCP`. Usa:

- file provider con un fichero dinámico y `watch: false`;
- ningún provider Docker/Swarm y ningún socket Docker;
- DNS-01 de ACME mediante Cloudflare;
- `/srv/dockerswarm/traefik/acme.json` como estado persistente;
- el secret externo `cloudflare_dns_api_token_v2`;
- dos Docker Configs inmutables con nombre derivado de SHA-256;
- ocho overlays de workload aisladas y una overlay de monitorización, todas
  externas, cifradas y no attachable;
- nueve routers explícitos: `/ping` de `edge.apptolast.com` y los ocho
  servicios HTTP aprobados.

Esto es estado declarado, no evidencia de despliegue. El Docker Secret
`cloudflare_dns_api_token_v1` sigue existiendo (rotación pendiente de revocar
hasta verificar el servicio con la v2, según el propio procedimiento de este
documento) pero ya no está referenciado por `edge_traefik_cloudflare_secret_name`.
`cloudflare_dns_api_token_v2` es la versión activa, instalada y verificada
(TXT efímero) el 2026-07-27. No existen todavía certificado, overlays, stack
ni servicios.

## Separación de credenciales

No se reutiliza un token entre funciones:

- **Token Cloudflare ACME:** crea y elimina TXT de DNS-01 desde Traefik.
  Solo recibe `Zone / Zone / Read` y `Zone / DNS / Edit`, limitados a
  `apptolast.com`.
- **Token Cloudflare Terraform:** gestiona únicamente el DNS declarado.
  Recibe los mismos permisos mínimos sobre la zona, pero es otro token.
- **Credencial R2 DNS state:** accede al backend del root DNS con
  `Object Read & Write` limitado a su bucket.
- **Credencial R2 Netcup state:** accede al backend del root Netcup con
  `Object Read & Write` limitado a un bucket distinto.
- **Token Netcup SCP:** accede al root de perímetro con solo la cuenta y las
  operaciones necesarias según SCP.

Aunque ACME y Terraform requieran permisos DNS parecidos, sus tokens son
distintos. Así se puede revocar o rotar el runtime sin interrumpir Terraform, y
una credencial del CI no se instala en el host.

**Excepción decidida por el propietario (2026-07-27):** para las credenciales
R2, el propietario del servidor autorizó explícitamente usar un token R2 de
cuenta con alcance sobre los tres buckets (`apptolast-tfstate-dns`,
`apptolast-tfstate-netcup`, `apptolast-backups`) en lugar de tres credenciales
`Object Read & Write` separadas y limitadas a un único bucket cada una. Es una
decisión informada de simplicidad operativa frente a aislamiento de blast
radius, tomada tras explicar la separación por defecto descrita arriba; no es
un descuido. `infra/terraform/backend-identities.json` registra el hash
SHA-256 del mismo Access Key ID en las tres identidades por ese motivo.

El cliente ACME usado por Traefik admite variables terminadas en `_FILE`. Su
documentación oficial para Cloudflare exige DNS Edit y Zone Read y permite
separar ambos permisos:
[lego Cloudflare](https://go-acme.github.io/lego/dns/cloudflare/). El stack usa
`CF_DNS_API_TOKEN_FILE`, por lo que el valor se lee desde el fichero del secret,
no desde el YAML.

Las credenciales R2 se crean desde el flujo específico de R2, no desde el
creador de tokens DNS:
[autenticación R2](https://developers.cloudflare.com/r2/api/tokens/).

## Creación inicial del secret Swarm

### Precondiciones

- identidad del manager y contexto Docker comprobados;
- shell administrativa aislada, sin grabación ni tracing;
- token ACME recién creado, verificado mediante la API/UI oficial y limitado a
  la zona;
- nombre versionado confirmado en
  `edge_traefik_cloudflare_secret_name`;
- backup de Swarm y procedimiento de recuperación disponibles.

Docker secrets cifra el material en tránsito y en el Raft log, lo entrega solo
a los servicios autorizados y no permite leer su contenido después:
[modelo de Docker secrets](https://docs.docker.com/engine/swarm/secrets/).

El workflow soportado usa
[`scripts/install-cloudflare-secret.sh`](../scripts/install-cloudflare-secret.sh).
Sin `--token-file`, lee silenciosamente desde el terminal de control. Antes de
crear el secret:

- comprueba que el proceso puede acceder a un manager Swarm;
- rechaza un nombre de secret existente;
- resuelve exactamente una zona activa `apptolast.com`;
- crea y elimina un TXT efímero para comprobar `Zone Read` y `DNS Edit`;
- pasa el token por stdin a `docker secret create`;
- no imprime el valor.

El TXT de verificación es una mutación real y breve en Cloudflare. Se ejecuta
solo después de confirmar la zona y durante una ventana en la que no haya otra
verificación con el mismo propósito.

La invocación interactiva no incluye el token en argumentos, entorno ni
historial:

```bash
sudo -- ./scripts/install-cloudflare-secret.sh \
  --secret-name cloudflare_dns_api_token_v2 \
  --zone apptolast.com
```

Para automatización, `--token-file` solo acepta un fichero regular, no symlink,
sin permisos de grupo/otros. El operador debe retirarlo de forma segura después;
el modo interactivo es preferible para el bootstrap manual.

El operador debe confirmar primero que ese nombre no existe y que no hay una
operación Ansible activa. Los secrets no se actualizan in-place:
`cloudflare_dns_api_token_v1` no se borra ni se reemplaza a ciegas. Se crea una
versión nueva, se cambia la referencia versionada y solo se revoca la anterior
tras verificar el servicio.

No se registra el token ni un hash reutilizable de su valor. Sí se registra el
ID/nombre de Docker, `CreatedAt`, identificador visible del token Cloudflare,
scope, custodio y fecha de próxima revisión.

### Registro de secrets instalados

<!-- markdownlint-disable MD013 -->

| Docker secret | ID Docker | `CreatedAt` | ID visible Cloudflare | Scope | Custodio | Próxima revisión |
| --- | --- | --- | --- | --- | --- | --- |
| `cloudflare_dns_api_token_v2` | `2l8zyn0elq7ir45hm87qx63zv` | 2026-07-27T09:20:10Z | `185f75d78a7b79a5b1d41e595fdaf90f` | Zone Read + DNS Edit sobre `apptolast.com` (uso ACME/Traefik) | Pablo Hurtado Gonzalo | 2026-10-25 |
| `cloudflare_dns_api_token_v1` | `hq1sjhfojnyujxf96ryngdiu0` | 2026-07-26 (aprox.) | desconocido | desconocido; expuesto fuera del gestor previsto | Pablo Hurtado Gonzalo | revocar tras verificar `v2` en servicio |

<!-- markdownlint-enable MD013 -->

## ACME: staging antes de producción

El valor declarado actualmente para `edge_traefik_acme_ca_server` es el
directorio de producción. Esto no demuestra que exista un ensayo staging. El
primer despliegue de producción queda bloqueado hasta completar este flujo:

1. Configurar temporalmente
   `https://acme-staging-v02.api.letsencrypt.org/directory`.
2. Usar un almacenamiento ACME exclusivo de staging. Nunca reutilizar
   `acme.json` de producción ni mezclar cuentas/certificados de ambos entornos.
3. Desplegar solo `edge.apptolast.com` y comprobar creación/limpieza del TXT,
   emisión staging, healthcheck y renovación controlada.
4. Guardar evidencia y retirar el estado staging del path de producción.
5. Configurar
   `https://acme-v02.api.letsencrypt.org/directory`.
6. Crear un `acme.json` de producción vacío, propietario `65532:65532`, modo
   `0600`, y desplegar una sola réplica.
7. Verificar cadena, SAN, expiración, logs y HTTPS externo.
8. Respaldar inmediatamente el estado ACME de producción de forma cifrada y
   offsite.

Let's Encrypt recomienda su entorno staging para pruebas y advierte que sus
raíces no son de confianza pública:
[entorno staging](https://letsencrypt.org/docs/staging-environment/). Las
pruebas repetidas directamente en producción pueden alcanzar
[límites de emisión](https://letsencrypt.org/docs/rate-limits/).

Traefik documenta `caServer`, DNS challenge, renovación y el fichero de storage
en su
[resolver ACME 3.7](https://doc.traefik.io/traefik/v3.7/reference/install-configuration/tls/certificate-resolvers/acme/).

El wrapper usa los dos overrides revisados y registra el perfil
`acme-staging`, sin tocar los valores productivos:

```bash
./scripts/deploy-ansible.sh \
  --playbook edge \
  --profile acme-staging \
  --confirm-production \
  --ask-become-pass
```

Producción vuelve a los valores versionados
`https://acme-v02.api.letsencrypt.org/directory` y `acme.json`. El certificado
staging no es confiable públicamente; se validan DNS-01, logs, health y cadena
staging sin desactivar permanentemente la verificación TLS.

El despliegue posterior de producción usa el mismo wrapper sin `--profile` y
sobrescribe la huella instalada con `profile: production`.

## Arranque DNS-only

El root DNS gestiona exactamente diez A DNS-only:

- un registro nuevo, `edge.apptolast.com`;
- nueve registros de aplicación existentes que primero se adoptan sin cambiar;
- de esos nueve, ocho HTTP cambian a la IPv4 nueva en el cutover actual;
- Minecraft permanece en la IPv4 legacy porque su gate vale `false`.

El orden es:

1. importar los nueve A existentes mediante el flujo de adopción y comprobar
   que el plan no cambia contenido;
2. confirmar que ninguna otra automatización los gestiona;
3. validar Traefik y los ocho workloads con resolución forzada local;
4. revisar un plan separado que crea `edge` y mueve solo los ocho A HTTP;
5. aplicar y verificar DNS autoritativo/resolvers externos;
6. completar ACME y comprobar `/ping` y cada router;
7. confirmar que Minecraft no cambió.

DNS-01 utiliza un TXT de challenge y no exige mover el A/AAAA de una aplicación
para emitir su certificado:
[challenge types de Let's Encrypt](https://letsencrypt.org/docs/challenge-types/).
Los TXT efímeros de ACME no autorizan modificar los registros que dirigen
tráfico.

Los registros A, AAAA, CNAME o SRV de aplicaciones no se cambian hasta que su
dataset y runtime hayan superado [`MIGRATION.md`](MIGRATION.md). En particular,
el edge inicial no autoriza abrir Minecraft ni publicar `25565/TCP`.

## Despliegue y comprobaciones

Antes de cada despliegue:

- validar templates y stack renderizados;
- confirmar que el secret externo y la overlay cumplen identidad/contrato;
- confirmar que ningún Traefik antiguo ocupa `80/443`;
- comprobar espacio, ownership y modo de `acme.json`;
- conservar los Configs actualmente usados para rollback;
- revisar el diff de configuración y el digest de imagen.

Después:

- nodo `Ready`, `Active` y `Leader`;
- servicio `edge_traefik` exactamente `1/1`;
- task `Running` y healthcheck healthy, sin bucle de reinicios;
- imagen observada igual al digest declarado;
- Config IDs/nombres observados iguales a los renderizados;
- ausencia de mount/endpoint hacia el API de Docker;
- solo los puertos públicos contractuales;
- `https://edge.apptolast.com/ping` válido desde fuera;
- certificado del entorno correcto, SAN y expiración revisados;
- logs y métricas revisados durante los 90 segundos de monitor y después;
- rollback automático no activado y update no pausado.

Con una réplica y `stop-first`, una interrupción breve es esperable durante
actualizaciones. Si se requiere cero downtime hay que rediseñar topología,
publicación y número de nodos; no basta con cambiar `order`.

## Rotación del token ACME

Docker recomienda versionar nombres para rotar secrets. El procedimiento es:

1. Crear en Cloudflare un token nuevo con el mismo scope mínimo y verificarlo.
2. Ensayar DNS-01 con staging y storage staging independiente.
3. Crear con `scripts/install-cloudflare-secret.sh` un secret nuevo, por ejemplo
   `cloudflare_dns_api_token_v2`; el helper volverá a comprobar lectura, creación
   y borrado DNS.
4. Cambiar en Git `edge_traefik_cloudflare_secret_name` al nombre nuevo.
5. Validar y desplegar; comprobar task healthy y referencias del servicio.
6. Verificar una operación ACME controlada sin forzar emisiones innecesarias.
7. Revocar el token anterior en Cloudflare.
8. Solo después de confirmar que ningún servicio referencia el secret anterior,
   eliminarlo de Swarm.
9. Actualizar el registro operativo sin guardar el valor.

Si el despliegue falla, el token anterior no se revoca: se vuelve al Config y
secret anteriores, se verifica servicio y se investiga. Docker impide eliminar
un secret usado por un servicio, pero esa protección no sustituye la inspección
previa:
[ejemplo oficial de rotación](https://docs.docker.com/engine/swarm/secrets/#example-rotate-a-secret).

La misma separación se conserva al rotar Terraform DNS y las dos credenciales
R2. Rotar una no debe exigir distribuirla a consumidores de otra.

## Docker Configs inmutables

Ansible calcula SHA-256 de los YAML renderizados y crea nombres:

```text
edge-traefik-static-<16-hex>
edge-traefik-dynamic-<16-hex>
```

Cada objeto lleva labels `com.apptolast.managed-by=ansible`,
`com.apptolast.stack=edge`, `com.apptolast.kind=static|dynamic` y el SHA-256
completo. El contenido y las labels se verifican antes del deploy. El stack se
aplica con `prune: true` y después se exige exactamente el servicio
`edge_traefik`; esto retira servicios huérfanos del stack, no Docker Configs
históricos.

Este comportamiento evita mutaciones invisibles y mantiene material de
rollback. También genera objetos huérfanos con el tiempo; no se borran como
parte automática de un deploy.

### Garbage collection seguro

Una versión solo es candidata si:

- tiene las labels de gestión esperadas;
- su nombre y hash son coherentes;
- ningún servicio de ningún stack referencia su ID;
- no es la versión activa;
- no es la última versión conocida como buena;
- terminó la ventana de rollback y existe el commit que la reproduce;
- el edge actual lleva estable el periodo acordado.

Procedimiento:

1. Ejecutar
   [`scripts/gc-edge-configs.sh`](../scripts/gc-edge-configs.sh) sin argumentos.
   Este es el modo por defecto y solo presenta un dry-run.
2. El helper excluye los Configs referenciados por el spec actual o
   `PreviousSpec` de **todos** los servicios, no solo `edge_traefik`.
3. También conserva las dos generaciones más recientes de cada tipo
   `static|dynamic`. Esa regla evita limpieza agresiva, pero no demuestra por sí
   sola cuál fue la última pareja buena.
4. Revisar manualmente la lista con nombre, ID, fecha, hash y commit. Si la
   ventana acordada exige más de dos generaciones, no ejecutar `--apply` hasta
   que termine esa retención.
5. Solo con la lista y retención aprobadas, ejecutar el modo destructivo. El
   borrado es uno a uno; nunca usar globs, sustituciones no revisadas ni limpieza
   masiva.
6. Volver a inspeccionar servicio, réplicas y `/ping`.

Docker Configs son inmutables y no pueden eliminarse mientras un servicio los
usa:
[Docker Configs](https://docs.docker.com/engine/swarm/configs/). Si Docker
rechaza un borrado por referencia, no se fuerza ni se elimina el servicio para
facilitar el GC.

## Backup y recuperación del edge

Respaldar por separado:

- `/srv/dockerswarm/traefik/acme.json`, preservando owner/mode;
- `/var/lib/docker/swarm` mediante el procedimiento coherente de Docker;
- commit, templates y hashes de Configs desplegados;
- inventario de secret/config IDs, nunca el contenido recuperado desde un task;
- snapshot DNS y del firewall.

Los backups se cifran antes de salir del VPS, se almacenan fuera del VPS y se
restauran periódicamente en un entorno aislado. `acme.json` contiene material de
cuenta y claves privadas; no se imprime, adjunta a incidencias ni sube a Git.

La recuperación no crea un token Cloudflare válido: la custodia externa debe
permitir emitir un token nuevo si el anterior se revoca. La recuperación de
Swarm sigue el
[procedimiento oficial de Docker](https://docs.docker.com/engine/swarm/admin_guide/#recover-from-disaster).

## Bloqueos actuales que requieren evidencia externa

- contenido/validez/scope del token ACME no observable y rotación pendiente;
- secret `cloudflare_dns_api_token_v1` existente pero todavía sin consumidor;
- staging ACME no demostrado;
- estado, permisos y backup de `acme.json` no verificados;
- registro DNS aplicado y delegación autoritativa no verificados;
- stack, red, puertos y logs de producción no observados desde este documento;
- destino offsite y custodio de claves aún no documentados;
- rutas y stacks de aplicaciones declarados, pero todavía no desplegados;
- ocho overlays de workload y la overlay de monitorización aún no creadas.

Ninguno de estos bloqueos se resuelve inventando un valor en Git.

## Referencias oficiales

- [Traefik 3.7: file provider](https://doc.traefik.io/traefik/v3.7/reference/install-configuration/providers/others/file/)
- [Traefik 3.7: provider Swarm y acceso al API](https://doc.traefik.io/traefik/v3.7/reference/install-configuration/providers/swarm/)
- [Traefik 3.7: ACME](https://doc.traefik.io/traefik/v3.7/reference/install-configuration/tls/certificate-resolvers/acme/)
- [lego: credenciales Cloudflare](https://go-acme.github.io/lego/dns/cloudflare/)
- [Cloudflare: crear API tokens](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/)
- [Docker secrets](https://docs.docker.com/engine/swarm/secrets/)
- [Docker Configs](https://docs.docker.com/engine/swarm/configs/)
- [Let's Encrypt staging](https://letsencrypt.org/docs/staging-environment/)
- [Let's Encrypt rate limits](https://letsencrypt.org/docs/rate-limits/)
