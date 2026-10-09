# Oficina de agentes (`ax-web` 1.0.3)

Código de la Oficina de agentes de AppToLast, el panel que sirve
`https://ax.apptolast.com` (y `https://oficina.apptolast.com`) detrás de
Traefik. Este directorio solo contiene la aplicación, sus pruebas y la
definición de la imagen; el despliegue en el laboratorio (Deployment,
PVC, NodePort, NetworkPolicy, reenviador y secretos) está en
[`docs/AX_WEB.md`](../../docs/AX_WEB.md). `ax-server` sigue sin
publicarse nunca.

## Qué es

Un equipo de agentes (personas con un prompt de sistema, un CLI —Claude
Code 2.1.274 o Codex 0.156.1—, un modelo, un esfuerzo y un modo) que
trabaja sobre repositorios públicos de GitHub en el único sandbox de AX,
de uno en uno, con una cola persistente:

- **Agentes.** Nueve de partida (Ada, Linus, Grace, Kent, Hedy, Margaret,
  Guido con Codex, el becario Haiku y el Coach). Cada cambio de CLI,
  modelo, esfuerzo, modo o prompt crea una versión nueva; el historial
  guarda las 20 anteriores y se puede restaurar cualquiera.
- **Proyectos.** Los revisados de la configuración (`projects`) más los
  que se añadan a mano o se importen de los repositorios de una
  organización de GitHub. Cada uno tiene notas y una memoria de lecciones
  aprobadas que entra en los prompts. AX clona sin credenciales, así que
  solo los repositorios públicos pueden trabajar. Un proyecto creado a
  mano nunca lo sobrescribe uno de la configuración con el mismo
  identificador y otro repositorio (se avisa).
- **Trabajos.** Pregunta, plan, cambio o revisión, con prioridad, rama y
  ajustes por trabajo, escritos a mano o a partir de una issue o un PR
  (su título, su descripción y, de un PR, su diff llegan al agente como
  datos no confiables). Un cambio deja un parche y los ficheros
  completos; con un token de GitHub se convierte en un PR en borrador. Los
  trabajos se pueden reintentar, valorar, encadenar («Pedir a…») y
  comentar en una issue o un PR; antes de publicar se ve (y se puede
  editar) exactamente el título y el texto que irán a GitHub.
- **Equipos.** Plantillas de varios pasos: plan → código → revisión con
  iteraciones, panel de revisión (corrección, seguridad, tests y
  síntesis), debate de diseño, rojo/azul de seguridad, evaluación con
  juez y auto-mejora de la propia Oficina. Los papeles que cambian código
  exigen un agente en modo completo. Si un paso de cambio no deja cambios
  aplicables, el equipo termina con ese resultado («sin cambios
  aplicables», «parche truncado» o «no se pudieron recoger los cambios»)
  en lugar de revisar o juzgar un árbol sin tocar. Un equipo admitido en
  la cola recibe siempre sus pasos siguientes, aunque la cola se llene.
- **Turnos.** Trabajos o equipos programados por día de la semana y hora
  UTC. Cada hora prevista se dispara una sola vez (se guarda la última,
  `last_slot`), como mucho una hora tarde, y nunca por una hora que ya
  había pasado al crear, editar o activar el turno.
- **Evolución con humano en el bucle.** Las lecciones de cada trabajo se
  proponen (o se aprueban solas, o se ignoran, según los ajustes); el
  Coach propone prompts mejores a partir de las métricas, las revisiones y
  las valoraciones; nada cambia sin aprobación. Un banco de evaluaciones
  puntúa versiones y modelos.
- **Vista AX.** Las tareas de AX con Suspender, Reanudar y Borrar (exige
  escribir el nombre exacto), los workspaces y los gateways, solo lectura.

## Arquitectura

<!-- markdownlint-disable MD013 -->

