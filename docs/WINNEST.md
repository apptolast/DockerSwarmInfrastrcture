# WinNest: despliegue independiente de la web

Aplica el catálogo `config/winnest.yml` sin tocar `config/services.yml`, sus
generaciones legacy ni su marcador de restauración. Como RacingGame, la web
no formó parte de la auditoría de migración, así que no es un
`approved_service`: vive como stack propio.

Es la web estática de WinNest (Astro): portada, precios, descarga, notas de
versión y páginas legales, en inglés y español. Un NGINX sin privilegios
sirve los ficheros ya generados. No tiene volumen, ni base de datos, ni
secret, ni cookies, ni nada de terceros. Un servidor perdido se reconstruye
desde este commit más la imagen publicada, y nada más.

## Alcance y precondiciones

- Commit revisado, CI verde y checkout limpio en el host. Ejecutar
  bootstrap, validate-iac y lint según AGENTS, y conservar sus salidas.
- Un solo nodo Swarm, manager activo y elegible para workloads. El role lo
  comprueba antes de usar healthchecks locales.
- No abre puertos públicos nuevos: el tráfico entra por Traefik en el 443 ya
  publicado. `platform_public_tcp_ports` no cambia.
- El DNS de `winnest.apptolast.com` (A a `159.195.156.57`, DNS-only, sin
  AAAA) lo creó el propietario a mano en Cloudflare, igual que el de
  RacingGame. No está bajo Terraform por el aviso de
  [`docs/DEPLOYMENT_STATUS.md`](DEPLOYMENT_STATUS.md) («Advertencia sobre
  Terraform y DNS»). Sin AAAA a propósito: Traefik solo publica 80/443 en
  IPv4.
- El playbook `edge` debe aplicarse antes: es quien crea la red
  `apptolast-edge-winnest` y publica la ruta. El role de la web la
  inspecciona, nunca la crea.

## Contrato

<!-- markdownlint-disable MD013 -->

| Fichero | Qué fija |
| --- | --- |
| `config/winnest.yml` | Release, hostname, red edge e imagen baseline |
| `config/image-channels.yml` | Lo que realmente ejecuta el servicio |
| `config/capacity-profiles.yml` | Presupuesto del stack dentro del perfil |
| `stacks/winnest/stack.yml.j2` | El stack Swarm renderizado |
| `stacks/edge/dynamic.yml.j2` | Router `winnest` y su upstream `http://winnest_web:8080` |
| `scripts/validate-winnest.py` | Valida catálogo, canal, endurecimiento y formato Docker |

<!-- markdownlint-enable MD013 -->

`images.web` del catálogo es la evidencia de restauración: la imagen exacta
revisada, fijada por digest y construida desde el commit `release` del
repositorio privado `PabloHurtadoGonzalo86/WinNest`. Lo que el servicio
ejecuta lo decide el canal.

## La imagen

`docker.io/ocholoko888/winnest-website` sale de `website/Dockerfile` del
repositorio de WinNest, con la raíz del repositorio como contexto:

- una etapa Node genera la web con `pnpm website:build`, que ya exige
  `astro check` sin errores, avisos ni sugerencias;
- la imagen final es `nginxinc/nginx-unprivileged` (Alpine, rama estable),
  fijada por digest, con su propio `nginx.conf`: escucha en el 8080, corre
  como `101:101`, guarda el PID y los temporales en `/tmp`, sin log de
  acceso (ya lo guarda Traefik) y con `/healthz`;
- cada página lleva la `Content-Security-Policy` de la web, `nosniff`,
  `X-Frame-Options: DENY` y su `Referrer-Policy`; `/_astro/` se cachea un
  año (`immutable`), y los 404 sirven la página en inglés o, bajo `/es/`, en
  español.

El workflow `website-image.yml` de ese repositorio la construye, la prueba
con `website/deploy/smoke-test.sh` sobre el contenedor endurecido y, en cada
push a `main` que toque la web, la publica como `:latest` y
`:sha-<commit>`, con el commit en
`org.opencontainers.image.revision`. Sus acciones van fijadas por commit y
el token de Docker Hub solo llega al paso de login.

La imagen baseline no salió de ese workflow: su secret `DOCKERHUB_TOKEN`
aún no existía. Se construyó el 2026-10-07 en este host con el mismo
Dockerfile, desde un clon limpio del commit `release`, pasó la misma prueba
de humo con el endurecimiento del stack y se publicó con el login de Docker
Hub de `ocholoko888` que root ya tenía en `/root/.docker/config.json` (desde
el 2026-09-11, fuera de este repositorio). Ese login es una credencial de
escritura de toda la cuenta guardada en el host; retirarlo es una decisión
del propietario, porque nada en este repositorio documenta quién más lo
usa.

