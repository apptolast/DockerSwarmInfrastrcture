# Actualización automática por canales de imagen

Este documento describe el modelo de canales de imagen decidido por el owner
el 2026-09-11: todo servicio Swarm, actual o futuro, se actualiza desde un
**canal revisado** en Git en lugar de un digest revisado. Las imágenes propias
siguen `:latest`; las bases de datos de terceros siguen un canal de versión
mayor fijado. La regla de oro del repositorio se reinterpreta así: un servidor
perdido se reconstruye desde un commit revisado que fija **canales**, no
bytes; la reconstrucción descarga la cabeza actual de cada canal.

## Piezas del modelo

- [`config/image-channels.yml`](../config/image-channels.yml) es la única
  fuente de lo que ejecuta cada servicio renderizado por los stacks `edge`,
  `workloads`, `organizationweb` y `observability`.
- [`scripts/validate-image-channels.py`](../scripts/validate-image-channels.py)
  valida el fichero, lo liga a su baseline y comprueba los stacks
  renderizados. `scripts/validate-iac.sh` lo ejecuta tras renderizar todo.
- [`scripts/resolve-image-channel.py`](../scripts/resolve-image-channel.py)
  resuelve un canal a su digest actual durante el preflight de imágenes y
  verifica la imagen descargada.
- El rol `image_channels` deriva en el controlador el mapa por stack que
  consumen `image_preflight`, `edge`, `workloads`, `organizationweb`,
  `observability` y `autoupdater`.
- [`config/autoupdater.yml`](../config/autoupdater.yml),
  [`stacks/autoupdater/stack.yml.j2`](../stacks/autoupdater/stack.yml.j2) y
  [`scripts/validate-autoupdater.py`](../scripts/validate-autoupdater.py)
  definen el vigilante (Shepherd); el playbook `autoupdater` lo despliega.

`config/services.yml`, las imágenes de `config/organizationweb.yml` y el pin
de Traefik en `ansible/group_vars/all.yml` no cambian: son el baseline
revisado y la evidencia de restauración. El hash de `config/services.yml`
sigue ligado al marker de restauración, así que no se edita para actualizar
imágenes.

## Formas de una entrada

Cada entrada de `image_channel_services` declara `stack`, `service`,
`baseline` (`catalog` y `component`), `reference`, `class` y `autoupdate`.

<!-- markdownlint-disable MD013 -->

| Forma de `reference` | Modo | `autoupdate` | Uso |
| --- | --- | --- | --- |
| `repo:tag` | canal | `true` o `false` | El CLI la resuelve al desplegar; con `true`, el vigilante la re-fija entre despliegues. |
| `repo:tag@sha256:...` | hold | siempre `false` | Migración escalonada o rollback a un digest concreto. |
| `repo@sha256:...` | hold base | siempre `false` | Solo si es byte a byte la referencia del baseline; conserva el servicio actual sin reiniciarlo. |
| `repo` sin tag ni digest | rechazada | — | Implicaría `:latest` sin revisión. |

<!-- markdownlint-enable MD013 -->

Reglas que el validador aplica sin excepciones:

- El repositorio normalizado de `reference` es el del baseline.
- `class` es `owner` para `apptolast`, `ocholoko888` y `hgarciaalberto`, y su
  tag es siempre `latest`; `stateful-major` para bases de datos, colas y
  Traefik; `third-party` para el resto.
- Una entrada `stateful-major` solo acepta el tag exacto de la tabla del
  validador, cuya versión mayor debe coincidir con la del baseline: `pg16`,
  `15-alpine`, `16-alpine`, `7.2-alpine`, `v3`, `17-alpine` y
  `4.3-management-alpine`. Un `postgres:17` o `rabbitmq:4.3` sin sufijo usa
  otra distribución y otro uid, y se rechaza.
- Todo servicio renderizado aparece exactamente una vez, en
  `image_channel_services` o en `image_channel_exclusions`. Un servicio
  nuevo sin entrada rompe CI.
- La etiqueta de servicio `apptolast.autoupdate` vale `"true"` si y solo si
  `autoupdate` es verdadero. Las exclusiones nunca valen `"true"`.