| Paquete | Qué hace |
| --- | --- |
| `internal/office` | Estado (JSON en el PVC), cola y despachador, composición de prompts, lectura de resultados, equipos, turnos, evolución, métricas, credenciales y el broker de GitHub. Solo conoce `harness.Executor`. |
| `internal/runs` | La única forma de ejecutar en AX: una ejecución a la vez, con todas las salvaguardas de abajo. Implementa `harness.Executor`. |
| `internal/harness` | El contrato entre los dos: `Spec`, eventos, resultado, cambios, los argv fijos de Claude y Codex y sus intérpretes de salida. |
| `internal/web` | API JSON, un flujo SSE y la interfaz estática (`static/`). |
| `internal/config` | La configuración revisada, JSON estricto. |
| `internal/demo` | La demo local sin AX (ver abajo). |

<!-- markdownlint-enable MD013 -->

El estado vive en `state_dir` (`/var/lib/ax-web`, el único sitio con
escritura del pod): `office.json`, `jobs.json`, un directorio por trabajo
(encargo, prompt final, persona, resultado, eventos, parche y ficheros) y
`credentials/codex-auth.json`.

## Salvaguardas

- **Una sola ejecución a la vez.** Tampoco arranca si existe cualquier
  tarea `web-*` o `tarea-*` (las de `ax-tarea`) ni si se cruzaría con la
  ventana del Observatorio: el trabajo sigue en cola y dice por qué
  espera. Su prompt se prepara una sola vez mientras espera (no se
  reescribe en cada reintento) y se vuelve a preparar si cambian el
  agente, el proyecto o los ajustes. Sin la credencial de su CLI (Claude o
  Codex) el trabajo falla antes de crear nada en AX.
- **Argv fijos.** En el sandbox solo arrancan la comprobación del clon,
  el agente y, tras él, órdenes `git` fijas para recoger los cambios
  (`add -A` sin hooks, `diff --cached` sin programas externos, `archive`
  con rutas literales). El encargo va por stdin, nunca en `argv`.
- **Modos.** Lectura: Claude con `--restricted` (sin herramientas que
  ejecuten órdenes o código ni WebFetch, sin los ajustes del repositorio,
  ficheros confinados al directorio de trabajo) y `--strict-mcp-config`.
  Completo: Claude con `--setting-sources user` y
  `--permission-mode bypassPermissions` dentro de gVisor, que es la
  frontera. Codex no puede usar su propio sandbox bajo gVisor
  (`danger-full-access`), así que su modo lectura es solo una instrucción
  del prompt.
- **Ventana sin ejecuciones (opcional).** Si la configuración fija
  `blackout` (una ventana UTC diaria `HH:MM-HH:MM`), no arranca nada cuyo
  intervalo previsto la toque (el trabajo espera en la cola), cancela la
  ejecución activa con la antelación configurada y no reanuda tareas
  dentro de ella. Con `""` no hay ventana: el laboratorio no fija ninguna
  desde el 2026-09-28, por decisión del propietario (antes, 22:30-00:40,
  la del Observatorio; ver `docs/AX.md`).
- **Salida en directo.** El Envoy de atenet-router corta cada flujo a los
  10 s si no se le da otro `--route-timeout`; tras cada corte se pregunta
  al sandbox con `GetProcess` y se reanuda la salida. Solo se mata al
  agente si el sandbox deja de responder seis veces seguidas.
- **Credenciales.** El token de Claude (volumen del Secret `ax-web-agent`)
  y el `auth.json` de Codex (Secret opcional `ax-web-office` o la copia
  renovada guardada) se leen al arrancar cada agente y viajan solo en
  `StartProcess.env`: nunca en `Task.spec.env`, en el navegador, en los
  logs ni en los ficheros de los trabajos. El `auth.json` que Codex
  renueva se relee tras cada ejecución y solo se guarda si es el mismo
  objeto JSON (sin claves duplicadas, las mismas claves, la misma cuenta
  y los mismos campos), con un `refresh_token` nuevo y un `last_refresh`
  en UTC posterior al actual y coherente con la hora de la tarea; además,
  `id_token`, `access_token` y `refresh_token` deben ser textos de como
  mucho 16 KiB con solo `[A-Za-z0-9._-]`, y los JWT `id_token` y
  `access_token` (leídos sin verificar, solo para comparar) deben nombrar
  el mismo `sub` y la misma cuenta de ChatGPT que la copia actual. Si no,
  se descarta y se avisa sin mostrar valores. La copia guardada se puede
  olvidar (`POST /api/credentials/codex/forget`): desde la siguiente
  ejecución se vuelve a usar la del Secret. Suspender está desactivado en
  tareas que llevan una credencial.
