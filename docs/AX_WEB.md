# Panel web de AX: `ax.apptolast.com`

El panel pequeño y propio de `images/ax-web` publicado en
`https://ax.apptolast.com`, por decisión del propietario del 2026-09-25: se
publica el panel y nada más del laboratorio. `ax-server`, que no tiene
autenticación (google/ax#376), sigue sin publicarse nunca. Sin Cloudflare
Access y sin lista de IP permitidas, también por decisión del propietario.

Desde la versión 1.0.0 el panel es la Oficina de agentes: un equipo de
agentes que reciben encargos sobre repositorios públicos de AppToLast y los
ejecutan de uno en uno en el sandbox de AX. Qué hace y cómo se usa está en
[OFICINA.md](OFICINA.md).

Este documento cubre el despliegue en el laboratorio (playbook `ax-lab`):
el panel en Kubernetes, su reenviador TCP, su volumen de estado, sus
credenciales, su imagen y su verificación. La ruta de Traefik, el
`basicAuth`, los límites de peticiones y los registros DNS son del playbook
`edge` (ver [EDGE.md](EDGE.md)). La aplicación, sus salvaguardas y sus
pruebas están en [`images/ax-web/README.md`](../images/ax-web/README.md). El
laboratorio en general está en [AX.md](AX.md).

## Camino de una petición

```text
navegador --HTTPS--> edge_traefik (basicAuth, límites; playbook edge)
  --TLS 1.3 con certificado cliente (mTLS), red apptolast-edge-ax-->
ax-web-edge (contenedor suelto en apptolast-edge-ax y en kind; tubería TCP)
  --TCP--> kind-control-plane:30843 (NodePort; solo en el puente kind)
  --> Service ax-web (ns ax-web, externalTrafficPolicy Local) --> pod :8443
  --gRPC--> ax-server.ax-system:8080 y atenet-router.ate-system:80
  --HTTPS--> api.github.com (solo la Oficina, con el token de GitHub)
estado: PVC ax-web-state montado en /var/lib/ax-web
```

- Traefik solo alcanza redes overlay de Swarm y `kind` es un puente local:
  el único contenedor en las dos es el reenviador. No termina TLS.
- El reenviador solo admite pares de la subred de `apptolast-edge-ax`
  (`--allow-cidr`): un sandbox del puente `kind` no ocupa sus plazas.
- El NodePort no se publica en el host: los puertos del nodo de kind solo
  existen en el puente `kind`. Un pod o un sandbox sí puede llegar a él (la
  NetworkPolicy no es fiable ante un NodePort), así que el panel exige el
  certificado cliente de Traefik: CN `edge-traefik`, uso `clientAuth`,
  emitido por la CA privada del arranque.
- El puerto de sondas, 8081 en HTTP plano, no está en el Service ni en la
  NetworkPolicy: las sondas del kubelet salen del propio nodo, y una
  NetworkPolicy siempre admite al nodo del pod («the only allowed
  connections into the pod are those from the pod's node and those allowed
  by the `ingress` list», kubernetes.io, Network Policies). kindnet lo
  cumple aceptando el tráfico que el nodo genera como root antes de mirar
  las políticas, no por la regla de entrada: el kubelet sale con la
  dirección `10.244.0.1`, de la red de pods.
- La NetworkPolicy del panel solo admite el 8443, y solo desde fuera de la
  red de pods (`10.244.0.0/16`, la de kind por defecto). Por el NodePort
  eso es el reenviador y cualquier otro par del puente `kind` que no sea un
  pod: el host, lo que usa su red (como `ate-setup`) y `kind-registry`. A
  todos ellos los rechaza el mTLS.
- De salida, la misma NetworkPolicy solo deja DNS, `ax-server`, el router y
  HTTPS (TCP 443) a direcciones fuera de `10.0.0.0/8`, `172.16.0.0/12`,
  `192.168.0.0/16`, `100.64.0.0/10`, `169.254.0.0/16` y `127.0.0.0/8`: la API
  de GitHub para la Oficina, y nunca las redes de pods, servicios, nodo y
  Docker de este host, que están todas en esos rangos. La IP pública del
  host no está excluida.
- El panel no tiene token de la API de Kubernetes ni permisos RBAC.

## Contrato

<!-- markdownlint-disable MD013 -->

| Fichero | Qué fija |
| --- | --- |
| `config/ax-lab.yml`, `web` | Imagen por digest, NodePort 30843, puertos, origen y orígenes extra, nombres TLS, directorio TLS, repositorios, ventana, límites de ejecución, la Oficina (volumen, cola, retención y proyectos) y el reenviador con su red, su subred y sus límites |
| `config/capacity-profiles.yml` | `host_containers.ax-lab.ax-web-edge`: 10m/16 MiB reservados, 250m/32 MiB de límite, 64 PIDs |
| `scripts/validate-ax-lab.py` | Valida `web`, lo cruza con la capacidad y con los servicios y catálogos del repositorio, renderiza el manifiesto y lo comprueba; `--web-manifest-sha256` imprime su sha256 |
| `ansible/roles/ax_lab/templates/ax-web.yaml.j2` | Namespace, ServiceAccount, ConfigMap, PersistentVolumeClaim, Deployment, Service NodePort y dos NetworkPolicies |
| `ansible/roles/ax_lab/tasks/web_read.yml`, `web.yml` | Lectura y prueba de propiedad (ambos modos) y despliegue (solo apply) |
| `scripts/manage-ax-lab-substrate.py seed-layout` | Copia la imagen fijada del layout OCI de la CI a la copia de seguridad |
| `scripts/ax-web-bootstrap.sh` | Arranque del propietario: CA, certificados, Docker Secrets, Secrets de Kubernetes y el Secret opcional de la Oficina |
| `.github/workflows/ax-web.yml` | Compila la imagen dos veces, exige el mismo digest y el fijado, y guarda el layout OCI |

<!-- markdownlint-enable MD013 -->

## La red de Traefik y su subred

El playbook `edge` crea `apptolast-edge-ax`, overlay cifrada y `attachable`;
este rol nunca la crea, solo la inspecciona. Su subred es fija y revisada,
`10.0.250.0/24` (`web.forwarder.edge_subnet`), porque el reenviador la pasa
como `--allow-cidr`. Está fuera de todas las redes del host el 2026-09-26
(overlays de Swarm en `10.0.0.0/24` a `10.0.29.0/24` y puentes en
`172.17.0.0/16` a `172.23.0.0/16`) y de las subredes de pods y servicios de
kind. El rol se detiene si la red falta, no es una overlay `swarm`
`attachable` o tiene otra subred. El playbook `edge` tiene que crearla con
exactamente esa subred:

```bash
docker network create --driver overlay --opt encrypted --attachable \
  --subnet 10.0.250.0/24 apptolast-edge-ax
```

## Imagen

`.github/workflows/ax-web.yml` compila `images/ax-web` con ko v0.19.1 dos
veces (la segunda con la caché vacía), exige el mismo digest y además el que
fija `web.image.digest`:
`sha256:ab1b4f959b53f7495f37f130b73b867b3f793d11f02fb2460202e9676f2b66ef`.
Solo entonces guarda el layout OCI como artefacto `ax-web-oci-layout`. El
host nunca compila la imagen. El propietario la siembra en la copia de
seguridad del laboratorio, desde el artefacto descomprimido en un
directorio de root (el gestor no lee rutas de otros usuarios), una vez por
cada `web.image.tag` y digest nuevos y siempre antes del apply que los usa:
sin ella, el apply se detiene en el panel y retiene el marker. Cada imagen
nueva lleva otro tag (la copia nunca da dos digests a un mismo
`ax-web:<tag>`), así que la anterior sigue en la copia para volver atrás:

```bash
sudo install -d -o root -g root -m 0700 /run/ax-web-seed
sudo -- /usr/bin/python3 -m zipfile -e ax-web-oci-layout.zip /run/ax-web-seed
sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py seed-layout \
  --image-set web --source /run/ax-web-seed \
  --layout /var/backups/dockerswarm/ax-lab/images --tag 1.0.0 \
  --image=ax-web=sha256:ab1b4f959b53f7495f37f130b73b867b3f793d11f02fb2460202e9676f2b66ef
sudo rm -rf /run/ax-web-seed
```

`seed-layout` toma el lock host-global, exige que el `index.json` del
artefacto liste el digest fijado, que el manifiesto dé ese digest y que cada
blob dé el suyo mientras se copia, y no copia nada más. El apply restaura la
imagen de la copia al registro como las de AX. El host la descarga del
registro de loopback por digest para el reenviador (Docker trata
`127.0.0.0/8` como registro inseguro por defecto).

## Arranque

Lo ejecuta el propietario en su propia sesión SSH, desde un checkout del
commit revisado y con el lock host-global, que el script toma solo.
Ninguna clave ni ningún token pasa por la salida, los logs ni `argv`, y
nada existente se sobrescribe nunca.

```bash
sudo -- ./scripts/ax-web-bootstrap.sh init
```

`init` crea, en un directorio `tmpfs` bajo `/run`, una CA privada ECDSA
P-256 y dos hojas de 3 años firmadas por ella: la del panel (SAN
`DNS:ax-web`, `serverAuth`) y la de Traefik (CN `edge-traefik`,
`clientAuth`). Borra la clave de la CA en cuanto firma las dos. Crea los
Docker Secrets que monta Traefik, con las etiquetas
`com.apptolast.managed-by=manual-bootstrap` y
`com.apptolast.purpose=traefik-upstream-mtls`: `edge-ax-upstream-client-v1`
(certificado y clave del cliente en un PEM) y `edge-ax-upstream-ca-v1` (el
certificado de la CA). Guarda el certificado, la clave del panel y el
certificado de la CA en `/etc/dockerswarm/ax/web-tls` (`root:root 0700`,
ficheros `0600`) para restaurar el Secret de Kubernetes tras recrear el
clúster sin tocar Traefik. Imprime las fechas de caducidad, que se anotan
en [DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md).

```bash
sudo -- ./scripts/ax-web-bootstrap.sh k8s
```

`k8s` crea en el espacio de nombres `ax-web`, que crea el playbook, los
Secrets `ax-web-tls` (`tls.crt`, `tls.key`, `client-ca.crt`) y
`ax-web-agent` (`claude-oauth-token`) con
`kubectl create secret generic --from-file` desde los ficheros de root. El
token de Claude sigue en `/etc/dockerswarm/ax/claude-oauth-token` y solo
llega a Kubernetes así. El panel monta el Secret como volumen de solo
lectura y lo lee al arrancar cada ejecución. En el clúster también lo
puede leer quien lea Secrets en todo el clúster, hoy `ate-controller` de
Substrate (ver [AX.md](AX.md), «Credenciales»).

```bash
sudo -- ./scripts/ax-web-bootstrap.sh office
```

`office` crea el Secret opcional `ax-web-office` de la Oficina con
`kubectl create secret generic --from-file`, solo con los ficheros que
existan, y al menos uno:

- `/etc/dockerswarm/ax/codex/auth.json` (`root:root 0600`, en `codex/`
  `0700`), la sesión de Codex, como `codex-auth-json`;
- `/etc/dockerswarm/ax/github-token` (`root:root 0600`), como
  `github-token`: un token para todas las organizaciones o líneas
  `organizacion=token` (`*` es la de por defecto y `#` empieza un
  comentario).

Igual que `k8s`, nunca imprime un valor ni lo pasa en `argv`, toma el lock
host-global y exige el espacio de nombres del playbook. Si el Secret ya
existe se detiene, salvo con `office --replace`, que lo borra y lo crea de
nuevo: entre las dos órdenes, la Oficina no tiene ni Codex ni GitHub. El
panel no se reinicia: el kubelet actualiza el volumen del Secret por su
cuenta, y la Oficina lee el token de GitHub en cada uso y la sesión de
Codex en cada ejecución. Sin el Secret, la Oficina funciona solo con
Claude y sin GitHub.

## Oficina

`web.office` de `config/ax-lab.yml` y lo que el manifiesto añade para la
Oficina:

- **Estado.** El PersistentVolumeClaim `ax-web-state`, de `storage_mib`
  (1 024 MiB), en la StorageClass `standard` de kind (`local-path`, como
  `ax-redis-data`), montado en `/var/lib/ax-web`: la única ruta escribible
  del pod. Guarda los trabajos con sus registros y parches, los agentes, los
  proyectos, su memoria y la sesión de Codex que la Oficina renueva.
  `local-path` no limita el tamaño y el volumen vive en el nodo: se pierde
  al recrear el clúster o al borrar el espacio de nombres, así que antes se
  descarga una copia sin credenciales con «Ajustes → Exportar».
  `storage_mib` se trata como fijo: cambiarlo después pide ampliar el PVC,
  que Kubernetes rechaza si la StorageClass no lo admite, y en kind no se ha
  comprobado.
- **Cola y retención.** Como mucho `max_queue` (200) trabajos en cola; por
  encima de `retention_jobs` (3 000), la Oficina borra los terminados más
  antiguos.
- **Límites de una ejecución.** `max_turns` 150 y `max_timeout_minutes` 90;
  el panel admite de 1 a 500 y de 5 a 180.
- **Orígenes.** Además de `origin`, el panel acepta POST con el origen de
  `extra_origins`, `https://oficina.apptolast.com`. Su ruta de Traefik y su
  registro DNS son del playbook `edge` (ver [EDGE.md](EDGE.md)).
- **Configuración.** El ConfigMap añade `extra_origins`, `state_dir`,
  `office_secret_dir` (`/var/run/ax-web/office`, donde se monta el Secret
  opcional), `codex_auth_key` (`codex-auth-json`), `github_token_key`
  (`github-token`), `max_queue`, `retention_jobs` y `projects`. Se renderiza
  entero con `to_json`, así que ningún nombre ni descripción puede salirse de
  su cadena JSON.
- **Recursos.** El pod pide 50m y 96 MiB y tiene 500m y 384 MiB de límite
  (ver «Capacidad»).

### Proyectos

`projects` son los repositorios que la Oficina siembra; en su interfaz se
pueden archivar, no borrar, y se añaden otros a mano. El validador exige que
cada uno sea un `https://github.com/<propietario>/<repositorio>` sin `.git`,
con una rama válida, un `id` único (`^[a-z][a-z0-9-]{1,31}$`), un nombre de
hasta 60 bytes y una descripción de hasta 300, cada uno en una línea, y que
`dockerswarm-infra` sea este repositorio en `main`: la Oficina se mejora a sí
misma a través de él. `service` y `url` solo se rellenan si este repositorio
prueba el vínculo: `service` tiene que ser un servicio de Swarm de
`config/image-channels.yml` y `url`, `https://` más un nombre que publique la
entrada de catálogo de su imagen (`config/services.yml` o el catálogo de su
stack), y solo para su componente de entrada (`app` o `web`), nunca para una
base de datos. Cada repositorio es público y su rama la de por defecto, según
`gh repo view` el 2026-10-02 (AX clona sin credenciales).

<!-- markdownlint-disable MD013 -->

| `id` | Repositorio (rama) | Servicio y URL | Prueba del vínculo |
| --- | --- | --- | --- |
| `dockerswarm-infra` | `apptolast/DockerSwarmInfrastrcture` (`main`) | — | Este repositorio; la Oficina vive en `images/ax-web` |
| `dockerswarm-docs` | `apptolast/DockerSwarmDocs` (`main`) | — | Su README: documentación de esta infraestructura (se publica en GitHub Pages, que nada de aquí prueba) |
| `dockerswarm-memoria` | `apptolast/DockerSwarmMemoria` (`main`) | — | Su README: el bot que lee este repositorio y propone documentos a DockerSwarmDocs |
| `organizationweb` | `apptolast/OrganizacionWeb` (`main`) | `organizationweb_web`, `https://organizacion.apptolast.com` | `release` de `config/organizationweb.yml` es un commit suyo y su workflow publica las dos imágenes |
| `kropia` | `apptolast/KropiaWeb` (`main`) | `workloads_kropia`, `https://kropia.apptolast.com` | Su workflow publica `docker.io/apptolast/kropia-web`, y la imagen fijada en `config/services.yml` lo declara en `org.opencontainers.image.source` |
| `shlink` | `apptolast/shlink-apptolast` (`develop`) | `workloads_shlink`, `https://generadorcodigosqr.apptolast.com` | La imagen fijada `docker.io/apptolast/shlink-apptolast` lo declara en `org.opencontainers.image.source` |

<!-- markdownlint-enable MD013 -->

No están, por no cumplir esas reglas: los repositorios privados (entre
ellos `MigracionNetCup`), los servicios cuyas imágenes no salen de un
repositorio de `apptolast` (los dos portfolios, `minecraft-stats` y
`racinggame`), los de los servicios que la migración denegó
(`denied_services` de `config/services.yml`) y `TemplateSSDUncleBob`,
porque [adopcion-templatessd.md](adopcion-templatessd.md) adopta la
plantilla de Cénit Digital y nada prueba que sea esta copia.

### Credenciales de la Oficina

- **Claude**: el token de `ax-web-agent`, como antes.
- **Codex**: la sesión de `codex-auth-json` es solo la semilla. Su refresh
  token es de un solo uso: cuando Codex la renueva en un sandbox, la
  Oficina guarda la nueva en su volumen (`credentials/codex-auth.json`,
  `0600`) y en cada ejecución usa la más reciente de las dos por
  `last_refresh`. Desde entonces la del host deja de valer para
  `ax-tarea … codex`, y al revés: tras usar Codex con `ax-tarea`, la
  Oficina necesita la sesión nueva con `office --replace`. Del panel al host
  no hay vía codificada. Con una sola cuenta, mejor usar Codex desde uno de
  los dos.
- **GitHub**: el token nunca entra en un sandbox. Solo lo usa el panel,
  contra `api.github.com`, para listar issues y PR, leer el diff de una PR,
  crear una rama `oficina/<trabajo>`, su commit y una PR en borrador, y
  comentar. Un token de grano fino con «Contents», «Pull requests» e
  «Issues» de lectura y escritura sobre los repositorios de los proyectos
  cubre todo eso.

## Propiedad

- El espacio de nombres `ax-web` solo lo crea el rol, que anota su UID y el
  ID del nodo en `/opt/dockerswarm/ax-lab/state/web.json` (`root 0600`),
  como `ax.json` para AX. Uno sin esa prueba, o recreado por otra mano, no
  se adopta nunca: se borra en una ventana exclusiva.
- El reenviador lleva `com.apptolast.managed-by=ansible` y
  `com.apptolast.ax-lab=web-edge`. Un contenedor `ax-web-edge` sin ellas no
  se toca nunca: se borra a mano con el lock.
- Los Secrets son del propietario. El rol solo lee su nombre, su tipo y su
  número de claves con la tabla del servidor de `kubectl get`, que no trae
  ningún valor, y se detiene con la orden exacta de `k8s` si faltan
  `ax-web-tls` o `ax-web-agent`. De `ax-web-office`, opcional, solo informa
  de si existe, de su tipo y de su número de claves, y de la orden `office`
  si falta: nunca se detiene por él.

## Aplicación

Tras AX, en el mismo `ax-lab`:

1. comprueba la red `apptolast-edge-ax` y la imagen (copia o registro);
2. aplica el manifiesto con `kubectl apply --server-side` solo si
   `kubectl diff --server-side` o `web.json` detectan deriva;
3. se detiene hasta que existan los dos Secrets obligatorios (primera vez y
   tras recrear el clúster; el pod espera solo);
4. espera al despliegue;
5. ejecuta el reenviador exactamente así, y lo recrea solo si difiere
   (`docker container inspect`), también si comparte un espacio de nombres
   del host o lleva dispositivos, grupos extra o tmpfs; si solo está
   parado, lo arranca:

   ```bash
   imagen=localhost:5001/ax-web@sha256:ab1b4f959b53f7495f37f130b73b867b3f793d11f02fb2460202e9676f2b66ef
   docker run --detach --pull never --name ax-web-edge --restart no \
     --user 65532:65532 --read-only --cap-drop ALL \
     --security-opt no-new-privileges --memory 33554432 \
     --memory-swap 33554432 --memory-reservation 16777216 --cpus 0.250 \
     --pids-limit 64 --label com.apptolast.managed-by=ansible \
     --label com.apptolast.ax-lab=web-edge --network kind "${imagen}" \
     forward --listen :8443 --target kind-control-plane:30843 \
     --allow-cidr 10.0.250.0/24
   docker network connect apptolast-edge-ax ax-web-edge
   ```

6. vuelve a leerlo todo y exige: sin deriva, los Secrets, la imagen en los
   dos sitios, el reenviador en marcha con su configuración en `kind` y
   `apptolast-edge-ax` y con una dirección de `10.0.250.0/24`; solo entonces
   anota `phase: installed`;
7. informa de si existe el Secret opcional `ax-web-office`.

En `--check` informa de lo que haría en cada punto, incluidas las paradas, y
del Secret opcional.
La política de reinicio `no` es la del laboratorio: tras reiniciar el host,
`ax.apptolast.com` responde 502 hasta aplicar `ax-lab`.

## Verificación

Desde el host, contra el NodePort y sin certificado cliente, TLS 1.3 tiene
que acabar con la alerta 116 `certificate_required`. En TLS 1.3 el servidor
la envía después del handshake, así que hay que leerla en la salida, no
basta con que la conexión falle:

```bash
nodo="$(sudo docker container inspect --format \
  '{{(index .NetworkSettings.Networks "kind").IPAddress}}' kind-control-plane)"
echo | sudo openssl s_client -connect "${nodo}:30843" -servername ax-web \
  -tls1_3 -CAfile /etc/dockerswarm/ax/web-tls/client-ca.crt \
  -verify_return_error -ign_eof 2>&1 | grep 'alert certificate required'
```

`grep` debe encontrar `tlsv13 alert certificate required`. Con eso quedan
probados el NodePort, el certificado del panel frente a su CA y la
exigencia del certificado cliente. El resto:

- `sudo docker container inspect ax-web-edge`: en marcha, redes `kind` y
  `apptolast-edge-ax`, sin puertos publicados;
- la sonda del playbook `edge`: `ax.apptolast.com` responde 401 con
  `realm="AX"`, y `GET /healthz` sin credenciales prueba Traefik, el
  reenviador y el mTLS de extremo a extremo;
- en el navegador: el acceso, la Oficina con su equipo y sus proyectos, un
  trabajo corto de tipo pregunta sobre `dockerswarm-infra` con la salida en
  directo, y que la tarea y su workspace desaparecen al terminar
  (`sudo ax get tasks -a default`);
- que el trabajo sigue en la Oficina tras reiniciar el pod: el estado está
  en `ax-web-state`;
- en «Ajustes», las credenciales: Claude siempre, y Codex y GitHub solo
  tras `office`, con los ficheros que hubiera;
- dos applies seguidos de `ax-lab` sin cambios.

## Ventana 2, parte del laboratorio

Con el laboratorio codificado ya en marcha (ventanas de los cambios 3, 4 y
5 hechas), fuera de 22:30-00:40 UTC, un solo operador, sin tareas de AX en
marcha y desde un checkout limpio del commit fusionado:

1. el propietario, en su sesión SSH: `ax-web-bootstrap.sh init` y la
   siembra de la imagen (ver «Imagen»);
2. el playbook `edge` con la ruta de AX (crea `apptolast-edge-ax` con la
   subred revisada; ver [EDGE.md](EDGE.md));
3. `ax-lab` con `--check` y después el apply: se detiene pidiendo
   `ax-web-bootstrap.sh k8s`;
4. el propietario ejecuta `ax-web-bootstrap.sh k8s`;
5. `ax-lab` otra vez: el despliegue termina y se verifica;
6. «Verificación»;
7. un último `ax-lab` que no cambia nada, y el registro de la ventana y de
   las caducidades en [DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md).

## Ventana de la Oficina

El paso del panel 0.2.0 a la Oficina 1.0.0, con las mismas condiciones que
la ventana 2 y sin ejecuciones en marcha (el pod se sustituye):

1. la CI compila `images/ax-web` 1.0.0 y el digest que da se fija en
   `web.image.digest` en un cambio revisado y fusionado;
2. el propietario siembra la imagen con `seed-layout` y `--tag 1.0.0` (ver
   «Imagen»), y si quiere GitHub, crea `/etc/dockerswarm/ax/github-token`
   (`root:root 0600`);
3. `ax-lab` con `--check` y después el apply: aplica el manifiesto (el
   PVC, la configuración nueva, los recursos y la salida HTTPS), sustituye
   el pod y recrea el reenviador, que usa la misma imagen por digest; hasta
   que vuelve, `ax.apptolast.com` responde 502;
4. el propietario ejecuta `ax-web-bootstrap.sh office`, si tiene la sesión
   de Codex o el token de GitHub;
5. «Verificación», y un último `ax-lab` que no cambia nada.

## Recrear el clúster

Recrear el clúster borra el espacio de nombres y sus Secrets, no el
reenviador ni los Docker Secrets. El apply siguiente crea el espacio de
nombres, se detiene pidiendo `ax-web-bootstrap.sh k8s`, y tras ejecutarlo
un segundo apply termina. `init` no se repite: el material sigue en
`/etc/dockerswarm/ax/web-tls`. El Secret opcional se crea de nuevo con
`ax-web-bootstrap.sh office`. El estado de la Oficina (`ax-web-state`) se
pierde con el clúster: la Oficina arranca con su equipo y sus proyectos de
siempre, sin trabajos, y con la sesión de Codex del Secret, que puede estar
gastada si la Oficina la había renovado (ver «Credenciales de la Oficina»).

## Marcha atrás

Solo el panel, con el lock host-global: su espacio de nombres, la
NetworkPolicy que abre `ax-server` al panel, que vive en `ax-system`, y el
reenviador.

```bash
sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
  --operation ax-web-remove -- /usr/bin/env \
  HOME=/opt/dockerswarm/ax-lab/home \
  /opt/dockerswarm/ax-lab/bin/kubectl \
  --kubeconfig /opt/dockerswarm/ax-lab/home/.kube/config \
  --context kind-kind delete namespace ax-web
sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
  --operation ax-web-remove -- /usr/bin/env \
  HOME=/opt/dockerswarm/ax-lab/home \
  /opt/dockerswarm/ax-lab/bin/kubectl \
  --kubeconfig /opt/dockerswarm/ax-lab/home/.kube/config \
  --context kind-kind --namespace ax-system \
  delete networkpolicy ax-web-to-ax-server
sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
  --operation ax-web-remove -- /usr/bin/docker rm --force ax-web-edge
```

Borrar el espacio de nombres borra también el PVC `ax-web-state` y, con él,
el estado de la Oficina: antes se descarga una copia desde «Ajustes →
Exportar».

`ax.apptolast.com` pasa a 502 y ningún otro sitio cambia. Un apply
posterior lo crearía de nuevo: la retirada definitiva es un PR que quite
`web` del contrato, y se anota en [DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md).
La ruta de Traefik se revierte con el playbook `edge` (ver
[EDGE.md](EDGE.md)).

## Capacidad

El reenviador entra en el grupo `ax-lab` del plan activo: el plan queda en
3 110m y 5 682 MiB reservados y 16 900m y 12 173 MiB de límite, 224 MiB bajo
los 12 397 MiB del presupuesto. Como `ate-setup` corre junto al nodo y sus
límites tienen que caber en lo que el plan deja libre, baja de 256 a
224 MiB (112 MiB reservados) y el techo de CPU libre pasa de 850m a 600m
(ver [AX.md](AX.md), «Capacidad»). El panel corre dentro del nodo: pide
50m y 96 MiB y tiene 500m y 384 MiB de límite. Sus 384 MiB caben en los
1 792 MiB que el validador reserva a todo lo que no son workers (ver
[AX.md](AX.md), «WorkerPool y capacidad dentro del nodo»), pero nada dentro
del nodo lo impone: el kubelet ve toda la memoria del host.

## Pendiente de comprobar en la ventana

- Que `claude -p --restricted` lee la instrucción por la entrada estándar
  y emite `stream-json` (si no, `prompt_mode: argument`).
- Que kindnet aplica la NetworkPolicy de salida del panel, también su regla
  de HTTPS por `ipBlock`, y la de `ax-web-to-ax-server`, y si un sandbox
  llega al NodePort (el mTLS lo rechaza igualmente).
- Que el panel, uid 65532, escribe en el volumen `local-path` de
  `ax-web-state`.
- Que el kubelet actualiza el volumen de `ax-web-office` cuando `office` lo
  crea o lo sustituye con el pod ya en marcha.
- Que el Envoy de atenet-router no acumula flujos largos. Con
  `max_timeout_minutes: 90`, una ejecución puede durar más que el
  `--route-timeout=1h` del laboratorio (ver [AX.md](AX.md), «Router»):
  cuando el router corta el flujo, el gestor de ejecuciones pregunta al
  invitado por el proceso y lo vuelve a seguir; queda comprobar una
  ejecución de más de una hora.
- Que HTTP/2 de Traefik al panel funciona a través del reenviador.
- `ax-tarea` no comprueba si el panel tiene una ejecución en marcha: con un
  solo worker, se quedaría esperando. El panel sí rechaza ejecutar si hay
  una tarea `tarea-*`. Además, `ax-tarea` lanza `claude -p` sin
  `--restricted`, como el modo completo de la Oficina y a diferencia de su
  modo lectura.
- El despliegue es solo sobre el laboratorio codificado: el laboratorio
  manual de `/opt/ax-lab` no lo recibe.
