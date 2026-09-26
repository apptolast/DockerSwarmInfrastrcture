# Panel web de AX: `ax.apptolast.com`

El panel pequeño y propio de `images/ax-web` publicado en
`https://ax.apptolast.com`, por decisión del propietario del 2026-09-26: se
publica el panel y nada más del laboratorio. `ax-server`, que no tiene
autenticación (google/ax#376), sigue sin publicarse nunca. Sin Cloudflare
Access y sin lista de IP permitidas, también por decisión del propietario.

Este documento cubre el despliegue en el laboratorio (playbook `ax-lab`):
el panel en Kubernetes, su reenviador TCP, sus credenciales, su imagen y su
verificación. La ruta de Traefik, el `basicAuth`, los límites de peticiones
y el registro DNS son del playbook `edge` (ver [EDGE.md](EDGE.md)). La
aplicación, sus salvaguardas y sus pruebas están en
[`images/ax-web/README.md`](../images/ax-web/README.md). El laboratorio en
general está en [AX.md](AX.md).

## Camino de una petición

```text
navegador --HTTPS--> edge_traefik (basicAuth, límites; playbook edge)
  --TLS 1.3 con certificado cliente (mTLS), red apptolast-edge-ax-->
ax-web-edge (contenedor suelto en apptolast-edge-ax y en kind; tubería TCP)
  --TCP--> kind-control-plane:30843 (NodePort; solo en el puente kind)
  --> Service ax-web (ns ax-web, externalTrafficPolicy Local) --> pod :8443
  --gRPC--> ax-server.ax-system:8080 y atenet-router.ate-system:80
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
- El panel no tiene token de la API de Kubernetes ni permisos RBAC.

## Contrato

<!-- markdownlint-disable MD013 -->

| Fichero | Qué fija |
| --- | --- |
| `config/ax-lab.yml`, `web` | Imagen por digest, NodePort 30843, puertos, origen, nombres TLS, directorio TLS, repositorios, ventana, límites de ejecución y el reenviador con su red, su subred y sus límites |
| `config/capacity-profiles.yml` | `host_containers.ax-lab.ax-web-edge`: 10m/16 MiB reservados, 250m/32 MiB de límite, 64 PIDs |
| `scripts/validate-ax-lab.py` | Valida `web`, lo cruza con la capacidad, renderiza el manifiesto y lo comprueba; `--web-manifest-sha256` imprime su sha256 |
| `ansible/roles/ax_lab/templates/ax-web.yaml.j2` | Namespace, ServiceAccount, ConfigMap, Deployment, Service NodePort y dos NetworkPolicies |
| `ansible/roles/ax_lab/tasks/web_read.yml`, `web.yml` | Lectura y prueba de propiedad (ambos modos) y despliegue (solo apply) |
| `scripts/manage-ax-lab-substrate.py seed-layout` | Copia la imagen fijada del layout OCI de la CI a la copia de seguridad |
| `scripts/ax-web-bootstrap.sh` | Arranque del propietario: CA, certificados, Docker Secrets y Secrets de Kubernetes |
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
`sha256:e8128547a95a3adcbb9a488faa9b379fdb9fa132337d7404cad073b2c3d78faa`.
Solo entonces guarda el layout OCI como artefacto `ax-web-oci-layout`. El
host nunca compila la imagen. El propietario la siembra una vez en la copia
de seguridad del laboratorio, desde el artefacto descomprimido en un
directorio de root (el gestor no lee rutas de otros usuarios):

```bash
sudo install -d -o root -g root -m 0700 /run/ax-web-seed
sudo -- /usr/bin/python3 -m zipfile -e ax-web-oci-layout.zip /run/ax-web-seed
sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py seed-layout \
  --image-set web --source /run/ax-web-seed \
  --layout /var/backups/dockerswarm/ax-lab/images --tag 0.1.0 \
  --image=ax-web=sha256:e8128547a95a3adcbb9a488faa9b379fdb9fa132337d7404cad073b2c3d78faa
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
  ningún valor, y se detiene con la orden exacta de `k8s` si faltan.

## Aplicación

Tras AX, en el mismo `ax-lab`:

1. comprueba la red `apptolast-edge-ax` y la imagen (copia o registro);
2. aplica el manifiesto con `kubectl apply --server-side` solo si
   `kubectl diff --server-side` o `web.json` detectan deriva;
3. se detiene hasta que existan los dos Secrets (primera vez y tras
   recrear el clúster; el pod espera solo);
4. espera al despliegue;
5. ejecuta el reenviador exactamente así, y lo recrea solo si difiere
   (`docker container inspect`), también si comparte un espacio de nombres
   del host o lleva dispositivos, grupos extra o tmpfs; si solo está
   parado, lo arranca:

   ```bash
   imagen=localhost:5001/ax-web@sha256:e8128547a95a3adcbb9a488faa9b379fdb9fa132337d7404cad073b2c3d78faa
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
   anota `phase: installed`.

En `--check` informa de lo que haría en cada punto, incluidas las paradas.
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
- en el navegador: el acceso, la lista de tareas, una ejecución de prueba
  corta (3 turnos, 5 minutos) sobre un repositorio público pequeño con la
  salida en directo, y que la tarea y su workspace desaparecen al terminar
  (`sudo ax get tasks -a default`);
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

## Recrear el clúster

Recrear el clúster borra el espacio de nombres y sus Secrets, no el
reenviador ni los Docker Secrets. El apply siguiente crea el espacio de
nombres, se detiene pidiendo `ax-web-bootstrap.sh k8s`, y tras ejecutarlo
un segundo apply termina. `init` no se repite: el material sigue en
`/etc/dockerswarm/ax/web-tls`.

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
(ver [AX.md](AX.md), «Capacidad»). El panel cabe dentro del nodo: pide
20m y 32 MiB y tiene 250m y 128 MiB de límite.

## Pendiente de comprobar en la ventana

- Que `claude -p --restricted` lee la instrucción por la entrada estándar
  y emite `stream-json` (si no, `prompt_mode: argument`).
- Que kindnet aplica la NetworkPolicy de salida del panel y la de
  `ax-web-to-ax-server`, y si un sandbox llega al NodePort (el mTLS lo
  rechaza igualmente).
- Que el Envoy de atenet-router no acumula flujos largos y que el
  `--route-timeout=1h` del laboratorio (ver [AX.md](AX.md), «Router») basta.
- Que HTTP/2 de Traefik al panel funciona a través del reenviador.
- `ax-tarea` no comprueba si el panel tiene una ejecución en marcha: con un
  solo worker, se quedaría esperando. El panel sí rechaza ejecutar si hay
  una tarea `tarea-*`. Además, `ax-tarea` lanza `claude -p` sin
  `--restricted`, a diferencia del panel.
- El despliegue es solo sobre el laboratorio codificado: el laboratorio
  manual de `/opt/ax-lab` no lo recibe.