- **GitHub fuera del sandbox.** El token de GitHub (por propietario, en
  `ax-web-office`) lo usa solo la Oficina, por HTTPS a `api.github.com`,
  se lee en cada uso y nunca entra en un sandbox. Los PR se construyen con
  la API de Git (blobs, árbol, commit, rama `oficina/<trabajo>`) sobre el
  commit base del trabajo y se abren siempre como borrador; si el PR de
  esa rama ya existía (un intento anterior), se registra ese. La Oficina
  nunca publica cambios en `.github/` (CI) ni en `.gitmodules`: ese PR se
  rechaza con el motivo y esos cambios se aplican a mano tras revisarlos.
  Por lo mismo, el token **no debe tener el permiso Workflows** (en un
  token de grano fino, «Workflows: sin acceso»; en uno clásico, sin el
  ámbito `workflow`): basta con Contents y Pull requests de lectura y
  escritura e Issues para comentar. Antes de publicar,
  `GET /api/jobs/{id}/github-preview` enseña el título y el texto exactos;
  las menciones `@nombre` del texto (también del editado) se publican
  como código para no avisar a nadie, los títulos tienen como mucho 200
  caracteres y los textos 60 000 bytes. Las respuestas están acotadas
  (2 MiB; diff de un PR, 300 KiB) y nunca se registran.
- **Datos no confiables.** Issues y PR (título, descripción y diff)
  entran en el prompt dentro de un bloque `<datos_externos>` marcado como
  datos, nunca como instrucciones; cualquier variante de la etiqueta
  dentro de ellos (mayúsculas, espacios, apertura o cierre) se
  neutraliza. Sin token de GitHub para el propietario, el trabajo se
  ejecuta igual con una nota en su lugar. Cada sección del prompt está
  acotada.
- **Lo que ve el navegador.** JSON y la interfaz estática, nada más. De
  AX, una lista blanca de campos: las variables de entorno aparecen como
  `[oculto]` y las URL de los repositorios sin usuario, consulta ni
  fragmento. Las credenciales solo como «presente o no».
- **CSRF.** Todo POST exige el mismo origen (`Origin` igual a `origin` o
  a uno de `extra_origins`, o `Sec-Fetch-Site: same-origin`),
  `Content-Type: application/json` y la cabecera `X-AX-Web: 1`. Nunca se
  envían cabeceras CORS. Los cuerpos están limitados a 128 KiB.
- **CSP estricta.** `default-src 'self'`, sin código ni estilos en línea.
  La interfaz se sirve como un solo CSS y un solo JS con el sha256 en el
  nombre y caché inmutable; `index.html` nunca se cachea.
- **La salida de los agentes es un dato hostil.** El Markdown y los diffs
  se analizan en tiempo lineal (256 KiB de Markdown en menos de 1 s, 4 MiB
  de diff en menos de 2 s); un texto de más de 64 KiB se muestra sin
  formato hasta pulsar «Renderizar igualmente». Los caracteres invisibles
  o de control (bidi, ancho cero, etiquetas) se ven como `⟦U+202E⟧` en
  resultados, diffs, lecciones, memoria y prompts, y se eliminan de
  resúmenes, lecciones y prompts del Coach antes de proponerlos. Los
  enlaces no admiten credenciales, llevan su dominio como título y, si el
  texto aparenta otra dirección, muestran el dominio real.
- **mTLS.** El puerto de la aplicación solo acepta TLS 1.3 con el
  certificado cliente de Traefik (CN `edge-traefik`, uso `clientAuth`),
  emitido por una CA privada. El puerto de sondas no sirve la aplicación y
  acota cada conexión (lectura 10 s, inactividad 30 s, cabeceras 4 KiB).
- **Reenviador (`ax-web forward`).** Tubería TCP de la red de Traefik al
  NodePort del panel, sin terminar TLS. `--allow-cidr` es obligatorio y
  nombra las redes que pueden conectar: cualquier otro par se cierra antes
  de ocupar una de las 64 plazas.
