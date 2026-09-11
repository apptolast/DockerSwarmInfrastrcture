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
  consumen `image_preflight`, `edge`, `workloads`, `organizationweb` y
  `observability`.

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

Exclusiones revisadas: `workloads/n8n-runners`, que se construye en local
desde `images/n8n-runners`, y `autoupdater/shepherd`, que nunca se actualiza
a sí mismo.

## Por qué Ansible ya no pelea con el vigilante

Los stacks se despliegan con `resolve_image: changed`. El CLI de Docker
compara la imagen renderizada con la etiqueta de servicio
`com.docker.stack.image`:

- Si son iguales, conserva el digest vivo del servicio. Un nuevo apply no
  revierte lo que aplicó el vigilante y el segundo apply sigue en
  `changed=0`.
- Si difieren, o el servicio es nuevo, consulta el registro y fija el
  digest actual del canal.

Tras el despliegue, una entrada hold exige la identidad exacta revisada en el
spec vivo y una entrada de canal exige un digest de su propio `repo:tag`.
Cada apply registra la imagen observada de cada servicio en
`observed-images.yml` junto al stack instalado (`/opt/dockerswarm/...`).

## Estado tras este cambio

Ningún servicio tiene `autoupdate: true` y no hay vigilante desplegado, así
que nada se actualiza solo todavía. Las entradas iniciales reproducen lo que
se renderiza hoy, con dos salvedades deliberadas:

- `kropia`, `portfolio-pablo`, `minecraft-stats` y `minecraft` ya ejecutan
  la cabeza de `:latest` en producción; sus entradas adoptan ese canal con
  `autoupdate: false`. El primer apply fija la cabeza actual.
- `portfolio-alberto` queda en hold en el digest que antes aprobaba
  `config/workload-image-updates.yml`, fichero que desaparece.

El registro del stack `autoupdater` (Shepherd, filtro exacto
`label=apptolast.autoupdate=true`) llega en un cambio posterior y revisado.

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
   `--confirm-production`. Repite el apply y exige `changed=0`.

## Interruptor y rollback

- **Interruptor general.** Un PR que deshabilita el vigilante y un apply de
  su playbook. En emergencia,
  `sudo -- docker service scale autoupdater_shepherd=0`, codificado en Git
  el mismo día.
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
- El vigilante necesitará el socket Docker de lectura y escritura en el
  único manager, equivalente a root.
- Sin protección de rama, cualquier merge a `main` de una aplicación propia
  que pase su CI llega a producción.
- n8n y sus runners locales deben avanzar juntos; n8n sigue en hold hasta
  que el owner decida cómo publicar los runners.