- Un servicio con `autoupdate: true` necesita
  `update_config.failure_action: rollback`, una ventana `monitor` no nula y
  un healthcheck activo.
- El socket Docker solo se permite en el stack `autoupdater`.
- El tag de un hold es solo texto: manda el digest. Un hold `stateful-major`
  que no sea byte a byte su baseline (por ejemplo
  `postgres:16-alpine@sha256:...`) prueba su versión mayor antes de mutar
  ningún stack. El preflight, o el rol `organizationweb`, que no pasa por él,
  descarga la imagen y exige el valor de la tabla en `PG_MAJOR`,
  `REDIS_VERSION`, `RABBITMQ_VERSION` o, para Traefik, en la etiqueta OCI
  `org.opencontainers.image.version`. Así un digest de PostgreSQL 17 bajo el
  tag `16-alpine` no cruza la mayor en silencio.

Exclusiones revisadas: `workloads/n8n-runners`, que se construye en local
desde `images/n8n-runners`, y `autoupdater/shepherd`, que nunca se actualiza
a sí mismo.

Efecto aceptado de excluir `n8n-runners`: con `resolve_image: changed`, el
CLI también consulta Docker Hub por `apptolast/n8n-runners:src-<sha256>`
cuando ese tag cambia (cualquier bump de Dependabot en `images/n8n-runners`
lo cambia) y al crear el servicio. `apptolast` es un namespace del owner en
Docker Hub: si ese tag existiera allí, Swarm fijaría y descargaría los bytes
remotos en lugar de la build local. Por eso el rol `workloads` consulta el
registro antes de mutar y se detiene si el tag existe. Un fallo de la
consulta, por ejemplo sin red, deja igualmente al CLI sin resolverlo, así que
no se trata como existencia. Tras el deploy, el runner sigue exigiendo
igualdad exacta con su tag local.

## Por qué Ansible ya no pelea con el vigilante

Los stacks se despliegan con `resolve_image: changed`. El CLI de Docker
compara la imagen renderizada con la etiqueta de servicio
`com.docker.stack.image`:

- Si son iguales, conserva el digest vivo del servicio. Un nuevo apply no
  revierte lo que aplicó el vigilante y `docker_stack` sigue en
  `changed=0`.
- Si difieren, o el servicio es nuevo, consulta el registro y fija el
  digest actual del canal.

Antes del despliegue, cada rol lee el spec vivo de sus servicios. Un hold cuya
cadena renderizada no cambió debe ejecutar ya su identidad exacta: si alguien
lo movió fuera de Git (`docker service update --image`), el CLI conservaría
esa imagen, así que el apply se detiene antes de mutar. Primero se registra
la imagen viva en su entrada; el apply no la repara.

Tras el despliegue, una entrada hold exige la identidad exacta revisada en el
spec vivo. Una entrada de canal exige un digest de su propio `repo:tag` y, en
`edge`, `workloads` y `observability`, además que sea el digest que el
preflight resolvió, descargó y verificó como `linux/amd64`, o el digest vivo
que el servicio ya tenía antes del deploy. El CLI vuelve a resolver el canal
durante `docker stack deploy`: si el canal avanzó entre el preflight y el
deploy, el apply falla después de mutar y basta con repetirlo. OrganizationWeb
no pasa por el preflight y verifica plataforma, digest y revisión de la
imagen que cada servicio ejecuta tras el deploy.

Cada apply registra la imagen observada de cada servicio en
`observed-images.yml` junto al stack instalado (`/opt/dockerswarm/...`). Ese
fichero cambia legítimamente cuando el vigilante movió un digest desde el
apply anterior o cuando cambia la revisión fuente, así que su tarea puede
informar `changed` aunque `docker_stack` no cambie. Tras una actualización
del vigilante, el primer apply registra el digest nuevo y el siguiente apply
consecutivo desde el mismo commit vuelve a `changed=0`.

## Estado tras este cambio

Ningún servicio tiene `autoupdate: true` en Git. El vigilante que corría
en el host sin registrar (`autoupdater_shepherd`, con `IGNORELIST_SERVICES`
en lugar del filtro por etiqueta) queda registrado en Git por el stack
`autoupdater` (ver «Vigilante registrado»). Hasta que ese stack se aplique,
el preflight de capacidad de un checkout anterior rechaza cualquier apply de
`edge`, `workloads` u `organizationweb` porque ese servicio vivo queda fuera
del perfil revisado.