- **Sin root.** La imagen se ejecuta como el uid 65532 (`nonroot` de
  distroless) salvo que el despliegue diga otra cosa.
- **Limpieza.** Tras cualquier final (salida, cancelación, tiempo agotado,
  error o parada del panel) borra la tarea y el workspace y espera a que
  desaparezcan; si no, lo avisa y reintenta. Al arrancar retira las
  `web-*` que queden.
- **Estado a prueba de cortes.** Cada escritura es atómica (fichero
  temporal en el mismo directorio, fsync, rename, fsync del directorio) y
  la versión anterior de `office.json` y `jobs.json` queda como `.bak`.
  Solo cuenta como dañado un fichero que se leyó entero y no es el JSON
  esperado: entonces se usa la copia, y si las dos lo están se apartan
  como `*.corrupt-<unix>` y se empieza de cero con un aviso. Cualquier
  otro fallo al leerlos (permisos, E/S, más de 32 MiB) o un esquema
  desconocido detiene el arranque sin renombrar nada. Nunca se escribe un
  `office.json` o `jobs.json` de más de 32 MiB (de `jobs.json` se borran
  antes los trabajos terminados más antiguos). Un trabajo que estaba en
  marcha al reiniciarse queda «Interrumpido por un reinicio del panel» y
  su equipo, fallido. Un fichero de un trabajo que no se pudo guardar se
  avisa en el trabajo y en los avisos de la Oficina.
- **Retención.** Se guardan como mucho `retention_jobs` trabajos y, además,
  los directorios de los trabajos (`jobs/<id>/`) no pasan de 768 MiB
  (`MaxJobsBytes`, en un volumen de 1 GiB): por encima se borran los
  trabajos terminados más antiguos que nada necesita. Cuando un trabajo ya
  tiene su PR, se borra la copia de sus ficheros (`contents.json`).
- **Memoria acotada.** Cada evento de un agente se recorta (texto 4 KiB,
  entrada de herramienta 600 bytes, modelo y herramienta 128 bytes); el
  coste y los contadores que declara se acotan (coste finito de 0 a
  10 000 USD, contadores de 0 a 2^40); si la lista de cambios de una
  ejecución pasa de 4000 ficheros (o de 1 MiB) o tiene una ruta de más de
  4096 bytes, la captura falla con «demasiados cambios». Si `GOMEMLIMIT` no está
  fijado, `serve` lo pone al 80 % del `memory.max` del cgroup. Una
  respuesta que no se puede serializar es un 500 con su error, nunca un
  200 vacío.
- **Auditoría.** Cada acción deja una línea JSON en stdout con la IP del
  cliente (la última de `X-Forwarded-For`, la que añade Traefik) y, de
  cada encargo, solo su longitud y su sha256.

## API

Todo es JSON en español; los errores son `{"error": "…"}` y, si señalan un
campo, `{"error": "…", "field": "…"}` con 400.

<!-- markdownlint-disable MD013 -->

