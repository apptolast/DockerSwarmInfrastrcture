# AX: laboratorio de agentes sobre Kubernetes

Despliega [AX](https://github.com/google/ax) (Agent Executor) de Google tal y
como lo plantea upstream: Kubernetes, [Agent
Substrate](https://github.com/agent-substrate/substrate) para los sandboxes y
el plano de control de AX encima. No es un stack Swarm: vive junto a él, en el
mismo Docker Engine, como clúster [kind](https://kind.sigs.k8s.io/). kind es
el entorno de desarrollo del quickstart de Substrate en el commit fijado (su
`README.md`, «Quickstart (Development)»); el otro es GKE.

Es un laboratorio, no un servicio. Substrate declara en ese mismo `README.md`
que «is not ready for production use». Nada del laboratorio se publica a
Internet y nada de él entra en el backup.

## Qué codifica este repositorio y qué sigue siendo manual

El laboratorio se montó a mano entre el 2026-09-24 y el 2026-09-25 en
`/opt/ax-lab`, y figura como estado temporal en
[`DEPLOYMENT_STATUS.md`](DEPLOYMENT_STATUS.md). Se codifica por partes, en
cambios revisados sucesivos.

Codificado ya, por el playbook `ax-lab`:

- los límites de inotify que kind necesita, persistentes en
  `/etc/sysctl.d/99-z-dockerswarm-ax-lab.conf` y convergidos en vivo;
- el directorio `/opt/dockerswarm/ax-lab` y los binarios `kind` y `kubectl`
  fijados por versión y sha256 en `/opt/dockerswarm/ax-lab/bin`;
- el clúster kind (`kind-control-plane`) y su registro local
  (`kind-registry`): configuración de kind, creación cuando falta, prueba de
  propiedad, límites de recursos, política de reinicio, `proxy_arp`,
  `proxy_ndp` y el espejo del registro dentro del nodo (ver «Ciclo de vida
  del clúster»);
- su presupuesto en `config/capacity-profiles.yml` (grupo `ax-lab` de
  `host_containers`) y `capacity_preflight` en el playbook;
- Agent Substrate (ver «Substrate»): sus siete imágenes fijadas por digest,
  su copia de seguridad byte a byte en `/var/backups/dockerswarm/ax-lab`,
  su restauración en el registro, una compilación de reserva acotada que
  solo corre si el propietario la autoriza, y su instalación con `ate-setup`
  de upstream, que solo se ejecuta cuando detecta deriva;
- un job de CI que recompila desde el código fijado cada imagen de
  Substrate y exige que su digest coincida con el fijado (ver
  «Reproducibilidad»);
- el contrato `config/ax-lab.yml`, que además fija ya el commit de AX y las
  imágenes upstream por digest para los cambios siguientes, y la aceptación
  del nodo privilegiado.

Sigue siendo manual, fuera del estado reconstruible:

- el plano de control de AX, sus imágenes compiladas en el host y el parche
  local;
- el laboratorio manual entero (`/opt/ax-lab`, su clúster y su registro)
  hasta que se retire con el procedimiento de «Retirar el laboratorio
  manual»;
- los ficheros de credenciales de `/etc/dockerswarm/ax`, que por diseño
  siempre los provisiona el propietario (ver «Credenciales»).

Cambios previstos, en este orden:

1. Contrato, validador, playbook y prerrequisitos del host (fusionado y
   aplicado el 2026-09-25, ver `DEPLOYMENT_STATUS.md`). Deja inotify
   persistente.
2. El ciclo de vida del clúster y del registro, su declaración de capacidad
   con `capacity_preflight` en el playbook y el procedimiento para retirar
   el laboratorio manual. Solo se fusiona en la ventana del laboratorio (ver
   «Por qué este cambio solo se fusiona en la ventana»).
3. Este cambio: las imágenes fijadas de Substrate, su copia de seguridad y
   su restauración, la compilación de reserva, el job de reproducibilidad y
   la instalación con `ate-setup`, que solo se ejecuta cuando detecta
   deriva. Separa el arranque de un nodo parado en `node.yml`, que corre
   cuando el registro ya sirve todas las imágenes. Se fusiona en la misma
   ventana que el cambio 2.
4. El plano de control de AX, el parche versionado con su sha256, las
   imágenes del runner y de agentes, el WorkerPool y el proxy de OpenAI con
   su secreto.
5. La prueba de humo, la prueba de reinicio y la versión final de este
   documento.

El cambio 3 ya pone una puerta sobre el registro: sube las imágenes con su
límite de 256 MiB aplicado y exige después `oom_kill 0` en el
`memory.events` de `kind-registry` (ver «Límites y política de reinicio»);
los cambios 4 y 5 la heredan. El cambio 4 también exige referenciar por
digest toda imagen que el clúster descargue del registro (ver
«Exposición»).

Suspender las Tasks de AX solo tiene hoy una orden documentada: el paso 1
de «Retirar el laboratorio manual», con la CLI `/opt/ax-lab/bin/ax` y
`HOME=/opt/ax-lab/home`, que el paso 6 de ese mismo procedimiento borra.
Entre la retirada y el cambio 4 no hay plano de control de AX ni Tasks que
suspender. El cambio 4 instala la CLI `ax` codificada, con su `HOME`, y
actualiza las cuatro instrucciones que piden suspenderlas y hoy solo
cuentan con esa CLI: antes de un reinicio planificado («Límites y política
de reinicio» y [OPERATIONS.md](OPERATIONS.md), «Reinicios»), en «Recrear
el clúster» y en el paso 1 de «Si se descarta el laboratorio».

En una ventana exclusiva (ver «Ventana del laboratorio»): sembrar la copia
de seguridad desde el laboratorio manual antes de retirarlo, retirarlo,
fusionar los cambios 2 y 3 y aplicar con `--check` y después de verdad; el
cambio 4 los sigue, y con él la prueba de humo y el paso del laboratorio en
`DEPLOYMENT_STATUS.md` a «Aplicado y verificado».

## Por qué kind y no un servicio Swarm

kind crea cada nodo con `--cgroupns=private`, `--privileged`,
`--security-opt seccomp=unconfined` y `--security-opt apparmor=unconfined`
(kind v0.33.0, `pkg/cluster/internal/providers/docker/provision.go`, líneas
175 y 226 a 228). La [referencia de `docker service
create`](https://docs.docker.com/reference/cli/docker/service/create/) no
ofrece ninguna de esas opciones, así que el nodo de Kubernetes no puede ser un
servicio del Swarm. kind se ejecuta en el mismo Engine, fuera de la
planificación del Swarm y sin tocar sus redes overlay.

## Contrato

<!-- markdownlint-disable MD013 -->

| Fichero | Qué fija |
| --- | --- |
| `config/ax-lab.yml` | Raíz de instalación, ruta de credenciales, sysctl, commits, binarios, imágenes, clúster, registro y aceptación del nodo privilegiado |
| `config/capacity-profiles.yml` | Grupo `ax-lab` de `host_containers`, solo en el plan activo `organizationweb` |
| `scripts/validate-ax-lab.py` | Valida el contrato sin red, lo cruza con la capacidad y renderiza el sysctl y la configuración de kind; con `--substrate-plan` imprime el plan de compilación de Substrate |
| `scripts/manage-ax-lab-substrate.py` | Lee, copia, respalda, restaura y, como reserva, compila las imágenes de Substrate, retira de la copia la entrada de un digest anterior, y ejecuta `ate-setup` acotado |
| `.github/workflows/ax-lab-reproducibility.yml` | Recompila las imágenes de Substrate desde el commit fijado y exige sus digests |
| `ansible/playbooks/ax-lab.yml` | Lock host-global, `capacity_preflight`, rol `ax_lab` y metadatos de despliegue |
| `ansible/roles/ax_lab/` | Prerrequisitos del host, prueba de propiedad, ciclo de vida del clúster y del registro, imágenes e instalación de Substrate |
| `ansible/roles/ax_lab/templates/kind-config.yaml.j2` | Configuración de kind |

<!-- markdownlint-enable MD013 -->

El validador rechaza:

- claves o tipos distintos de los revisados, y claves YAML duplicadas;
- commits que no sean SHA completos de 40 caracteres hexadecimales en
  minúscula, o repositorios distintos de los upstream;
- sha256 que no tengan 64 caracteres hexadecimales en minúscula;
- URL de binarios que no sean exactamente el asset `linux-amd64` oficial de
  su versión, por `https`, en `github.com/kubernetes-sigs/kind/releases` o
  `dl.k8s.io`;
- imágenes sin la forma `repositorio:tag@sha256:<64 hex>`, en otro
  repositorio o con `:latest`, y un nodo kind cuya versión no sea la de
  `kubectl`;
- una raíz de instalación que no sea un directorio propio directamente bajo
  `/opt/dockerswarm`, y una ruta de credenciales que no sea absoluta y esté
  bajo `/etc/dockerswarm`;
- claves de sysctl que coincidan con `host_baseline_sysctl`
  (`ansible/roles/host_baseline/defaults/main.yml`) o con las del fichero del
  rol `platform` (`99-z-dockerswarm-network.conf`), y valores distintos de
  los revisados;
- una API de Kubernetes que no escuche en la loopback IPv4, o en un puerto
  fuera de 1024 a 32767: el rango efímero del kernel (32768 a 60999 en este
  host) podría tenerlo ocupado una conexión saliente;
- un registro publicado en una dirección que no sea de loopback, con el
  mismo puerto que la API, en otro puerto interno que el 5000 o con el
  mismo nombre que el nodo;
- un nodo que no se llame `<nombre>-control-plane` o una red que no sea
  `kind`, que son los nombres que kind usa;
- límites que no sean enteros de al menos 1, reservas mayores que el
  límite, una relación límite/reserva de memoria por encima de
  `service_memory_limit_to_reservation_ratio` de `config/capacity.yml`
  (2,50), o límites distintos de los del grupo `ax-lab` de
  `config/capacity-profiles.yml`;
- una política de reinicio que no sea la cadena `"no"`: sin comillas, YAML
  lee `no` como el booleano `false`;
- una configuración de kind que no sea exactamente la de Substrate más la
  API en loopback y el directorio del registro de containerd (ver «Ciclo de
  vida del clúster»);
- para Substrate: una versión que no sea una abreviatura del commit fijado
  de al menos 7 caracteres y apta como etiqueta; un conjunto de imágenes
  distinto de las siete revisadas o sin `sha256:<64 hex>` (solo
  `ate-setup` puede quedar pendiente); otro directorio de copia que
  `/var/backups/dockerswarm/ax-lab/images`; una imagen de herramientas que
  no sea `golang:1.27.1` por digest; una compilación de reserva con más
  memoria, reserva, CPU o PIDs que el nodo, CPU no entera, sin el suelo de
  `MemAvailable` y el margen operativo de `config/capacity.yml` o con un
  tiempo fuera de 600 a 7 200 s; una instalación con otro router que
  `envoy` (ni `agentgateway` ni sdsmint, que no tienen clave), con más
  memoria o CPU de las que el plan de capacidad que ejecuta el laboratorio
  deja libres bajo el presupuesto de límites (256 MiB y 850m hoy), sin el
  suelo de `MemAvailable` o con un tiempo que no cubra las esperas del
  propio `ate-setup`; y un inventario de cargas desordenado, repetido,
  fuera de los tres espacios de nombres, sin el DaemonSet
  `atelet-<versión>`, sin alguna imagen instalada o con referencias upstream
  sin tag y digest;
- en `ax_lab_substrate_fallback_builds`, una imagen desconocida, repetida o
  sin digest que reproducir;
- cualquier clave o valor con forma de credencial, en cualquier parte del
  fichero.

## Prerrequisitos del host

El playbook toma el lock host-global con `operation_lock_guard`, pasa
`capacity_preflight` (ver «Capacidad»), ejecuta `ax_lab` y registra el
componente `ax-lab` con `deployment_metadata`.

El rol comprueba primero que sus entradas son exactamente las de
`config/ax-lab.yml`, ejecuta el validador en el controlador y le pide el
sha256 de la configuración de kind, exige que la raíz de instalación cuelgue
de `platform_install_root` y que el host sea `x86_64`, y revisa sin seguir
enlaces que ninguna ruta que va a escribir sea un enlace simbólico, tenga
otro tipo, otro dueño que `root:root` o permiso de escritura para grupo u
otros. Después prueba que cada contenedor del laboratorio que ya existe es
suyo (ver «Prueba de propiedad»), y solo entonces escribe algo.

### Límites de inotify

La [página de problemas conocidos de
kind](https://kind.sigs.k8s.io/docs/user/known-issues/), en «Pod errors due
to too many open files», atribuye esos errores a los límites de inotify y
recomienda subirlos a `fs.inotify.max_user_watches=524288` y
`fs.inotify.max_user_instances=512`, y hacerlo de forma persistente. El rol:

1. renderiza el fichero y lo valida con `sysctl --dry-run` antes de
   instalarlo como `root:root 0644`;
2. ejecuta `sysctl --dry-run --system` y exige que el último valor de cada
   clave sea el del contrato, que es el que queda tras un arranque: «the
   entry in the file with the lexicographically latest name will take
   precedence» (`sysctl.d(5)`);
3. escribe con `sysctl -w` solo las claves cuyo valor vivo difiere, y solo
   esas, y después comprueba los valores vivos.

Nunca aplica `sysctl --system`, que solo simula con `--dry-run` para
comprobar el valor de arranque: aplicarlo recargaría también ficheros que
este repositorio no gestiona, como `/etc/sysctl.d/99-hardening.conf`, que
pone `net.ipv6.conf.all.forwarding = 0` mientras el host corre con 1. Las
claves no están en `host_baseline_sysctl` porque solo las necesita el
laboratorio y se retiran con él (ver «Verificación»).

### Binarios

`kind` y `kubectl` se instalan con `ansible.builtin.get_url` y
`checksum: sha256:<fijado>`, como `root:root 0755`, en
`/opt/dockerswarm/ax-lab/bin` (`root:root 0750`). `get_url` calcula el hash
del fichero existente y solo descarga si difiere, y una descarga que no
coincide falla antes de sustituir nada. Tras el apply, el rol vuelve a leer
cada binario sin seguir enlaces y exige fichero regular, dueño, modo y
sha256. Los hashes son los que upstream publica junto a cada asset:
`kind-linux-amd64.sha256sum` en la release de kind y `kubectl.sha256` en
`dl.k8s.io`.

### Modo check e idempotencia

A diferencia del despliegue de una aplicación, las tareas del host también
se ejecutan con `--check`: las lecturas son seguras en ese modo, las
escrituras solo informan y las comprobaciones en vivo esperan a un apply
real. En un host sin el directorio, `--check` no intenta la descarga, porque
`get_url` no puede informar sobre un destino cuyo directorio no existe. Un
segundo apply sobre un host convergido debe informar `changed=0`; el primer
apply real lo comprueba.

## Ciclo de vida del clúster

El rol crea el clúster y el registro cuando faltan, y después solo corrige
lo que difiere del contrato y lo vuelve a comprobar. Nunca ejecuta
`hack/create-kind-cluster.sh` de Substrate, que borra el clúster existente
antes de crear otro (líneas 128 y 129 en el commit fijado), ni borra nada.

### Configuración de kind

`ansible/roles/ax_lab/templates/kind-config.yaml.j2` es la configuración que
genera `hack/create-kind-cluster.sh` de Substrate en el commit fijado (líneas
88 a 126) con `IP_FAMILY=ipv4`, línea a línea y con sus comentarios: las
feature gates de `ClusterTrustBundle`, `ClusterTrustBundleProjection` y
`PodCertificateRequest`, `certificates.k8s.io/v1beta1` en `runtimeConfig` y
el parche del kubelet con `serializeImagePulls: false` y
`maxParallelImagePulls: 4`. Coincide con la del laboratorio manual. El host
no tiene `/dev/kvm`, así que, como el script de Substrate en ese caso, no
monta nada en el nodo. Añade solo:

- `networking.apiServerAddress: "127.0.0.1"` y `apiServerPort: 6443`: kind
  elige por defecto un puerto aleatorio, y su [guía de
  configuración](https://kind.sigs.k8s.io/docs/user/configuration/#api-server)
  recomienda con insistencia no salir de la loopback. Con un puerto fijo y
  revisado, un clúster recreado vuelve a la misma dirección. Ningún otro
  proceso del host escucha en el 6443;
- `containerdConfigPatches` con `config_path = "/etc/containerd/certs.d"`,
  el parche de la [guía del registro
  local](https://kind.sigs.k8s.io/docs/user/local-registry/)
  (`site/static/examples/kind-with-registry.sh`, líneas 37 a 44 en v0.33.0).
  La propia guía dice que no hace falta con imágenes de kind v0.27.0 o
  posteriores (líneas 29 a 31): containerd 2.x usa por defecto
  `/etc/containerd/certs.d:/etc/docker/certs.d`
  (`internal/cri/config/config.go`, líneas 228 a 233 en v2.3.4, la versión
  del nodo). Se declara para no depender de ese valor por defecto, con la
  sintaxis de la versión 2 del fichero que usa el nodo.

El validador la renderiza igual que el módulo `template` de Ansible, la
compara con esa estructura exacta y publica su sha256. El rol la escribe en
`/opt/dockerswarm/ax-lab/kind-config.yaml` (`root:root 0640`) y exige que el
fichero tenga ese mismo sha256.

### Prueba de propiedad

kind no permite etiquetar el contenedor del nodo: solo le pone
`io.x-k8s.kind.cluster` e `io.x-k8s.kind.role` (`provision.go`, líneas 140 y
220). Por eso, justo después de crear el clúster, el rol escribe
`/opt/dockerswarm/ax-lab/state/cluster.json` (`root:root 0600`, en un
directorio `0700`) con el ID del contenedor del nodo, el sha256 de la
configuración de kind, la imagen del nodo y la versión de kind. El registro
sí lleva etiquetas: `com.apptolast.managed-by=ansible` y
`com.apptolast.ax-lab=registry`.

Antes de cambiar nada, en `--check` y en el apply, el rol lee los dos
contenedores con `docker container inspect` (solo los campos que compara,
nunca el entorno) y se detiene:

- si existe `kind-control-plane` sin ese fichero, o el fichero no es un
  fichero regular `root:root 0600`: nunca se adopta. El laboratorio manual
  (`/opt/ax-lab`) o un clúster hecho a mano se retira con «Retirar el
  laboratorio manual», y el nodo que deja un primer apply de este rol
  interrumpido durante `kind create cluster` o justo después, cuando aún no
  existe el fichero, se borra como en «Recrear el clúster»;
- si el fichero no coincide exactamente con el nodo: otro ID de contenedor,
  otra configuración de kind, otra imagen u otra versión de kind. kind no
  reconfigura un clúster en marcha, así que el cambio exige recrearlo (ver
  «Recrear el clúster»);
- si el nodo tiene otra imagen, otra etiqueta de clúster, otra red u otro
  puerto publicado para la API, o si el registro no lleva las dos etiquetas
  del rol o tiene otra imagen, otras publicaciones, otra red o otro volumen;
- si alguno está `paused`, `restarting`, `removing` o `dead`;
- si `ax_lab_privileged_node_accepted` es `false`.

Un contenedor ausente no es un error: solo cuenta como ausente si Docker
responde que no existe ese nombre exacto.

### Creación

Si falta el nodo, el rol ejecuta, con `HOME=/opt/dockerswarm/ax-lab/home`:

```bash
/opt/dockerswarm/ax-lab/bin/kind create cluster \
  --name kind \
  --config /opt/dockerswarm/ax-lab/kind-config.yaml \
  --image "docker.io/kindest/node:v1.37.0@sha256:<digest fijado>" \
  --kubeconfig /opt/dockerswarm/ax-lab/home/.kube/config
```

kind descarga la imagen por digest si no está (`images.go`, líneas 52 a
75), crea la red `kind` si no existe (`provider.go`, líneas 71 a 77),
ejecuta `kubeadm init`, instala la CNI y el StorageClass (`create.go`,
líneas 111 a 130) y escribe un kubeconfig de administrador solo en esa
ruta, con modo `0600` (`write.go`, línea 40); esos ficheros están en
`pkg/cluster/internal` de kind v0.33.0. Si falla la creación del nodo o una
de esas acciones, borra el nodo, porque no se pasa `--retain` (`create.go`,
líneas 102 a 108 y 135 a 141). Si solo falla la exportación del kubeconfig
(líneas 149 a 161), o si kind se interrumpe, el nodo queda sin prueba de
propiedad (ver «Recrear el clúster»).

El rol no pasa `--wait`, cuyo valor por defecto es `0s`
(`pkg/cmd/kind/create/cluster/createcluster.go`, líneas 82 a 87): kind se
salta entonces la espera a que el nodo esté `Ready`
(`create/actions/waitforready/waitforready.go`, líneas 47 a 49), así que el
`docker update` de los límites llega en cuanto kind termina y el rol
escribe la prueba de propiedad, antes de crear el registro, y las esperas
del propio rol ya corren con ellos (ver «Límites y política de reinicio»).
Con `--wait`, un plazo agotado solo deja un aviso y kind termina con éxito
igualmente (líneas 87 a 91 del mismo fichero), así que esa espera no
añadía ninguna comprobación.

Si el kubeconfig desaparece con el nodo en marcha, el rol lo regenera con
`kind export kubeconfig`, y siempre exige que sea `root:root 0600`. Todas
las llamadas a `kubectl` usan ese kubeconfig y
`HOME=/opt/dockerswarm/ax-lab/home`, así que su caché queda en el árbol del
laboratorio y nunca en `/root`, y abandonan cada petición a los 10 segundos
(`--request-timeout 10s`; por defecto, `kubectl` no la abandona nunca), de
modo que los reintentos acotan cada espera.

Si falta el registro, lo crea desde su digest fijado, ya con sus límites:

```bash
docker run --detach --name kind-registry --restart no \
  --memory 268435456 --memory-swap 268435456 \
  --memory-reservation 134217728 --cpus 0.500 --pids-limit 256 \
  --label com.apptolast.managed-by=ansible \
  --label com.apptolast.ax-lab=registry \
  --network kind \
  --mount type=volume,source=ax-lab-registry,target=/var/lib/registry \
  --publish=127.0.0.1:5001:5000 --publish=[::1]:5001:5000 \
  "docker.io/library/registry:3@sha256:<digest fijado>"
```

La guía de kind arranca el registro en la red `bridge` por defecto y después
lo conecta a `kind`, porque lo crea antes que el clúster. Aquí el clúster ya
existe, así que el registro solo está en la red `kind`: ningún contenedor
que solo esté en el bridge por defecto lo alcanza. Si pierde esa red, el rol
lo vuelve a conectar; si está en cualquier otra, se detiene. Las imágenes
viven en el volumen con nombre `ax-lab-registry`, que sobrevive a una
recreación del contenedor.

El registro, como el de la guía de kind y el de Substrate
(`hack/create-kind-cluster.sh`, líneas 63 a 69), no tiene autenticación:
cualquier proceso del host, de cualquier usuario, puede subir, sobrescribir
o leer imágenes por `127.0.0.1:5001` y `[::1]:5001`, y cualquier carga del
clúster puede hacerlo por `kind-registry:5000`, porque comparte la red
`kind` con el nodo. Es un riesgo aceptado del laboratorio (ver
«Exposición»).

Las dos publicaciones son las de Substrate (`hack/create-kind-cluster.sh`,
líneas 66 y 67). El daemon corre con `"userland-proxy": false`
(`config/daemon.json`), y la documentación de Docker solo describe que un
puerto IPv6 del host llegue a un contenedor IPv4 con el proxy de usuario
activo ([Port publishing and
mapping](https://docs.docker.com/engine/network/port-publishing/)). El
2026-09-25, contra el registro manual, `http://127.0.0.1:5001/v2/` devolvió
200 y `http://[::1]:5001/v2/` agotó el tiempo de espera, con ese registro
conectado a la vez al bridge por defecto (`172.17.0.2`) y a la red `kind`
(`172.23.0.3` y `fc00:f853:ccd:e793::3`) y sin proxy de usuario. Con el
registro solo en `kind`, como lo crea el rol, no se ha comprobado. La
publicación en `[::1]` se mantiene porque, como mucho, reserva un puerto de
loopback.

### Límites y política de reinicio

<!-- markdownlint-disable MD013 -->

| Contenedor | Memoria límite | Memoria reservada | Swap | CPU límite | PIDs | Reinicio |
| --- | ---: | ---: | --- | ---: | ---: | --- |
| `kind-control-plane` | 3 584 MiB | 1 792 MiB | ninguna | 2 CPU | 4 096 | `no` |
| `kind-registry` | 256 MiB | 128 MiB | ninguna | 0,5 CPU | 256 | `no` |

<!-- markdownlint-enable MD013 -->

El rol compara `Memory`, `MemorySwap`, `MemoryReservation`, `NanoCpus`,
`PidsLimit` y `RestartPolicy` de cada contenedor con esos valores y ejecuta
[`docker
update`](https://docs.docker.com/reference/cli/docker/container/update/)
con `--memory`, `--memory-swap` (igual que la memoria: sin swap),
`--memory-reservation`, `--cpus`, `--pids-limit` y `--restart` solo en el
que difiere. kind crea el nodo sin límites y con `--restart=on-failure:1`
(`provision.go`, línea 167), así que el primer apply lo corrige en cuanto
escribe la prueba de propiedad, antes de crear el registro o descargar su
imagen: el nodo solo corre sin límites durante todo `kind create cluster`
(incluido `kubeadm init`) y mientras el rol escribe la prueba. Si el apply
se interrumpe en ese intervalo, el nodo sigue sin límites: con la prueba
escrita, hasta el siguiente apply de `ax-lab`; sin ella, hasta que se borra
(ver «Recrear el clúster»).

El registro del laboratorio manual tiene un `memory.peak` de 376,8 MiB
(395 116 544 bytes) y `oom_kill 0`. Se creó el 2026-09-24 a las 23:16 UTC y
no se ha reiniciado, así que ese pico incluye todas las subidas de imágenes
del ensayo. Es sobre todo caché de ficheros de los blobs: el 2026-09-25 su
memoria anónima era de 12,6 MiB, y su uso sin esa caché estuvo entre 17 y
29 MiB. Con un límite, el kernel recupera esa caché antes de recurrir al OOM
killer (`memory.max` en la [documentación de cgroup
v2](https://docs.kernel.org/admin-guide/cgroup-v2.html)), así que 256 MiB
dejan más de 200 MiB de caché a un proceso que usa menos de 30. Los cambios
4 y 5 lo comprobarán: subirán las imágenes con el límite ya aplicado y
exigirán después `oom_kill 0` en
`/sys/fs/cgroup/system.slice/docker-<ID>.scope/memory.events`, con el ID que
da `docker container inspect --format '{{.ID}}' kind-registry`.

Con la política `no`, nada arranca el laboratorio tras un reinicio del host
o de Docker, como el que hace el handler «Restart Docker» del rol `platform`
cuando cambian `daemon.json`, la política de puertos publicados o el
override de systemd de Docker (el daemon no usa `live-restore`): los dos
contenedores quedan `exited`, con sus límites. Eso no detiene ningún otro
despliegue, porque el preflight de capacidad acepta parado un contenedor
del host del plan activo (ver «Capacidad»). El laboratorio sigue parado
hasta que, si se quiere de vuelta, se aplica `ax-lab`: el playbook pasa
`capacity_preflight`, arranca con `docker start` el registro, comprueba que
sirve cada imagen fijada de Substrate (ver «Imágenes fijadas»), arranca el
nodo, comprueba que corre con exactamente sus límites, espera a que
`/readyz` de la API responda `ok` y a que el nodo esté `Ready`, vuelve a
aplicar `proxy_arp` y `proxy_ndp` y espera a que Substrate esté listo. El
nodo se arranca en `node.yml`, después del registro y de sus imágenes, así
que un nodo parado no detiene nada de lo anterior. Antes de un reinicio
planificado del host o de Docker se suspenden las Tasks de AX, como en el
paso 1 de «Retirar el laboratorio manual». Si `ax-lab` se niega a
arrancarlo porque la prueba de propiedad ya no coincide, el laboratorio
sigue parado, sin detener nada más, hasta que se recrea (ver «Recrear el
clúster»).

### Dentro del nodo

Solo cuando el valor difiere, y comprobándolo después:

- `net.ipv4.conf.all.proxy_arp=1` y `net.ipv6.conf.all.proxy_ndp=1`, que
  Substrate activa en cada nodo para la red de pod a pod de gVisor
  (`hack/create-kind-cluster.sh`, líneas 174 a 182), sin mirar la familia
  del clúster: «nodes of either family carry the other's addresses on the
  Docker bridge». Aunque los pods solo son IPv4, la red `kind` es de doble
  pila y el nodo tiene también una dirección IPv6. `proxy_ndp` se escribe
  con `sysctl -e`, como en Substrate, y la comprobación final exige `1` en
  los dos. Viven en el espacio de red del nodo y se pierden en cada
  reinicio: tras el del 2026-09-25 el laboratorio manual tenía `proxy_arp` a
  0;
- `/etc/containerd/certs.d/localhost:5001/hosts.toml` con
  `[host."http://kind-registry:5000"]`, el paso 3 de la guía del registro
  local: containerd envía `localhost:5001` al contenedor del registro;
- el ConfigMap `kube-public/local-registry-hosting` del paso 5 de esa guía
  (KEP-1755), aplicado con `kubectl apply --server-side`.

### Modo check

Con `--check`, el rol ejecuta todas las lecturas y todas las comprobaciones
de propiedad, igual que el apply, y termina con un informe de lo que el
apply haría: crear el registro o el clúster, `docker update` o
`docker start` de cada contenedor, y los ajustes dentro del nodo. No crea,
arranca ni cambia ningún contenedor. Con el laboratorio manual en marcha,
`--check` se detiene en el mismo punto que el apply.

## Substrate

Agent Substrate se instala en el commit fijado en `sources.substrate`
(`672533541dbf…`) con la versión `67253354`. Tres reglas lo gobiernan:

- un apply normal del host nunca compila: solo verifica e instala imágenes
  fijadas por digest;
- las imágenes llegan, por este orden, desde la copia de seguridad sembrada
  desde el laboratorio manual y desde el registro local; compilarlas en el
  host es una reserva explícita que el propietario autoriza imagen a imagen
  (ver «Compilación de reserva»);
- Substrate se instala con `ate-setup` de upstream en modo precompilado, y
  solo cuando lo instalado difiere de lo fijado (ver «Instalación de
  Substrate»).

### Imágenes fijadas

`config/ax-lab.yml` fija en `substrate.images` el digest del manifiesto de
cada imagen, que ko construye de `./cmd/<nombre>` para una sola plataforma
(`linux/amd64`):

<!-- markdownlint-disable MD013 -->

| Imagen | Digest | Uso |
| --- | --- | --- |
| `ateapi` | `sha256:c1422c85…30f3` | `ate-api-server` |
| `atecontroller` | `sha256:0893131c…0c14` | `ate-controller` |
| `atelet` | `sha256:8e88c4e0…0f7b` | DaemonSet `atelet-67253354` |
| `atenet` | `sha256:929ca468…fcd9` | `atenet-router` y `atenet-egress` |
| `podcertcontroller` | `sha256:2baed2f1…8f99` | `podcertificate-controller` |
| `ateom-gvisor` | `sha256:fdd1d0ad…92e7` | Imagen de los workers del WorkerPool (cambio 4) |
| `ate-setup` | `sha256:43c9e2db…1098` | El instalador (ver «Reproducibilidad») |

<!-- markdownlint-enable MD013 -->

Los seis primeros son los que sirve el registro del laboratorio manual,
leídos el 2026-09-25 con `curl -I` de su `Docker-Content-Digest`. El
registro local los guarda como `<nombre>:67253354`, el nombre que busca
`ate-setup --image-repo` (el de `--base-import-paths` de ko,
`cmd/ate-setup/differences.md`, «Image sources», en el commit fijado). El
laboratorio manual usó el nombrador por defecto de ko, con el md5 de la
ruta de importación (`ateapi-752889f8…`); el nombre del repositorio no
forma parte del manifiesto, así que el digest es el mismo.

Las imágenes solo se mueven por la API HTTP del registro, nunca con
`docker pull` y `docker push`, que guardan el manifiesto en el almacén de
Docker y lo vuelven a serializar, con otro digest.
`scripts/manage-ax-lab-substrate.py` calcula el sha256 de cada manifiesto y
de cada blob mientras los copia y exige que coincidan con su descriptor y
con el digest fijado; solo habla HTTP con la loopback.

En cada apply, con el registro ya en marcha, el rol (`images.yml`) lee qué
imágenes sirve el registro bajo su tag y cuáles tiene completas la copia de
seguridad; copia a la copia las que le falten, restaura en el registro las
que falten o cuyo tag apunte a otro manifiesto (el registro no tiene
autenticación), vuelve a comprobarlas todas y exige `oom_kill 0` en el
`memory.events` de `kind-registry`. Una imagen que no esté en ninguno de
los dos detiene el apply. Solo después arranca el nodo si estaba parado
(`node.yml`). El que `kind create cluster` acaba de crear en un primer apply
ya corre desde antes, pero nada descarga una imagen de Substrate hasta que
`ate-setup` aplica sus cargas, y para entonces el registro las sirve todas.

### Copia de seguridad

Es un [OCI image
layout](https://github.com/opencontainers/image-spec/blob/main/image-layout.md)
en `/var/backups/dockerswarm/ax-lab/images`: directorios `root:root 0700`,
ficheros `root:root 0600`, un blob por digest, compartidos entre imágenes, y
en `index.json` una referencia `<nombre>:67253354` por imagen. Ocupa unos
250 MB. El gestor lo lee sin seguir enlaces, con `O_NOFOLLOW` y `fstat`
sobre el descriptor abierto (tipo, modo, dueño y un solo enlace duro), y en
cada apply recalcula el sha256 de todos sus blobs: si uno no coincide, el
apply se detiene y la copia nunca se repara sola.

La restauración es automática: cuando el registro pierde una imagen, porque
se borró el volumen `ax-lab-registry` o se recreó el registro, el siguiente
apply la copia desde aquí. A mano, con el mismo lock, es la misma orden que
ejecuta el rol:

```bash
sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py import \
  --registry 127.0.0.1:5001 \
  --layout /var/backups/dockerswarm/ax-lab/images \
  --tag 67253354 \
  --image=<nombre>=<digest fijado>
```

`image-status`, con los mismos argumentos, solo lee e informa de cada
imagen: `pinned`, `missing` o `moved` en el registro, `complete` o
`missing` en la copia. La copia vive en el mismo host: protege del borrado
del registro o de su volumen y de recrear el clúster, no de perder el host
(ver «Límites conocidos»).

La copia nunca guarda un mismo `<nombre>:67253354` con dos digests. Si un
cambio revisado fija otro digest para una imagen con la misma versión (un
digest del laboratorio manual que la CI no reproduce y el propietario
sustituye por el de la CI, o un nuevo `ate-setup` tras cambiar la imagen de
herramientas), todo `--check` y todo apply se detienen en `image-status`
con «names `<nombre>`:67253354 with another digest» hasta retirar la entrada
del digest anterior. Se retira con el mismo lock, desde el checkout del
cambio que fija el nuevo, nombrando el digest anterior exacto; los blobs se
quedan, porque son direccionables por contenido:

```bash
sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py forget \
  --layout /var/backups/dockerswarm/ax-lab/images \
  --tag 67253354 \
  --image=<nombre>=<digest anterior>
```

Dice `forgotten`, o `absent` si ya no estaba, y se niega si la entrada
nombra otro digest. Después, la imagen nueva llega como cualquier otra que
falte: desde el registro si ya la sirve con su digest, o compilándola con la
reserva (ver «Compilación de reserva»). El registro puede seguir
sirviendo el digest anterior bajo ese tag; el apply lo trata como un tag
movido y lo restaura desde la copia.

### Semilla desde el laboratorio manual

El registro codificado y el manual se llaman igual (`kind-registry`) y
publican el mismo `127.0.0.1:5001`, así que no pueden convivir y no hay
copia directa de uno a otro. La semilla pasa por la copia de seguridad: con
el laboratorio manual todavía en marcha, antes de retirarlo, se exportan
sus seis imágenes a la copia, y el primer apply las restaura en el registro
nuevo. Como la semilla precede a la fusión, se ejecuta desde un clon limpio
en el commit aprobado del cambio 3 (en el clon operativo, `git checkout
--detach <commit>`). `--source-naming ko-md5` lee cada imagen del
repositorio con el nombre del nombrador por defecto de ko (ko v0.19.1,
`pkg/commands/options/publish.go`, líneas 109 a 113), por su digest:

<!-- markdownlint-disable MD013 -->

```bash
sudo -- install -d -o root -g root -m 0700 /var/backups/dockerswarm/ax-lab
sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py export \
  --registry 127.0.0.1:5001 \
  --layout /var/backups/dockerswarm/ax-lab/images \
  --tag 67253354 \
  --source-naming ko-md5 \
  --image=ateapi=sha256:c1422c85315e87c0710ec9e22447ea68185f96cd47e10473b75966f79c2d30f3 \
  --image=atecontroller=sha256:0893131c560ee326933dc06494c86d31011f48c0c60e8dbebb40aaaed4c80c14 \
  --image=atelet=sha256:8e88c4e0d0289c88cb84f18f679494645700e67bea0e1a4c0bf56f461bf30f7b \
  --image=atenet=sha256:929ca468e20fc7d06e356cb844d3cd5d2d30632db68ba5360adb4850b975fcd9 \
  --image=podcertcontroller=sha256:2baed2f1e88434f8d018e595c41f7117f06c6af6410b6fbd2210bb79c54c8f99 \
  --image=ateom-gvisor=sha256:fdd1d0ad3b30578d86f11aaa67740448a8477ce706e70207b8b51a96646592e7
```

<!-- markdownlint-enable MD013 -->

En un host con Swarm activo, el gestor se vuelve a ejecutar él mismo bajo
el lock host-global (`host_global_operation_lock.py run --operation
ax-lab-substrate-export`). La salida, en JSON, dice `exported` para cada
imagen. `image-status` con esos mismos `--layout`, `--tag` e `--image` debe
decir `complete` para las seis antes de seguir con la retirada. El
laboratorio manual nunca tuvo imagen de `ate-setup`: esa llega por la
compilación de reserva (ver «Ventana del laboratorio»).

### Compilación de reserva

Solo compila una imagen que cumpla todo esto:

- está en `ax_lab_substrate_fallback_builds` de `config/ax-lab.yml`, que
  solo cambia en un cambio revisado (el wrapper no admite variables extra) y
  queda vacía fuera de una ventana;
- no está completa en la copia de seguridad ni la sirve el registro;
- el nodo no está en marcha: la compilación corre antes de que
  `cluster.yml` cree o arranque el nodo, y usa su presupuesto (registro
  256 MiB más compilación 3 072 MiB, dentro de los 3 840 MiB del grupo
  `ax-lab`). Si el nodo corre, el apply se detiene: se suspenden las Tasks
  de AX y se para el nodo con el lock host-global,
  `sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run
  --operation ax-lab-stop -- /usr/bin/docker stop --time 60
  kind-control-plane`, y se aplica de nuevo. `--check` ya lo anuncia así en
  lugar de prometer la compilación.

Cada imagen se compila sola, una tras otra, en un contenedor de la imagen
oficial `golang:1.27.1` fijada por digest (`images.toolbox`), con el
`make build-images` de upstream (`Makefile`, líneas 72 a 75) e
`IMAGES=./cmd/<nombre>`, `VERSION=67253354`, `KO_DOCKER_REPO` en la línea
de make (la línea 19 exporta el suyo) y `KO_FLAGS=--push=false
--oci-layout-path=... --sbom=none`: ko escribe la imagen en un layout OCI en
lugar de subirla, y sin SBOM. El contenedor:

- 3 072 MiB de memoria sin swap, 1 536 MiB reservados, 2 CPU, 1 024 PIDs;
  `GOMAXPROCS=2`, `GOFLAGS=-p=1`, `GOGC=40`, `GOTOOLCHAIN=local`;
- `--cap-drop ALL`, `no-new-privileges`, raíz de solo lectura, red `bridge`
  y ningún `docker.sock`; el checkout de Substrate montado de solo lectura y
  las cachés de Go, `TMPDIR` y `HOME` en `/opt/dockerswarm/ax-lab/cache`
  (`0700`), nunca en un tmpfs, que contaría contra su memoria;
- no arranca con menos de 3 584 MiB de `MemAvailable` en el host (su límite
  más el margen operativo de `config/capacity.yml`), y se mata si
  `MemAvailable` baja de 512 MiB o pasa de 3 600 s. La presión de memoria
  del host y del contenedor solo se registra: la propia compilación la hace
  subir al acercarse a su límite;
- se ejecuta sin `--rm`: el gestor lee su código de salida y `OOMKilled` y
  solo entonces lo borra con `docker rm`. Un contenedor que quede de una
  ejecución anterior detiene todo apply y todo `--check` hasta que se
  revisan sus logs y se borra a mano (ver «Contenedores transitorios»).

El digest que imprime ko tiene que ser exactamente el fijado; solo entonces
se copia la imagen a la copia de seguridad, y de ella al registro. Cada
compilación imprime en JSON sus segundos, el `memory.peak` y los
`oom_kill` de su cgroup y la presión máxima. En el laboratorio manual,
`ateapi` tardó 47 min 51 s con 1 CPU, 1 400 MiB y el host sin memoria; con
estos límites no se ha medido, y un reintento reaprovecha la caché.

### Reproducibilidad

`.github/workflows/ax-lab-reproducibility.yml` prueba en la CI pública que
los digests fijados salen del código fijado. Corre en cada pull request o
push a `main` que toque `config/ax-lab.yml`, el gestor, el validador o el
propio workflow, y a mano. Con `permissions: contents: read` y cada action
fijada por su commit completo:

1. valida el contrato y lee de `validate-ax-lab.py --substrate-plan` el
   commit, las imágenes y los límites;
2. hace checkout de `agent-substrate/substrate` en ese commit completo y
   exige el árbol limpio;
3. compila cada imagen con exactamente la compilación de reserva del rol
   (`manage-ax-lab-substrate.py build`, con `sudo` como en el host) y las
   restaura en un `registry:3` desechable en la loopback del runner;
4. exige que cada tag apunte al digest fijado, y resume en el run cada
   digest compilado, sus segundos y su pico de memoria.

No publica nada fuera del runner ni usa credenciales. La prueba de extremo
a extremo en kind es del cambio 5. Su primer run con compilación (run
36187353468, 2026-09-25) reprodujo exactamente los seis digests del
laboratorio manual e imprimió el de `ate-setup`, que el propio cambio 3 fija
en `config/ax-lab.yml` junto con `ax_lab_substrate_fallback_builds:
[ate-setup]`. Con él, el job tiene que salir en verde.

Si un digest del laboratorio manual no se reproduce, el job falla y el
propietario decide antes de la ventana: conservar el del laboratorio
manual, que la semilla copia tal cual, y aceptar que no se reproduce; o
fijar en un cambio revisado el digest de la CI y añadir la imagen a
`ax_lab_substrate_fallback_builds`, porque el laboratorio manual no la
tiene y la CI no publica nada, y quitarla de la orden de la semilla. Si la
copia ya guardaba esa imagen con el digest anterior, `forget` retira antes
esa entrada (ver «Copia de seguridad»).

### Instalación de Substrate

El rol ejecuta `ate-setup` de upstream en modo precompilado
(`cmd/ate-setup/commands.md`, líneas 34 a 71): con `--image-repo` instala
las imágenes que ya están en el registro y nunca invoca ko, go ni git. Los
manifiestos salen del checkout fijado en
`/opt/dockerswarm/ax-lab/src/substrate`, que el rol clona solo si falta y
exige en el commit fijado, con el `origin` fijado y sin cambios, ni siquiera
ficheros sin seguimiento. No usa `git describe`: la versión se pasa
explícita.

`ate-setup` corre desde su propia imagen fijada, no como binario del host:
`docker run` de `localhost:5001/ate-setup@<digest>` con red del host (para
la API en `127.0.0.1:6443` y el registro), 256 MiB sin swap, 0,5 CPU,
256 PIDs, sin capacidades, raíz de solo lectura con un tmpfs de 16 MiB en
`/tmp`, el checkout y el kubeconfig montados de solo lectura y:

```bash
ate-setup --kind --no-dev-env --kubeconfig /kubeconfig --context kind-kind \
  --atenet-router envoy --rollout-timeout 600s \
  --image-repo localhost:5001 --image-tag 67253354 deploy ate-system
```

Corre junto al nodo, así que sus límites caben en los 256 MiB y los 850m
de límites que el plan de capacidad activo deja libres bajo su presupuesto;
el margen operativo de 512 MiB queda aparte (ver
[CAPACITY.md](CAPACITY.md), «Contenedores del host»). El gestor lo acota a
6 480 s: diez esperas que acota `--rollout-timeout` (600 s), 180 s de
esperas fijas y 300 s de margen, para que `ate-setup` informe de su propio
error antes. `deploy ate-system` espera al espacio de nombres (60 s), a dos
CRD (30 s cada una) y al despliegue del controlador de certificados de pod
y sus trust bundles (`cmd/ate-setup/internal/steps/deploy.go`, líneas 55 a
98); `SetupCSI` corre aunque no haya driver (`deploy.go`, línea 99) y, antes
de volver, repite la espera del espacio de nombres, la del controlador y la
de los trust bundles (`csi.go`, líneas 61 a 104); después vienen seis
despliegues (`deploy.go`, líneas 150 a 174). Con `--rollout-timeout`, esa
cota alcanza a todas (`internal/config/config.go`, líneas 343 a 347).
Ansible le concede 300 s más que al gestor.

Solo se ejecuta si hay deriva:

- el fichero de estado `/opt/dockerswarm/ax-lab/state/substrate.json`
  (`root:root 0600`) falta, no dice `installed` o no coincide con lo fijado:
  versión, digests, router, `--rollout-timeout` e ID del contenedor del
  nodo;
- el nodo no tiene la etiqueta `ate.dev/substrate-version`;
- las cargas de `ate-system`, `otel-system` y
  `podcertificate-controller-system`, con las imágenes de sus contenedores
  de inicio y principales, difieren del inventario de
  `substrate.workloads`. Ese inventario sale del código de `ate-setup` y de
  los manifiestos fijados, no del laboratorio manual, que se instaló con el
  script de shell.

Justo antes, el gestor comprueba que el tag de cada imagen instalada sigue
apuntando a su digest: `ate-setup` resuelve cada tag con su propio `HEAD` y
solo admite un `tag@digest` común a todas las imágenes
(`internal/images/prebuilt.go`), así que no puede fijar cada una. Después,
el rol exige que las cargas en marcha usen exactamente las referencias
`localhost:5001/<nombre>:67253354@<digest fijado>`, de modo que un tag
movido en ese intervalo se detecta, aunque no se impide.

### Objetos generados una sola vez

`ate-setup` nunca reconcilia los pools de claves
`ate-system/actor-id-jwt-pool` y `actor-id-ca-pool`, el certificado raíz
derivado `actor-id-ca-certs`, los pools `service-dns-ca-pool` y
`pod-identity-ca-pool` de `podcertificate-controller-system` y el ConfigMap
`ate-api-authentication`
(`cmd/ate-setup/internal/steps/prereqs.go`, líneas 33 a 63, y `create.go`),
pero crea uno nuevo cada vez que falta: `ensureSecret` y `createCAPool`
solo miran si existe (`prereqs.go`, líneas 36 a 48 y 87 a 97; `create.go`,
líneas 204 a 224 y 245 a 270). Son Secrets `Opaque` con la clave `pool`; el
script de shell los creaba como `kubernetes.io/tls`.
`ate-api-server-envvars` se reconcilia en cada ejecución, y
`ate-api-server-secret-envvars` solo lo crea el script de shell. El rol
solo lee su UID y su tipo, nunca su contenido, y nunca deja que `ate-setup`
regenere en silencio una CA viva:

- escribe el fichero de estado con `installing` antes de ejecutar
  `ate-setup`, y con `installed` y los UID de esos objetos después de
  verificarlo todo. Una reinstalación (por deriva de las cargas, de la
  etiqueta o de lo fijado) conserva en `installing` los UID que ya había
  registrado para este nodo; una primera instalación no tiene ninguno;
- se detiene si existen sin un fichero de estado de este mismo nodo: nunca
  los adopta;
- se detiene si falta o tiene otro UID cualquiera de los que registra el
  fichero de este nodo, sea cual sea la fase, y con `installed` también si
  aparece uno que no registró: nadie los rota, y regenerarlos invalidaría lo
  emitido con ellos;
- se detiene si solo existe uno de los dos pools de certificados de pod:
  `deploy ate-system` crearía el que falta junto al que existe
  (`deploy.go`, línea 99; `csi.go`, línea 72; `prereqs.go`, líneas 67 a 77;
  `create.go`, líneas 94 a 103 y 209 a 223), una CA de pod nueva en un
  clúster vivo;
- se detiene si existe `actor-id-ca-certs` sin `actor-id-ca-pool`: la raíz
  solo se deriva cuando falta (`prereqs.go`, líneas 39 a 44), así que
  `ate-setup` crearía un pool nuevo junto a la raíz antigua;
- se detiene si un Secret no es `Opaque`;
- con `installing` del mismo nodo, un apply interrumpido se reanuda
  ejecutando `ate-setup` otra vez, que conserva lo que ya creó. Una primera
  instalación interrumpida no tiene UID registrados que exigir, así que solo
  la protegen las dos reglas anteriores.

Tras `ate-setup`, el rol exige la etiqueta del nodo, el inventario exacto,
los seis objetos presentes con los UID que ya tenían, y el StatefulSet
`postgres` y el Job `rustfs-bucket-init` con el mismo UID y la misma
generación: `ate-setup` los vuelve a aplicar con `force` aunque sus
plantillas son inmutables. Solo entonces registra `installed`. Después, en
todo apply, espera hasta 10 minutos a que cada carga esté lista; si no lo
está, el apply falla, sin reinstalar.

Los pools de CA valen 365 días (`create.go`, línea 49) y nada los rota:
renovarlos es recrear el clúster.

### Contenedores transitorios

El gestor lanza dos clases de contenedores con nombre fijo:
`ax-lab-build-<imagen>` para cada compilación de reserva y
`ax-lab-ate-setup` para la instalación. Los vigila hasta su tiempo máximo y
el suelo de `MemAvailable`, lee su salida y los borra. Si algo falla
mientras corre uno (el daemon no responde a una inspección, el proceso
recibe `SIGTERM`, `SIGHUP` o `Ctrl-C`), el gestor le manda `docker kill`
antes de propagar el error y lo deja parado con sus logs, sin borrarlo.

Ansible ejecuta la compilación y la instalación como tareas asíncronas. Si
se interrumpe el controlador, el lock host-global se libera, pero el gestor
sigue en el host, fuera del lock y sin que ningún preflight de capacidad lo
vea, hasta que termina el contenedor o vence su tiempo máximo: 3 600 s una
compilación, 6 480 s la instalación. Por eso `inspect.yml`, en `--check` y
en el apply y antes de escribir o arrancar nada, lee esos nombres y se
detiene si existe alguno:

- en marcha: queda de un apply cuyo controlador se interrumpió. Hay que
  esperar a que termine, que su propio tiempo máximo acota. El gestor que
  lo lanzó lo borra al acabar si sigue vivo; si no, queda parado, como en
  el caso siguiente. Después se aplica de nuevo;
- parado: se mató tras un error o lo dejó así un gestor que no pudo
  borrarlo. Se revisan sus logs con `sudo -- docker logs <nombre>` y se
  borra con el lock host-global:
  `sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run
  --operation ax-lab-transient -- /usr/bin/docker rm <nombre>`.

El propio gestor repite la comprobación para el nombre que va a usar, con
el mismo mensaje.

### Versión de Substrate y etiqueta del nodo

`67253354` es la versión que `git describe` dio en el laboratorio manual
(`Makefile`, línea 36). El rol no la recalcula: depende de los tags
alcanzables y de la longitud de abreviatura de git. Se pasa como `VERSION`
a la compilación y como tag a `ate-setup`, que la usa como etiqueta del
nodo y como sufijo del DaemonSet `atelet-67253354`. `ate-setup` solo
etiqueta nodos sin etiqueta (`steps/version.go`), así que un nodo con otra
versión detiene el apply. Subir Substrate es un cambio revisado de los
digests, la versión y el inventario, y exige recrear el clúster (ver
«Recrear el clúster»).

### Desviaciones de upstream

- El camino probado de upstream es `hack/install-ate-kind.sh
  --deploy-ate-system` (`README.md` y su CI). Recompila todas las imágenes
  en cada ejecución, y en el laboratorio manual agotó la memoria del host
  cinco veces. `ate-setup`, su alternativa, «does not yet replace the shell
  scripts» y upstream no lo prueba de extremo a extremo; lo hará el cambio 5.
- `ate-setup --kind` aplica el `postgres.yaml` base, no el overlay
  `kind/postgres` del script de shell: el StatefulSet pide 500 GiB y 2 CPU.
  El aprovisionador `local-path` de kind no limita el tamaño y el kubelet ve
  las 8 CPU del host, así que funciona; el límite del contenedor del nodo
  sigue acotándolo.
- La compilación escribe en un layout OCI en lugar de subir la imagen, sin
  SBOM, y el registro guarda los nombres de `--base-import-paths`.
- La reparación de las IP de los workers tras reiniciar el nodo es del
  cambio 4, con la regla de recrear cada pod de worker anterior al arranque
  del nodo.

### Salida a Internet

- `github.com`: el clon de Substrate, solo si falta el checkout.
- `docker.io`: las imágenes de terceros que descarga el nodo (postgres,
  rustfs, aws-cli, envoy, otel, jaeger, prometheus) y, para la reserva, la
  imagen `golang`.
- `gcr.io`: la base distroless, solo en una compilación de reserva.
- `proxy.golang.org`: los módulos de la herramienta ko, solo en una
  compilación de reserva.
- `storage.googleapis.com`: el tarball de gVisor que `atelet` descarga en
  tiempo de ejecución.
- `registry.k8s.io`: la imagen `pause`
  (`manifests/ate-install/sandboxconfig-gvisor.yaml`, línea 30).

### Modo check de Substrate

Con `--check`, el rol lee el registro si está en marcha, la copia de
seguridad y, si el nodo corre con su kubeconfig, Substrate en el clúster,
con las mismas comprobaciones de propiedad que el apply, y termina con un
informe: clonar, compilar, copiar o restaurar cada imagen, e instalar
Substrate y por qué. Usa la copia revisada del gestor desde el controlador,
así que no necesita nada instalado en el host. Con el nodo parado, informa
de que Substrate se lee cuando el apply lo arranque. Con el nodo en marcha
y una imagen que compilar, no promete la compilación: informa de que el
apply se detendrá y de cómo parar el nodo (ver «Compilación de reserva»).
Un contenedor transitorio que quede de otra ejecución lo detiene igual que
al apply (ver «Contenedores transitorios»).

## Capacidad

`config/capacity-profiles.yml` declara el grupo `ax-lab` en
`host_containers` con los límites y reservas de la tabla anterior y 500m y
50m de CPU reservada, que solo cuentan en el presupuesto porque Docker no
reserva CPU para un contenedor suelto. El validador exige que coincidan con
`config/ax-lab.yml`. Solo lo ejecuta el plan activo `organizationweb`, que
queda en 3 100m y 5 666 MiB reservados y 16 650m y 12 141 MiB de límite:
256 MiB por debajo de los 12 397 MiB del presupuesto de memoria. El plan
`observability` no lo incluye, así que exige el laboratorio ausente o
parado (ver [CAPACITY.md](CAPACITY.md), «Contenedores del host»).

Ningún playbook de producción depende de que el laboratorio esté en marcha.
Con el grupo en el plan activo, el preflight de cada playbook acepta los dos
contenedores ausentes o parados: no ejecutan ningún proceso y su
presupuesto sigue reservado en el plan. Uno que exista, en marcha o parado,
tiene que tener exactamente sus límites, o el preflight se detiene: parado
con otros límites, correría fuera de su presupuesto en cuanto alguien lo
arrancara. La única excepción es el propio
`ax-lab`: `scripts/validate-capacity-profiles.py` solo acepta
`--requested-stack ax-lab` si el plan activo ejecuta el grupo `ax-lab`, y
entonces admite además sus contenedores con otros límites, porque es el
playbook que los crea, los arranca y los converge, y los comprueba él mismo
al final. Todo lo demás del preflight sigue igual para todos: los servicios
Swarm, los aparcados, cualquier otro grupo del plan y cualquier estado que
no sea en marcha ni parado (`paused`, `restarting` o `removing`).

Así, tras un reinicio del host o de Docker, o en un host reconstruido, los
despliegues de producción siguen adelante con el laboratorio parado o
ausente, y `ax-lab` lo recupera cuando se quiera (ver «Límites y política de
reinicio»). En cambio, un nodo con otros límites, como el que dejaría un
apply interrumpido entre `kind create cluster` y el `docker update`, detiene
cualquier otro playbook hasta que `ax-lab` lo converge o, si le falta la
prueba de propiedad, hasta que se borra (ver «Recrear el clúster»).

## Por qué este cambio solo se fusiona en la ventana

En cuanto el grupo `ax-lab` entra en el plan activo, el preflight de
capacidad de todos los playbooks lee `kind-control-plane` y `kind-registry`
y exige que, si existen, tengan sus límites exactos, en marcha o parados. El
laboratorio manual no los cumple: el nodo no tiene límite de CPU ni de PIDs
ni reserva de memoria, y el registro no tiene ningún límite. Fusionar este
cambio con el laboratorio manual todavía en el host detendría cualquier
apply salvo `ax-lab`, y el propio `ax-lab` se negaría a tocar un nodo que no
creó. Por eso los cambios 2 y 3 se fusionan juntos, en la ventana en la que
se siembra la copia de Substrate, se retira el laboratorio manual y se
aplica `ax-lab` (ver «Ventana del laboratorio»); el cambio 4 los sigue. Una
vez retirado, su ausencia ya no detiene nada.

## Ventana del laboratorio

En una ventana exclusiva, en este orden:

1. Antes: los cambios 2 y 3 aprobados, con la CI en verde, incluido el job
   de reproducibilidad. El cambio 3 ya fija el digest de `ate-setup` (el
   que imprimió su primer run) y pone `ax_lab_substrate_fallback_builds:
   [ate-setup]`, porque el laboratorio manual no tiene esa imagen.
   Cualquier otra decisión sobre un digest que no se reproduzca también va
   en él (ver «Reproducibilidad»).
2. Suspender las Tasks de AX (paso 1 de «Retirar el laboratorio manual»).
3. Sembrar la copia de seguridad desde el laboratorio manual, todavía en
   marcha (ver «Semilla desde el laboratorio manual»), y comprobar con
   `image-status` que las seis imágenes están `complete`. Sin esto, retirar
   el laboratorio borra las únicas copias de sus imágenes.
4. Retirar el laboratorio manual (ver «Retirar el laboratorio manual»),
   conservando la imagen `golang` fijada, que ahorra una descarga de
   1,3 GB a la compilación de `ate-setup`.
5. Fusionar el cambio 2 y después el 3, y dejar el clon operativo en el
   commit fusionado, limpio.
6. `--check`: debe informar de crear el clúster y el registro, clonar
   Substrate, compilar `ate-setup`, que el registro se lee cuando arranque y
   que Substrate se lee cuando arranque el nodo.
7. El apply real: compila `ate-setup` con el nodo aún ausente, crea el
   clúster y el registro (`kind create cluster` deja el nodo ya en marcha,
   pero nada descarga una imagen de Substrate hasta `ate-setup`), restaura
   las siete imágenes, instala Substrate con `ate-setup` y espera a que esté
   listo. Revisar el JSON de la compilación: segundos, `memory.peak` y
   `oom_kill 0`.
8. Un segundo apply informa `changed=0`.
9. Un cambio revisado devuelve `ax_lab_substrate_fallback_builds` a `[]`.
   Después, el cambio 4.

## Retirar el laboratorio manual

Es una operación destructiva y de una sola vez, en una ventana exclusiva:
nadie más aplica ni opera en el host. Las órdenes que cambian algo (pasos 3
a 6) se ejecutan como operación directa bajo el lock host-global, desde el
checkout revisado y con rutas absolutas:

```bash
sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
  --operation ax-lab-retire -- <orden con ruta absoluta>
```

Nunca se toca `/etc/dockerswarm/ax`: son las credenciales del propietario,
que el laboratorio codificado vuelve a usar.

1. Parar el trabajo de AX. Con la CLI del laboratorio manual
   (`/opt/ax-lab/bin/ax`, con `HOME=/opt/ax-lab/home`), listar las Tasks con
   `ax get tasks` y suspender cada una con `ax suspend task <nombre>` o
   borrarla con `ax delete task <nombre>`. Después, comprobar con
   `ax tunnel list` que no queda ningún túnel y cerrarlos con
   `ax tunnel stop`. Un actor despierto pasa a `CRASHED` unos 60 segundos
   después de perder su worker, así que nada se para con Tasks activas.
2. Guardar la evidencia en un directorio nuevo `root:root 0700` bajo
   `/var/backups/dockerswarm/ax-lab-retirement/<hora UTC>/`, sin copiar
   nunca un secreto:
   - `docker container inspect --format` de los dos contenedores, solo con
     nombre, ID, imagen, estado, etiquetas, límites, publicaciones, redes y
     montajes;
   - `docker volume ls` y el tamaño de los volúmenes del nodo, del registro
     y `ax-lab-gomod`;
   - `kubectl get nodes,pods -A -o wide` con el kubeconfig manual
     (`/opt/ax-lab/home/.kube/config`), nunca `get secrets`;
   - la lista de repositorios del registro (`/v2/_catalog`);
   - copias de `/opt/ax-lab/SPIKE-LOG.txt` y
     `/opt/ax-lab/substrate/bin/kind-config.yaml`.
3. Borrar el clúster manual con el kind v0.33.0 oficial que el playbook ya
   instaló, `/opt/dockerswarm/ax-lab/bin/kind` (sha256 de
   `config/ax-lab.yml`), pero con el `HOME` y el kubeconfig del laboratorio
   manual. Es la misma versión con la que se creó, y borrar solo necesita
   Docker y ese kubeconfig. Antes, guardar en la evidencia la salida de
   `/opt/dockerswarm/ax-lab/bin/kind version`:

   ```bash
   sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
     --operation ax-lab-retire -- \
     /usr/bin/env HOME=/opt/ax-lab/home \
     KUBECONFIG=/opt/ax-lab/home/.kube/config \
     /opt/dockerswarm/ax-lab/bin/kind delete cluster --name kind
   ```

   Solo si ese binario faltara, se usa el del caché de Go del laboratorio
   manual, que `hack/kind.sh` de Substrate ejecuta con `go tool` y que un
   `go clean -cache` puede haber borrado: `sudo -- find
   /opt/ax-lab/home/.cache/go-build -name kind -type f` lo localiza (el
   2026-09-25 estaba en `87/87500ad5…-d/kind` e imprimía
   `kind v0.33.0 go1.27.1 linux/amd64`), y su `kind version` también va a la
   evidencia. kind borra el contenedor con `docker rm -f -v`, que se lleva su
   volumen anónimo de `/var`
   (`pkg/cluster/internal/providers/docker/provider.go`, líneas 136 a 145 en
   v0.33.0).
4. Borrar el registro manual con su volumen anónimo:
   `/usr/bin/docker rm --force --volumes kind-registry`. Comprobar con
   `docker volume ls` que el volumen anotado en la evidencia ya no existe; si
   quedara, borrarlo por ese nombre exacto con `/usr/bin/docker volume rm`.
5. Borrar la red `kind` que dejó el laboratorio manual (borrar el clúster no
   la borra, `hack/create-kind-cluster.sh`, líneas 131 y 132), después de
   comprobar con `docker network inspect kind --format '{{json
   .Containers}}'` que no le queda ningún contenedor:
   `/usr/bin/docker network rm kind`. `kind create cluster` la vuelve a
   crear.
6. Borrar el resto del ensayo: `/opt/ax-lab` entero, la imagen
   `ax-toolbox:spike` y el volumen `ax-lab-gomod`, y revisar con
   `docker image ls` las demás imágenes que el ensayo construyó o descargó
   en el host antes de borrarlas una a una. Se conserva la imagen
   `golang@sha256:3680233e…` de `images.toolbox`, la de la compilación de
   reserva. Antes de este paso y del 3, la copia de Substrate tiene que
   estar sembrada (ver «Ventana del laboratorio»).
7. Comprobar que no queda nada: ni contenedores `kind-control-plane` y
   `kind-registry`, ni red `kind`, ni `/opt/ax-lab`. Después, con los cambios
   2 y 3 fusionados (ver «Ventana del laboratorio»), aplicar `ax-lab` con
   `--check` y de verdad (ver «Aplicación»); el cambio 4 los sigue.

## Recrear el clúster

Cuando cambian la configuración de kind, la imagen del nodo, la versión de
kind o la versión de Substrate, o el rol se detiene porque una prueba de
propiedad ya no coincide (la del nodo o la de los objetos de Substrate), el
clúster se recrea en una ventana exclusiva, suspendiendo antes las Tasks de
AX y con el mismo lock:

```bash
sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
  --operation ax-lab-recreate -- \
  /usr/bin/env HOME=/opt/dockerswarm/ax-lab/home \
  KUBECONFIG=/opt/dockerswarm/ax-lab/home/.kube/config \
  /opt/dockerswarm/ax-lab/bin/kind delete cluster --name kind
```

Después se aplica `ax-lab`: el nodo ya no existe, así que el rol lo crea y
escribe una prueba de propiedad nueva. Lo mismo vale para el nodo que deja
un apply interrumpido durante el propio `kind create cluster` o en la
lectura que lo separa de la escritura de la prueba, o el que deja un
`kind create cluster` que solo falla al exportar el kubeconfig: el nodo
queda sin prueba y el rol se niega a tocarlo. En un primer apply aún no
existe el fichero de estado, así que el rol lo rechaza como un nodo sin
fichero (ver «Prueba de propiedad»), y también se borra con esta orden,
nunca con «Retirar el laboratorio manual».

El registro se conserva. Si lo que cambió es el propio registro (imagen,
publicaciones o red), se borra con el mismo lock y
`/usr/bin/docker rm --force kind-registry`, y el rol lo recrea sobre el
mismo volumen `ax-lab-registry`, al que el siguiente apply restaura desde la
copia de seguridad cualquier imagen que falte. Recrear el clúster pierde
todo lo que corría dentro: el siguiente apply encuentra un nodo con otro ID
y sin objetos de Substrate, así que vuelve a instalarlo con `ate-setup` sin
compilar nada, y tras el cambio 4 reinstala AX. El fichero
`state/substrate.json` del nodo anterior no se borra: se sobrescribe con el
del nuevo.

## Aceptación del nodo privilegiado

`ax_lab_privileged_node_accepted` en `config/ax-lab.yml` es la aceptación
explícita del propietario de que el nodo kind corra con `--privileged` y sin
confinamiento de seccomp ni AppArmor (`provision.go`, líneas 226 a 228), lo
que equivale a root en el host. Mientras valga `false`, el rol se niega a
crear o tocar el clúster y el playbook se detiene antes de escribir nada. El
validador exige un booleano real, con el mismo patrón que
`platform_minecraft_offline_public_accepted` en `config/platform.yml`.

Vale `true` desde el 2026-09-25: el propietario delegó la decisión en todas
las recomendaciones del plan del laboratorio («Si a todo, autorizo todo,
seguiré todas tus recomendaciones»), y el comentario de
`config/ax-lab.yml` lo deja registrado. Acepta un nodo privilegiado, solo en
loopback y nunca publicado.

Volver a `false` no para ni borra el nodo: solo hace que el rol se niegue a
ejecutarse, así que `ax-lab` se detiene antes de escribir nada y el nodo que
ya exista sigue como esté, en marcha o parado. Retirar la aceptación exige
además retirar el laboratorio con el procedimiento de «Si se descarta el
laboratorio» (ver «Verificación»).

## Versiones revisadas

<!-- markdownlint-disable MD013 -->

| Pieza | Versión | Motivo |
| --- | --- | --- |
| AX | `f009cc8` (main, 2026-09-24) | Incluye `e6211f8` (google/ax#390), que no está en ninguna release: v0.3.0 es anterior |
| Agent Substrate | `67253354` (2026-09-11) | El commit que AX fija en su `go.mod` (línea 7) |
| kind | v0.33.0 | El que fija Substrate en `hack/tools/kind/go.mod` (línea 19) |
| Kubernetes | v1.37.0 | La imagen por defecto de kind v0.33.0, con el mismo digest (`pkg/apis/config/defaults/image.go`, línea 21) |
| gVisor | nightly 2026-09-02 | El que fija Substrate en `manifests/ate-install/sandboxconfig-gvisor.yaml` (línea 34) |
| Go | 1.27.1 (`golang@sha256:3680233e…`) | Con el que el laboratorio manual compiló las imágenes; `go.mod` de Substrate pide `go 1.27.0` |
| ko | v0.19.1 | El que fija Substrate en `hack/tools/ko/go.mod` (línea 89), compilado con `go tool` desde su `go.sum` |

<!-- markdownlint-enable MD013 -->

`config/ax-lab.yml` fija además por tag y digest las imágenes upstream que
usa el laboratorio (`kindest/node`, `registry`, `redis`,
`nginx-unprivileged` y `golang`), y las de Substrate por digest (ver
«Imágenes fijadas»). Subir una versión es un cambio revisado de
`config/ax-lab.yml` y de su prueba de contrato, que fija los valores
revisados, nunca una edición en el host.

## Parche local

El laboratorio manual compila AX con `/opt/ax-lab/patches/ax-issue-375.patch`,
basado en los cambios propuestos en google/ax#375. Todavía no está en este
repositorio: se versionará, con su sha256 y su atribución, junto al plano de
control de AX. El parche:

- separa `/readyz`, la sonda de Substrate que abre el egress, de
  `/readyz?check=workspace`, la pregunta del controlador. Sin él, el runner
  espera a preparar el workspace para responder a la sonda que le daría red,
  así que nunca clona ni ejecuta el `goal`;
- añade el proveedor `openai`, para que el `goal` use
  `LocalOpenAIAgentConfig` del SDK de Antigravity con `AX_MODEL_BASE_URL`,
  `AX_MODEL_NAME` y `AX_MODEL_API_KEY`;
- corrige `.dockerignore` para `Dockerfile.task-runner` (google/ax#364).

## Credenciales

Los ficheros viven fuera de Git, bajo `/etc/dockerswarm/ax/`, y los
provisiona el propietario. `config/ax-lab.yml` solo guarda la ruta del
directorio. Ningún playbook lee, copia ni imprime su contenido, y este
documento solo recoge sus metadatos:

<!-- markdownlint-disable MD013 -->

| Ruta | Dueño y modo | Uso |
| --- | --- | --- |
| `/etc/dockerswarm/ax/` | `root:root 0700` | Directorio de credenciales del laboratorio |
| `claude-oauth-token` | `root:root 0600` | Token OAuth de Claude Code (`CLAUDE_CODE_OAUTH_TOKEN`) |
| `codex/` | `root:root 0700` | Configuración de Codex CLI |
| `codex/auth.json`, `codex/config.toml` | `root:root 0600` | Sesión y configuración de Codex CLI |
| `openai-api-key` | `root:root 0600` | Clave del proxy de OpenAI que usa el `goal` de los Workspaces |

<!-- markdownlint-enable MD013 -->

AX solo entrega variables de entorno a una Task: `EnvVar` no tiene
`valueFrom` (google/ax#348). Una credencial pasada así queda en claro en
Redis, en la `ActorTemplate` de Substrate y dentro del sandbox. Por eso la
clave de OpenAI llega a través del proxy y no como variable de la Task. Las
de Claude Code y Codex solo pueden llegar por variables (la imagen de agentes
del laboratorio manual lee la sesión de Codex de `CODEX_AUTH_JSON_B64`), así
que quedan expuestas de esa forma; este repositorio no las codifica.

El kubeconfig de administrador del clúster,
`/opt/dockerswarm/ax-lab/home/.kube/config`, es otra credencial del host:
`root:root 0600`, lo escribe kind y el rol solo comprueba sus metadatos.

## Exposición

Nada del laboratorio se publica:

- `ax-server` no tiene autenticación ni autorización (google/ax#376). No se
  publica nunca: ni ruta de Traefik, ni NodePort, ni LoadBalancer, ni
  Ingress, ni `hostPort`. Solo se alcanza con `kubectl port-forward` desde el
  propio host.
- La API de Kubernetes solo escucha en `127.0.0.1:6443` y el registro local
  en `127.0.0.1:5001` y `[::1]:5001`. El validador rechaza cualquier otra
  dirección, y el rol se detiene si el contenedor vivo publica otra cosa.
- El registro local no tiene autenticación. Desde el host, cualquier
  proceso de cualquier usuario puede subir, sobrescribir o leer imágenes en
  `127.0.0.1:5001` y `[::1]:5001`, y desde el clúster cualquier carga puede
  hacerlo en `kind-registry:5000`, porque está en la red `kind` con el nodo
  y nada filtra el tráfico de los pods (ver «Límites conocidos»). También
  puede llenar el volumen `ax-lab-registry` en el disco del host. Es un
  riesgo aceptado del laboratorio. Desde el cambio 3, cada carga de
  Substrate referencia su imagen por digest (`@sha256:`), el rol exige esas
  referencias exactas y restaura en el registro cualquier tag movido desde
  la copia de seguridad. El cambio 4 lo extiende a toda imagen de AX que el
  clúster descargue de `localhost:5001`, con su validador. Así, sobrescribir
  un tag en el registro no cambia lo que ejecuta el clúster.
- El laboratorio no añade nada a `platform_public_tcp_ports` ni toca el
  firewall del host.

## Aplicación

Solo la ejecuta una persona, desde un checkout limpio de un commit revisado,
con el mismo wrapper y lock que el resto de playbooks:

```bash
./scripts/deploy-ansible.sh --playbook ax-lab --check --ask-become-pass
./scripts/deploy-ansible.sh \
  --playbook ax-lab \
  --confirm-production \
  --ask-become-pass
```

El primer apply, tras retirar el laboratorio manual, crea el clúster y el
registro e instala Substrate; si compila `ate-setup`, puede tardar una hora
(ver «Ventana del laboratorio»). No toca Swarm. Mientras exista el
laboratorio manual, `--check` y el apply se detienen en la prueba de
propiedad, antes de escribir nada. Mientras un digest de Substrate esté
pendiente, los dos se niegan a empezar.

## Verificación

- `sysctl -n fs.inotify.max_user_watches fs.inotify.max_user_instances`
  devuelve `524288` y `512`.
- `/etc/sysctl.d/99-z-dockerswarm-ax-lab.conf` es `root:root 0644` y
  contiene exactamente esas dos claves.
- `sudo -- sha256sum /opt/dockerswarm/ax-lab/bin/kind
  /opt/dockerswarm/ax-lab/bin/kubectl` coincide con `config/ax-lab.yml`.
- `sudo -- sha256sum /opt/dockerswarm/ax-lab/kind-config.yaml` coincide con
  `scripts/validate-ax-lab.py --kind-config-sha256`.
- `sudo -- docker container inspect kind-control-plane kind-registry`
  muestra los límites de la tabla, `RestartPolicy` `no`, la API en
  `127.0.0.1:6443` y el registro en `127.0.0.1:5001` y `[::1]:5001`, solo en
  la red `kind`.
- `/opt/dockerswarm/ax-lab/state/cluster.json` es `root:root 0600` y guarda
  el ID del nodo vivo.
- `kubectl get --raw /readyz` con el kubeconfig del laboratorio devuelve
  `ok` y el nodo está `Ready`.
- `sudo -- docker exec kind-control-plane sysctl -n
  net.ipv4.conf.all.proxy_arp net.ipv6.conf.all.proxy_ndp` devuelve `1` y
  `1`.
- `sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py
  image-status --registry 127.0.0.1:5001 --layout
  /var/backups/dockerswarm/ax-lab/images --tag 67253354` con las siete
  `--image` de `config/ax-lab.yml` dice `pinned` y `complete` en todas.
- `/opt/dockerswarm/ax-lab/state/substrate.json` es `root:root 0600`, dice
  `installed` y guarda el ID del nodo vivo y los UID de los objetos
  generados una sola vez.
- El nodo tiene `ate.dev/substrate-version=67253354`, existe el DaemonSet
  `atelet-67253354` y cada carga de `substrate.workloads` corre con sus
  imágenes exactas y está lista.
- `/opt/dockerswarm/deployments/ax-lab.yml` registra el commit aplicado.
- Un segundo apply informa `changed=0`.

### Si se descarta el laboratorio

Se retira a mano, en una ventana exclusiva y con el mismo lock que en
«Recrear el clúster», y en este orden:

1. Suspender o borrar las Tasks de AX, como en el paso 1 de «Retirar el
   laboratorio manual».
2. Borrar el clúster con la orden de «Recrear el clúster», sin volver a
   aplicar.
3. Borrar el registro y su volumen:
   `/usr/bin/docker rm --force kind-registry` y
   `/usr/bin/docker volume rm ax-lab-registry`.
4. Borrar la red `kind`, que `kind delete cluster` no borra, después de
   comprobar que no le queda ningún contenedor: `/usr/bin/docker network rm
   kind`.
5. Borrar `/etc/sysctl.d/99-z-dockerswarm-ax-lab.conf`,
   `/opt/dockerswarm/ax-lab` (con el checkout, las cachés y el fichero de
   estado de Substrate) y `/var/backups/dockerswarm/ax-lab`, y las imágenes
   que Docker guardó en el host: `localhost:5001/ate-setup@<digest>`, que
   descargó para ejecutarlo, y `golang@<digest>` de `images.toolbox`. Los
   límites de inotify vuelven a sus valores previos en el siguiente
   arranque.
6. Un cambio revisado quita `ax-lab` de la lista `host_containers` del plan
   activo, con su agregado, y devuelve `ax_lab_privileged_node_accepted` a
   `false`. El grupo sigue declarado en `host_containers`, porque
   `scripts/validate-ax-lab.py` lo exige, y el plan activo recupera su
   presupuesto: 2 550m y 3 746 MiB reservados y 14 150m y 8 301 MiB de
   límite.

Mientras tanto, nada se detiene: con el grupo aún en el plan activo, el
preflight acepta sus contenedores ausentes. Al revés, con el grupo ya fuera
del plan y el laboratorio en marcha, cualquier apply se detendría con «host
container outside the active profile is running».

## Límites conocidos

- La salida a Internet de los sandboxes no se filtra en el commit fijado de
  Substrate. El commit posterior `f980a57d` de Substrate, que empieza a
  aplicar la `EgressPolicy`, dice que hasta entonces su manejador de egress
  «let everything through», y en la prueba del ensayo manual del 2026-09-25
  las dos pasarelas dejaron pasar `github.com` y `api.openai.com`. No hay
  aislamiento de salida que prometer; una política de `DOCKER-USER` para la
  red de kind queda para un cambio posterior.
- No hay presupuestos de gasto: AX los retiró
  (`pkg/apis/v1alpha1/ax.proto`, línea 88). `TaskSpec.resources` no se aplica
  (google/ax#369). El límite de gasto real es el de cada proveedor.
- La URL y la rama de git de un workspace no se validan (google/ax#363):
  solo operadores de confianza crean Workspaces.
- El kubelet ve toda la memoria del host, no los 3 584 MiB del contenedor:
  en el laboratorio manual aceptó 4 946 MiB de peticiones. El límite de
  Docker protege el host; solo el laboratorio arriesga un OOM. Ajustar
  `systemReserved` y las peticiones exige recrear el clúster y queda para un
  cambio posterior.
- El nodo corre sin límites desde que kind lo crea hasta el `docker update`
  del mismo apply: durante todo `kind create cluster` (el aprovisionamiento,
  `kubeadm init`, la CNI, el StorageClass y la exportación del kubeconfig) y
  mientras el rol escribe la prueba de propiedad. El rol lo limita antes de
  crear el registro, así que un fallo al crearlo no lo deja sin límites. Si
  el apply se interrumpe dentro de ese intervalo, el preflight de capacidad
  detiene los demás playbooks, pero nada protege el host, que no tiene swap
  (ver [CAPACITY.md](CAPACITY.md)), hasta que `ax-lab` lo converge o, sin
  prueba, hasta que se borra (ver «Límites y política de reinicio»).
- El registro local no tiene autenticación y cualquier proceso del host o
  carga del clúster puede escribir en él (ver «Exposición»). Para Substrate
  lo compensan las referencias por digest y la restauración de cada apply;
  para AX, el cambio 4.
- El laboratorio no guarda estado que haya que conservar: no entra en el
  backup, igual que RacingGame. De un laboratorio perdido, el clúster, el
  registro y Substrate se recrean desde este repositorio; AX, hasta el
  cambio 4, se reinstala a mano.
- La copia de seguridad de las imágenes de Substrate vive en el mismo host.
  En un host reconstruido no existe: las imágenes vuelven con una copia
  llevada a mano al nuevo host y restaurada con `import`, o con la
  compilación de reserva autorizada imagen a imagen, que depende de que
  `gcr.io` siga sirviendo la base distroless fijada y `proxy.golang.org` los
  módulos de ko. El tarball nocturno de gVisor que fija Substrate
  (`sandboxconfig-gvisor.yaml`, líneas 34 y 35) también tiene que seguir en
  `storage.googleapis.com`.
- `ate-setup` es la alternativa de upstream al script de shell, que upstream
  aún no da por sustituido ni prueba de extremo a extremo (ver
  «Desviaciones de upstream»).
- `ate-setup` solo admite un digest común a todas las imágenes, así que un
  tag movido entre la comprobación del gestor y el `HEAD` de `ate-setup` se
  detecta después, en las cargas, pero no se impide (ver «Instalación de
  Substrate»).
- Los pools de CA de Substrate caducan a los 365 días y nada los rota; los
  manifiestos fijados llevan en claro el par de claves S3 estático de
  upstream para rustfs, dentro del clúster y solo en loopback, que el rol
  nunca lee ni registra.
- Un `ate-setup` en marcha ocupa hasta 256 MiB y 0,5 CPU que ningún plan
  declara, dentro de los 256 MiB y 850m de límites que el plan activo deja
  libres bajo su presupuesto; el margen operativo no se toca (ver
  [CAPACITY.md](CAPACITY.md)).
- Si se interrumpe el controlador durante una compilación de reserva o una
  instalación, el contenedor sigue en marcha fuera del lock host-global
  hasta su tiempo máximo (3 600 o 6 480 s), y el preflight de capacidad de
  otro playbook no lo ve; solo el siguiente `ax-lab` se detiene ante él
  (ver «Contenedores transitorios»).
- El nodo del laboratorio manual llegó a un `memory.peak` de 3 494 457 344
  bytes (3,25 GiB), el 93 % de su límite de 3 584 MiB, con Substrate, AX y
  dos workers. Substrate solo no se ha medido con el nodo codificado; el
  cambio 4 tiene que medir el pico con AX antes de dar el límite por bueno.