Las entradas iniciales parten del inventario vivo del 2026-09-12:

- Canal con `autoupdate: false`, adoptando en Git el tag que ya ejecuta cada
  servicio: `kropia`, `minecraft`, `minecraft-stats`, `passbolt`,
  `portfolio-alberto`, `portfolio-pablo`, `selenium`, `shlink` y
  `organizationweb` `backend`/`web` en `:latest`, y Traefik en `v3`. Un
  canal tolera que el vigilante mueva el digest, así que el apply solo
  reescribe etiquetas de servicio. Traefik es la excepción: su spec vivo es
  `traefik:latest@...` y la cadena pasa a `traefik:v3@...`. El digest es el
  mismo (v3.7.13 en ambos tags), pero el cambio reinicia la tarea
  (`stop-first`).
- `redis-coordinator` vuelve en hold a los bytes revisados de 7.2.11
  (`redis:7.2-alpine@sha256:...`). Una actualización no revisada dejó
  `redis:latest` (8.x, sin digest) en vivo. No tiene volumen, así que ningún
  dato cruza la versión mayor; su tarea se reinicia.
- El resto de entradas quedan en hold con la referencia que se renderiza
  hoy, igual a su spec vivo.
- `config/workload-image-updates.yml` desaparece.

El primer apply de cada stack (`edge`, `workloads`, `organizationweb` y, al
activarse, `observability`) informa `changed` en `docker_stack` aunque no se
adopte nada: todo servicio renderizado gana la etiqueta de servicio
`apptolast.autoupdate`, que cambia `Spec.Labels`, y el módulo compara el
`docker service inspect` completo antes y después. `TaskTemplate` no cambia,
así que solo se reinician `edge_traefik` y `workloads_redis-coordinator`,
más cualquier canal cuya cabeza haya avanzado desde el último ciclo del
vigilante. El segundo apply consecutivo debe devolver `changed=0`.

El stack `autoupdater` (Shepherd, filtro exacto
`label=apptolast.autoupdate=true`) está registrado desde el cambio siguiente
a la introducción de los canales. Como ninguna entrada tiene
`autoupdate: true`, tras aplicarlo el vigilante **no selecciona ningún
servicio**: la activación por servicio llega en cambios posteriores y
revisados.

## Vigilante registrado

El stack `autoupdater` tiene un único servicio, `shepherd`. Todo su contrato
está en `config/autoupdater.yml` y lo fija
`scripts/validate-autoupdater.py`, que `scripts/validate-iac.sh` ejecuta y
renderiza en `.build/autoupdater/stack.yml` para que
`scripts/validate-image-channels.py` y los validadores de capacidad lo
comprueben.

- **Imagen.** `containrrr/shepherd:v1.8.1`, fijada por digest y desplegada
  con `resolve_image: never`. Es una exclusión del mapa de canales: nunca se
  actualiza a sí mismo y su etiqueta `apptolast.autoupdate` vale `"false"`.
  La investigación del 2026-09-11 registró que `v1.8.1` y `latest` resolvían
  al mismo digest de índice que el servicio vivo; este repositorio no puede
  volver a verificarlo sin consultar Docker Hub.
- **Entorno exacto.** `FILTER_SERVICES=label=apptolast.autoupdate=true`,
  `SLEEP_TIME=1h`, `TIMEOUT=900`, `VERBOSE=true`, `TZ=UTC`,
  `WITH_REGISTRY_AUTH=true` y `REGISTRY_USER=ocholoko888`. Ninguna otra
  clave: Shepherd activa `WITH_NO_RESOLVE_IMAGE`, `RUN_ONCE_AND_EXIT`,
  `WITH_INSECURE_REGISTRY` y `ROLLBACK_ON_FAILURE` por mera presencia, y
  `IGNORELIST_SERVICES`, `UPDATE_OPTIONS` e `IMAGE_AUTOCLEAN_LIMIT` amplían o
  alteran su alcance. Un filtro vacío seleccionaría todos los servicios.
