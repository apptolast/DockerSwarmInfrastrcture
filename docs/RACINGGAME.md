# RacingGame: despliegue independiente del juego

Aplica el catálogo `config/racinggame.yml` sin tocar `config/services.yml`,
sus generaciones legacy ni su marcador de restauración. El juego no formó
parte de la auditoría de migración, así que no es un `approved_service`:
sigue el precedente de OrganizationWeb y vive como stack propio.

Es un único proceso Node sin estado. Sirve el cliente estático y ejecuta la
simulación autoritativa de la carrera en memoria. No tiene volumen, ni base
de datos, ni secreto. Un servidor perdido se reconstruye desde este commit
más la imagen publicada, y nada más.

## Alcance y precondiciones

- Commit revisado, CI verde y checkout limpio en el host. Ejecutar
  bootstrap, validate-iac y lint según AGENTS, y conservar sus salidas.
- Un solo nodo Swarm, manager activo y elegible para workloads. El role lo
  comprueba antes de usar healthchecks locales.
- No abre puertos públicos nuevos: el tráfico entra por Traefik en el 443 ya
  publicado. `platform_public_tcp_ports` no cambia.
- El DNS de `racinggame.apptolast.com` lo creó el operador a mano en
  Cloudflare, igual que el de OrganizationWeb. No está bajo Terraform: el
  root `cloudflare/apptolast-dns` sigue bloqueado por el aviso de
  [`docs/DEPLOYMENT_STATUS.md`](DEPLOYMENT_STATUS.md), y ejecutarlo
  reapuntaría nueve registros vivos a un servidor que ya no existe.
- El playbook `edge` debe aplicarse antes: es quien crea la red
  `apptolast-edge-racinggame` y publica la ruta. El role del juego la
  inspecciona, nunca la crea.

## Contrato

| Fichero | Qué fija |
| --- | --- |
| `config/racinggame.yml` | Release, hostname, red edge e imagen baseline |
| `config/image-channels.yml` | Lo que realmente ejecuta el servicio |
| `config/capacity-profiles.yml` | Presupuesto del stack dentro del perfil |
| `stacks/racinggame/stack.yml.j2` | El stack Swarm renderizado |
| `scripts/validate-racinggame.py` | Valida catálogo, canal y formato Docker |

`images.web` del catálogo es la evidencia de restauración: la imagen exacta
revisada, fijada por digest. Lo que el servicio ejecuta lo decide el canal.

## Canal de imagen y auto-actualización

La entrada es un canal, no un hold:

```yaml
- stack: racinggame
  service: web
  baseline: {catalog: racinggame, component: web}
  reference: docker.io/ocholoko888/racinggame:latest
  class: owner
  autoupdate: true
```

`ocholoko888` es un namespace propio, y el validador obliga a que las
imágenes propias sigan `:latest`. Al ser un servicio sin datos propios puede
optar al vigilante, que exige a cambio `failure_action: rollback`, una
ventana `monitor` positiva y un healthcheck activo; el stack cumple los
tres. Shepherd filtra por la etiqueta `apptolast.autoupdate=true` y revisa
una vez por hora, de modo que publicar una imagen nueva en `:latest` la pone
en producción sin intervención.

Cualquier imagen que llegue a ejecutarse debe declarar su commit de origen
en `org.opencontainers.image.revision`; el apply lo comprueba y lo registra
en `/opt/dockerswarm/racinggame/observed-images.yml`.

## Capacidad

El perfil activo `organizationweb` pasa a incluir dos aplicaciones. El juego
aporta 200 mcpu y 128 MiB de reserva, con límites de 1000 mcpu y 256 MiB:

```text
edge             res  100 /   64    lim   500 /   128
workloads        res 2300 / 5920    lim 11600 /  9728
organizationweb  res  500 /  800    lim  2250 /  1600
racinggame       res  200 /  128    lim  1000 /   256
autoupdater      res  100 /   18    lim   250 /    45
-------------------------------------------------------
total activo     res 3200 / 6930    lim 15600 / 11757
presupuesto                         lim 17500 / 12397
```