## Versión anunciada

La web anuncia la release marcada como «Latest» en el repositorio público
`PabloHurtadoGonzalo86/WinNest-releases`, la misma que instala su botón de
descarga (`releases/latest/download/WinNest-setup-x64.exe`). El workflow
`website-image.yml` la resuelve con la API de GitHub, comprueba que tenga el
instalador y la pasa a la construcción (`WINNEST_RELEASE_TAG`). La portada,
la descarga y las notas de versión muestran esa versión y las anteriores; si
el `CHANGELOG.md` no tiene su sección, la construcción falla. La imagen la
lleva en `org.opencontainers.image.version` y la prueba de humo comprueba
que las páginas la anuncian.

Ese workflow se ejecuta en cada cambio de la web en `main`, cuando
`pnpm release:publish` publica una versión (lo lanza al terminar) y cada
tres horas, por si una versión se publicó a mano en GitHub. Esa revisión
solo publica si `:latest` no anuncia ya la release o le falta algún cambio
de la web. El vigilante la despliega en menos de una hora.

## Canal de imagen y auto-actualización

La entrada es un canal, no un hold:

```yaml
- stack: winnest
  service: web
  baseline: {catalog: winnest, component: web}
  reference: docker.io/ocholoko888/winnest-website:latest
  class: owner
  autoupdate: true
```

`ocholoko888` es un namespace propio y el validador obliga a que sus
imágenes sigan `:latest`. La web no guarda datos, así que puede optar al
vigilante, que exige `failure_action: rollback`, una ventana `monitor`
positiva y un healthcheck activo; el stack cumple los tres. Shepherd revisa
una vez por hora: un cambio de la web fusionado en `main` de WinNest llega
a producción sin intervención, en menos de una hora más lo que tarde su CI.

Suma una consulta de manifiesto a Docker Hub por ciclo: siete servicios
vigilados en Docker Hub son 42 consultas cada 6 h, dentro de las 200 de la
cuenta gratuita autenticada (ver `config/autoupdater.yml`).

Cualquier imagen que llegue a ejecutarse debe declarar su commit de origen
en `org.opencontainers.image.revision`; el apply lo comprueba y lo registra
en `/opt/dockerswarm/winnest/observed-images.yml`.

## Endurecimiento del servicio

- `read_only: true`, `cap_drop: [ALL]`, `user: "101:101"` e `init: true`;
  el daemon ya aplica `no-new-privileges`.
- Un único `tmpfs` de 16 MiB en `/tmp`, sin bind mounts, secrets, configs
  ni puertos publicados. `scripts/validate-winnest.py` rechaza cualquiera
  de ellos, otro usuario u otro entorno.
- `NGINX_ENTRYPOINT_QUIET_LOGS=1` silencia los scripts de arranque de la
  imagen base, que no tienen nada que configurar sobre una raíz de solo
  lectura.

## Capacidad

El perfil activo `organizationweb` pasa a incluir tres aplicaciones. La web
aporta 50 mcpu y 16 MiB de reserva, con límites de 100 mcpu y 32 MiB. Se
midió en este host el 2026-10-07 con la imagen publicada, limitada a
0,1 CPU y 32 MiB: 3 000 peticiones, 64 a la vez, todas `200`, con un pico
de 7 MiB y ningún OOM.

```text
perfil activo     res 3160 / 5698    lim 17000 / 12205
presupuesto                          lim 17500 / 12397
```

Quedan 192 MiB y 500 mcpu de límites libres, que son el techo de
`ate-setup`, el instalador transitorio de Substrate que corre junto al
nodo del laboratorio AX (ver [AX.md](AX.md), «Capacidad»). Por eso baja de
224 a 192 MiB (96 MiB reservados, `MemAvailable` mínimo de 704 MiB), con la
misma CPU, igual que bajó de 256 a 224 MiB cuando entró el reenviador del
panel web. Solo corre cuando la instalación de Substrate deriva y nada mide
aún su consumo real: si un día no le basta, el kernel lo mata dentro de su
propio límite y el apply de `ax-lab` falla, sin afectar al resto. El perfil
`observability` no cambia y no incluye la web. Las cifras vigentes están en
[CAPACITY.md](CAPACITY.md).

## Ruta edge

```yaml
winnest:
  rule: "Host(`winnest.apptolast.com`)"
  entryPoints: [websecure]
  middlewares: [edge-default]
  service: winnest
  tls: {certResolver: letsencrypt}
```