- **Credencial.** Con `REGISTRY_USER` definido, Shepherd lee la contraseña de
  `/run/secrets/shepherd_registry_password` y solo si ese fichero no existe
  recurre a `REGISTRY_PASSWORD`, que el validador prohíbe. El stack monta el
  secret externo `autoupdater-dockerhub-pat-v1` en ese destino (uid 0, gid 0,
  modo `0400`). El token nunca está en Git: el owner creó el secret a mano
  desde un fichero solo legible por root, con las etiquetas
  `com.apptolast.managed-by=manual-bootstrap` y
  `com.apptolast.purpose=autoupdater`, que el rol exige antes de desplegar.
  Rotación: crear `-v2`, cambiar el nombre en `config/autoupdater.yml`,
  aplicar y después revocar el token anterior.
- **Socket.** Solo este stack puede montar `/var/run/docker.sock` y solo en
  lectura. Un bind de solo lectura no limita la API: el servicio sigue
  siendo equivalente a root en el único manager.
- **Ubicación.** Una réplica en `node.role == manager`, sin puertos, en una
  red overlay propia no attachable para salir a Docker Hub.

### Por qué `SLEEP_TIME=1h`

En cada ciclo Shepherd ejecuta, por servicio seleccionado, un
`docker manifest inspect` (un GET de manifiesto, que Docker Hub cuenta) y un
`docker service update --image` que vuelve a resolver el tag; cuenta hasta
dos peticiones por servicio, más la descarga real si el digest cambió. Una
cuenta gratuita autenticada tiene 200 descargas cada 6 horas.

<!-- markdownlint-disable MD013 -->

| `SLEEP_TIME` | Ciclos / 6 h | Con 19 servicios de Docker Hub (1 petición) | Con 2 peticiones |
| --- | --- | --- | --- |
| `20m` (valor vivo sin revisar) | 18 | 342 | 684 |
| `1h` | 6 | 114 | 228 |

<!-- markdownlint-enable MD013 -->

Con `20m` el presupuesto se agota aunque cada servicio cueste una sola
petición. Con `1h` quedan 86 descargas para actualizaciones reales y applies
si cuesta una; si cuesta dos, el límite es 16 servicios (200 / 12). Antes de
activar muchos servicios, comprueba la cabecera `ratelimit-remaining`. Tras
este cambio el vigilante no selecciona ninguno y solo inicia sesión.

### Recursos y riesgo de OOM

El servicio reserva 100m CPU y 18 MiB, con límites de 250m CPU y 45 MiB
(el vivo sin revisar tenía 128 MiB). Los límites de memoria del perfil de
plataforma completa (`edge`, `workloads`, `observability`) sumaban
12352 MiB sobre un presupuesto de 12397 MiB (15981 − 3072 de reserva − 512
de margen operativo); 45 MiB es todo lo que queda y la relación
límite/reserva de 2,5 fija la reserva en 18 MiB. En el perfil activo
(`organizationweb`) los límites suben de 11456 a 11501 MiB. Shepherd es bash
más el CLI de Docker, y este repositorio no tiene una medida de su memoria
real: si el contenedor muere por OOM, se reinicia sin tocar ningún servicio
a medias (Swarm conserva la actualización ya enviada). Vigila
`docker service ps autoupdater_shepherd` tras el primer apply y, si hay OOM,
reequilibra el presupuesto en un PR revisado.

### Aplicar el registro

1. Tras el merge del cambio de canales, aplica en orden `edge`, `workloads` y
   `organizationweb` desde su checkout (sección siguiente).
2. Desde un checkout limpio de este cambio:
   `./scripts/deploy-ansible.sh --playbook autoupdater --check`, y después
   con `--confirm-production`. El apply sustituye el spec vivo sin revisar
   por el revisado; el rol comprueba después imagen, entorno, filtro,
   réplicas, colocación, secret y socket.
3. `sudo -- docker service logs autoupdater_shepherd` debe mostrar el inicio
   de sesión y la espera de una hora sin intentar actualizar ningún servicio.
4. Repite `--playbook workloads` y exige `changed=0`.

Shepherd solo se reactiva con este apply, nunca con `docker service scale`.

## Antes del primer apply