| Ruta | Qué hace |
| --- | --- |
| `GET /api/office` | Instantánea completa (agentes, proyectos, trabajos, equipos, plantillas, turnos, propuestas, evaluaciones, métricas, catálogo de modelos, estado de AX y de las credenciales, límites, avisos) |
| `GET /api/stream[?job=<id>]` | SSE: `hello`, deltas `office` (como mucho uno por segundo) y, con `job`, sus eventos `log` (los 1500 últimos y los nuevos); `: ping` cada 15 s |
| `POST /api/agents`, `/api/agents/{id}`, `…/delete`, `…/coach`, `…/rollback` | Agentes, versiones y Coach |
| `POST /api/projects`, `/api/projects/{id}`, `…/delete`, `…/memory`; `GET …/github` | Proyectos, memoria e issues y PR abiertos |
| `POST /api/jobs`; `GET /api/jobs/{id}`, `…/events?after=`, `…/patch[?download=1]` | Trabajos y su detalle; `source: {"type": "issue"\|"pr", "number": N}` lleva la issue o el PR al prompt como datos |
| `POST /api/jobs/{id}/cancel`, `retry`, `delete`, `rate`, `priority`, `followup` | Acciones sobre un trabajo (un reintento que aplicaba los cambios de otro trabajo exige que sigan existiendo y sean aplicables: si no, 409) |
| `GET /api/jobs/{id}/github-preview?target=pr\|comment` | `{"title", "body"}` exactos que se publicarían (409 si no procede) |
| `POST /api/jobs/{id}/pr` `{"title"?, "body"?}`; `POST /api/jobs/{id}/comment` `{"number", "body"?}` | PR en borrador (devuelve el trabajo con `pr`) o comentario (`{"url"}`); vacíos, los textos generados |
| `POST /api/pipelines`; `GET /api/pipelines/{id}`; `POST …/cancel` | Equipos |
| `POST /api/schedules`, `…/{id}/delete`, `…/{id}/run` | Turnos |
| `POST /api/proposals/{id}/approve`, `…/reject` | Propuestas |
| `POST /api/evals`, `…/{id}/delete`, `/api/evals/run` | Banco de evaluaciones |
| `POST /api/settings`, `/api/queue/pause` | Ajustes y pausa de la cola |
| `POST /api/credentials/codex/forget` | Borra la copia renovada de Codex (`{"ok": true}`) |
| `GET /api/github/repos?owner=<propietario>` | `{"owner", "authenticated", "repos": [{"name", "full_name", "html_url", "description", "default_branch", "private", "archived", "fork", "pushed_at", "language"}]}`: hasta 300 repositorios de una organización (o de un usuario), con el token del propietario si lo hay y si no los públicos; caché de 5 minutos. Se importan con `POST /api/projects` |
| `GET /api/export` | Descarga de `office.json` y `jobs.json` (sin credenciales) |
| `GET /api/status`, `/api/tasks…`, `/api/gateways`, `/api/workspaces` | Vista AX |

<!-- markdownlint-enable MD013 -->

## Demo local

Sirve la misma Oficina y la misma API sobre un ejecutor simulado y un AX
en memoria, sin sandbox, modelos ni GitHub, solo en loopback y por HTTP:

```bash
cd images/ax-web
go run . demo --listen 127.0.0.1:8090 --state ./demo-state --speed 3
```

y abre `http://127.0.0.1:8090/`. Cada ejecución simulada dura unos 15 s
divididos por `--speed`; los cambios dejan un parche en `README.md`, las
revisiones alternan los veredictos, el juez puntúa y el Coach propone. Las
credenciales son ficticias (Claude y Codex presentes, GitHub no); un
GitHub falso en loopback solo lista repositorios inventados para probar la
importación de proyectos. Con un estado vacío pone en cola unos trabajos
de ejemplo (`--samples=false` lo evita). Rechaza cualquier dirección que
no sea loopback.

## Pruebas e imagen

Las pruebas usan un AX y un sandbox falsos en memoria (bufconn) y un
GitHub falso (`httptest`):

```bash
cd images/ax-web
go vet ./...
go test -race ./...
```

La imagen se construye con ko v0.19.1 sobre la base distroless que fija
Substrate, sin cgo ni sello VCS y con `--image-user=65532`:

```bash
cd images/ax-web
KO_DOCKER_REPO=localhost:5001/ax-web ko build --bare --push=false \
  --sbom=none --image-user=65532 --oci-layout-path=<directorio> .
```

`.github/workflows/ax-web.yml` pasa las pruebas y `govulncheck`, la
construye dos veces (la segunda con la caché vacía), exige el mismo digest
y guarda el layout OCI como artefacto. El digest está fijado en
`config/ax-lab.yml` (`web.image.digest`) y el workflow falla si el digest
construido no coincide con el fijado (ver `docs/AX_WEB.md`, «Imagen»).

## Pendiente de comprobar en la ventana del laboratorio

- Que Codex 0.156.1 renueva y escribe `/root/.codex/auth.json` como se
  espera tras una ejecución larga, y que la copia guardada se usa en la
  siguiente.
- Que el Envoy de atenet-router no acumula flujos largos y que su
  `--route-timeout` cubre el tiempo máximo de una ejecución.
- Que el egress a `api.github.com` funciona desde el pod con la
  NetworkPolicy nueva y que un PR en borrador se crea con el token
  configurado.
