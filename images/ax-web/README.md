# Panel web de AX (`ax-web`)

Código del panel que servirá `https://ax.apptolast.com` detrás de Traefik,
por decisión del propietario del 2026-09-26. Este directorio solo contiene
la aplicación, sus pruebas y la definición de la imagen. No despliega nada:
el despliegue en el laboratorio (Deployment, NodePort, NetworkPolicy,
reenviador y secretos) y la ruta de Traefik llegan en cambios posteriores.
`ax-server` sigue sin publicarse nunca.

## Qué hace

- Lista las tareas de AX y muestra el detalle de una, con Suspender,
  Reanudar y Borrar. Borrar exige escribir el nombre exacto de la tarea y,
  como `ax-tarea`, sigue borrando hasta que AX ya no la tiene.
- Lanza una ejecución: un repositorio público `https://github.com/…`, una
  rama, una instrucción, solo Claude, turnos máximos (1-50), tiempo máximo
  (5-45 min) y CPU y memoria. AX no aplica estas dos últimas
  (google/ax#369): el límite real es el del worker y el tiempo máximo.
- Muestra la salida en directo (SSE) con un botón Cancelar (SIGTERM y, a
  los 10 s, SIGKILL).
- Lista gateways y workspaces, solo lectura.

No hay terminal ni órdenes libres. En el sandbox solo arranca dos argv
fijos: la comprobación del clon
(`git -C /workspace/repo rev-parse --verify HEAD`, sin credencial) y
`ax-agent claude -p --restricted --strict-mcp-config --output-format
stream-json --verbose --max-turns N`. `--restricted` quita a Claude las
herramientas que ejecutan órdenes o código y WebFetch, ignora los ajustes
del repositorio y limita los ficheros al directorio de trabajo: un
repositorio público no puede ejecutar nada junto al token. El resultado es
solo texto; la tarea y su workspace se borran al terminar.

## Salvaguardas

- **Una sola ejecución a la vez.** Tampoco arranca si existe cualquier tarea
  `web-*` o `tarea-*` (las de `ax-tarea`).
- **Ventana del Observatorio.** Rechaza una ejecución cuyo intervalo
  `[ahora, ahora + 3 min + tiempo máximo + max(2 min, antelación)]` toque
  22:30-00:40 UTC, cancela la activa con esa antelación (5 minutos) y no
  reanuda tareas dentro de ella. Así el vigilante nunca corta una ejecución
  que se aceptó.
- **Salida en directo.** El Envoy de atenet-router corta cada flujo a los
  10 s si no se le da otro `--route-timeout`. Tras cada corte el panel
  pregunta al sandbox con `GetProcess` y, mientras el agente sigue en
  marcha, reanuda la salida desde lo ya leído. Solo mata al agente si el
  sandbox deja de responder seis veces seguidas.
- **Credencial.** El token de Claude se lee del volumen del Secret solo al
  arrancar el agente y viaja únicamente en `StartProcess.env`. Nunca entra
  en `Task.spec.env`, ni en el navegador, ni en los logs. Suspender está
  desactivado en tareas que llevan una credencial.
- **Lo que ve el navegador.** Una lista blanca de campos, nunca el objeto
  de AX: las variables de entorno aparecen como `[oculto]` y las URL de los
  repositorios sin usuario, consulta ni fragmento.
- **CSRF.** Todo POST exige el mismo origen (`Origin` o `Sec-Fetch-Site`),
  `Content-Type: application/json` y la cabecera `X-AX-Web: 1`. Nunca se
  envían cabeceras CORS.
- **mTLS.** El puerto de la aplicación solo acepta TLS 1.3 con el
  certificado cliente de Traefik (CN `edge-traefik`, uso `clientAuth`),
  emitido por una CA privada. El puerto de sondas no sirve la aplicación y
  acota cada conexión (lectura 10 s, inactividad 30 s, cabeceras 4 KiB).
- **Reenviador (`ax-web forward`).** Tubería TCP de la red de Traefik al
  NodePort del panel, sin terminar TLS. `--allow-cidr` es obligatorio y
  nombra las redes que pueden conectar (la de Traefik): cualquier otro par,
  como un sandbox en el puente `kind`, se cierra antes de ocupar una de las
  64 plazas. Una conexión inactiva se cierra a los 5 min y, cuando el panel
  cierra la suya, el cliente tiene 30 s para terminar antes de perder la
  plaza.
- **Sin root.** La imagen se ejecuta como el uid 65532 (`nonroot` de
  distroless) salvo que el despliegue diga otra cosa.
- **Limpieza.** Tras cualquier final (salida, cancelación, tiempo agotado,
  error o parada del panel) borra la tarea y el workspace y espera a que
  desaparezcan; si no, lo avisa y reintenta cada 30 s. Al arrancar retira
  las `web-*` que queden.
- **Auditoría.** Cada acción deja una línea JSON en stdout con la IP del
  cliente, y de la instrucción solo su longitud y su sha256.

## Pruebas e imagen

Las pruebas usan un AX y un sandbox falsos en memoria (bufconn):

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
y guarda el layout OCI como artefacto. El digest se fijará en
`config/ax-lab.yml` con el despliegue, y entonces el workflow también
fallará si el digest construido no coincide con el fijado.

## Pendiente de comprobar en la ventana del laboratorio

- Que `claude -p --restricted` lee la instrucción por stdin y emite
  `stream-json`; si no, `prompt_mode: argument` la pasa tras `--`.
- Que el Envoy de atenet-router no acumula flujos largos.
- Que atenet-router corre con `--route-timeout` de al menos el tiempo
  máximo de una ejecución más margen, como el laboratorio manual (`1h`). El
  laboratorio codificado no lo fija todavía (`ate-setup` deja los 10 s por
  defecto): es requisito del despliegue. El panel tolera los cortes, pero
  cada uno cuesta una reconexión.