Inventario de solo lectura, obligatorio antes de aplicar este cambio: para
cada entrada hold, en especial las bases de datos y `organizationweb_*`, la
imagen viva debe ser su `spec_exact` (`validate-image-channels.py derive`),
salvo `redis-coordinator`, cuya cadena renderizada cambia a propósito.

```bash
sudo -- docker service inspect \
  --format '{{.Spec.TaskTemplate.ContainerSpec.Image}}' \
  organizationweb_postgres
```

Si un servicio se movió fuera de Git, se cambia antes su entrada en
`config/image-channels.yml` a esa imagen viva, en un PR revisado. El apply lo
rechaza antes de mutar, pero no lo repara. La investigación del 2026-09-11
indica que `organizationweb_postgres` y `organizationweb_rabbitmq` ejecutan
sus pins; Traefik queda por confirmar.

## Cambiar una entrada

1. Edita solo `config/image-channels.yml` en una rama. Para pasar un
   servicio a su canal usa la forma `repo:tag`; para activar la
   actualización automática pon además `autoupdate: true`.
2. Para un servicio con estado, haz antes un volcado lógico documentado:
   no hay copia fuera del host (STOP gate 5) y las migraciones de esquema de
   n8n, Passbolt, Shlink y OrganizationWeb no se deshacen.
3. Ejecuta `./scripts/bootstrap-tooling.sh`, `./scripts/validate-iac.sh` y
   `./scripts/lint.sh`, y registra el cambio en `CHANGELOG.md`.
4. Tras el merge, ejecuta el playbook del stack con `--check` y después con
   `--confirm-production`. Repite el apply y exige `changed=0` en ese segundo
   apply consecutivo.

## Interruptor y rollback

- **Interruptor general.** Un PR que pone `enabled: false` en
  `config/autoupdater.yml` (renderiza `replicas: 0`, el servicio sigue
  registrado y su presupuesto de capacidad reservado) y
  `--playbook autoupdater`. Reactivarlo es el mismo PR con `true`. Nunca uses
  `docker service scale` a mano salvo emergencia:
  `sudo -- docker service scale autoupdater_shepherd=0`, y ese mismo día un PR
  con `enabled: false` y su apply, porque el siguiente apply devolvería la
  réplica.
- **Rollback de un servicio.** Un PR que cambia la entrada a hold
  (`repo:tag@sha256:<último bueno>`, `autoupdate: false`) y el playbook de
  su stack. La cadena cambia respecto a la etiqueta, así que Swarm fija ese
  digest. El último digest bueno está en `observed-images.yml`, en
  `sudo -- docker image ls --digests <repo>` o, para imágenes propias, en su
  tag `:<sha>`. Para aplicaciones propias el rollback normal es un revert en
  su repositorio, que publica un `:latest` nuevo.
- **No uses `docker service rollback`.** El vigilante reescribe el
  `PreviousSpec` en cada ciclo y un `PreviousSpec` antiguo puede apuntar a
  una versión mayor distinta.
- **Datos.** Nunca se cruza una versión mayor sin migración. Bajar un parche
  de PostgreSQL dentro de la misma mayor es seguro; RabbitMQ puede fallar por
  feature flags ya activados.
- **Todo el modelo.** No hagas `git revert` a los digests del baseline:
  degradaría aplicaciones ya migradas. Pon cada servicio en hold en su
  digest vivo actual y después retira el vigilante.

## Riesgos aceptados

- La reconstrucción reproduce canales, no bytes. Solo `observed-images.yml`
  registra los digests de cada apply; los que aplique el vigilante entre
  applies no quedan registrados hasta el siguiente.
- Sin backups externos (STOP gate 5), las aplicaciones aplican migraciones
  de esquema por sí solas.
- El vigilante usa el socket Docker en el único manager, equivalente a root
  aunque el bind sea de solo lectura. El owner acepta este consumidor.
- Shepherd v1.8.1 apenas se mantiene (último commit 2025-11-11) y tiene
  fallos latentes que ocultan errores en su log; el `failure_action:
  rollback` de Swarm sigue aplicando.
- Sin protección de rama, cualquier merge a `main` de una aplicación propia
  que pase su CI llega a producción.
- n8n y sus runners locales deben avanzar juntos; n8n sigue en hold hasta
  que el owner decida cómo publicar los runners.