`edge-default` es `edge-security` más compresión, como los dos portfolios:
la web solo sirve ficheros estáticos y NGINX no comprime. `edge-security`
añade HSTS, `Permissions-Policy` y borra la cabecera `Server`, y sustituye
la `Referrer-Policy` de la web (`strict-origin-when-cross-origin`) por
`no-referrer`, más estricta. La `Content-Security-Policy` y la caché de
`/_astro/` llegan intactas. El upstream se sondea en `/healthz` cada 15 s.

## Despliegue

El wrapper rechaza un worktree sucio y toma el mutex host-global, así que
los pasos son estrictamente secuenciales. El paso `edge` sigue su ventana
(ver «Ventana de la ruta» abajo).

```bash
./scripts/deploy-ansible.sh --playbook edge --local --check
./scripts/deploy-ansible.sh --playbook edge --local --confirm-production
./scripts/deploy-ansible.sh --playbook winnest --local --check
./scripts/deploy-ansible.sh --playbook winnest --local --confirm-production
```

El role termina probando la ruta pública con el certificado verificado:
`https://winnest.apptolast.com/healthz` debe responder `ok` a través de
Traefik, con hasta cinco minutos de espera por el certificado DNS-01.

### Ventana de la ruta

Es la ventana de `edge` que publica `winnest.apptolast.com`. Sigue las
reglas de la compuerta STOP 10 de `CLAUDE.md`, igual que «Ventana del
segundo nombre» de [EDGE.md](EDGE.md): unos 13 s sin conexiones nuevas en
80/443 para todos los hostnames, cortes en los WebSocket y SSE abiertos,
una sola persona, fuera de 22:30–00:40 UTC, desde un checkout limpio en
detached HEAD sobre el commit fusionado, tras `git fetch --all --prune`.

1. Igual que el paso 1 de «Ventana de aplicación de la ruta» de
   [EDGE.md](EDGE.md): el `Version.Index` y las dos Configs deben ser los
   registrados por la última ventana, y cada secret de la ruta de AX debe
   mostrar su nombre y sus dos etiquetas. Un índice distinto con las mismas
   Configs se explica antes de seguir, comparando `Spec` con
   `PreviousSpec`.
2. `edge_probe > /tmp/edge-before-winnest.txt`. La línea de
   `https://winnest.apptolast.com/` sale con `000`: aún no hay router ni
   certificado y `sniStrict` rechaza el TLS.
3. `--check` y, si está limpio, el apply de `edge`. Cambios esperados: la
   red `apptolast-edge-winnest`, la Config dinámica nueva y el servicio, que
   gana la red y relanza la tarea.
4. `edge_probe > /tmp/edge-after-winnest.txt` y `diff` de los dos: solo
   cambia la línea de `https://winnest.apptolast.com/`, que sigue en `000`
   mientras Traefik obtiene el certificado y pasa a `502` o `503`, porque
   el stack de la web aún no existe. Cualquier otra diferencia es motivo de
   rollback (el de «Rollback de la ruta» de EDGE.md).
5. `--check` y apply de `winnest`, y después `website/deploy/smoke-test.sh
   https://winnest.apptolast.com` desde el repositorio de WinNest.
6. Solo si los pasos 4 y 5 salieron bien, repetir los dos applies:
   `changed=0` y el mismo ID de tarea en `edge_traefik` y `winnest_web`.
7. Registrar la evidencia en `docs/DEPLOYMENT_STATUS.md`, con el
   `Version.Index` y las dos Configs de `edge_traefik` tras el apply
   repetido.

## Verificación

- `https://winnest.apptolast.com/` y `/es/` responden 200 con la CSP de la
  web; `/no-existe/` responde 404 con la página en inglés y `/es/no-existe/`
  con la española; `http://` redirige a `https://`.
- El certificado es de Let's Encrypt producción y cubre el nombre.
- `docker service ls` muestra `winnest_web` en `1/1` y el contenedor en
  `healthy`.

## Límites conocidos

- Una réplica: un reinicio o una actualización dejan la web sin servicio
  unos segundos (`stop-first`); Traefik responde 503 mientras tanto.
- El digest de la imagen base de NGINX y el de Node se actualizan con
  Dependabot en el repositorio de WinNest; un cambio fusionado llega al
  canal `:latest` como cualquier otro.
- Hasta que el repositorio de WinNest tenga el secret `DOCKERHUB_TOKEN`
  (ver su `website/DEPLOY.md`), su CI construye y prueba la imagen pero no
  la publica, y el vigilante no ve versiones nuevas.