Queda margen, pero es un sizing nominal, no una garantía de carga sostenida.
El perfil `observability` sigue siendo la alternativa excluyente y no
incluye el juego: su agregado permanece intacto en el techo de 12397 MiB.

## Ruta edge

```yaml
racinggame:
  rule: "Host(`racinggame.apptolast.com`)"
  entryPoints: [websecure]
  middlewares: [edge-security]
  service: racinggame
  tls: {certResolver: letsencrypt}
```

Dos decisiones que no son cosméticas:

- **`passHostHeader: true` es obligatorio.** El endpoint `/info` construye
  la URL del mando desde la cabecera `Host`. Sin ella el código QR
  apuntaría a `http://racinggame_web:3000/pad.html`, un nombre interno de
  Swarm que ningún móvil puede alcanzar.
- **`edge-security` a solas**, como n8n y Atlas. `edge-default` añadiría
  compresión sobre una ruta con WebSockets, y `edge-rate-limit` cuenta por
  IP de cliente: varios jugadores compartiendo una conexión doméstica
  saldrían por la misma IP y se estrangularían entre ellos.

## El timeout que rompía los WebSockets

Traefik cambió en v2.11.2 el valor por defecto de
`respondingTimeouts.readTimeout` a 60s. Ese timeout acota la petición
completa, y Go **no limpia el deadline** de una conexión secuestrada para un
WebSocket: `net/http/httputil` no toca los deadlines en
`handleUpgradeResponse`, y el propio Traefik fija además el deadline en el
socket. El efecto es que toda conexión larga moría al minuto.

Como los `respondingTimeouts` solo existen por entryPoint, no se puede
acotar la corrección a una ruta. `websecure` pasa a `readTimeout: 3600s` y
`writeTimeout: 0s`, que es el valor por defecto de Traefik. Mantiene un tope
frente a cuerpos lentos y deja vivir una partida entera.

Es un cambio compartido por todos los hostnames del edge, aceptado
explícitamente por el propietario.

## Despliegue

El wrapper rechaza un worktree sucio y toma el mutex host-global, así que
los dos pasos son estrictamente secuenciales.

```bash
./scripts/deploy-ansible.sh --playbook edge --check
./scripts/deploy-ansible.sh --playbook edge --confirm-production
./scripts/deploy-ansible.sh --playbook racinggame --check
./scripts/deploy-ansible.sh --playbook racinggame --confirm-production
```

El paso `edge` **reinicia Traefik**. El provider de fichero usa
`watch: false`, de modo que la recarga se consigue cambiando el hash de la
config: nuevo objeto Docker Config, nueva spec de servicio y reemplazo de la
tarea con `order: stop-first`. Son unos segundos sin edge para todos los
hostnames, protegidos por `failure_action: rollback`.

## Verificación

- `https://racinggame.apptolast.com/` responde 200 y sirve el cliente.
- `wss://racinggame.apptolast.com/online` acepta el upgrade y sobrevive a
  una partida completa.
- `monitor.apptolast.com` y el resto de hostnames siguen respondiendo tras
  el reinicio del edge.
- `docker service ls` muestra `racinggame_web` en `1/1` y el contenedor en
  `healthy`.

El repositorio del juego trae su propia prueba de regresión:
`node selfcheck.mjs` arranca el servidor real y comprueba el aislamiento del
relay por sesión, la URL del mando tras un proxy inverso, la superficie
estática y una carrera en sala.

## Límites conocidos

- El juego no persiste nada. Un reinicio vacía las salas en curso; no hay
  backup que hacer ni que restaurar.
- Una sala admite 8 jugadores (`MAX_PLAYERS`). No hay cuota por IP ni
  autenticación: cualquiera que conozca el nombre de una sala puede entrar.
- Con una sola réplica no hace falta afinidad de sesión. Escalar el servicio
  rompería los WebSockets, porque el edge no tiene ninguna configurada.
- El digest de la imagen base del Dockerfile se actualiza a mano; nada
  vigila sus CVE.
