# AX: laboratorio de agentes sobre Kubernetes

Despliega [AX](https://github.com/google/ax) (Agent Executor) de Google tal y
como lo plantea upstream: Kubernetes, [Agent
Substrate](https://github.com/agent-substrate/substrate) para los sandboxes y
el plano de control de AX encima. No es un stack Swarm: vive junto a él, en el
mismo Docker Engine, como clúster [kind](https://kind.sigs.k8s.io/). kind es
el entorno de desarrollo del quickstart de Substrate en el commit fijado (su
`README.md`, «Quickstart (Development)»); el otro es GKE.

Es un laboratorio, no un servicio. Substrate declara en ese mismo `README.md`
que «is not ready for production use». Del laboratorio solo se publica el
panel web de `https://ax.apptolast.com`, por decisión del propietario del
2026-09-25 (ver [AX_WEB.md](AX_WEB.md)); `ax-server` no se publica nunca y
nada del laboratorio entra en el backup.

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
- AX sobre Substrate (ver «AX»): sus cuatro imágenes, sembradas byte a byte
  desde el laboratorio manual y fijadas por digest, con su copia y su
  restauración junto a las de Substrate; el parche google/ax#375 versionado
  con su sha256; el plano de control, sin RBAC ni proxy de OpenAI, aplicado
  del lado del servidor solo cuando detecta deriva; el WorkerPool; el
  `--route-timeout` del router; la reparación de los workers tras un
  reinicio del nodo; la CLI `ax` y las dos herramientas del operador,
  `ax` y `ax-tarea`;
- un job de CI que recompila desde el código fijado cada imagen de
  Substrate y exige que su digest coincida con el fijado, y otro que
  recompila de AX la CLI, el binario del runner y las dos imágenes ko (ver
  «Reproducibilidad» y «Reproducibilidad de AX»);
- el contrato `config/ax-lab.yml`, con la aceptación del nodo privilegiado.

Sigue siendo manual, fuera del estado reconstruible:

- la semilla, una sola vez, de las imágenes de AX y de su CLI desde el
  laboratorio manual (ver «Semilla de AX»): las imágenes del runner y de
  agentes no se pueden reconstruir byte a byte (ver «Lo que no se puede
  codificar fielmente»);
- el laboratorio manual entero (`/opt/ax-lab`, su clúster y su registro)
  hasta su retirada, el 2026-09-26, con el procedimiento de «Retirar el
  laboratorio manual»;
- los ficheros de credenciales de `/etc/dockerswarm/ax`, que por diseño
  siempre los provisiona el propietario (ver «Credenciales»).

Cambios previstos, en este orden:

1. Contrato, validador, playbook y prerrequisitos del host (fusionado y
   aplicado el 2026-09-25, ver `DEPLOYMENT_STATUS.md`). Deja inotify
   persistente.
2. El ciclo de vida del clúster y del registro, su declaración de capacidad
   con `capacity_preflight` en el playbook y el procedimiento para retirar
   el laboratorio manual. Solo se fusiona en la ventana del laboratorio (ver
   «Por qué este cambio solo se fusiona en la ventana»). Fusionado (#70) y
   aplicado el 2026-09-26, ver `DEPLOYMENT_STATUS.md`.
3. Las imágenes fijadas de Substrate, su copia de seguridad y su
   restauración, la compilación de reserva, el job de reproducibilidad y la
   instalación con `ate-setup`, que solo se ejecuta cuando detecta deriva.
   Separa el arranque de un nodo parado en `node.yml`, que corre cuando el
   registro ya sirve todas las imágenes. Se fusiona en la misma ventana que
   el cambio 2. Fusionado (#73) y aplicado con él el 2026-09-26.
4. Este cambio: AX sobre Substrate (ver «AX»). Sus imágenes, sembradas y
   fijadas por digest, el parche versionado con su sha256, el plano de
   control sin RBAC ni proxy de OpenAI, el WorkerPool, el router, la
   reparación de los workers, la CLI y las herramientas del operador. Se
   aplica en su propia ventana, después de la de los cambios 2 y 3, con una
   prueba de humo (ver «Ventana del cambio 4»). Fusionado (#76) y aplicado
   el 2026-09-26, ver `DEPLOYMENT_STATUS.md`.
5. La prueba de reinicio del nodo, si no se hizo en esa ventana, el paso del
   laboratorio en `DEPLOYMENT_STATUS.md` a «Aplicado y verificado» y la
   versión final de este documento.

El cambio 3 pone una puerta sobre el registro: sube las imágenes con su
límite de 256 MiB aplicado y exige después `oom_kill 0` en el
`memory.events` de `kind-registry` (ver «Límites y política de reinicio»).
El cambio 4 la repite tras subir las de AX y referencia por digest toda
imagen que el clúster descarga del registro (ver «Exposición»).

Las Tasks de AX se listan y se borran con la CLI codificada:
`sudo ax get tasks -a default` y `sudo ax delete task <nombre> -a default`
(ver «Herramientas del operador»). `ax-tarea` ya borra las suyas al
terminar, así que antes de un reinicio planificado solo quedan las creadas
con `CONSERVAR=1` o a mano; se borran, porque suspender una no la conserva
entera a través de un reinicio (ver «Redis»).

En una ventana exclusiva (ver «Ventana del laboratorio»): sembrar la copia
de seguridad de Substrate y de AX desde el laboratorio manual antes de
retirarlo, retirarlo, fusionar los cambios 2 y 3 y aplicar con `--check` y
después de verdad. El cambio 4 se aplica en otra ventana posterior, con su
prueba de humo; el cambio 5 recoge el resultado en `DEPLOYMENT_STATUS.md`.

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
| `config/ax-lab.yml` | Raíz de instalación, ruta de credenciales, sysctl, commits, parche, binarios, imágenes, clúster, registro, Substrate, AX y aceptación del nodo privilegiado |
| `config/capacity-profiles.yml` | Grupo `ax-lab` de `host_containers`, solo en el plan activo `organizationweb` |
| `scripts/validate-ax-lab.py` | Valida el contrato sin red, lo cruza con la capacidad y renderiza el sysctl, la configuración de kind y los manifiestos de AX; con `--substrate-plan` y `--ax-plan` imprime los planes de los jobs de reproducibilidad, y con `--ax-manifests-sha256` el sha256 de cada manifiesto de AX |
| `scripts/manage-ax-lab-substrate.py` | Lee, copia, respalda y restaura las imágenes de Substrate y, con `--image-set ax`, las de AX; como reserva compila las de Substrate, retira de la copia la entrada de un digest anterior y ejecuta `ate-setup` acotado |
| `.github/workflows/ax-lab-reproducibility.yml` | Recompila las imágenes de Substrate desde el commit fijado y exige sus digests; de AX, recompila la CLI, el binario del runner (con `images.runner_toolbox`, si está fijada) y las imágenes ko y exige lo que se puede probar. Solo compila y compara |
| `.github/workflows/ax-lab-images.yml` | Compila las mismas entradas, exige los pines, prueba que la copia de seguridad puede sembrarse con ellas y sube los dos artefactos que consume la ventana de resiembra (ver «Imágenes compiladas en la CI y ventana de resiembra») |
| `ansible/playbooks/ax-lab.yml` | Lock host-global, `capacity_preflight`, rol `ax_lab` y metadatos de despliegue |
| `ansible/roles/ax_lab/` | Prerrequisitos del host, prueba de propiedad, ciclo de vida del clúster y del registro, imágenes e instalación de Substrate y de AX |
| `ansible/roles/ax_lab/templates/kind-config.yaml.j2` | Configuración de kind |
| `ansible/roles/ax_lab/templates/ax/` | Manifiestos de AX y las herramientas del operador `ax` y `ax-tarea` |
| `images/ax/` | El parche google/ax#375 byte a byte, los manifiestos fijados de las dos imágenes ko y, cuando `config/ax-lab.yml` los fija, los parches de módulos de AX y de Substrate, que solo tocan `go.mod` y `go.sum` |
| `images/ax-task-runner/`, `images/ax-agents/` | Las entradas con las que el laboratorio manual compiló las imágenes del runner y de agentes, como registro de su procedencia |
| `ansible/roles/ax_lab/templates/ax-web.yaml.j2`, `tasks/web*.yml` | El panel web y su reenviador (ver [AX_WEB.md](AX_WEB.md)) |
| `ansible/roles/ax_lab/templates/ejemplos/`, `files/ejemplos/` | Los ejemplos y los clientes remotos del propietario (ver «Ejemplos») |
| `scripts/ax-web-bootstrap.sh` | Arranque de las credenciales del panel y de su Oficina de agentes, que ejecuta el propietario |

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
  no sea `golang:1.27.1` por digest (`golang:1.27.2` una vez fijados los
  parches de módulos); una compilación de reserva con más memoria, reserva,
  CPU o PIDs que el nodo, CPU no entera, sin el suelo de `MemAvailable` y el
  margen operativo de `config/capacity.yml` o con un tiempo fuera de 600 a
  7 200 s; una instalación con otro router que
  `envoy` (ni `agentgateway` ni sdsmint, que no tienen clave), con más
  memoria o CPU de las que el plan de capacidad que ejecuta el laboratorio
  deja libres bajo el presupuesto de límites (192 MiB y 500m hoy), sin el
  suelo de `MemAvailable` o con un tiempo que no cubra las esperas del
  propio `ate-setup`; y un inventario de cargas desordenado, repetido,
  fuera de los tres espacios de nombres, sin el DaemonSet
  `atelet-<versión>`, sin alguna imagen instalada o con referencias upstream
  sin tag y digest;
- en `ax_lab_substrate_fallback_builds`, una imagen desconocida, repetida o
  sin digest que reproducir, y cualquier imagen en cuanto
  `sources.substrate.modules_patch` está fijado: el host nunca parchea ni
  vendoriza Substrate (ver «Compilación de reserva»);
- en los campos de resiembra, todos opcionales (`images.runner_toolbox`,
  `sources.ax.modules_patch` con `path` y `sha256`, y
  `sources.substrate.modules_patch` con `path`, `sha256` y `tree`): un
  parche sin el otro o sin `images.runner_toolbox`; una imagen del runner
  que no sea `golang:1.27.1`; un parche en otra ruta que
  `images/ax/google-ax-modules.patch` (AX) o
  `images/ax/substrate-modules.patch` (Substrate), cuyos bytes no den su
  sha256, que toque algo más que `go.mod` y `go.sum`, que añada una línea
  `replace`, `exclude`, `go`, `toolchain`, `godebug`, `tool`, `ignore` o
  `retract` o cualquier `=>`, que lleve un retorno de carro, un separador de
  línea Unicode o una marca de orden de bytes, que tenga una línea suelta
  entre sus cabeceras `diff --git`, `index`, `---` y `+++` o entre sus
  hunks, o un hunk con más o menos líneas que las que cuenta su cabecera; y
  un `tree` que no sea un id de árbol de git de 40 caracteres hexadecimales
  en minúscula (ver «Imágenes compiladas en la CI y ventana de resiembra»);
- para AX: un parche que no sea `images/ax/google-ax-375.patch` o cuyos
  bytes no den su sha256; otras imágenes que las cuatro revisadas o sin
  `sha256:<64 hex>`; un manifiesto ko versionado cuyos bytes no den el
  digest fijado; un tag que no empiece por los 7 primeros caracteres del
  commit de AX o que diga `latest`; otra copia de la CLI que
  `/var/backups/dockerswarm/ax-lab/binaries/ax`; otro bucket de snapshots
  que `gs://ate-snapshots/ax/`; un volumen de Redis fuera de 256 a
  4 096 MiB o un intervalo de guardado fuera de 10 a 3 600 s; un WorkerPool
  en otro espacio de nombres, con más CPU pedida que su límite, con un
  límite de CPU que no deje 500m del nodo o con más memoria que el límite
  del nodo menos `node_platform_reserve_mib` (que no baja de 1 792 MiB); y
  un `--route-timeout` que no sea de minutos u horas enteros o que pase de
  2 h;
- en los manifiestos de AX renderizados, cualquier objeto distinto del
  inventario revisado (en particular un Secret, un Role, un ClusterRole o
  un Ingress), un Service que no sea ClusterIP o abra un puerto del nodo,
  un pod que monte un token de la API de Kubernetes, use un espacio de
  nombres del host, un `hostPath` o un `hostPort`, o ejecute otra imagen que
  la fijada, una imagen `ko://`, un Redis sin RDB en su volumen, un
  controlador sin su bucket o con otro token proyectado que el de
  Substrate, NetworkPolicies distintas de las revisadas y un WorkerPool
  distinto de `ax.worker_pool`;
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
| `ax-web-edge` | 32 MiB | 16 MiB | ninguna | 0,25 CPU | 64 | `no` |

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
3 y 4 lo comprueban: suben las imágenes con el límite ya aplicado y exigen
después `oom_kill 0` en
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
aplicar `proxy_arp` y `proxy_ndp`, espera a que Substrate esté listo y
recrea los workers de AX anteriores al arranque (ver «Workers tras un
reinicio»). El nodo se arranca en `node.yml`, después del registro y de sus
imágenes, así que un nodo parado no detiene nada de lo anterior. Antes de un
reinicio planificado del host o de Docker se borran las Tasks de AX que
queden (ver «Redis»). Si `ax-lab` se niega a
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
- las imágenes llegan, por este orden, desde la copia de seguridad (sembrada
  desde el laboratorio manual o, con los campos de resiembra fijados, desde
  los artefactos de la CI) y desde el registro local; mientras
  `sources.substrate.modules_patch` no esté fijado, compilarlas en el host es
  una reserva explícita que el propietario autoriza imagen a imagen (ver
  «Compilación de reserva»), y una vez fijado el host no compila ninguna;
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
| `ateom-gvisor` | `sha256:fdd1d0ad…92e7` | Imagen de los workers del WorkerPool (ver «WorkerPool y capacidad dentro del nodo») |
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
sustituye por el de la CI, un nuevo `ate-setup` tras cambiar la imagen de
herramientas o las imágenes que la CI compila con los parches de módulos),
todo `--check` y todo apply se detienen en `image-status` con «names
`<nombre>`:67253354 with another digest» hasta retirar la entrada del digest
anterior. Se retira con el mismo lock, desde el checkout del cambio que fija
el nuevo, nombrando el digest anterior exacto; los blobs se quedan, porque
son direccionables por contenido:

```bash
sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py forget \
  --layout /var/backups/dockerswarm/ax-lab/images \
  --tag 67253354 \
  --image=<nombre>=<digest anterior>
```

Dice `forgotten`, o `absent` si ya no estaba, y se niega si la entrada
nombra otro digest. Después, la imagen nueva llega como cualquier otra que
falte: desde el registro si ya la sirve con su digest o, mientras el host
conserve la reserva (ver «Compilación de reserva»), compilándola. Con los
campos de resiembra fijados el host nunca la compila: el digest nuevo entra
en la copia con `seed-layout`, desde los artefactos de la CI (ver «Imágenes
compiladas en la CI y ventana de resiembra»), y por eso un re-pin pasa
siempre por `forget` más `seed-layout`, en una ventana. El registro puede
seguir sirviendo el digest anterior bajo ese tag; el apply lo trata como un
tag movido y lo restaura desde la copia.

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

Es la reserva de antes de los campos de resiembra, y solo existe mientras
`sources.substrate.modules_patch` no esté fijado. Con ese campo, las
imágenes de Substrate que fija el contrato salen de un árbol parcheado (un
parche que solo toca `go.mod` y `go.sum`) y vendorizado (`go mod vendor`) en
la CI; el host solo tiene el checkout limpio del commit fijado y la imagen
`images.toolbox`, así que nunca podría reproducir esos digests, y
`ate-setup` lee sus manifiestos de ese checkout, que tiene que seguir
limpio. Por eso, una vez fijado el campo, el host nunca parchea ni
vendoriza Substrate, el validador rechaza una
`ax_lab_substrate_fallback_builds` que no esté vacía y re-fijar una imagen
pasa por `forget` más `seed-layout` (ver «Copia de seguridad» e «Imágenes
compiladas en la CI y ventana de resiembra»). Lo que sigue describe la
reserva mientras el campo no está fijado.

Solo compila una imagen que cumpla todo esto:

- está en `ax_lab_substrate_fallback_builds` de `config/ax-lab.yml`, que
  solo cambia en un cambio revisado (el wrapper no admite variables extra) y
  queda vacía fuera de una ventana;
- no está completa en la copia de seguridad ni la sirve el registro;
- el nodo no está en marcha: la compilación corre antes de que
  `cluster.yml` cree o arranque el nodo, y usa su presupuesto (registro
  256 MiB más compilación 3 072 MiB, dentro de los 3 872 MiB del grupo
  `ax-lab`). Si el nodo corre, el apply se detiene: se borran las Tasks de
  AX que queden (ver «Redis») y se para el nodo con el lock host-global,
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
3. si `sources.substrate.modules_patch` está fijado, comprueba su sha256, lo
   aplica con `git apply --index` (solo puede tocar `go.mod` y `go.sum`),
   ejecuta `go mod vendor` en la imagen `images.toolbox` y exige que el
   árbol raíz de git que resulta (`git write-tree`) sea el `tree` fijado. El
   parche y el `vendor/` quedan preparados en el índice y nunca en un
   commit: Go sella `vcs.modified` desde el árbol preparado y un commit
   añadiría una revisión que no se reproduce. El host no hace nada de esto
   (ver «Compilación de reserva»);
4. compila cada imagen con exactamente la compilación de reserva del rol
   (`manage-ax-lab-substrate.py build`, con `sudo` como en el host) y las
   restaura en un `registry:3` desechable en la loopback del runner;
5. exige que cada tag apunte al digest fijado, y resume en el run cada
   digest compilado, sus segundos y su pico de memoria.

Este workflow solo compila y compara: no publica nada fuera del runner, no
sube artefactos y no usa credenciales. Los artefactos que consume la ventana
de resiembra los sube su hermano `ax-lab-images.yml` (ver «Imágenes
compiladas en la CI y ventana de resiembra»). La prueba de extremo
a extremo en kind es del cambio 5. Su primer run con compilación (run
36187353468, 2026-09-25) reprodujo exactamente los seis digests del
laboratorio manual e imprimió el de `ate-setup`, que el propio cambio 3 fija
en `config/ax-lab.yml` junto con `ax_lab_substrate_fallback_builds:
[ate-setup]`. Con él, el job tiene que salir en verde. La ventana del
2026-09-26 compiló `ate-setup` en el host con ese mismo digest, y la lista
volvió a quedar vacía.

Si un digest del laboratorio manual no se reproduce, el job falla y el
propietario decide antes de la ventana: conservar el del laboratorio
manual, que la semilla copia tal cual, y aceptar que no se reproduce; o
fijar en un cambio revisado el digest de la CI y añadir la imagen a
`ax_lab_substrate_fallback_builds`, porque el laboratorio manual no la
tiene y la CI no publica nada, y quitarla de la orden de la semilla. Si la
copia ya guardaba esa imagen con el digest anterior, `forget` retira antes
esa entrada (ver «Copia de seguridad»).

Eso vale mientras `sources.substrate.modules_patch` no esté fijado. Con él
fijado, ninguna imagen de Substrate se compila en el host y el validador no
admite otra cosa que `ax_lab_substrate_fallback_builds: []`: el digest que no
coincide es siempre el de la CI, se corrige en un cambio revisado con el que
imprime el run, y la copia lo recibe con `forget` más `seed-layout` desde los
artefactos de `ax-lab-images.yml`, nunca compilándolo en el host (ver
«Imágenes compiladas en la CI y ventana de resiembra»).

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
la API en `127.0.0.1:6443` y el registro), 192 MiB sin swap (96 MiB
reservados), 0,5 CPU, 256 PIDs, sin capacidades, raíz de solo lectura con un
tmpfs de 16 MiB en `/tmp`, el checkout y el kubeconfig montados de solo
lectura y:

```bash
ate-setup --kind --no-dev-env --kubeconfig /kubeconfig --context kind-kind \
  --atenet-router envoy --rollout-timeout 600s \
  --image-repo localhost:5001 --image-tag 67253354 deploy ate-system
```

Corre junto al nodo, así que sus límites caben en los 192 MiB y los 500m
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

En este host su consumo real no se ha medido nunca. La única instalación
real, la del 2026-09-26, corrió con 256 MiB y no se conservó su resultado: un
`ate-setup` correcto no imprime nada en el apply, y solo el que falla deja
a la vista el JSON del gestor (ver «Criterios de aborto»). El tope bajó a
224 MiB ese mismo día y a 192 MiB el 2026-10-08, y desde entonces no ha
corrido ninguna instalación aquí. Sí se midió, el 2026-10-12, en una
ejecución desechable de la CI en un clúster kind con los pines de la
resiembra y estos mismos límites: el peor pico fue de 56,0 MiB, el 29,2 % del
tope, sin OOM (el detalle, lo que prueba y lo que no, está en el paso 0 de
«Precondiciones de la ventana de resiembra»).

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

Lo que sí borra el gestor, porque lee su estado y su salida antes de
informar, es un contenedor que el kernel mató por memoria (`oom_killed`) o
que venció su tiempo: ese fallo no deja contenedor, solo el marker del
wrapper y el estado `installing` (ver «Criterios de aborto»).

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
- La reparación de las IP de los workers tras reiniciar el nodo está en
  «Workers tras un reinicio».

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

## AX

AX se instala en el commit fijado en `sources.ax` (`f009cc8…`) con el parche
de google/ax#375 (ver «Parche local»), sobre el Substrate de la sección
anterior. Las mismas tres reglas que para Substrate, más una:

- un apply normal del host nunca compila AX: sus imágenes y su CLI llegan
  sembradas, la primera vez desde el laboratorio manual (ver «Semilla de
  AX») y en un re-pin desde los artefactos de la CI (ver «Imágenes
  compiladas en la CI y ventana de resiembra»), y el rol solo las copia, las
  restaura y las verifica por digest;
- no hay compilación de reserva de AX: una imagen que falte en la copia y
  en el registro detiene el apply;
- el plano de control y el WorkerPool se aplican del lado del servidor y
  solo cuando `kubectl diff --server-side` o el fichero de estado detectan
  deriva (ver «Plano de control de AX»);
- ningún pod de AX recibe un token de la API de Kubernetes ni permiso RBAC
  alguno, y nada de AX se publica (ver «Exposición»).

### Imágenes de AX

`config/ax-lab.yml` fija en `ax.images` el digest del manifiesto de una
sola plataforma (`linux/amd64`) de cada imagen, y el registro local las
guarda como `<nombre>:f009cc8-issue375` (`ax.tag`):

<!-- markdownlint-disable MD013 -->

| Imagen | Digest | Origen en el laboratorio manual | Uso |
| --- | --- | --- | --- |
| `ax-controller` | `sha256:2a744737…51d2` | ko, `ax-controller-7ebf6094…` | Deployment `ax-controller` |
| `ax-server` | `sha256:621ce24e…67f1` | ko, `ax-server-340c3583…` | Deployment `ax-server` |
| `ax-task-runner` | `sha256:a5f9ee65…0bd4` | buildx, índice `5d536baa…` | Base de la imagen de agentes |
| `ax-agents` | `sha256:d136aebb…2682` | buildx, índice `f82e9848…` | Imagen de cada Task de `ax-tarea` |

<!-- markdownlint-enable MD013 -->

Los dos primeros son exactamente los manifiestos que ko subió, y sus bytes
están en `images/ax/manifests/`: el validador exige que den el digest
fijado. De los otros dos, el laboratorio manual subió un índice de buildx
con dos entradas: el manifiesto `linux/amd64` y una atestación de
procedencia (`vnd.docker.reference.type: attestation-manifest`). El gestor
solo copia manifiestos de una plataforma, así que se fija ese manifiesto y
no el índice; Substrate solo exige que la imagen de una Task lleve `@`
(`cmd/ateapi/internal/controlapi/actor_template.go`, líneas 271 a 278, en
el commit fijado), y `ax-tarea` la referencia por ese digest. El manifiesto
es el mismo, byte a byte; la atestación se queda en la evidencia de la
retirada.

Como las de Substrate, se mueven solo por la API HTTP del registro,
verificando cada blob, y comparten la copia de seguridad de
`/var/backups/dockerswarm/ax-lab/images` (unos 480 MB más: la de agentes
ocupa 432 MiB, con una capa de 241 MiB, y comparte sus ocho primeras capas
con la del runner). En cada apply, `ax_images.yml`
copia a la copia las que le falten, restaura en el registro las que falten
o cuyo tag apunte a otro manifiesto, las comprueba todas y vuelve a exigir
`oom_kill 0` en el `memory.events` de `kind-registry`, porque la capa de
241 MiB se sube con el límite de 256 MiB aplicado. Todo antes de arrancar
el nodo.

La CLI `ax` es un binario del host, no una imagen: se guarda, una sola
vez, en `/var/backups/dockerswarm/ax-lab/binaries/ax` (`root:root 0600`, en
un directorio `0700`), y el rol la instala en
`/opt/dockerswarm/ax-lab/bin/ax` (`root:root 0755`) solo si su sha256
difiere, después de exigir que la copia tenga el sha256 fijado
(`ax.cli_sha256`). Está enlazada dinámicamente (`CGO_ENABLED=1`, `Makefile`
de AX, línea 36) contra la glibc de la imagen de herramientas con la que se
compila (la 2.41 en `golang:1.27.1`); el host tiene la 2.43, y
`ax-lab-images.yml` falla si la CLI que sube necesita una glibc más nueva.

### Semilla de AX

Se siembra en la ventana de los cambios 2 y 3, con el laboratorio manual
todavía en marcha (ver «Ventana del laboratorio»): son copias inofensivas,
y la retirada borra las únicas fuentes. Como la semilla precede a la fusión
del cambio 4, se ejecuta desde un clon limpio en su commit aprobado (en el
clon operativo, `git checkout --detach <commit>`), que es el que conoce
`--image-set ax`. `--source-naming ko-md5` lee las imágenes de ko del
repositorio con el md5 de su ruta de importación en `github.com/google/ax`;
las de buildx se leen por su nombre:

<!-- markdownlint-disable MD013 -->

```bash
sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py export \
  --image-set ax \
  --registry 127.0.0.1:5001 \
  --layout /var/backups/dockerswarm/ax-lab/images \
  --tag f009cc8-issue375 \
  --source-naming ko-md5 \
  --image=ax-controller=sha256:2a744737051c6e877213e25f49ff2408b3399e8f3176f4e203df9c515fc351d2 \
  --image=ax-server=sha256:621ce24e8887a6fcbf4d4b31da1dc005e0c54210bc28275a912640b97c2667f1
sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py export \
  --image-set ax \
  --registry 127.0.0.1:5001 \
  --layout /var/backups/dockerswarm/ax-lab/images \
  --tag f009cc8-issue375 \
  --image=ax-task-runner=sha256:a5f9ee65df155af434ecf06cb79be4beb7d09b2621d05b38730c682925220bd4 \
  --image=ax-agents=sha256:d136aebb3f8393e4c994ecd7e4dd4296d5eea7b38e2a32d8087776dae9bb2682
sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
  --operation ax-lab-seed -- \
  /usr/bin/install -d -o root -g root -m 0700 \
  /var/backups/dockerswarm/ax-lab/binaries
sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
  --operation ax-lab-seed -- \
  /usr/bin/install -o root -g root -m 0600 /opt/ax-lab/bin/ax \
  /var/backups/dockerswarm/ax-lab/binaries/ax
sudo -- sha256sum /var/backups/dockerswarm/ax-lab/binaries/ax
```

<!-- markdownlint-enable MD013 -->

La salida de cada `export` dice `exported` para cada imagen, y
`image-status` con `--image-set ax`, el mismo `--layout`, el mismo `--tag` y
las cuatro `--image` tiene que decir `complete` para las cuatro. El
`sha256sum` de la CLI tiene que ser
`acd1f36d86557a697c49956110c9f80ba260db2cefb1a6497b166fa43f52ebb7`, el de
`ax.cli_sha256`. Solo entonces se sigue con la retirada.

Esa semilla es la del laboratorio manual y ya está hecha. Un re-pin posterior
de las imágenes de ko o de la CLI no vuelve a usar `export`, porque el
laboratorio manual ya no existe: se siembra con `seed-layout` desde los
artefactos de la CI, tras retirar con `forget` las entradas anteriores (ver
«Imágenes compiladas en la CI y ventana de resiembra»).

### Reproducibilidad de AX

El job `reproduce-ax` de `.github/workflows/ax-lab-reproducibility.yml`
prueba en la CI pública lo que se puede probar de esas pins, sin publicar
nada ni usar credenciales:

1. lee de `validate-ax-lab.py --ax-plan` el commit de AX, el parche (y el
   de módulos, si está fijado), el de Substrate, la imagen de herramientas
   (`golang:1.27.1` mientras no haya parches de módulos), la del runner
   (`images.runner_toolbox`, o la de herramientas si no está fijada) y la
   base de ko;
2. clona google/ax con todos sus tags (el binario lleva la
   pseudo-versión `v0.3.1-0.20260924134026-f009cc81c9a5+dirty`, que Go
   deriva del tag `v0.3.0`) y exige ese commit, el árbol limpio y
   `git describe --tags --abbrev=0` igual a `v0.3.0`;
3. ejecuta `go test ./...` en el commit sin parche como línea base;
4. comprueba el sha256 del parche, lo aplica con `git apply --index` (sin
   commit: los binarios del laboratorio manual llevan `vcs.modified=true`)
   y exige que `git diff --cached` dé ese mismo sha256;
5. recompila el binario del runner y la CLI con las opciones exactas del
   `Makefile` (líneas 49 y 36) y exige sus sha256
   (`ax.task_runner_binary_sha256` y `ax.cli_sha256`). El runner va primero,
   con solo el primer parche y en la imagen `images.runner_toolbox`
   (`golang:1.27.1`), porque su pin describe el binario que llevan dentro las
   imágenes sembradas, y Go incrusta en él la versión de cada módulo: con
   otro toolchain o tras la subida de módulos no se reproduciría (ver «Lo que
   no se puede codificar fielmente»). Después, si `sources.ax.modules_patch`
   está fijado, comprueba su sha256 y lo aplica encima con
   `git apply --index` (solo `go.mod` y `go.sum`, sin commit), y la CLI se
   compila con él en la imagen `images.toolbox`;
6. recompila con ko v0.19.1 (el de `hack/tools/ko/go.mod` de Substrate,
   línea 89) `ax-controller` y `ax-server`, con ambos parches, con la base
   `cgr.dev/chainguard/static@sha256:41e17ed8…` (`ax.ko_base_image`), y
   exige que cada manifiesto sea igual al fijado de `images/ax/manifests`
   salvo la anotación `org.opencontainers.image.base.name`, que ko escribe
   con la referencia de la base: `…:latest` en el laboratorio manual,
   `…@sha256:…` en la CI. La configuración y las capas son las mismas. Con
   `sources.ax.modules_patch` fijado exige además el digest exacto de cada
   imagen;
7. vuelve a ejecutar `go test ./...` con el parche y falla si aparece un
   paquete que no fallaba ya sin él.

### Lo que no se puede codificar fielmente

- Las imágenes del runner y de agentes. Se compilaron con buildx a partir
  de `python:3.12-slim` y `node:24-trixie-slim` por digest, pero con
  paquetes de apt de los espejos vivos (versiones `+deb13uN` que
  desaparecen), `google-antigravity==0.1.18` sin hashes de sus 44
  dependencias, `npm ci` (con integridad en el lock, pero con el script de
  instalación de Claude Code), fechas de fichero de la compilación y la
  atestación de procedencia. Recompilarlas da otros digests: solo se
  siembran. Sus entradas exactas están en `images/ax-task-runner/` y
  `images/ax-agents/` como registro de su procedencia (con sus sha256 en
  `tests/test_ax_lab_ax_contract.py`), y reconstruirlas sería un cambio
  revisado con pins nuevos. Lo que sí se prueba es el binario del runner
  que llevan dentro (paso 5 de «Reproducibilidad de AX»). Rama K: la
  resiembra con los parches de módulos no las toca. `ax-task-runner` y
  `ax-agents` se quedan sembradas tal como están, con Go 1.27.1 y con los
  módulos de antes de la subida, así que los hallazgos de `govulncheck` de
  su binario (la biblioteca estándar y `golang.org/x/net`) siguen abiertos
  (ver «Imágenes compiladas en la CI y ventana de resiembra»).
- La copia de las imágenes vive en el mismo host: perder el host es
  perderlas, igual que las de Substrate (ver «Límites conocidos»).
- La prueba de ko depende de que `cgr.dev` siga sirviendo el digest de la
  base; si deja de hacerlo, la prueba se queda en los binarios y las copias
  sembradas siguen siendo la referencia.
- La sesión de Codex es estado: su refresh token es de un solo uso (ver
  «Herramientas del operador»).
- Las credenciales de las Tasks quedan en reposo en Redis, en las
  `ActorTemplate` y en los snapshots, porque AX no tiene referencias a
  secretos (google/ax#348; ver «Credenciales»).
- Los túneles de la CLI (google/ax#376) se contienen con los scopes de las
  herramientas, pero no se arreglan; la reparación de los workers es un
  rodeo; y `TaskSpec.resources` no se aplica (google/ax#369).

### Plano de control de AX

`ansible/roles/ax_lab/templates/ax/ax-system.yaml.j2` es
`deploy/redis.yaml`, `deploy/ax-server.yaml` y `deploy/ax-controller.yaml`
de upstream en el commit fijado con estas desviaciones, y ninguna más (el
validador y `tests/test_ax_lab_ax_contract.py` las fijan campo a campo):

- imágenes por tag y digest fijado en lugar de las rutas `ko://`;
- sin el ClusterRole ni el ClusterRoleBinding de upstream, que conceden al
  controlador `get`, `list` y `watch` de todos los Secrets del clúster
  (`deploy/ax-controller.yaml`, líneas 26 a 46): su única llamada a la API
  de Kubernetes lee el Secret `gemini-api-secret`, que este laboratorio no
  crea, y sin token se queda en cadena vacía;
- `automountServiceAccountToken: false` en la ServiceAccount del
  controlador y en los tres pods. El controlador conserva su token
  proyectado para Substrate (audiencia `api.ate-system.svc`, 7 200 s), que
  es otro volumen;
- `AX_SNAPSHOTS_BUCKET=gs://ate-snapshots/ax/`, el bucket de rustfs de
  Substrate, que el laboratorio manual puso con `kubectl set env`;
- Redis con un volumen y solo snapshots RDB (ver «Redis»);
- `type: ClusterIP` escrito en los dos Services, aunque es el valor por
  defecto: así su dueño es el gestor de campos `ax-lab` y el diff del
  servidor ve que otra mano los publique. `kubectl apply --server-side` deja
  en paz un campo del que no es dueño, así que un NodePort puesto con
  `kubectl patch` sobre un tipo implícito no sería deriva;
- dos NetworkPolicies de entrada en `ax-system` (ver «Exposición»);
- las cantidades de CPU en forma canónica (`"1"` en lugar de `1000m`), para
  que el servidor no vea una diferencia que no hay.

`templates/ax/ax-workers.yaml.j2` añade el espacio de nombres `ax-workers`
y el WorkerPool (ver «WorkerPool y capacidad dentro del nodo»). Los dos
espacios de nombres llevan `com.apptolast.managed-by: ansible`.

El validador renderiza las dos plantillas como el lookup `template` de
Ansible e imprime el sha256 de cada una con `--ax-manifests-sha256`; el rol
las renderiza en el controlador y exige esos mismos sha256 antes de
escribir nada. Nunca se escriben en el host: van por la entrada estándar a
`kubectl`.

`ax_read.yml` compara lo fijado con el clúster con
`kubectl diff --server-side --field-manager ax-lab --force-conflicts`: el
código 0 es que no hay diferencia, el 1 que la hay y cualquier otro, error.
Si falta un espacio de nombres no hay diff posible y cuenta como deriva.
Solo con deriva, `ax.yml`:

1. escribe `installing` en `/opt/dockerswarm/ax-lab/state/ax.json`;
2. aplica `ax-system.yaml` con `kubectl apply --server-side` y el mismo
   gestor de campos, espera el despliegue de `ax-redis`, `ax-server` y
   `ax-controller` y exige que cada pod de `ax-controller` que haya
   arrancado este apply (uno cuyo UID no existía antes y que no se está
   borrando) haya escrito «starting AX task worker»
   (`internal/controller/worker.go`, línea 68): no tiene sonda de
   preparación y los eventos publicados antes de suscribirse se pierden
   (google/ax#354). Lo escribe una vez, al arrancar, así que se leen los
   primeros 64 KiB del log de ese pod, que no se muestra. Un controlador
   que ya corría, como con una deriva solo de `state` o de
   `ax-workers.yaml`, no se vuelve a leer: #354 solo afecta a lo que se
   publica justo tras un arranque, y su log puede haber crecido más allá de
   cualquier lectura acotada;
3. aplica `ax-workers.yaml`.

Siempre, haya deriva o no: el `--route-timeout` del router (ver «Router»),
la reparación de los workers (ver «Workers tras un reinicio») y la espera a
que el WorkerPool esté listo. Después vuelve a leerlo todo y exige que no
quede diferencia, que el router tenga su argumento, que no quede ningún
worker viejo, que los espacios de nombres que ya existían sean los mismos
objetos y que el nodo nunca haya llegado a su límite de memoria (ver
«WorkerPool y capacidad dentro del nodo»); solo entonces escribe
`installed` con los UID de los dos espacios de nombres. Un segundo apply
informa `changed=0`. Ninguna reinstalación de Substrate ni ningún cambio de
AX corre mientras un `ax-tarea` o una orden `ax` estén en marcha (sus
scopes `ax-tarea-*` y `ax-cli-*`): `ate-setup` reinicia el router, que
cortaría el `ax ssh` de la tarea. El apply se detiene antes y hay que
repetirlo cuando terminen. Arrancar un nodo parado o volver a poner
`proxy_arp` no lo impide: con el nodo parado no corre ninguna tarea.

Todo `kubectl` del rol usa el kubeconfig y el `HOME` del laboratorio,
`--context kind-kind` y `--request-timeout 10s`; las esperas son sondeos
acotados, nunca un `watch`.

### Propiedad de AX

Como con Substrate, los espacios de nombres de AX nunca se adoptan:

- si `ax-system` o `ax-workers` existen sin un fichero de estado de este
  mismo nodo (el ID del contenedor), el rol se detiene. Así se detiene ante
  los del laboratorio manual, que se borran con él;
- si el fichero de este nodo registró un UID y el espacio de nombres vivo
  tiene otro, alguien lo recreó a mano, y el rol se detiene;
- si falta uno que el fichero registró, se vuelve a crear: no guarda nada
  que no se pueda recrear;
- `installing` del mismo nodo se reanuda: el apply vuelve a aplicar.

Para retirar AX o recrear sus espacios de nombres, con el mismo lock:
`/opt/dockerswarm/ax-lab/bin/kubectl delete namespace ax-workers ax-system`
con el kubeconfig del laboratorio, y borrar
`/opt/dockerswarm/ax-lab/state/ax.json`.

### Redis

Upstream despliega Redis sin volumen (`deploy/redis.yaml`, líneas 38 a
50), así que el reinicio del nodo del 2026-09-25 borró las cuatro Tasks del
laboratorio manual mientras Substrate conservaba sus plantillas, sus
actores y sus snapshots, sin nadie que los borrara. El rol le da un
PersistentVolumeClaim `ax-redis-data` de 1 024 MiB en la StorageClass
`standard` de kind (`local-path`, en el volumen anónimo `/var` del nodo),
estrategia `Recreate` y `--save 60 1 --appendonly no`:

- solo RDB, no AOF: AOF conserva cada `SET`, token incluido, hasta que se
  reescribe, y con estos tamaños casi nunca se reescribiría. Con RDB, una
  Task borrada desaparece del disco en el siguiente guardado, en 60 s;
- tras un reinicio, las Tasks que existían siguen ahí y borrarlas borra de
  verdad su actor y sus plantillas. Una Task que estaba en marcha muestra
  un estado viejo, porque el controlador solo reacciona a eventos: se
  borra;
- el guardado puede perder hasta 60 s, y un evento entregado pero no
  confirmado en el momento del reinicio se pierde (cada arranque crea un
  consumidor nuevo que solo lee eventos nuevos,
  `internal/store/redis/store.go`, líneas 767 a 775);
- el volumen se pierde al recrear el clúster, como todo lo demás.

`ax-tarea` borra sus Tasks al terminar, así que antes de un reinicio
planificado del host o de Docker solo quedan las de `CONSERVAR=1` o las
creadas a mano: se listan con `sudo ax get tasks -a default` y se borran
con `sudo ax delete task <nombre> -a default`.

### WorkerPool y capacidad dentro del nodo

AX no crea WorkerPools (sus `ActorTemplate` no llevan selector de worker,
google/ax#368): el laboratorio declara uno, `ax-workers/ax`, con
`ax.worker_pool`:

- 1 réplica con 1 536 MiB pedidos y de límite y 250m a 1 500m de CPU, la
  imagen `ateom-gvisor` fijada de Substrate, `sandboxClass: gvisor` y el
  selector de nodo `ate.dev/substrate-version: "67253354"`;
- una Task a la vez: `ax-tarea` toma un lock y el único worker solo tiene
  sitio para un actor. El actor dorado que Substrate crea para cada
  plantilla también corre en un worker del pool y se suspende antes de que
  la plantilla esté lista
  (`cmd/ateapi/internal/controlapi/template_reconciler.go` de Substrate,
  líneas 183 a 189 y 240 a 245), así que basta uno.

El kubelet ve toda la memoria del host, no los 3 584 MiB del contenedor, y
nada dentro del nodo impide que los límites de los workers pasen del límite
del nodo. El validador exige por eso:

- `réplicas × memoria ≤ 3 584 − node_platform_reserve_mib`, con la reserva
  de la plataforma en 1 792 MiB, que el validador no deja bajar: el
  2026-09-25 el nodo medía 1 214 MiB de memoria anónima y 82 MiB de kernel,
  más la caché caliente de etcd, los binarios y las imágenes. Dos workers de
  1 536 MiB, como en el laboratorio manual, se pasarían en 1 280 MiB. Dentro
  de esa reserva corre también el panel web, con 384 MiB de límite (ver
  [AX_WEB.md](AX_WEB.md), «Capacidad»);
- `réplicas × límite de CPU ≤ límite de CPU del nodo − 500m`: con el nodo
  en 2 CPU, los workers juntos nunca dejan sin CPU a etcd, la API y
  Substrate. Un worker tiene hasta 1 500m; dos, 750m cada uno.

Tras cada apply, el rol lee
`/sys/fs/cgroup/system.slice/docker-<ID>.scope/memory.events.local` del
nodo y exige `oom 0`: el nodo nunca se quedó sin memoria en su propio
límite. Según `memory.events` en la documentación de cgroup v2 del kernel,
`oom` cuenta las veces que una reserva iba a fallar en ese límite; llegar a
él y recuperar memoria, por ejemplo caché, cuenta en `max`, que no se
exige. El fichero local no cuenta a un worker que llegue al suyo, que es lo
esperado.

### Router

`atenet-router` corta a los 10 s cada petición de la ruta de un actor
(`defaultRouteTimeout`, `cmd/atenet/internal/router/xds.go`, línea 143, en
el Substrate fijado), y una sesión de `ax ssh` con un agente dura mucho más:
en el laboratorio manual, Codex agotó ese tiempo. El rol añade
`--route-timeout=1h` (`ax.router_route_timeout`,
`cmd/atenet/internal/router/cmd.go`, línea 73) a los argumentos del
contenedor `atenet-router` con un parche JSON cuyas
operaciones `test` fallan si el contenedor o el argumento ya no están donde
se leyeron, y espera su despliegue. `ate-setup` es dueño de esa lista y la
reescribe en cada reinstalación de Substrate, así que el apply siguiente la
vuelve a corregir. Más de un `--route-timeout` detiene el apply: solo lo
pone una edición a mano. Upstream avisa de que el drenaje no crece con él
(`manifests/ate-install/atenet-router.yaml`, líneas 176 a 183): un turno
largo no sobrevive a un reinicio del router.

### Workers tras un reinicio

Tras un reinicio del nodo, Substrate conserva la IP antigua de cada worker:
un Worker se llama como el UID de su pod y su IP no cambia nunca
(`cmd/atecontroller/internal/workersync/syncer.go`, línea 67 y líneas 302
a 308), pero el pod vuelve con otra. Una Task en ese worker arranca y sale
a Internet, pero nadie entra en ella: su espacio de trabajo nunca pasa a
listo y `ax ssh` falla. El 2026-09-25 el laboratorio manual estuvo así unas
cinco horas, con 124 avisos «registered worker IP disagrees with its pod»
del `ate-controller` y ninguna alerta.

La regla, en `ax_read.yml` y `workers.yml`: todo pod con la etiqueta
`ate.dev/worker-pool`, en cualquier espacio de nombres, cuyo
`creationTimestamp` sea anterior al `State.StartedAt` del contenedor del
nodo se borra, y su WorkerPool crea otro. Docker da `StartedAt` con
nanosegundos y Kubernetes el `creationTimestamp` en segundos enteros, así
que un pod creado en el mismo segundo del arranque se conserva: no puede
existir antes de la API. `--check` informa de cuántos se recrearían; el
apply los borra y espera hasta que no quede ninguno y el WorkerPool esté
listo. Solo ocurre tras un reinicio del nodo, cuando todo actor de esos
workers ya murió. `proxy_arp` y `proxy_ndp`, la otra mitad de la
reparación, son de `node.yml`.

### Herramientas del operador

El rol instala dos órdenes, como `root:root 0750` y comprobadas con
`sh -n`, y se niega a escribir sobre un enlace simbólico (el laboratorio
manual deja `/usr/local/sbin/ax-tarea` como enlace, así que la retirada lo
borra). Las dos se ejecutan solo como root.

`sudo ax <argumentos>` (`/usr/local/sbin/ax`) es la CLI de AX con el
kubeconfig, el `HOME` y el `PATH` del laboratorio. La CLI de google/ax deja
vivos sus port-forward a `ax-server`, que no tiene autenticación
(google/ax#376), y al router, en la loopback y reutilizables por cualquier
usuario local, y los recuerda por PID en `$AX_HOME/tunnels`. La orden corre
cada ejecución en un scope de systemd propio (`ax-cli-<pid>-<hora>`) con un
`AX_HOME` temporal bajo `/run`, para el scope al terminar y borra ese
directorio: no queda ningún túnel abierto. También con `Ctrl+C`, un `kill` o
al cerrarse la terminal: el port-forward de la CLI va en su propio grupo de
procesos (`internal/tunnel/tunnel.go`, líneas 241 y 326) y no recibe la
señal, así que las trampas de `INT`, `TERM` y `HUP` esperan a que la CLI
termine, paran el scope y salen con 130, 143 o 129.

`sudo ax-tarea <repo> "<instrucción>" [claude|codex]`
(`/usr/local/sbin/ax-tarea`) crea un Workspace con el repositorio y una
Task con la imagen `ax-agents` fijada, ejecuta el agente con `ax ssh` y lo
borra todo al terminar. Opcionales: `RAMA` (`main`), `TURNOS` (20, solo
Claude), `CPU` (1) y `MEMORIA` (`1Gi`), que AX pide pero no aplica
(google/ax#369: el techo real es el del worker, 1 500m y 1 536 MiB), y
`CONSERVAR=1`. Es la herramienta del laboratorio manual codificada, tal
como estaba en el host el 2026-09-25 a las 23:17Z, con:

- el mismo scope de systemd y `AX_HOME` temporal que `ax`, con las mismas
  trampas de `INT`, `TERM` y `HUP` fuera del scope: con `Ctrl+C`, la parte
  de dentro borra antes la Task y solo después se para el scope con sus
  túneles;
- solo repositorios `https://`, sin credenciales en la URL ni comillas; la
  rama, `CPU`, `MEMORIA` y el directorio del repositorio validados, y sin
  saltos de línea, porque acaban dentro del YAML de la Task o del `sh -c`
  del sandbox; `TURNOS` entero;
- un `flock` en `/run/lock/ax-tarea.lock`: una sola a la vez; y se niega a
  empezar mientras exista una marca `/run/lock/dockerswarm-*.marker`, es
  decir, junto a un apply o una operación directa bajo el lock host-global.
  El playbook, a su vez, ni reinstala Substrate ni cambia AX mientras corre
  una (ver «Plano de control de AX»);
- la autorreparación del laboratorio manual antes de crear la Task:
  `proxy_arp` y `proxy_ndp` si el nodo los perdió y, si algún pod del pool
  en `ax-workers` es anterior al arranque del nodo, borra todos los del
  pool en ese espacio de nombres y espera a su despliegue. El playbook, en
  cambio, borra solo los anteriores al arranque, en cualquier espacio de
  nombres. Los dos solo actúan tras un reinicio del nodo, cuando ningún
  actor sigue vivo;
- el token solo en la entrada estándar de `ax apply -f -`, nunca en un
  argumento; sin `set -x`;
- Claude con `--setting-sources user --strict-mcp-config` (Claude Code
  2.1.274, el de `images/ax-agents/package.json`): un
  `.claude/settings.json` o un `.mcp.json` del repositorio clonado no
  pueden añadir hooks, permisos ni servidores MCP junto al token;
- `trap` de salida, más `INT`, `TERM` y `HUP`, que borran la Task y el
  Workspace; y como `ax delete` solo los marca, espera hasta 90 s a que
  desaparezcan. Solo cuenta como borrado el `NotFound` de `ax-server`
  (`internal/server/server.go`, líneas 83-84 y 315-316): un túnel o un
  servidor caídos también hacen fallar `ax get`. Si no, avisa con la orden
  para reintentar. Con `CONSERVAR=1` avisa de que el token sigue en la
  definición de la Task;
- para Codex, la copia de vuelta de su sesión (abajo).

Diferencias con la del laboratorio manual: las rutas de
`/opt/dockerswarm/ax-lab`, la imagen por el digest de su manifiesto, el
texto de uso, la validación de `RAMA`, `CPU`, `MEMORIA` y la URL, la marca
del lock host-global, el aviso de `CONSERVAR=1`, el mensaje de los límites,
la trampa de `HUP` dentro del scope, el borrado confirmado por `NotFound`,
la orden de reintento con `-a default` y la copia de vuelta de Codex.

La sesión de Codex (`/etc/dockerswarm/ax/codex/auth.json`) lleva un refresh
token de un solo uso: el binario de Codex fijado rechaza uno ya usado («your
refresh token was already used»). La Task recibe la sesión del host, y si
Codex la renueva dentro del sandbox, la del host deja de valer para la
siguiente. `ax-tarea` la lee de vuelta del sandbox con `ax ssh` (de
`$CODEX_HOME/auth.json`, `/root/.codex` en la imagen de agentes), una sola
vez: al terminar Codex o, si la tarea llegó a estar lista, antes de
borrarla tras una interrupción. Lee como mucho 64 KiB y un byte, a un
fichero `0600` de su `AX_HOME`: un `auth.json` que fuera un flujo sin fin no
llena `/run`, que es memoria del host. Solo sustituye la del host, de forma
atómica (fichero temporal `0600` en el mismo directorio, `fsync` y
`rename`), si:

- la del host es un fichero regular `0600` de root, sin enlaces, y las dos
  miden como mucho 64 KiB y son JSON sin claves repetidas;
- tienen las mismas claves, también dentro de `tokens`, y ningún token
  vacío;
- `tokens.account_id` y el resto de campos, salvo los tokens y
  `last_refresh`, no cambian, y el refresh token es otro;
- `last_refresh` es una fecha RFC 3339 en UTC posterior a la del host y
  de esta tarea: ni anterior a su creación ni futura, con 2 minutos de
  margen. Así un fichero con una fecha lejana no bloquea las copias de
  vuelta siguientes.

Estas comprobaciones evitan copiar por error otra sesión o un fichero a
medias, no a un sandbox hostil: su código tiene la sesión entera y puede
escribir un `auth.json` con el `account_id` del host y tokens suyos, que
serían la sesión de las siguientes tareas de Codex (ver «Límites
conocidos»). Si algo no cuadra, o no se puede leer, la del host no se toca
y se avisa. Nunca se imprime ningún valor.

Los sandboxes de gVisor inundan de `ptrace` la auditoría del Observatorio.
Hasta el 2026-09-28 no se lanzaban Tasks en su ventana de medida
(22:30-00:40 UTC), y el panel las rechazaba. Desde entonces, por decisión del
propietario («yo no quiero esa ventana de interrupción»), AX no tiene
ventana: se retira la regla de no lanzar Tasks en ella, también las de
`ax-tarea`, y el panel ya no la aplica (`web.blackout_utc: ""`). Las Tasks de
esa franja llegan a la auditoría del Observatorio.

### Ejemplos

El rol instala en `/opt/dockerswarm/ax-lab/ejemplos` (`root 0755`, ficheros
`0644`) los ejemplos que el propietario escribió a mano en el laboratorio
manual (`/opt/ax-lab/ejemplos`): la guía `LEEME.md`, tres manifiestos
(`01-tarea-sencilla.yaml`, `02-workspace.yaml` y
`03-tarea-con-workspace.yaml`) y los clientes remotos `cliente/ax-remoto` y
`cliente/ax-remoto.ps1`. La guía y los manifiestos se renderizan con las
rutas de este laboratorio, su WorkerPool y la imagen de agentes fijada por
digest; los clientes se copian byte a byte. Ninguno lleva credenciales. En
`--check` solo informa. Se leen con `sudo`, porque
`/opt/dockerswarm/ax-lab` es `root 0750`:
`sudo cat /opt/dockerswarm/ax-lab/ejemplos/LEEME.md | less`.

### Modo check de AX

Con `--check`, el rol lee las imágenes de AX en el registro y en la copia,
y AX en el clúster si el nodo corre y Substrate coincide con lo fijado,
con las mismas comprobaciones de propiedad que el apply, e informa de qué
copiaría o restauraría, si aplicaría AX y por qué (`state`, un manifiesto o
un espacio de nombres), si pondría el `--route-timeout`, cuántos workers
recrearía y si el apply se detendría por un `ax-tarea` o una orden `ax` en
marcha, con Substrate o AX por cambiar. Los
módulos `copy` y `template` de `ax_host.yml` informan de si instalarían la
CLI o las herramientas.

## Capacidad

`config/capacity-profiles.yml` declara el grupo `ax-lab` en
`host_containers` con los límites y reservas de la tabla anterior y 500m,
50m y 10m de CPU reservada, que solo cuentan en el presupuesto porque Docker
no reserva CPU para un contenedor suelto. El tercer contenedor es
`ax-web-edge`, el reenviador del panel web (ver [AX_WEB.md](AX_WEB.md)). El
validador exige que coincidan con `config/ax-lab.yml`. Solo lo ejecuta el
plan activo `organizationweb`, que con el reenviador quedaba en 3 110m y
5 682 MiB reservados y 16 900m y 12 173 MiB de límite: 224 MiB por debajo
de los 12 397 MiB del presupuesto de memoria. Por eso `ate-setup`, que corre
junto al nodo y tiene que caber en lo que el plan deja libre, bajó de 256 a
224 MiB (ver «Instalación de Substrate»). Con la web de WinNest (ver
[WINNEST.md](WINNEST.md)) el plan queda en 3 160m y 5 698 MiB reservados y
17 000m y 12 205 MiB de límite, 192 MiB y 500m por debajo del presupuesto,
y `ate-setup` baja a 192 MiB (96 MiB reservados), con la misma CPU. El plan
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
se siembran las copias de Substrate y de AX, se retira el laboratorio manual
y se aplica `ax-lab` (ver «Ventana del laboratorio»). El cambio 4 los sigue
en su propia ventana (ver «Ventana del cambio 4»). Una vez retirado, su
ausencia ya no detiene nada. La misma regla vale para un cambio que re-fija
las imágenes ya sembradas: se fusiona dentro de su ventana, después de la
siembra (ver «Imágenes compiladas en la CI y ventana de resiembra»).

## Ventana del laboratorio

En una ventana exclusiva, en este orden:

1. Antes: los cambios 2 y 3 aprobados, con la CI en verde, incluido el job
   de reproducibilidad. El cambio 3 ya fija el digest de `ate-setup` (el
   que imprimió su primer run) y pone `ax_lab_substrate_fallback_builds:
   [ate-setup]`, porque el laboratorio manual no tiene esa imagen.
   Cualquier otra decisión sobre un digest que no se reproduzca también va
   en él (ver «Reproducibilidad»). El cambio 4 también aprobado, con su job
   `reproduce-ax` en verde: su gestor es el que siembra AX en el paso 3.
2. Suspender las Tasks de AX (paso 1 de «Retirar el laboratorio manual»).
3. Sembrar la copia de seguridad desde el laboratorio manual, todavía en
   marcha: las seis imágenes de Substrate (ver «Semilla desde el
   laboratorio manual»), las cuatro de AX y la CLI (ver «Semilla de AX»).
   Comprobar con `image-status` que las diez están `complete` y el sha256
   de la CLI. Sin esto, retirar el laboratorio borra las únicas copias de
   sus imágenes.
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
   El cambio 4 se aplica después, en otra ventana (ver «Ventana del cambio
   4»).

## Ventana del cambio 4

Separada de la anterior: su retirada es borrar dos espacios de nombres, y
la de los cambios 2 y 3 ya dejó el laboratorio manual retirado y las copias
de AX sembradas. Exclusiva y fuera de las ventanas de medida del
Observatorio, bajo el mismo lock y desde el clon operativo en el commit
fusionado:

1. Antes: el cambio 4 aprobado, con los dos jobs de reproducibilidad en
   verde, y `image-status --image-set ax` con las cuatro imágenes
   `complete` en la copia.
2. Fusionar el cambio 4 y dejar el clon operativo en ese commit, limpio.
3. `--check`: debe informar de instalar la CLI y las dos órdenes, de
   restaurar en el registro las cuatro imágenes de AX y de aplicar AX
   (`state`, `ax-system.yaml`, `ax-workers.yaml`) y el `--route-timeout`.
4. El apply real. Revisar el `memory.peak` del registro y que su
   `memory.events` siga en `oom_kill 0`; el del nodo lo exige el rol (ver
   «WorkerPool y capacidad dentro del nodo»).
5. Un segundo apply informa `changed=0`.
6. Antes de la prueba de humo, que es la primera vez que un worker de
   1 500m corre bajo el tope de 2 CPU del nodo, anotar del nodo
   (`/sys/fs/cgroup/system.slice/docker-<ID>.scope`, con el ID de
   `docker inspect -f '{{.Id}}' kind-control-plane`) `memory.peak`,
   `memory.events.local` y `cpu.stat` (`nr_periods`, `nr_throttled` y
   `throttled_usec`), y los reinicios de los pods de `kube-system` y
   `ate-system`, con el kubeconfig del laboratorio: `kubectl get pods -n
   kube-system -o
   custom-columns=NAME:.metadata.name,RESTARTS:.status.containerStatuses[*].restartCount`,
   y lo mismo con `-n ate-system`.
7. Comprobación (ver «Verificación»), y la prueba de humo con
   `sudo ax-tarea https://github.com/<repo público pequeño>
   "<instrucción de solo lectura>" claude` (Codex, cuando su cuota lo
   permita). Durante una segunda Task con `CONSERVAR=1`, desde dentro con
   `sudo ax ssh`: `curl -m5
   http://ax-server.ax-system.svc.cluster.local:8080/healthz` y una conexión
   TCP a `ax-redis.ax-system:6379` tienen que fallar, y como control
   positivo en la misma ejecución, `getent hosts github.com` y un `curl` a
   `https://github.com` tienen que funcionar; anotar si responden
   `rustfs.ate-system.svc:9000` y `kind-registry:5000`. Después, borrarla.
8. Tras la prueba de humo, repetir las lecturas del paso 6. Tienen que
   seguir `oom 0` en `memory.events.local` del nodo, que no cuenta a un
   worker que llegue a su propio límite, y los mismos reinicios en
   `kube-system` y `ate-system`: etcd, la API y Substrate aguantaron un
   worker ocupado bajo el tope. Si no, AX se retira (abajo). Anotar en
   [CAPACITY.md](CAPACITY.md) el `memory.peak` del nodo con AX y lo que
   crecieron `nr_throttled` y `throttled_usec` durante la prueba.
9. Opcional en esta ventana, y si no, del cambio 5: la prueba de reinicio,
   `docker stop --time 60 kind-control-plane` con el lock y otro apply, que
   debe informar de recrear un worker (este cambio ya codifica la
   reparación, en `workers.yml`), y repetir la prueba de humo con las
   lecturas de los pasos 6 y 8.

Si algo falla antes de dar AX por bueno, con el mismo lock se borran
`ax-workers` y `ax-system` (ver «Propiedad de AX»), `state/ax.json`,
`/usr/local/sbin/ax`, `/usr/local/sbin/ax-tarea` y la CLI instalada,
`/opt/dockerswarm/ax-lab/bin/ax`; se revierte el cambio y se aplica
`ax-lab`. Sin el cambio 4 nada gestiona esas tres órdenes: se quedarían,
con la autorreparación de `ax-tarea`, que borra pods de workers. Substrate
no se toca: `atenet-router` conserva `--route-timeout=1h`, que no molesta,
hasta que `ate-setup` lo reinstale, y las copias de las imágenes de AX y de
la CLI siguen en `/var/backups/dockerswarm/ax-lab`.

## Imágenes compiladas en la CI y ventana de resiembra

Las imágenes de Substrate y las dos de ko de AX se fijaron primero con los
digests del laboratorio manual (ver «Semilla desde el laboratorio manual» y
«Semilla de AX»). Esta sección describe cómo se fijan en su lugar las que
compila la CI con los módulos de Go actualizados y cómo las siembra el
propietario en la copia de seguridad. Es solo el mecanismo: ningún digest,
hash ni valor de pin figura aquí, porque la ventana los toma siempre de los
planes que imprime el validador. Mientras `config/ax-lab.yml` no fije los
campos de resiembra, nada de esto cambia el comportamiento del laboratorio.

### Los campos de resiembra

Son tres claves opcionales de `config/ax-lab.yml`. Un contrato sin ellas
valida y se comporta como antes:

- `images.runner_toolbox`: la imagen `golang:1.27.1`, por digest, con la que
  `reproduce-ax` recompila el binario del runner (ver «Rama K»);
- `sources.ax.modules_patch` (`path` y `sha256`):
  `images/ax/google-ax-modules.patch`, un segundo parche de AX, encima del de
  google/ax#375, que solo toca `go.mod` y `go.sum`;
- `sources.substrate.modules_patch` (`path`, `sha256` y `tree`):
  `images/ax/substrate-modules.patch`, el equivalente de Substrate, también
  solo de `go.mod` y `go.sum`. La CI regenera `vendor/` con `go mod vendor`
  en lugar de versionar su diff, y `tree` es el árbol raíz de git
  (`git write-tree`) después de aplicar el parche, vendorizar y ejecutar
  `git add --all`, sin commit. AX no tiene `tree`: `git apply` sobre el
  commit fijado con dos parches fijados por su sha256 es determinista.

Los dos parches se fijan juntos y exigen `images.runner_toolbox`. Con ellos,
el validador exige que `images.toolbox` sea `golang:1.27.2` y que
`ax_lab_substrate_fallback_builds` esté vacía: el host nunca parchea ni
vendoriza Substrate, y un digest nuevo entra en la copia solo con
`seed-layout` (ver «Compilación de reserva»). `--substrate-plan` y
`--ax-plan` imprimen los parches y, el de AX, la imagen del runner y el tag;
los workflows y la ventana leen solo esos planes, ya validados.

### El workflow de imágenes

`.github/workflows/ax-lab-images.yml`, con los jobs `substrate` y `ax`,
compila las mismas entradas que `ax-lab-reproducibility.yml`, con los mismos
pasos, y añade lo que aquel no hace:

- exige los pines: los siete digests de Substrate y el sha256 de la CLI
  siempre, y los de las dos imágenes ko cuando `sources.ax.modules_patch`
  está fijado (antes no pueden coincidir con los del laboratorio manual por
  la anotación `base.name`; ver «Reproducibilidad de AX»). Un pin que no
  coincide falla el job después de imprimir todo lo compilado, y no sube nada;
- deja las capas preparadas como las recibirá el host y prueba con ellas, en
  un layout de prueba del runner, `seed-layout`, `forget`, `image-status` y
  otra vez `seed-layout`, antes de subir;
- falla si la CLI necesita una glibc más nueva que la 2.43 del host;
- sube, solo en un push a `main` y en `workflow_dispatch` (un pull request
  compila y prueba, pero no sube), los artefactos
  `ax-lab-substrate-oci-layout` (el layout OCI de las siete imágenes) y
  `ax-lab-ax-artifacts` (la CLI, el layout de cada imagen ko y sus
  manifiestos).

No usa credenciales ni publica en ningún registro: el artefacto es lo único
que sale del runner, como con `ax-web-oci-layout` (ver [AX_WEB.md](AX_WEB.md),
«Imagen»). `ax-lab-reproducibility.yml` sigue solo compilando y comparando, y
recompila el binario del runner con `images.runner_toolbox`.

### Rama K

`ax-task-runner` y `ax-agents` son imágenes de buildx que no se pueden
recompilar byte a byte (ver «Lo que no se puede codificar fielmente»). La
ventana no las toca: se quedan sembradas tal como están, con sus pines, con
Go 1.27.1 y con los módulos de antes de la subida, así que los hallazgos de
`govulncheck` de su binario siguen abiertos. Para que
`ax.task_runner_binary_sha256` siga describiendo lo que llevan dentro,
`reproduce-ax` compila ese binario con `images.runner_toolbox`, solo con el
primer parche y antes del de módulos. Reconstruirlas sería otro cambio
revisado.

### Riesgo aceptado

Las imágenes y la CLI que salen de la CI se ejecutan por primera vez en
producción: la CI las compila, comprueba sus digests y prueba que la copia
puede sembrarse con ellas, y nada las arranca antes con AX encima (la prueba
de extremo a extremo en kind sigue sin existir en el repositorio; ver
«Reproducibilidad»). La única red de seguridad es la marcha atrás desde la
instantánea del paso 2, y el propietario aceptó este riesgo.

Dentro de ese riesgo, `ate-setup` corre por primera vez en este host bajo el
tope de 192 MiB (ver «Instalación de Substrate»), y con un binario nuevo. Una
ejecución desechable de la CI, el 2026-10-12, lo midió en un clúster kind con
los pines nuevos y los límites del laboratorio: su pico más alto fue el
29,2 % del tope. El paso 0 dice qué prueba esa medida y qué no, y exige que
el propietario acepte, en el registro de la ventana, que vale para este
host. Lo que ninguna prueba ha visto es el relevo de `atelet` con workers
vivos, y es el riesgo operativo principal de la ventana.

### Reglas de la ventana de resiembra

- **Pines de los planes.** Los nuevos salen de
  `validate-ax-lab.py --substrate-plan` y `--ax-plan` en el head del PR; los
  anteriores, de los mismos planes en el commit que
  `/opt/dockerswarm/deployments/ax-lab.yml` registra como último aplicado,
  que es el que describe lo que guarda la copia. Nada se teclea: el paso 1 lo
  calcula y lo comprueba. Ese registro se lee una sola vez, en el paso 1: el
  paso 2 guarda el commit y los planes en `${RELOCK}` (ver «Recuperar la
  shell»), porque cada apply correcto reescribe el registro con su propio
  commit (`deployment_metadata`) y desde el paso 11 ya no describe la copia.
  Después solo se compara con lo guardado, para notar un apply ajeno.
- **Nada lee la copia viva con el contrato nuevo antes del `forget`.** Ni el
  `--check` del playbook ni `image-status`: la copia nombra los mismos
  `<nombre>:<tag>` con otro digest, `image-status` se detiene con «names …
  with another digest», las lecturas del rol fallan y el wrapper retiene un
  marker que hay que recuperar (ver «Stale marker recovery» en
  `CLAUDE.md`). La instantánea y el ensayo usan otros layouts y no cuentan.
  Las lecturas por imagen del paso 5 usan el pin anterior, que sí describe
  lo que la copia guarda.
- **El PR de pines se fusiona dentro de la ventana, después de la siembra,
  nunca antes.** Es la regla del repositorio (ver «Por qué este cambio solo
  se fusiona en la ventana» y «Semilla de AX»): con los pines nuevos en
  `main` y la copia aún con los anteriores, todo apply de `ax-lab` y todo
  release de `ax-web`, que usan el mismo fichero y el mismo playbook, se
  detienen en `image-status`. Fusionarlo no se deshace solo: abortar una vez
  fusionado (desde el paso 9) pide además el PR que lo revierta (ver «Marcha
  atrás»).
- **La siembra sale del head verde del PR, desacoplado**
  (`git checkout --detach`), con los artefactos de una ejecución
  `workflow_dispatch` de `ax-lab-images.yml` sobre ese head exacto. Si los
  artefactos caducaron, se repite esa ejecución sobre el mismo head: vuelve
  a exigir los pines. Un pull request compila y prueba pero no sube nada, y
  el push a `main` que sí sube llega después de fusionar, que está prohibido
  antes de la siembra: los artefactos solo pueden salir de esa ejecución
  manual. GitHub solo ofrece `workflow_dispatch` para un workflow que ya
  está en la rama por defecto, así que `ax-lab-images.yml` tiene que estar
  ya en `main` (el PR del mecanismo, fusionado) antes de esa ejecución, y el
  propietario comprueba, antes de la congelación, que «Run workflow» ofrece
  la rama del PR de pines.
- **Congelación.** Desde el paso 0 hasta el último `changed=0`, ningún
  release de `ax-web` se fusiona y ningún `ax-lab` corre (`--check`
  incluido) salvo los de esta ventana. Se avisa antes a quien publica
  releases y a toda sesión de agente del host. No empieza en el `forget`: el
  paso 1 lee el commit que la marcha atrás restaura de
  `/opt/dockerswarm/deployments/ax-lab.yml`, y un apply de `ax-lab` o un
  release de `ax-web` entre el paso 0 y el 5 lo mueve, de modo que volver a
  un `PREV` viejo bajaría de versión la Oficina sin que nada lo dijera. Las
  guardas de los pasos 5 y 11 y del apply de la marcha atrás comprueban que
  el registro sigue nombrando lo que la ventana guardó (`record_is`).
- **Quién toma el lock host-global.** No solo los playbooks. Lo toman todos
  los playbooks (`ax-lab`, los releases de `ax-web`, `edge`, `platform` y
  los demás), `validate-iac.sh` y `lint.sh` (operaciones directas bajo el lock
  de validación de Docker), los tests del repositorio que corren bajo ese
  lock, las sondas que las sesiones de agente lanzan sobre la Oficina
  (operaciones `ax-office-*`: la Oficina es un pod y no toma el lock) y toda
  sesión de agente del host que use cualquiera de ellos; en este host corren
  varias a la vez. Durante la ventana no corre ninguno. Un titular
  concurrente hace fallar la orden con lock de la ventana (sin marker: no
  obtuvo el lock), y uno que falla deja `/run/lock/dockerswarm-direct.marker`,
  que rechaza toda orden con lock de la ventana y de la marcha atrás. Las
  lecturas de partida lo comprueban sin tocarlo: `lock_free` mira con
  `fuser` que nadie tiene abierto `/run/lock/dockerswarm-iac.lock`, y se
  detiene si `fuser` falta o sale con algo distinto de 0 y 1, en lugar de dar
  el lock por libre. Además, `deployment_metadata` guarda un único registro
  de la última invocación, que reescribe cualquier playbook: un apply ajeno
  entre el apply y el último `--check` impide el `changed=0`, y uno anterior
  a la ventana lo impide en el `--check` de la marcha atrás desde `${PREV}`
  (`deployed_last` lo lee en el paso 0).
- **El clon operativo es solo de la ventana.** Nadie más hace `checkout`,
  `pull` ni edita nada en él, y ninguna otra sesión lo usa: el wrapper
  retiene el marker aunque el apply salga bien si el commit o el árbol
  cambian mientras corre («source HEAD changed during deployment»).
- **La Oficina, en pausa durante toda la ventana, no solo vacía al
  empezar.** La Oficina despacha sola desde su cola, por prioridad y
  antigüedad, y su planificador dispara los horarios que estén activos; las
  guardas del rol solo ven los scopes `ax-tarea-*` y `ax-cli-*`, no lo que
  despacha por `ax-server`. Pausar la cola (`POST /api/queue/pause`; en la
  interfaz, «Pausar cola» en la cabecera o «Pausar la cola» en Ajustes)
  impide que arranque ningún trabajo nuevo, y el que está en curso sigue.
  Se pausa en el paso 0, se lee de vuelta en `office.json` (`queue_paused`
  y los horarios) y en `jobs.json` (ningún trabajo en `preparando`,
  `en_curso` ni `limpiando`), y esa misma lectura se repite justo antes del
  paso 11 y del apply de la marcha atrás (`office_idle`). Un trabajo en
  marcha en ese momento es un `STOP`: se espera a que termine, no se aplica.
  La persona que usa la Oficina no encola nada hasta el final. La cola se
  reanuda al final del paso 13, o cuando la marcha atrás está verificada, o
  al abortar antes del `forget`, donde no hay marcha atrás (ver «Criterios de
  aborto»).
- **Exclusiva**, de un solo operador, fuera de las ventanas de medida del
  Observatorio y en una sola sesión de `tmux`, para que un corte de SSH no
  deje un apply a medias; todas las órdenes corren en esa misma shell. Si
  la shell se pierde (un panel nuevo, el servidor de `tmux`, un reinicio),
  la ventana se retoma con «Recuperar la shell», no de memoria.
- **Cada orden con lock se decide antes con una lectura.** `forget` y
  `seed-layout` (como `export`, `import`, `build` e `install`: son los
  `MUTATING_COMMANDS` del gestor) toman el lock host-global, y en este host,
  con Swarm activo, el gestor se re-ejecuta bajo
  `host_global_operation_lock.py run`. Si una orden con lock sale con un
  código distinto de cero, aunque sea la negativa limpia del gestor («names …
  with …, not …»), el wrapper deja a propósito
  `/run/lock/dockerswarm-direct.marker` y la orden siguiente con lock se
  rechaza hasta recuperarlo (ver «Un marker retenido»). `image-status` no
  está en esa lista: no toma lock y, si se detiene, no deja marker. Por eso
  `forget` y `seed-layout` corren por imagen y solo cuando un `image-status`
  de esa imagen, con el pin que toca, dice que hay algo que hacer, y ninguna
  orden con lock lleva detrás `|| echo` ni `|| true`: esconderían el fallo
  que deja el marker. Con esas lecturas, repetir un paso o la marcha atrás no
  vuelve a pedir lo que ya está hecho ni provoca un rechazo a propósito.
- **Las guardas detienen la ventana.** No se usa `set -e`: mataría la shell
  de `tmux` que guarda las variables. Lo hacen los ayudantes `stop`, `must` y
  `out` del paso 0: un fallo imprime `STOP:` y deja `HALT` puesto, y desde
  entonces `must` y `out` no ejecutan nada más, tampoco las líneas que siguen
  en el mismo bloque pegado, hasta que el operador decida (`HALT=` para
  seguir, o la marcha atrás). Toda orden con efecto va por `must` o por
  `out`, toda comprobación termina en `|| stop '...'` (o es `expect_*`, que
  llama a `stop`) y la comprobación de la CLI es una sola cadena con `&&`.
  Las funciones de lectura de las definiciones del paso 0 (`record_is`,
  `deployed_last`, `lock_free`, `office_idle`, `lab_scopes`, `lab_idle`,
  `host_memory`, `no_transient` y `worker_gate`)
  hacen lo mismo: llaman a `stop`. Aun así se pega un bloque cada vez y, tras
  un `STOP:`, ningún otro.

### Precondiciones de la ventana de resiembra

Paso 0, antes de tocar nada. La congelación rige desde aquí (ver «Reglas de
la ventana de resiembra»): se avisa a quien publica releases y a las demás
sesiones del host, y el clon operativo es de la ventana.

Primero, una sesión de `tmux` abierta, con los ayudantes de parada pegados en
ella antes de nada (ver «Las guardas detienen la ventana»):

```bash
HALT=
OUTFILE=$(mktemp)
stop() { # texto: deja constancia del fallo y frena lo que sigue
  HALT=${HALT:-$*}
  printf 'STOP: %s\n' "$*" >&2
  return 1
}
must() { # orden...: no corre tras un STOP, y su fallo es un STOP
  if [[ -n "${HALT}" ]]; then
    printf 'STOP previo, no se ejecuta: %s\n' "$*" >&2
    return 1
  fi
  "$@" || stop "$*"
}
out() { # orden...: como must, y deja su salida en OUT (si falla, la muestra)
  local status=0
  must "$@" >"${OUTFILE}" || status=$?
  OUT=$(<"${OUTFILE}")
  if [[ "${status}" -ne 0 ]]; then
    printf '%s\n' "${OUT}" >&2
  fi
  return "${status}"
}
```

Después, las definiciones, en tres bloques que solo definen y no ejecutan
nada. Con los ayudantes, son lo único que «Recuperar la shell» vuelve a
pegar, así que cada función vive aquí, una sola vez. El primero guarda las
rutas, las funciones que calculan los pines de los planes y las que escriben
y comprueban lo que la ventana guarda en `${RELOCK}`: `window.env` (el
commit del head del PR, el aplicado antes y, tras el paso 9, el de fusión) y
los planes. `load_plans` y `load_facts` leen un directorio con los cuatro
planes y el contrato anterior: el paso 1 les da el de trabajo y la
recuperación, una copia de lo guardado. `record_is` y `deployed_last` leen
los dos registros que escribe `deployment_metadata`: el de `ax-lab` y el de
la última invocación de cualquier playbook:

```bash
RELOCK=/var/backups/dockerswarm/ax-lab-relock
LAYOUT=/var/backups/dockerswarm/ax-lab/images
CLI_BACKUP=/var/backups/dockerswarm/ax-lab/binaries/ax
SNAP_LAYOUT=${RELOCK}/images
SNAP_CLI=${RELOCK}/ax
IN=${RELOCK}/in
REH=${RELOCK}/rehearsal
RECORD=/opt/dockerswarm/deployments/ax-lab.yml
DEPLOYED=/opt/dockerswarm/DEPLOYED_VERSION.yml
cfg() { # contrato [clave...]: un valor del contrato `ax_lab` de ese fichero
  .venv/bin/python -c '
import functools, sys, yaml
node = yaml.safe_load(open(sys.argv[1]))["ax_lab"]
print(functools.reduce(lambda part, key: part[key], sys.argv[2:], node))
' "$@"
}
plan() { .venv/bin/python "$1/scripts/validate-ax-lab.py" "$2"; }
pins() {
  jq -r --arg key "$2" '.[$key] | to_entries[] | select(.value != null)
    | "--image=\(.key)=\(.value)"' "$1"
}
sorted() { printf '%s\n' "$@" | LC_ALL=C sort; }
load_plans() { # directorio: los pines de los cuatro planes que hay en él
  mapfile -t NEW_SUB < <(pins "$1/sub-new.json" images)
  mapfile -t OLD_SUB < <(pins "$1/sub-old.json" images)
  mapfile -t NEW_KO < <(pins "$1/ax-new.json" ko_images)
  mapfile -t OLD_KO < <(pins "$1/ax-old.json" ko_images)
  mapfile -t STALE_SUB < <(LC_ALL=C comm -23 \
    <(sorted "${OLD_SUB[@]}") <(sorted "${NEW_SUB[@]}"))
  mapfile -t MOVED_SUB < <(LC_ALL=C comm -13 \
    <(sorted "${OLD_SUB[@]}") <(sorted "${NEW_SUB[@]}"))
  mapfile -t STALE_KO < <(LC_ALL=C comm -23 \
    <(sorted "${OLD_KO[@]}") <(sorted "${NEW_KO[@]}"))
  mapfile -t MOVED_KO < <(LC_ALL=C comm -13 \
    <(sorted "${OLD_KO[@]}") <(sorted "${NEW_KO[@]}"))
  [[ ${#NEW_SUB[@]} -eq 7 && ${#OLD_SUB[@]} -eq 7 ]] \
    || stop 'faltan pines de Substrate'
  [[ ${#NEW_KO[@]} -eq 2 && ${#OLD_KO[@]} -eq 2 ]] \
    || stop 'faltan pines de las imágenes ko'
  [[ ${#STALE_SUB[@]} -gt 0 && ${#STALE_SUB[@]} -eq ${#MOVED_SUB[@]} ]] \
    || stop 'los pines de Substrate no cambian por pares'
  [[ ${#STALE_KO[@]} -gt 0 && ${#STALE_KO[@]} -eq ${#MOVED_KO[@]} ]] \
    || stop 'los pines de las imágenes ko no cambian por pares'
}
load_facts() { # directorio contrato-anterior: etiquetas, CLI y pines fijos
  local name
  TAG_SUB=$(jq -r .version "$1/sub-new.json")
  TAG_AX=$(jq -r .tag "$1/ax-new.json")
  CLI_NEW=$(jq -r .cli_sha256 "$1/ax-new.json")
  CLI_OLD=$(jq -r .cli_sha256 "$1/ax-old.json")
  [[ -n "${TAG_SUB}" && -n "${TAG_AX}" && -n "${CLI_NEW}" \
    && -n "${CLI_OLD}" ]] \
    || stop 'los planes no traen la versión, el tag o el sha256 de la CLI'
  [[ "${TAG_SUB}" == "$(jq -r .version "$1/sub-old.json")" ]] \
    || stop 'cambia el tag de Substrate: esta ventana no hace falta'
  [[ "${TAG_AX}" == "$(cfg "$2" ax tag)" ]] \
    || stop 'cambia el tag de AX: esta ventana no hace falta'
  BUILDX=()
  for name in ax-task-runner ax-agents; do
    BUILDX+=("--image=${name}=$(cfg "$2" ax images "${name}")")
  done
  WEB_TAG=$(cfg "$2" web image tag)
  WEB_PIN="--image=ax-web=$(cfg "$2" web image digest)"
  WORKER_OLD=$(printf '%s\n' "${OLD_SUB[@]}" \
    | sed -n 's/^--image=ateom-gvisor=//p')
  WORKER_NEW=$(printf '%s\n' "${NEW_SUB[@]}" \
    | sed -n 's/^--image=ateom-gvisor=//p')
  [[ "${WORKER_OLD}" == sha256:* && "${WORKER_NEW}" == sha256:* ]] \
    || stop 'los planes no traen el pin de ateom-gvisor'
}
save_window() { # escribe window.env en ${RELOCK}, de root y 0600
  printf 'PR_HEAD=%s\nPREV=%s\n' "${PR_HEAD:?}" "${PREV:?}" \
    >"${WORK:?}/window.env"
  if [[ -n "${MERGE:-}" ]]; then
    printf 'MERGE=%s\n' "${MERGE}" >>"${WORK}/window.env"
  fi
  must sudo -- install -m 0600 -o root -g root "${WORK}/window.env" \
    "${RELOCK}/window.env"
}
record_is() { # commit...: el registro de ax-lab.yml nombra uno de ellos
  local now want
  now=$(sed -n 's/^revision: "\([a-f0-9]\{40\}\)"$/\1/p' "${RECORD}")
  for want in "$@"; do
    if [[ "${now}" == "${want}" ]]; then
      return 0
    fi
  done
  stop "ax-lab.yml nombra '${now}', no '$*': un apply ajeno movió el registro"
}
deployed_last() { # el último playbook fue ax-lab, en el commit del registro
  local playbook revision recorded
  playbook=$(sed -n 's/^playbook: "\([a-z-]*\)"$/\1/p' "${DEPLOYED}")
  revision=$(sed -n 's/^revision: "\([a-f0-9]\{40\}\)"$/\1/p' "${DEPLOYED}")
  recorded=$(sed -n 's/^revision: "\([a-f0-9]\{40\}\)"$/\1/p' "${RECORD}")
  if [[ -z "${playbook}" || -z "${recorded}" ]]; then
    stop "${DEPLOYED} sin playbook, o ${RECORD} sin commit"
  elif [[ "${playbook}" != ax-lab || "${revision}" != "${recorded}" ]]; then
    stop "${DEPLOYED} nombra ${playbook} en ${revision}, no ax-lab en" \
      "${recorded}: otro playbook corrió el último"
  fi
}
```

El segundo, las lecturas y las órdenes por imagen de la copia de seguridad.
`image_state` lee una sola imagen de una copia con `image-status`, que no
toma lock, y dice `complete`, `missing` u `other`: la copia nombra ese
`<nombre>:<tag>` con otro digest, el único fallo que espera y que
interpreta; cualquier otro es un `STOP`. Una por llamada, porque basta una
entrada con otro digest para que `image-status` se detenga con las demás.
`forget_one` retira la entrada solo si está `complete` con ese pin, así que
el gestor nunca se niega. `seed_one` siembra solo si falta y se niega él
mismo, sin pedir el lock, si la copia nombra otro digest (falta el
`forget`):

```bash
expect_all() { # n valor salida: n entradas, todas con ese valor
  printf '%s\n' "$3"
  jq -e --argjson n "$1" --arg v "$2" \
    'length == $n and all(.[]; . == $v)' <<<"$3" >/dev/null \
    || stop "se esperaban $1 entradas con el valor $2"
}
expect_complete() { # n salida: n imágenes, todas completas en la copia
  printf '%s\n' "$2"
  jq -e --argjson n "$1" \
    '.backup | length == $n and all(.[]; . == "complete")' \
    <<<"$2" >/dev/null \
    || stop "se esperaban $1 imágenes completas en la copia"
}
expect_pinned() { # n salida: como expect_complete, y fijadas en el registro
  printf '%s\n' "$2"
  jq -e --argjson n "$1" '
    (.registry | length == $n and all(.[]; . == "pinned"))
    and (.backup | length == $n and all(.[]; . == "complete"))
  ' <<<"$2" >/dev/null \
    || stop "se esperaban $1 imágenes fijadas en el registro y completas"
}
image_state() { # conjunto capa tag pin: complete, missing u other
  local json name=${4#--image=}
  name=${name%%=*}
  if json=$(sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py \
    image-status --image-set "$1" --layout "$2" --tag "$3" "$4" 2>&1); then
    jq -er '.backup | to_entries[0].value' <<<"${json}"
  elif [[ "${json}" == *"names ${name}:$3 with another digest"* ]]; then
    echo other
  else
    printf '%s\n' "${json}" >&2
    return 1
  fi
}
forget_one() { # conjunto capa tag pin: retira la entrada de ese pin
  local state name=${4#--image=}
  name=${name%%=*}
  state=$(image_state "$1" "$2" "$3" "$4") \
    || { stop "image-status de ${name} en $2"; return 1; }
  if [[ "${state}" == complete ]]; then
    out sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py \
      forget --image-set "$1" --layout "$2" --tag "$3" "$4" \
      && expect_all 1 forgotten "${OUT}"
  else
    printf '%s: nada que retirar en %s (%s)\n' "${name}" "$2" "${state}"
  fi
}
seed_one() { # conjunto fuente capa tag pin: siembra solo lo que falta
  local state name=${5#--image=}
  name=${name%%=*}
  state=$(image_state "$1" "$3" "$4" "$5") \
    || { stop "image-status de ${name} en $3"; return 1; }
  case "${state}" in
    complete) printf '%s: ya está en %s\n' "${name}" "$3" ;;
    missing)
      out sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py \
        seed-layout --image-set "$1" --source "$2" --layout "$3" \
        --tag "$4" "$5" \
        && expect_all 1 seeded "${OUT}"
      ;;
    *) stop "$3 nombra ${name} con otro digest: falta el forget" ;;
  esac
}
```

El tercero, las lecturas del laboratorio y de la Oficina que usan las
guardas, la línea base y la verificación. Todas llaman a `stop` y no
escriben nada fuera de `${OUTFILE}`. `KUBECTL` lleva `--request-timeout
10s`, que corta toda espera larga a los 10 s (un `get --watch` termina a los
10 s), así que lo que espera usa `KUBECTL_WAIT`, sin él y acotado por su
propio `--timeout`. `office_path` saca del propio PV la ruta del volumen de
la Oficina, dentro del nodo, sin suponerla. `lab_memory` lee de los cgroups
del nodo y del registro lo que la línea base compara, y `pod_table` los
reinicios de los pods que el apply no sustituye (`REPLACED` nombra los que
sí):

```bash
KUBECTL=(sudo -- /usr/bin/env HOME=/opt/dockerswarm/ax-lab/home
  /opt/dockerswarm/ax-lab/bin/kubectl
  --kubeconfig /opt/dockerswarm/ax-lab/home/.kube/config
  --context kind-kind --request-timeout 10s)
# Sin --request-timeout: con él, el cliente corta cada espera a los 10 s.
KUBECTL_WAIT=(sudo -- /usr/bin/env HOME=/opt/dockerswarm/ax-lab/home
  /opt/dockerswarm/ax-lab/bin/kubectl
  --kubeconfig /opt/dockerswarm/ax-lab/home/.kube/config
  --context kind-kind)
# Los pods que el apply sustituye, como espacio-de-nombres/pod.
REPLACED='^(ate-system/(ate-api-server|ate-controller|atelet'
REPLACED+='|atenet-router|atenet-egress)-'
REPLACED+='|podcertificate-controller-system/podcertificate-controller-'
REPLACED+='|ax-system/(ax-controller|ax-server)-|ax-workers/)'
office_path() { # fija PV_PATH: la ruta del volumen de la Oficina en el nodo
  local pv
  pv=$("${KUBECTL[@]}" --namespace ax-web get pvc ax-web-state \
    --output jsonpath='{.spec.volumeName}') || stop 'pvc de ax-web-state'
  PV_PATH=$("${KUBECTL[@]}" get pv "${pv:?}" \
    --output jsonpath='{.spec.hostPath.path}{.spec.local.path}') \
    || stop 'pv de ax-web-state'
  [[ "${PV_PATH}" == /?* ]] || stop 'el PV de ax-web-state no trae ruta'
}
office_idle() { # la cola en pausa, ningún trabajo en marcha, ningún horario
  office_path || return 1
  out sudo -- /usr/bin/docker exec kind-control-plane cat \
    "${PV_PATH}/office.json" || return 1
  jq -e '.settings.queue_paused == true
    and ([(.schedules // [])[] | select(.enabled)] | length == 0)' \
    <<<"${OUT}" >/dev/null \
    || stop 'la cola de la Oficina no está en pausa, o hay un horario activo'
  out sudo -- /usr/bin/docker exec kind-control-plane cat \
    "${PV_PATH}/jobs.json" || return 1
  jq -e '[(.jobs // [])[] | select(.status
    | IN("preparando", "en_curso", "limpiando"))] | length == 0' \
    <<<"${OUT}" >/dev/null \
    || stop 'la Oficina tiene un trabajo en marcha: se espera a que termine'
}
lab_scopes() { # sin ax-tarea ni ax en marcha: una lectura local de systemd
  out systemctl list-units --type=scope --state=active --plain --no-legend \
    --no-pager 'ax-tarea-*' 'ax-cli-*' || return 1
  [[ -z "${OUT}" ]] || stop 'hay un ax-tarea o un ax en marcha'
}
lab_idle() { # además, sin Tasks: eso lo lee ax-server
  lab_scopes || return 1
  out sudo -- /usr/local/sbin/ax get tasks -a default || return 1
  [[ -z "$(sed 1d <<<"${OUT}")" ]] || stop 'AX tiene Tasks'
}
host_memory() { # MemAvailable de al menos 3 GiB (3 145 728 kB)
  local available
  available=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
  [[ "${available:-0}" -ge 3145728 ]] \
    || stop "MemAvailable ${available:-0} kB: faltan 3 GiB para la ventana"
}
lock_free() { # nadie tiene abierto el lock host-global (solo lectura)
  local status=0
  if ! sudo -- test -x /usr/bin/fuser; then
    stop 'falta /usr/bin/fuser (paquete psmisc): no se puede leer el lock'
    return 1
  fi
  sudo -- /usr/bin/fuser /run/lock/dockerswarm-iac.lock >/dev/null 2>&1 \
    || status=$?
  case "${status}" in
  0)
    stop 'alguien tiene el lock host-global: otra sesión, validate-iac o lint'
    ;;
  1) ;;
  *) stop "fuser salió con ${status}: no se puede leer el lock" ;;
  esac
}
no_transient() { # sin contenedores transitorios del gestor
  out sudo -- /usr/bin/docker ps --all --format '{{.Names}} {{.Status}}' \
    --filter 'name=^ax-lab-ate-setup$' --filter 'name=^ax-lab-build-' \
    || return 1
  [[ -z "${OUT}" ]] || stop "contenedor transitorio del gestor: ${OUT}"
}
lab_memory() { # la memoria del nodo y del registro, una lectura por línea
  local name id dir file
  for name in kind-control-plane kind-registry; do
    out sudo -- /usr/bin/docker inspect --format '{{.Id}}' "${name}" \
      || return 1
    id=${OUT}
    [[ "${id}" =~ ^[0-9a-f]{64}$ ]] || stop "ID de ${name}"
    dir=/sys/fs/cgroup/system.slice/docker-${id}.scope
    for file in memory.events memory.events.local; do
      out sudo -- cat "${dir}/${file}" || return 1
      awk -v prefix="${name} ${file}" '{print prefix " " $0}' <<<"${OUT}"
    done
    out sudo -- grep -E '^anon ' "${dir}/memory.stat" || return 1
    awk -v prefix="${name} memory.stat" '{print prefix " " $0}' <<<"${OUT}"
    out sudo -- cat "${dir}/memory.current" || return 1
    printf '%s memory.current %s\n' "${name}" "${OUT}"
  done
}
oom_lines() { grep -E ' memory\.events(\.local)? oom'; }
pod_table() { # unreplaced|replaced: los pods que el apply no sustituye, o sí
  out "${KUBECTL[@]}" get pods --all-namespaces --output json || return 1
  jq -r --arg replaced "${REPLACED}" --arg which "$1" '
    .items[] | select(.metadata.deletionTimestamp == null)
    | "\(.metadata.namespace)/\(.metadata.name)" as $pod
    | select(($pod | test($replaced)) == ($which == "replaced"))
    | "\($pod) \([.status.containerStatuses[]?.restartCount] | add // 0)"
  ' <<<"${OUT}" | LC_ALL=C sort
}
worker_gate() { # digest: el rollout del worker, y un solo pod, el de ese digest
  local try
  must "${KUBECTL_WAIT[@]}" --namespace ax-workers rollout status \
    deployment/ax --timeout=300s
  # El rol del WorkerPool puede cambiar el despliegue después de que el
  # rollout de arriba diga que terminó: el pod se relee hasta 30 veces, con
  # 10 s de pausa (unos 5 minutos, lo que el rollout), antes de la guarda.
  for try in {1..30}; do
    if [[ "${try}" -gt 1 ]]; then
      sleep 10
    fi
    out "${KUBECTL[@]}" --namespace ax-workers get pods \
      --selector ate.dev/worker-pool=ax --output json || return 1
    if jq -e --arg digest "$1" '
      [.items[] | select(.metadata.deletionTimestamp == null)]
      | length == 1 and all(.[];
        (.spec.containers[0].image | endswith("@" + $digest))
        and ([.status.conditions[]? | select(.type == "Ready"
          and .status == "True")] | length == 1))
    ' <<<"${OUT}" >/dev/null; then
      return 0
    fi
  done
  stop "no hay un único pod de worker listo con la imagen de ${1}"
}
```

Con eso pegado, las precondiciones:

- La cola de la Oficina, en pausa y leída de vuelta, y avisada la persona que
  la use de que no encole nada hasta el final de la ventana: pulsa «Pausar
  cola» en la cabecera de la Oficina (el trabajo que esté en curso sigue, y
  se espera a que termine). Es lo que impide que arranque un trabajo nuevo,
  que el apply cortaría (ver [AX_WEB.md](AX_WEB.md), «Ventana de la
  Oficina»). Se comprueba con `office_idle` en las lecturas de abajo:
  `queue_paused` verdadero, ningún horario activo y ningún trabajo en
  `preparando`, `en_curso` ni `limpiando`.
- Sin trabajo de AX en marcha: `sudo ax get tasks -a default` no lista
  ninguna Task (se suspenden o se borran antes), y
  `systemctl list-units --type=scope --state=active --plain --no-legend
  --no-pager 'ax-tarea-*' 'ax-cli-*'` no imprime nada (`lab_idle`). Un túnel
  de `sudo ax` es un scope `ax-cli-*`: `ax tunnel list` no sirve de guarda,
  porque el wrapper abre un `AX_HOME` nuevo en cada llamada y siempre sale
  vacío, a propósito. Las guardas del rol solo miran esos scopes, no lo que
  la Oficina despacha por `ax-server`: de ahí la pausa.
- `fuser` (`/usr/bin/fuser`, del paquete `psmisc`), que `lock_free` necesita
  y que ningún rol de este repositorio instala: este host lo tiene, uno
  reconstruido puede no tenerlo, y codificarlo es un cambio revisado aparte,
  no algo que se instale a mano dentro de la ventana. Sin él, `lock_free` es
  un `STOP`, no un «libre».
- La memoria del host: `MemAvailable` de al menos 3 GiB, unos 3 145 728 kB
  (`grep MemAvailable /proc/meminfo`, `host_memory`), y ninguna otra sesión
  pesada ni compilación durante los pasos 10 a 12. El 2026-10-09 un
  análisis sin límite agotó la memoria del host y el kernel mató Postgres,
  `ateapi` y otros dentro del nodo (ver
  [DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md)); los mínimos del gestor
  (704 y 512 MiB) paran solo a `ate-setup`. Se lee de nuevo con las demás
  lecturas, justo antes del paso 11.
- Las lecturas de partida, que se repiten justo antes del paso 11 y antes
  del apply de la marcha atrás (con `record_is`, que nombra lo que la
  ventana guardó). Sin ningún marker retenido. `deployed_last`, que solo
  va aquí y antes del `--check` de la marcha atrás anterior al apply, lee
  `DEPLOYED_VERSION.yml`, que cada playbook reescribe, y se detiene si el
  último fue otro que `ax-lab` o no está en el commit del registro: el
  `--check` de la marcha atrás desde `${PREV}` contaría entonces un
  `changed` más (ese fichero), y seguir con `HALT=` es aceptarlo sabiéndolo
  (ver «Marcha atrás»):

  ```bash
  must test -z "$(compgen -G '/run/lock/dockerswarm-*.marker')"
  lock_free
  office_idle
  lab_idle
  host_memory
  deployed_last
  ```

- `ax-lab-images.yml` ya en `main` y «Run workflow» comprobado: ver la
  regla de la siembra. Sin eso, la ejecución sobre el head del PR de pines
  no existe.
- Las dos descargas de la ejecución sobre el head del PR (los zip de
  `ax-lab-substrate-oci-layout` y de `ax-lab-ax-artifacts`) en un directorio
  del propietario, no en un tmpfs.
- El pico de memoria de `ate-setup` bajo el tope de 192 MiB. Es la primera
  vez que corre bajo ese tope en este host, con un binario nuevo, y la
  marcha atrás vuelve a ejecutar el anterior bajo el mismo. Hay una medida,
  del 2026-10-12, de una ejecución desechable de la CI en un clúster kind con
  los pines nuevos y los límites del laboratorio (192 MiB sin swap, 0,5 CPU,
  256 PIDs, 600 s por rollout y 6 480 s en total), que no se fusionó ni es
  una receta: sus ejecuciones, 38249431882, 38251504380 y 38254971937 de
  `apptolast/DockerSwarmInfrastrcture`, son solo la evidencia. El peor pico
  de cualquier instalación (nueva, repetida o sobre una con los pines
  anteriores) fue de 58 765 312 bytes (56,0 MiB, el 29,2 % del tope), contra
  49,6 y 51,9 MiB con los pines anteriores en las mismas condiciones, sin OOM,
  con el contador `max` de `memory.events` a 0, un margen de 3,4 veces, seis
  cargas sustituidas al actualizar y las doce listas. No hubo reinicios en la
  última ejecución; en las otras, un reinicio de `postgres-0` por una carrera
  de arranque que también se vio con los pines anteriores. Es el
  `memory_peak_bytes` que imprime el gestor (ver «Criterios de aborto»).
- Lo que esa medida no prueba: ningún worker vivo durante el relevo de
  `atelet`, ni AX sobre Substrate, ni un clúster poblado con los objetos de
  AX y de semanas de antigüedad, ni este kernel ni este Docker, ni la marcha
  atrás (instalar el `ate-setup` anterior sobre el nuevo no se midió). Por
  eso la comprobación que sí se puede hacer es la aceptación explícita del
  propietario: acepta, en el registro de la ventana, que la medida vale para
  este host, para el `ate-setup` nuevo y para el anterior de la marcha atrás,
  y sin ella no se pasa del paso 0. El listón de la ventana sigue siendo
  como mucho 134 MiB (140 509 184 bytes), es decir, un margen de al menos
  58 MiB, el 30 % del tope, y la medida de la CI lo cumple de sobra. El
  apartado «Pico de `ate-setup`» del registro lleva la medida de la CI
  (56,0 MiB), la aceptación (quién y cuándo) y el `memory_peak_bytes` que
  informe la instalación real: en un apply correcto el rol no lo imprime y se
  anota «no informado», y solo un fallo lo deja a la vista.
- La línea base no se anota aquí sino justo antes del paso 11, con todo ya
  sembrado y comprobado (ver el paso 11). El `memory.peak` del nodo no sirve
  para eso: está saturado por la caché de página (supera su propio
  `memory.max`), así que no informa y no se anota. Lo que sí se compara son
  los contadores `oom` de `memory.events` y `memory.events.local` y los
  reinicios de los pods.
- Una copia del estado del PVC de la Oficina (`ax-web-state`) en un
  directorio de root `0700` bajo `/var/backups/dockerswarm/`, como en las
  ventanas anteriores (ver [DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md)),
  tomada después de pausar la cola y de las lecturas de arriba. La ruta del
  volumen sale del propio PV (`office_path`), no se supone, y la copia corre
  bajo el lock como las demás; con la cola en pausa y sin trabajos en marcha
  es una copia en caliente de un volumen sin trabajos en marcha. No hay
  ningún marker retenido antes de empezar, y si la copia falla deja el suyo
  (ver «Un marker retenido»):

  ```bash
  office_path
  STATE=/var/backups/dockerswarm/ax-web-state-$(date -u +%Y%m%dT%H%M%SZ)
  must test -z "$(compgen -G '/run/lock/dockerswarm-*.marker')"
  must sudo -- install -d -o root -g root -m 0700 "${STATE}" "${STATE}/data"
  must sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
    --operation ax-lab-seed -- /usr/bin/docker cp -a \
    "kind-control-plane:${PV_PATH}/." "${STATE}/data"
  must test -n "$(sudo -- ls -A "${STATE}/data")"
  ```

  El volumen de Redis (`ax-redis-data`) no se copia. Las lecturas de partida
  dejan el laboratorio sin Tasks, que es lo único que Redis guarda (las
  claves `ax:task:default:<nombre>`, ver «Redis»), la ventana no cambia ni
  la imagen de Redis ni su volumen, y la marcha atrás restaura imágenes de
  la copia, no datos de Redis.
- En la CI, en verde sobre el head del PR: `validate`, los dos jobs de
  `ax-lab-reproducibility.yml` y los dos de `ax-lab-images.yml`.

### Pasos de la ventana de resiembra

Las variables y las funciones viven en la shell de `tmux`, así que los pasos
las usan tal cual, igual que los ayudantes del paso 0. Lo que esa shell no
puede perder está además en disco, en `${RELOCK}`, de root y fuera de `/tmp`
(un tmpfs en este host): `window.env`, los planes y la línea base. Si la
shell se pierde, ver «Recuperar la shell». Una guarda o una orden que falla
imprime `STOP:` y deja `HALT` puesto: se decide entonces entre seguir
(`HALT=`) y la marcha atrás, según los criterios de aborto. Si lo que falló
fue una orden con lock, su marker se recupera antes (ver «Un marker
retenido»).

1. En el clon operativo, como `admin` (uid y gid 1001) y sin `sudo`, desde
   el head del PR y sin tocar nada: calcular los pines y comprobarlos.
   `PR_HEAD` es el commit completo sobre el que corrió `ax-lab-images.yml`, y
   se asigna aquí, una sola vez, pegando el suyo entre las comillas; la guarda
   que sigue se detiene si lo que hay no es un commit completo:

   ```bash
   PR_HEAD='PEGAR_AQUÍ_EL_COMMIT_COMPLETO_DEL_HEAD_DEL_PR'
   [[ "${PR_HEAD}" =~ ^[0-9a-f]{40}$ ]] \
     || stop 'PR_HEAD no es un commit completo'
   must git fetch origin
   must git checkout --detach "${PR_HEAD:?}"
   must test -z "$(git status --porcelain --untracked-files=normal)"
   PREV=$(sed -n 's/^revision: "\([a-f0-9]\{40\}\)"$/\1/p' "${RECORD}")
   [[ "${PREV}" =~ ^[0-9a-f]{40}$ ]] \
     || stop 'ax-lab.yml no registra ningún commit aplicado'
   must git merge-base --is-ancestor "${PREV}" HEAD
   WORK=$(mktemp -d) || stop 'mktemp'
   must mkdir "${WORK}/prev"
   must tar -x -C "${WORK}/prev" < <(git archive "${PREV}")
   [[ -f "${WORK}/prev/scripts/validate-ax-lab.py" ]] \
     || stop 'git archive no dejó el commit aplicado'
   must plan . --substrate-plan >"${WORK}/sub-new.json"
   must plan . --ax-plan >"${WORK}/ax-new.json"
   must plan "${WORK}/prev" --substrate-plan >"${WORK}/sub-old.json"
   must plan "${WORK}/prev" --ax-plan >"${WORK}/ax-old.json"
   load_plans "${WORK}"
   ```

   `STALE_*` son los pines anteriores de las imágenes que cambian y
   `MOVED_*`, sus pines nuevos; el `forget` usa los primeros y la siembra los
   segundos. Las dos imágenes de buildx y la del panel no cambian en esta
   ventana, y las aserciones del bloque siguiente lo comprueban. Las
   etiquetas y los sha256 de la CLI salen de los planes y, lo que el plan
   anterior no trae, del contrato anterior (`cfg`); el digest de
   `ateom-gvisor`, el del worker, sale de los mismos pines (`WORKER_OLD` y
   `WORKER_NEW`):

   ```bash
   OLD_CFG=${WORK}/prev/config/ax-lab.yml
   load_facts "${WORK}" "${OLD_CFG}"
   for name in ax-task-runner ax-agents; do
     [[ "$(cfg config/ax-lab.yml ax images "${name}")" \
       == "$(cfg "${OLD_CFG}" ax images "${name}")" ]] \
       || stop "${name} cambia, y la ventana no la toca"
   done
   [[ "$(cfg config/ax-lab.yml web image tag)" == "${WEB_TAG}" ]] \
     || stop 'cambia el tag del panel'
   [[ "--image=ax-web=$(cfg config/ax-lab.yml web image digest)" \
     == "${WEB_PIN}" ]] || stop 'cambia el digest del panel'
   ```

   Si el tag de Substrate o el de AX cambiara, la copia no tendría dos
   digests bajo un mismo nombre y esta ventana no haría falta; las guardas
   de las etiquetas la detienen entonces. Las rutas y las funciones que usan
   los demás pasos son las definiciones del paso 0.

2. La instantánea, antes de que nada se mute. Se copia la copia de seguridad
   entera y la CLI con `cp -a`, bajo el lock host-global y nunca con
   `cp -al` (el gestor rechaza ficheros con otros enlaces duros), y se
   prueba con `image-status` sobre la instantánea, con los pines anteriores,
   en los tres conjuntos: Substrate, AX y el panel. `df` tiene que mostrar
   espacio para la copia (unos 250 MB de Substrate y 480 MB de AX, ver
   «Copia de seguridad» e «Imágenes de AX»), los artefactos y el ensayo. Lo
   primero que entra en `${RELOCK}` son los hechos de la ventana, ya de root
   y fuera de `/tmp`: `window.env` y `plans/` (los cuatro planes y el contrato
   anterior), que son lo único que «Recuperar la shell» lee. Si `${RELOCK}`
   existe, es de un intento anterior y puede guardar la única copia de antes
   de la ventana: no se borra ni se reutiliza, porque un `cp -a` encima
   anidaría la instantánea y el `image-status` leería la antigua. Si aquel
   intento llegó al paso 5, se termina primero su marcha atrás con ella;
   después, o si no llegó, se aparta con
   `sudo -- mv "${RELOCK}" "${RELOCK}.<fecha>"` y se repite el paso. Un
   `cp -a` que falla deja el marker (ver «Un marker retenido»): se recupera,
   se aparta la instantánea a medias del mismo modo y se repite el paso:

   ```bash
   df -h /var/backups
   sudo -- test ! -e "${RELOCK}" \
     || stop "${RELOCK} existe: es de un intento anterior, no se pisa"
   must test -z "$(compgen -G '/run/lock/dockerswarm-*.marker')"
   must sudo -- install -d -o root -g root -m 0700 "${RELOCK}" \
     "${RELOCK}/plans"
   save_window
   for file in sub-old sub-new ax-old ax-new; do
     must sudo -- install -m 0600 -o root -g root "${WORK}/${file}.json" \
       "${RELOCK}/plans/"
   done
   must sudo -- install -m 0600 -o root -g root "${OLD_CFG}" \
     "${RELOCK}/plans/ax-lab-old.yml"
   must sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
     --operation ax-lab-seed -- /usr/bin/cp -a "${LAYOUT}" "${SNAP_LAYOUT}"
   must sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
     --operation ax-lab-seed -- /usr/bin/cp -a "${CLI_BACKUP}" "${SNAP_CLI}"
   out sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py \
     image-status --image-set substrate --layout "${SNAP_LAYOUT}" \
     --tag "${TAG_SUB}" "${OLD_SUB[@]}"
   expect_complete 7 "${OUT}"
   out sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py \
     image-status --image-set ax --layout "${SNAP_LAYOUT}" \
     --tag "${TAG_AX}" "${OLD_KO[@]}" "${BUILDX[@]}"
   expect_complete 4 "${OUT}"
   out sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py \
     image-status --image-set web --layout "${SNAP_LAYOUT}" \
     --tag "${WEB_TAG}" "${WEB_PIN}"
   expect_complete 1 "${OUT}"
   [[ "$(sudo -- sha256sum "${SNAP_CLI}" | cut -d' ' -f1)" == "${CLI_OLD}" ]] \
     || stop 'la CLI de la instantánea no es la anterior'
   ```

   Sin una instantánea completa y legible por el gestor, no se sigue. Los
   ficheros de `${RELOCK}` los crea root con `install -m 0600` (el `umask`
   de `sudo` en este host es `0027`, y no se confía en él para el modo).

3. Extraer los artefactos como root en directorios de root `0700`, nunca
   bajo `/home` ni en un tmpfs, porque el gestor rechaza lo que no es de
   root o es escribible por otros. `SUBSTRATE_ZIP` y `AX_ZIP` son los zip
   descargados. Con el `umask` `0007` de `admin`, `sudo` corre con el `0027`
   de root: lo extraído queda `0640` y `0750`, sin bits de escritura para
   nadie más, que es lo que la comprobación siguiente y el gestor piden.
   Después, la CLI, que se comprueba antes de instalarla, en una sola cadena
   con `&&` para que nada la ejecute tras una comprobación fallida: su
   sha256 contra el pin, que nada de lo extraído sea escribible por otros ni
   de otro dueño, `chmod 0700` (la extracción no conserva el bit de
   ejecución), que `ldd` no diga `not found` y `ax help`, que sale con 0 en
   una CLI sana (`--help` sale con 1 y no comprueba nada). Después, la glibc
   del host. La ejecución de la CI ya falló si la CLI pedía una glibc más
   nueva que la 2.43, pero solo esta ejecución prueba el binario en el host:

   ```bash
   must sudo -- install -d -o root -g root -m 0700 "${IN}" "${IN}/substrate" \
     "${IN}/ax"
   must sudo -- /usr/bin/python3 -I -m zipfile -e "${SUBSTRATE_ZIP:?}" \
     "${IN}/substrate"
   must sudo -- /usr/bin/python3 -I -m zipfile -e "${AX_ZIP:?}" "${IN}/ax"
   must sudo -- jq . "${IN}/substrate/pins.json" "${IN}/ax/pins.json"
   # Un fallo de cualquier eslabón es un STOP: es lo que `A && B || C` hace.
   # shellcheck disable=SC2015
   [[ "$(sudo -- sha256sum "${IN}/ax/ax" | cut -d' ' -f1)" == "${CLI_NEW}" ]] \
     && test -z "$(sudo -- find "${IN}" \( -perm /022 -o ! -user root \) \
       -print)" \
     && sudo -- chmod 0700 "${IN}/ax/ax" \
     && ! sudo -- ldd "${IN}/ax/ax" | grep -q 'not found' \
     && sudo -- "${IN}/ax/ax" help >/dev/null \
     || stop 'la CLI no pasa la comprobación: no se instala'
   getconf GNU_LIBC_VERSION
   ```

   Los `pins.json` dicen de qué ejecución y con qué pines se compiló cada
   artefacto; tienen que ser los de los planes.

4. El ensayo: sembrar los pines nuevos en un layout de prueba y comprobarlos
   con `image-status`, con los mismos artefactos y las mismas órdenes que la
   siembra real y sin tocar la copia viva. Lo que falle aquí (un zip malo,
   otro dueño, un pin que falta, un blob corrupto) falla antes del `forget`,
   y es para lo que está; aun así, un `seed-layout` que falla deja el marker
   (ver «Un marker retenido»), que se recupera antes de repetir. El bloque
   empieza borrando `${REH}`: es solo de ensayo, y un ensayo repetido no
   tiene que ver restos del anterior:

   ```bash
   must test -z "$(compgen -G '/run/lock/dockerswarm-*.marker')"
   must sudo -- rm -rf "${REH:?}"
   must sudo -- install -d -o root -g root -m 0700 "${REH}"
   for pin in "${MOVED_SUB[@]}"; do
     seed_one substrate "${IN}/substrate" "${REH}/images" "${TAG_SUB}" \
       "${pin}" || break
   done
   for pin in "${MOVED_KO[@]}"; do
     name=${pin#--image=}; name=${name%%=*}
     seed_one ax "${IN}/ax/ko-${name}" "${REH}/images" "${TAG_AX}" \
       "${pin}" || break
   done
   out sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py \
     image-status --image-set substrate --layout "${REH}/images" \
     --tag "${TAG_SUB}" "${MOVED_SUB[@]}"
   expect_complete "${#MOVED_SUB[@]}" "${OUT}"
   out sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py \
     image-status --image-set ax --layout "${REH}/images" \
     --tag "${TAG_AX}" "${MOVED_KO[@]}"
   expect_complete "${#MOVED_KO[@]}" "${OUT}"
   must sudo -- rm -rf "${REH:?}"
   ```

5. El `forget`, por los digests anteriores literales de los planes del
   commit aplicado, para que la guarda de digest exacto compruebe algo. Antes,
   dos guardas: ningún marker, y que el registro del último apply sigue
   nombrando el commit que la ventana guardó (ningún `ax-lab` ni release de
   `ax-web` aplicó entre el paso 0 y aquí, que mueve ese registro y deja
   `PREV` viejo). Cada imagen se decide antes con `image-status` y su pin
   anterior: solo se retira la que sigue completa con él, así que el gestor
   no se niega nunca, y la que ya no está (un paso repetido) se salta. Si un
   `forget` falla de verdad, deja el marker (ver «Un marker retenido»), el
   bucle se detiene y las anteriores en orden alfabético ya están retiradas:
   tras recuperar el marker se repite el bloque, que solo toca lo que falta,
   o se hace la marcha atrás. Los blobs se quedan, y no hay orden que vuelva
   a añadir una entrada salvo `seed-layout` desde una fuente que la liste: la
   instantánea es el camino de vuelta. Desde aquí rige, más estrictamente, la
   congelación:

   ```bash
   must test -z "$(compgen -G '/run/lock/dockerswarm-*.marker')"
   record_is "${PREV:?}"
   for old in "${STALE_SUB[@]}"; do
     forget_one substrate "${LAYOUT}" "${TAG_SUB}" "${old}" || break
   done
   for old in "${STALE_KO[@]}"; do
     forget_one ax "${LAYOUT}" "${TAG_AX}" "${old}" || break
   done
   ```

6. La siembra real, siempre con `--image-set` explícito (`seed-layout` toma
   `web` por defecto), con los pines nuevos de los planes. Una imagen ko por
   llamada, porque el `index.json` de cada layout de ko lista solo la suya,
   y cada imagen se decide antes con `image-status` y su pin nuevo: se
   siembra la que falta y se salta la que ya está completa (el gestor daría
   `present`, pero ni se le pide el lock). Si la copia nombra ese
   `<nombre>:<tag>` con otro digest, el `forget` no se hizo y se para antes
   de pedir un `seed-layout` que el gestor rechazaría. Si una siembra falla,
   deja el marker (ver «Un marker retenido») y las anteriores en orden
   alfabético quedan sembradas: tras recuperarlo se repite el bloque, y se
   juzga con el `image-status` del paso 7 y no con la cuenta de una
   repetición, que ya no ve `seeded` en todas:

   ```bash
   must test -z "$(compgen -G '/run/lock/dockerswarm-*.marker')"
   for pin in "${MOVED_SUB[@]}"; do
     seed_one substrate "${IN}/substrate" "${LAYOUT}" "${TAG_SUB}" \
       "${pin}" || break
   done
   for pin in "${MOVED_KO[@]}"; do
     name=${pin#--image=}; name=${name%%=*}
     seed_one ax "${IN}/ax/ko-${name}" "${LAYOUT}" "${TAG_AX}" \
       "${pin}" || break
   done
   ```

7. Comprobar la copia, de solo lectura y sin registro, con los pines nuevos
   de los tres conjuntos. Las dos imágenes de buildx y la del panel prueban
   que el `forget` y la siembra no las tocaron.

   ```bash
   out sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py \
     image-status --image-set substrate --layout "${LAYOUT}" \
     --tag "${TAG_SUB}" "${NEW_SUB[@]}"
   expect_complete 7 "${OUT}"
   out sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py \
     image-status --image-set ax --layout "${LAYOUT}" \
     --tag "${TAG_AX}" "${NEW_KO[@]}" "${BUILDX[@]}"
   expect_complete 4 "${OUT}"
   out sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py \
     image-status --image-set web --layout "${LAYOUT}" \
     --tag "${WEB_TAG}" "${WEB_PIN}"
   expect_complete 1 "${OUT}"
   ```

8. La copia de la CLI, que el rol exige con el sha256 fijado también en
   `--check` (`ax_host.yml`) y que sustituye a la única copia anterior, la
   de la instantánea. Ya se comprobó su sha256, su ejecución y su glibc en el
   paso 3. Solo se instala si la copia no tiene ya el sha256 nuevo, y un
   `install` que falla deja el marker (ver «Un marker retenido»):

   ```bash
   must test -z "$(compgen -G '/run/lock/dockerswarm-*.marker')"
   if [[ "$(sudo -- sha256sum "${CLI_BACKUP}" | cut -d' ' -f1)" \
     == "${CLI_NEW}" ]]; then
     echo 'la copia de la CLI ya es la nueva'
   else
     must sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
       --operation ax-lab-seed -- /usr/bin/install -o root -g root -m 0600 \
       "${IN}/ax/ax" "${CLI_BACKUP}"
   fi
   [[ "$(sudo -- sha256sum "${CLI_BACKUP}" | cut -d' ' -f1)" \
     == "${CLI_NEW}" ]] || stop 'la CLI de la copia no es la nueva'
   ```

9. Fusionar el PR de pines, ahora y no antes, con `main` congelado, y dejar
   el clon operativo en el commit de fusión, limpio y con los mismos planes
   que se sembraron. El commit de fusión (`MERGE`) se guarda en
   `window.env`, junto a los otros dos: tras el apply el registro del último
   apply lo nombra, y es el único sitio donde la recuperación lo encuentra.
   A partir de aquí, `main` y la copia coinciden:

   ```bash
   must git fetch origin
   must git checkout --detach origin/main
   must test -z "$(git status --porcelain --untracked-files=normal)"
   must diff <(plan . --substrate-plan) "${WORK}/sub-new.json"
   must diff <(plan . --ax-plan) "${WORK}/ax-new.json"
   MERGE=$(git rev-parse HEAD)
   [[ "${MERGE}" =~ ^[0-9a-f]{40}$ ]] || stop 'MERGE no es un commit completo'
   save_window
   git diff --stat "${PREV}" "${MERGE}" -- ansible
   ```

   El último comando solo lee: lista lo que cambió en el rol, sus plantillas y
   sus ficheros entre lo aplicado y la fusión. Lo normal es que no imprima
   nada, y el `--check` siguiente cuenta con eso (ver su `changed=3`).

10. `--check`, con el wrapper como `admin` y sin `sudo` (`--local` ejecuta
    Ansible por `sudo` él mismo). Leerlo entero: `image-status` no se
    detiene y los informes del plan, las tareas `debug` que el rol ejecuta
    solo en `--check`, son estos, en este orden, y ningún otro:

    - el del nodo, una línea: `the existing node has its reviewed preboot
      sysctl mask`;
    - el plan del clúster, una sola línea: `then, only where they differ:
      proxy_arp, proxy_ndp, the registry mirror and the local-registry-hosting
      ConfigMap inside the node`;
    - el de Substrate: una línea `restore into the registry: <imagen>` por
      cada pin de `MOVED_SUB` (cada imagen re-fijada aparece movida en el
      registro y completa en la copia: se importa; en esta ventana, las siete,
      `ate-setup` incluida), y `install Substrate with ate-setup: state,
      workloads`: la deriva de Substrate es el estado y las cargas;
    - el de AX: una línea `restore into the registry: <imagen>` por cada pin
      de `MOVED_KO` (las dos de ko), y `AX is read once the lab node runs and
      Substrate matches its pinned install`;
    - el del panel: `the web panel is read once AX matches its pinned
      install` y `ax-web-edge runs as reviewed`;
    - en el resumen, `changed=3`: la CLI («Install the AX CLI from its backup
      only when it differs», porque la copia ya tiene el sha256 nuevo y el
      instalado no) y los dos ficheros de `deployment_metadata`, que llevan
      el commit. Esos tres suponen que de `PREV` a la fusión no cambia nada de
      lo que el rol instala en `--check` (los binarios fijados, el sysctl, las
      herramientas del operador y los ejemplos): si el `git diff --stat` del
      paso 9 listó algo del rol, saldrán más `changed` por eso, y se leen
      contra esa lista, no se toman por un fallo.

    Ni una línea `back up from the registry` ni `in neither backup nor
    registry`, ni ninguna `the apply stops`, ni `clone`, `build with the
    fallback toolbox` o `kind create cluster`. Lo que este `--check` no
    muestra, porque el rol solo lee AX cuando Substrate ya coincide con lo
    fijado: el diff de AX, el WorkerPool, el `--route-timeout` del router y
    los workers obsoletos se evalúan solo dentro del apply (la prueba del
    paso 12 los cubre). Qué pods se reinician exactamente no se ha medido.
    Un `--check` que falla retiene el marker del wrapper,
    `/run/lock/dockerswarm-ansible.marker` (ver «Un marker retenido»). El PR
    de pines ya está fusionado (paso 9, como manda la regla del repositorio):
    abortar a partir de aquí exige además el PR que lo revierta (ver «Marcha
    atrás»).

    ```bash
    must ./scripts/deploy-ansible.sh --playbook ax-lab --local --check
    ```

11. El apply. El rol importa los tags movidos desde la copia (la
    importación sobrescribe un tag movido), reinstala Substrate si sus
    cargas derivan, despliega las dos imágenes de AX con su digest y, como
    el WorkerPool lleva el digest de `ateom-gvisor`, sustituye también el
    worker (el despliegue `ax` de `ax-workers`). Entre el paso 0 y aquí
    pasaron la CI, la fusión y dos `--check`: antes de nada se leen otra vez
    la Oficina (en pausa y sin trabajo en marcha: uno es un `STOP`, se
    espera a que termine y no se aplica), AX, la memoria, los contenedores
    transitorios y el registro del último apply, que no puede haber cambiado:

    ```bash
    must test -z "$(compgen -G '/run/lock/dockerswarm-*.marker')"
    lock_free
    record_is "${PREV:?}"
    office_idle
    lab_idle
    host_memory
    no_transient
    ```

    Después, la línea base, que compara el paso 12 y la marcha atrás: los
    contadores de `memory.events` y de `memory.events.local` del nodo y del
    registro, `anon` de `memory.stat` y `memory.current`, la tabla de
    reinicios de los pods que el apply no sustituye y la lista de los que
    sí. Que `memory.events` jerárquico ya lea `oom_kill` distinto de cero
    (el incidente del 2026-10-09 lo dejó así) no cuenta: el criterio es que
    no se mueva del valor que se anota ahora. Se guarda en
    `${RELOCK}/baseline/`, de root, y se anota en el registro de la ventana:

    ```bash
    must sudo -- install -d -o root -g root -m 0700 "${RELOCK}/baseline"
    lab_memory >"${WORK}/memory.txt" || stop 'la línea base de memoria'
    pod_table unreplaced >"${WORK}/restarts.txt" || stop 'los reinicios'
    pod_table replaced >"${WORK}/replaced.txt" || stop 'los pods sustituidos'
    for file in memory restarts replaced; do
      must sudo -- install -m 0600 -o root -g root "${WORK}/${file}.txt" \
        "${RELOCK}/baseline/"
    done
    must cat "${WORK}/memory.txt" "${WORK}/restarts.txt"
    ```

    El apply no se interrumpe con `Ctrl-C` ni cerrando la terminal (ver «Lo
    que se ve durante el apply» y «Criterios de aborto»). Un apply fallido
    retiene el marker del wrapper, que hay que recuperar antes de la marcha
    atrás (ver «Un marker retenido»):

    ```bash
    must ./scripts/deploy-ansible.sh --playbook ax-lab --local \
      --confirm-production
    ```

12. Verificar, con el registro, sin dar nada por bueno hasta tener todo. El
    registro se nombra por `127.0.0.1:5001`, nunca por `localhost`: en este
    host `localhost` resuelve primero a `::1`, el oyente IPv6 lo cortan las
    reglas del cortafuegos y el gestor espera 120 s de conexión por imagen,
    unos 22 minutos sin imprimir nada:

    ```bash
    out sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py \
      image-status --image-set substrate --registry 127.0.0.1:5001 \
      --layout "${LAYOUT}" --tag "${TAG_SUB}" "${NEW_SUB[@]}"
    expect_pinned 7 "${OUT}"
    out sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py \
      image-status --image-set ax --registry 127.0.0.1:5001 \
      --layout "${LAYOUT}" --tag "${TAG_AX}" "${NEW_KO[@]}" "${BUILDX[@]}"
    expect_pinned 4 "${OUT}"
    for cli in /opt/dockerswarm/ax-lab/bin/ax "${CLI_BACKUP}"; do
      [[ "$(sudo -- sha256sum "${cli}" | cut -d' ' -f1)" == "${CLI_NEW}" ]] \
        || stop "${cli} no es la CLI nueva"
    done
    ```

    Las dos CLI tienen que dar `ax.cli_sha256` (la aserción lo exige). Ni el
    apply ni el último `--check` ven el worker nuevo: la espera del rol por
    el WorkerPool lee `readyReplicas`, que durante el relevo cuenta el
    pod viejo y pasa al instante, y `cluster-status` solo mira `ate-system`,
    `otel-system` y `podcertificate-controller-system`. Por eso, antes de
    cerrar, el rollout del despliegue `ax` de `ax-workers` (hasta 300 s),
    exactamente un pod de worker que no se esté terminando, con el digest
    nuevo de `ateom-gvisor` y listo (si el rollout pasó antes de que el rol
    cambiara el despliegue, el pod se relee hasta 30 veces, con 10 s de
    pausa, unos 5 minutos como el rollout, antes de dar la guarda por
    fallida), y una prueba de humo de solo
    lectura, como la del cambio 4 (ver «Ventana del cambio 4»): `ax-tarea`
    crea un Workspace con `octocat/Hello-World` y una Task con Claude (como
    mucho 4 turnos, `TURNOS=4`; la del 2026-09-26 sobre ese repositorio gastó
    3 y 0,0358 USD, en la cuota de Claude), con el token de
    `/etc/dockerswarm/ax/claude-oauth-token`, y las borra al terminar; con
    `lab_idle` se comprueba que no queda ninguna Task ni ningún `ax` en
    marcha. El worker viejo puede seguir en `Terminating` mucho tiempo (su
    gracia es de 3 600 s) y no es un error; tampoco que `readyReplicas` valga
    2 un momento.
    `ax-tarea` se niega a empezar con un marker retenido, así que corre con el
    apply ya cerrado, y antes del último `--check`, que prueba que no dejó
    nada:

    ```bash
    worker_gate "${WORKER_NEW}"
    must sudo -- /usr/bin/env TURNOS=4 RAMA=master /usr/local/sbin/ax-tarea \
      https://github.com/octocat/Hello-World \
      'Lee el README y resume en una frase qué contiene. No modifiques nada.' \
      claude
    lab_idle
    ```

    El último `--check` tiene que informar `changed=0`; si se detiene,
    retiene el marker del wrapper (ver «Un marker retenido»). Con él, la
    comparación con la línea base del paso 11: los contadores `oom`,
    `oom_kill` y `oom_group_kill` de los dos `memory.events` del nodo y del
    registro, y los reinicios de los pods que el apply no sustituyó:

    ```bash
    must ./scripts/deploy-ansible.sh --playbook ax-lab --local --check
    must diff <(sudo -- cat "${RELOCK}/baseline/memory.txt" | oom_lines) \
      <(lab_memory | oom_lines)
    must diff <(sudo -- cat "${RELOCK}/baseline/restarts.txt") \
      <(pod_table unreplaced)
    ```

    Después, el resto de «Verificación» (nodo `Ready`, DaemonSet `atelet`,
    `sudo ax get tasks -a default`) y las cifras para
    [CAPACITY.md](CAPACITY.md): `anon` y `memory.current` del nodo, que
    informan donde el `memory.peak` saturado no, y el `MemAvailable`.

13. Cerrar. Solo con el último `changed=0` termina la congelación. Se
    borran los artefactos extraídos y los planes de trabajo, `${IN}` y
    `${WORK}` y nada más: lo que «Recuperar la shell» lee (`window.env`,
    `plans/` y `baseline/`, dentro de `${RELOCK}`) se queda con la
    instantánea (`${SNAP_LAYOUT}` y `${SNAP_CLI}`) hasta que el propietario
    acepte la resiembra, y entonces se borra `${RELOCK}` entera. El fichero
    temporal de `out` (`${OUTFILE}`) se queda mientras la marcha atrás siga
    siendo posible. La ventana se registra como las anteriores (ver
    [DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md)), con el pico de
    `ate-setup` (el de la CI, la aceptación del propietario y el que informó
    la instalación real, si lo hizo), la línea base y la final, el resultado
    de la prueba de humo y la hora de la pausa y de la reanudación de la
    cola:

    ```bash
    must sudo -- rm -rf "${IN:?}"
    must rm -rf "${WORK:?}"
    ```

    Lo último, y solo ahora, la cola de la Oficina: quien la usa pulsa
    «Reanudar la cola» y se lee de vuelta:

    ```bash
    office_path
    out sudo -- /usr/bin/docker exec kind-control-plane cat \
      "${PV_PATH}/office.json"
    jq -e '.settings.queue_paused == false' <<<"${OUT}" >/dev/null \
      || stop 'la cola de la Oficina sigue en pausa'
    ```

### Lo que se ve durante el apply

Lo normal, para no abortar un apply sano ni esperar a uno atascado (los
tiempos son estimaciones de los applies anteriores, no se han medido con
estas imágenes):

- La CLI de `/opt/dockerswarm/ax-lab/bin/ax` se sustituye al empezar.
- `ate-setup` corre en el contenedor `ax-lab-ate-setup`, que solo dura lo que
  dura la instalación, y sustituye en este orden `podcertificate-controller`
  y, de una vez, `ate-api-server` (tres pods a la vez: dos réplicas y una de
  sobrecarga), `ate-controller`, `atenet-router`, `atenet-egress` y `atelet`.
  `atelet`, un DaemonSet que sustituye de uno en uno, desaparece entre 10 y
  60 s, y hasta 330 s mientras drena.
- `ax-server` se sustituye con sonda de readiness (el viejo sirve hasta que
  el nuevo está listo); `ax-controller` no tiene sonda: hay un hueco de unos
  10 a 20 s en que se salta eventos (google/ax#354), y el rol espera a que el
  nuevo diga «starting AX task worker».
- `atenet-router` se sustituye dos veces: la de `ate-setup` y la que le
  vuelve a poner `--route-timeout=1h`.
- El worker se sustituye (despliegue `ax` de `ax-workers`, con un pod más
  mientras dura): el viejo se queda `Terminating` mucho tiempo, y
  `readyReplicas` del WorkerPool puede valer 2 un momento.
- Un pod nuevo puede reiniciarse una o dos veces mientras espera sus
  certificados de pod.
- `postgres-0`, que el apply no sustituye, reinició una vez por
  `FailedPostStartHook` (el paquete de certificados del pod aún sin emitir)
  en algunas instalaciones nuevas de la CI, también con los pines anteriores,
  y ninguna actualización de un clúster asentado añadió otro. Si pasa aquí,
  es un reinicio más que la línea base y se lee su causa antes de decidir
  (ver «Criterios de aborto»).
- La Oficina sigue sirviendo (su pod no se toca), pero lo que despacha por
  `ax-server` falla mientras este se sustituye: unos 5 a 12 minutos con el
  plano de control de AX degradado. Con la cola en pausa no hay nada en
  vuelo.
- En total, de 8 a 15 minutos (sin medir).

Para mirar sin tocar, en un segundo panel de `tmux`, solo lecturas:

```bash
sudo -- /usr/bin/env HOME=/opt/dockerswarm/ax-lab/home \
  /opt/dockerswarm/ax-lab/bin/kubectl \
  --kubeconfig /opt/dockerswarm/ax-lab/home/.kube/config \
  --context kind-kind get pods --all-namespaces --watch
```

y, en otro, `kubectl get events --all-namespaces --watch` con el mismo
prefijo, `sudo -- /usr/bin/docker ps --all --filter name=ax-lab-` para el
contenedor transitorio y, durante `ate-setup`, `sudo -- /usr/bin/docker
stats --no-stream ax-lab-ate-setup` (una muestra del uso, no el pico: en un
apply correcto el rol no imprime el resultado del gestor). Lo que decide el
aborto es que el rol falle, no el reloj: por debajo de unos 10 minutos por
Deployment no se interrumpe nada (ver «Criterios de aborto»).

### Recuperar la shell

Si la shell de `tmux` se pierde después del paso 2 (un panel abierto por
error, el servidor de `tmux`, una desconexión que lo mata, un reinicio), la
ventana no se retoma de memoria. Las variables, los arrays y las funciones
mueren con ella, y `${WORK}` también: vive en `/tmp`, que aquí es un tmpfs.
Tampoco se reconstruyen del registro del último apply
(`/opt/dockerswarm/deployments/ax-lab.yml`): `deployment_metadata` lo
reescribe con cada apply correcto, así que desde el paso 11 nombra el commit
de fusión y no el de la copia. Todo sale de `${RELOCK}`, que es de root y está
en `/var/backups`: `window.env` (`PR_HEAD`, `PREV` y, desde el paso 9,
`MERGE`), `plans/` (los cuatro planes y el contrato anterior) y `baseline/`.
Antes del paso 2 no hay nada que recuperar: se repite desde el paso 1. Tras
un `STOP` o un corte, antes de nada, `ls /run/lock/dockerswarm-*.marker` y la
guarda de contenedores transitorios (ver «Un marker retenido»). Después,
desde el clon operativo, en una `tmux` nueva, se pegan por este orden: los
ayudantes y los tres bloques de definiciones del paso 0 (sin tocar nada de
los pasos 1 a 13) y este bloque, que no repite ninguna función:

```bash
sudo -- test -f "${RELOCK}/window.env" \
  || stop "falta ${RELOCK}/window.env: el paso 2 no llegó a guardarlo"
PR_HEAD='' PREV='' MERGE=''
while IFS='=' read -r key value; do
  case "${key}" in
  PR_HEAD | PREV | MERGE) printf -v "${key}" '%s' "${value}" ;;
  *) stop "window.env trae una clave desconocida: ${key}" ;;
  esac
done < <(sudo -- cat "${RELOCK}/window.env")
[[ "${PR_HEAD}" =~ ^[0-9a-f]{40}$ && "${PREV}" =~ ^[0-9a-f]{40}$ ]] \
  || stop 'window.env no trae PR_HEAD y PREV completos'
[[ -z "${MERGE}" || "${MERGE}" =~ ^[0-9a-f]{40}$ ]] \
  || stop 'el MERGE de window.env no es un commit completo'
must git cat-file -e "${PREV}^{commit}"
must git merge-base --is-ancestor "${PREV}" "${PR_HEAD}"
WORK=$(mktemp -d) || stop 'mktemp'
for file in sub-old sub-new ax-old ax-new; do
  must sudo -- cat "${RELOCK}/plans/${file}.json" >"${WORK}/${file}.json"
done
must mkdir -p "${WORK}/prev/config"
must sudo -- cat "${RELOCK}/plans/ax-lab-old.yml" \
  >"${WORK}/prev/config/ax-lab.yml"
OLD_CFG=${WORK}/prev/config/ax-lab.yml
load_plans "${WORK}"
load_facts "${WORK}" "${OLD_CFG}"
printf 'PR_HEAD=%s PREV=%s MERGE=%s HALT=[%s]\n' \
  "${PR_HEAD}" "${PREV}" "${MERGE}" "${HALT}"
```

El bloque lee solo `${RELOCK}`: un `${WORK}` nuevo con una copia de lo
guardado, para que el paso 9 y el paso 13 sigan valiendo. Después de esto, se
sigue desde el principio del paso que estaba en marcha (las guardas piden
solo lo que falta) o se hace la marcha atrás, que usa las mismas variables.
No lee `${IN}` ni el registro del último apply, y el paso 13, que solo
borra `${IN}` y `${WORK}`, no borra nada de lo que lee; `${RELOCK}` entera
se borra solo al aceptar el propietario la resiembra.

### Un marker retenido

Las órdenes que toman el lock host-global lo toman con
`host_global_operation_lock.py run`: el `cp -a` de la instantánea, el
`install` de la CLI y el `docker cp` del paso 0 lo llevan escrito, y el
gestor se re-ejecuta él mismo bajo ese lock en `forget` y `seed-layout`
porque el host tiene Swarm activo. Cuando una de ellas sale con un código
distinto de cero (un fallo, o la negativa limpia del gestor, «names … with
…, not …»), el wrapper deja a propósito `/run/lock/dockerswarm-direct.marker`,
porque pudo quedar una escritura a medias, y la orden siguiente con lock se
rechaza («an active or stale operation marker exists») hasta que se
recupere. Un `deploy-ansible.sh` fallido, también en `--check`, retiene el
suyo, `/run/lock/dockerswarm-ansible.marker`, y cada uno rechaza también las
órdenes con lock del otro tipo. Una vez retenido, nada de la ventana lo
evita: ni repetir la orden ni la marcha atrás, que usa las mismas órdenes.
Nada avisa de un marker retenido (ningún servicio lo lee, ni la monitorización
ni una alerta): después de cada `STOP` se lista a mano
`ls /run/lock/dockerswarm-*.marker`.

Se recupera con el procedimiento de «Stale marker recovery» en `CLAUDE.md`
(y «Recuperar un marker abandonado» en [OPERATIONS.md](OPERATIONS.md)), sin
atajos: no se borra el fichero ni se fuerza nada. Solo después de comprobar
que el proceso que lo dejó está parado y que nada más muta (la propia
herramienta lo comprueba por `/proc`), y con la confirmación exacta que
imprime el dry-run, copiada byte a byte y nunca reconstruida. Se hace antes
de reiniciar el host, nunca después: el reinicio vacía `/run` y con él la
prueba de que el proceso original ya no existe. Primero, qué marker hay y el
directorio de evidencias, que se archiva antes de retirar el marker:

```bash
ls /run/lock/dockerswarm-*.marker
sudo -- install -d -o root -g root -m 0700 /var/backups/dockerswarm
```

El marker directo, que dejan el gestor, el `cp -a`, el `install` y el
`docker cp`: el primer comando es el dry-run y solo imprime la confirmación;
el segundo la lleva en lugar de `CONFIRMACION_EXACTA_MOSTRADA`:

```bash
sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py recover
sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py recover \
  --apply --confirm 'CONFIRMACION_EXACTA_MOSTRADA'
```

El marker de Ansible, que deja un `deploy-ansible.sh` fallido, con otro
script. El `ID_64_HEX` de `CLAUDE.md` es el `operation_id` del propio marker:

```bash
ID=$(sudo -- jq -r .operation_id /run/lock/dockerswarm-ansible.marker)
sudo -- /usr/bin/python3 scripts/ansible-operation-lock.py recover \
  --operation-id "${ID}"
sudo -- /usr/bin/python3 scripts/ansible-operation-lock.py recover \
  --operation-id "${ID}" --apply --confirm 'CONFIRMACION_EXACTA_MOSTRADA'
```

Dos salvedades. Si el dry-run se niega y lista un proceso que muta (otro
`ansible-playbook`, `apt` o `dpkg`, un `AnsiballZ_`, un `docker stack`,
`service`, `swarm` o `node` de cualquier proceso del host, también de un
contenedor, como el del autoupdater), no hay nada roto: se espera a que
termine y se repite el dry-run al cabo de un minuto, sin forzar. Y la
recuperación con éxito no prueba que el apply esté muerto: la herramienta
solo mira esos procesos, no ve una `install` del gestor, un contenedor de
`ate-setup` ni una tarea asíncrona de Ansible que sigan en el host.

Por eso, con el marker recuperado y antes de repetir nada, la guarda de
contenedores transitorios (ver «Contenedores transitorios»): el gestor deja
`ax-lab-ate-setup` y `ax-lab-build-*` parados, con sus logs, ante cualquier
interrupción, y el rol se niega a correr mientras exista uno, así que el
apply siguiente, o el de la marcha atrás, retendría un segundo marker. Se
pega sola, y su salida dice el nombre del contenedor; en un host limpio no
imprime nada y no hace falta nada más:

```bash
no_transient
```

Si imprime un contenedor en marcha, se espera a que termine: lo acota su
propio tiempo máximo, 6 480 s la instalación y 3 600 s una compilación (no
se mata ni se quita en marcha). Si está parado, se pega este segundo bloque,
solo entonces y con ese nombre en lugar del marcador: se leen sus logs y se
borra con el lock host-global y la operación `ax-lab-transient`, la de
«Contenedores transitorios»: `host_global_operation_lock.py` solo valida la
forma del nombre de la operación, no hay lista. `LEFTOVER` es el nombre que
imprimió la guarda, y `HALT=` levanta su `STOP` (es la decisión de borrarlo),
pero solo si el nombre es el de un transitorio del gestor: con el marcador
sin cambiar, o con otro nombre, el `STOP` sigue y nada de lo que viene
después corre. Un `docker rm` que falla, como el de un contenedor en marcha,
deja el marker directo, así que antes se comprueba que está parado:

```bash
LEFTOVER='NOMBRE_QUE_IMPRIMIÓ_LA_GUARDA'
if [[ "${LEFTOVER}" =~ ^ax-lab-(ate-setup|build-[a-z-]+)$ ]]; then
  HALT=
else
  stop 'LEFTOVER no es un contenedor transitorio del gestor'
fi
out sudo -- /usr/bin/docker ps --all --filter "name=^${LEFTOVER}\$" \
  --filter status=exited --format '{{.Names}}'
if [[ -z "${HALT}" && "${OUT}" != "${LEFTOVER}" ]]; then
  stop "${LEFTOVER} no está parado: se espera"
fi
must sudo -- /usr/bin/docker logs --tail 200 "${LEFTOVER}"
must sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
  --operation ax-lab-transient -- /usr/bin/docker rm "${LEFTOVER}"
no_transient
```

Con el marker recuperado y sin contenedores transitorios, el paso que falló
se repite desde su principio, con `HALT=` (las guardas piden solo lo que
falta; el paso 2, solo tras apartar `${RELOCK}`), o se hace la marcha atrás,
según los criterios de aborto. Siempre que se pueda, el helper de
recuperación de un apply fallido se ejecuta desde el commit que su marker
registra como `source_revision`.

### Criterios de aborto

La ventana se detiene, y según dónde pare se recorre la marcha atrás, cuando:

- falla una orden o una aserción de los pasos 1 a 8, o de los ayudantes del
  paso 0 (un `STOP:`). Antes del `forget` (pasos 1 a 4) la copia viva no se
  ha tocado y solo se borran el ensayo y los artefactos extraídos; no hay
  marcha atrás que reanude la cola, así que hay que reanudar la cola y
  leerla de vuelta (el último bloque del paso 13); desde el paso 5 se deshace
  por imagen, desde la instantánea;
- una orden con lock (el gestor en `forget` y `seed-layout`, el `cp -a`, el
  `install` o el `docker cp`) falla o se rechaza: deja
  `/run/lock/dockerswarm-direct.marker`, y ninguna orden con lock vuelve a
  correr mientras exista, tampoco las de la marcha atrás. Se recupera antes
  de seguir o de volver atrás (ver «Un marker retenido»);
- la lectura de partida del paso 0 o la de justo antes del paso 11 se
  detiene: la Oficina sin pausa, con un trabajo en marcha o con un horario
  activo; una Task, un `ax-tarea` o un `ax` en marcha (un túnel es un scope
  `ax-cli-*`); `MemAvailable` por debajo de 3 GiB; `fuser` ausente; un
  contenedor transitorio del gestor; `DEPLOYED_VERSION.yml` con otro playbook
  que `ax-lab` como último; o el registro del último apply con un
  commit que no es el que la ventana guardó, que es un apply ajeno. Un
  trabajo en marcha se espera, no se interrumpe: no se aplica con él. Un
  apply ajeno de `ax-lab` no se arregla repitiendo la lectura: `PREV` ya no
  describe lo que hay. Si la ventana no sigue, la cola se reanuda como en el
  primer punto;
- el `--check` del paso 10 se detiene (el rol falla), informa algo perdido
  (`in neither backup nor registry`), una línea `the apply stops` o
  `back up from the registry`, otras imágenes en `restore into the registry`
  que las de `MOVED_SUB` y `MOVED_KO`, o un `changed` que no es el de los tres
  ficheros descritos (la CLI y los dos de `deployment_metadata`) y que el
  `git diff --stat` del paso 9 no explica, o el apply
  del paso 11 falla. Como el PR de pines ya está fusionado desde el paso 9,
  abortar aquí exige además el PR que lo revierta. Un `deploy-ansible.sh`
  fallido retiene `/run/lock/dockerswarm-ansible.marker`, que se recupera
  con `ansible-operation-lock.py` antes de la marcha atrás y antes de volver
  a usar el wrapper, porque también rechaza las órdenes con lock directo (ver
  «Un marker retenido»);
- una carga no está lista dentro del tiempo de espera de rollout del rol. Es
  marcha atrás y no un reintento, porque esa imagen no se ha ejecutado nunca
  antes. Los tiempos reales, que son más largos de lo que parecen: `ate-setup`
  espera hasta 600 s por rollout (`substrate.install.rollout_timeout_seconds`)
  y tiene 6 480 s en total, más los 300 s que le añade Ansible; cada
  Deployment de `ax-system` (`ax-redis`, `ax-server`, `ax-controller`) tiene
  treinta intentos de `kubectl rollout status --timeout=10s` más diez
  segundos de pausa, unos 20 s cada uno, es decir, unos 10 minutos por
  Deployment y hasta unos 30 los tres; el router, lo mismo (10 minutos); el
  WorkerPool, treinta lecturas con diez segundos de pausa (5 minutos, y puede
  pasar al instante, ver el paso 12); la salud de Substrate, sesenta lecturas
  con diez segundos (10 minutos); y el log de cada controlador nuevo, 18 × 5 s.
  Lo que aborta es que el rol falle, no el reloj: por debajo de unos 10
  minutos por Deployment (o de los 6 480 s de `ate-setup`) no se interrumpe
  nada. Tampoco se interrumpe el apply con `Ctrl-C` ni cerrando la terminal:
  el gestor deja entonces `ax-lab-ate-setup` parado, o en marcha si el
  controlador murió sin avisar, y el apply de la marcha atrás se negaría con
  un segundo marker (ver «Un marker retenido»);
- `ate-setup` muere por memoria o se aborta. Su resultado es la línea JSON del
  gestor, en el campo `stdout` de la tarea fallida «Install Substrate with the
  pinned ate-setup only on drift» del apply (el motivo, `ERROR: ate-setup
  failed: …`, va en `stderr`): `exit_code`, `oom_killed`, `memory_peak_bytes`,
  `oom_kill_events`, `seconds` y `aborted` (`timeout` o `mem_available_floor`
  cuando no fue el kernel). `oom_killed` verdadero, con `memory_peak_bytes`
  cerca de los 201 326 592 bytes del tope de 192 MiB, es que no le bastó. En un
  apply correcto el rol no imprime ese resultado ni lo guarda
  (`/root/.ansible_async` queda vacío y el wrapper no lleva `-v`), así que el
  pico real de una instalación sana no se ve: de ahí que el paso 0 se apoye en
  la medida de la CI y en la aceptación del propietario. Un fallo así no deja
  contenedor (el gestor lo borra tras leerlo), pero sí `installing` en
  `state/substrate.json` y el marker del wrapper. No se reintenta a ciegas: es
  el mismo binario con los mismos datos y el mismo tope, y `ate-setup` conserva
  lo que ya creó, de modo que otro intento llega al mismo pico. La marcha atrás
  es la de siempre, y su apply desde `${PREV}` vuelve a ejecutar `ate-setup`, el
  anterior, bajo el mismo tope de 192 MiB (la CI lo midió en una instalación
  nueva, no sobre el nuevo). Si también muere por memoria, no hay un tercer
  intento en la ventana: `STOP`, con el laboratorio como lo dejó el último
  rollout correcto (`installing`, cargas de las dos versiones), y el propietario
  decide en un cambio revisado (subir `memory_limit_mib` exige liberar
  presupuesto en el perfil de capacidad, que hoy no deja margen);
- tras el apply, el rollout del despliegue `ax` de `ax-workers` no termina en
  300 s, no hay exactamente un pod de worker, sin terminarse, con el digest
  nuevo de `ateom-gvisor` y listo tras las 30 lecturas del paso 12, o la prueba
  de humo falla (salida distinta de 0, una Task que no llega a listo, un `ax
  ssh` que falla). Es marcha atrás, salvo que el propio mensaje de `ax-tarea`
  achaque el fallo a un tercero (la cuota de Claude, GitHub): entonces decide el
  propietario si se repite. Un worker viejo en `Terminating` no es un fallo;
- tras el apply, un `oom`, `oom_kill` u `oom_group_kill` que se mueva del valor
  de la línea base en `memory.events` o en `memory.events.local` del nodo o del
  registro (el `memory.events` jerárquico ya leía `oom_kill` distinto de cero
  tras el incidente del 2026-10-09: el criterio es que no se mueva, no que sea
  cero; el `memory.peak` no informa, está saturado por la caché), más reinicios
  que la línea base en `kube-system` o en una carga que el apply no sustituyó
  (un reinicio de `postgres-0` por `FailedPostStartHook` es una carrera de
  arranque que la CI vio también con los pines anteriores: se lee su causa
  antes de decidir), o una CLI que no responde.

### Marcha atrás de la ventana de resiembra

Se hace por imagen y desde la instantánea del paso 2, con los pines
anteriores, y cada orden con lock se decide con una lectura: de cada imagen,
`forget_one` retira el digest nuevo solo si `image-status` con el pin nuevo
dice que está, y `seed_one` siembra el anterior solo si `image-status` con el
pin anterior no dice ya que está completa. Que la copia nombre otro digest
(el `image-status` con el pin nuevo se detiene con «names … with another
digest») significa que ya está restaurada: esa lectura no deja marker y
nadie lanza un `forget` que el gestor rechazaría. Que una imagen ya esté
restaurada, o ausente, cuenta como hecha, así que el bucle se puede repetir
tras una marcha atrás interrumpida, en cualquier estado en que la ventana
parara: con todas las imágenes anteriores, con algunas nuevas o con todas
nuevas. Antes del bucle no puede haber ningún marker, porque sus órdenes
(`forget`, `seed-layout`, `install`) se rechazarían: el de Ansible de un
apply fallido incluido (ver «Un marker retenido»). Tampoco ningún contenedor
transitorio del gestor (`ax-lab-ate-setup`, `ax-lab-build-*`): si un apply se
interrumpió, o murió su controlador, el rol del apply de esta marcha atrás se
negaría a correr y dejaría un segundo marker. `no_transient` lo comprueba
entre la recuperación del marker y el bucle, y lo que hace falta si imprime
algo (esperar, o leer los logs y borrarlo con el lock) está en «Un marker
retenido». Con las variables de la shell (si se perdió, antes «Recuperar la
shell»), y con la decisión de volver atrás, que levanta el `STOP`:

```bash
HALT=
must test -z "$(compgen -G '/run/lock/dockerswarm-*.marker')"
no_transient
for old in "${STALE_SUB[@]}"; do
  name=${old#--image=}; name=${name%%=*}
  new=$(printf '%s\n' "${MOVED_SUB[@]}" | grep -F -- "--image=${name}=")
  forget_one substrate "${LAYOUT}" "${TAG_SUB}" "${new}" || break
  seed_one substrate "${SNAP_LAYOUT}" "${LAYOUT}" "${TAG_SUB}" "${old}" \
    || break
done
for old in "${STALE_KO[@]}"; do
  name=${old#--image=}; name=${name%%=*}
  new=$(printf '%s\n' "${MOVED_KO[@]}" | grep -F -- "--image=${name}=")
  forget_one ax "${LAYOUT}" "${TAG_AX}" "${new}" || break
  seed_one ax "${SNAP_LAYOUT}" "${LAYOUT}" "${TAG_AX}" "${old}" || break
done
out sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py \
  image-status --image-set substrate --layout "${LAYOUT}" \
  --tag "${TAG_SUB}" "${OLD_SUB[@]}"
expect_complete 7 "${OUT}"
out sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py \
  image-status --image-set ax --layout "${LAYOUT}" \
  --tag "${TAG_AX}" "${OLD_KO[@]}" "${BUILDX[@]}"
expect_complete 4 "${OUT}"
if [[ "$(sudo -- sha256sum "${CLI_BACKUP}" | cut -d' ' -f1)" \
  == "${CLI_OLD}" ]]; then
  echo 'la copia de la CLI ya es la anterior'
else
  must sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
    --operation ax-lab-seed -- /usr/bin/install -o root -g root -m 0600 \
    "${SNAP_CLI}" "${CLI_BACKUP}"
fi
[[ "$(sudo -- sha256sum "${CLI_BACKUP}" | cut -d' ' -f1)" \
  == "${CLI_OLD}" ]] || stop 'la CLI de la copia no es la anterior'
```

Si una imagen no se deja restaurar, hay dos recursos, los dos con el lock y
sin ninguna orden más de las de arriba:

- Un blob corrupto en la copia viva. `seed-layout` verifica el blob que ya
  existe y no lo repara, así que `seed_one` se detiene y la instantánea, que
  el bucle solo usa para las entradas, sirve entera: se aparta la copia viva
  (se conserva, por si hay que mirarla), se restaura la instantánea
  completa, se comprueban los tres conjuntos con los pines anteriores y se
  repite el bloque de arriba, que ya solo tiene que dejar la CLI. Es la
  marcha atrás de todo lo de la copia, no de una imagen:

  ```bash
  ASIDE=${LAYOUT}.aparte-$(date -u +%Y%m%dT%H%M%SZ)
  must sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
    --operation ax-lab-seed -- /usr/bin/mv "${LAYOUT}" "${ASIDE}"
  must sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
    --operation ax-lab-seed -- /usr/bin/cp -a "${SNAP_LAYOUT}" "${LAYOUT}"
  out sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py \
    image-status --image-set substrate --layout "${LAYOUT}" \
    --tag "${TAG_SUB}" "${OLD_SUB[@]}"
  expect_complete 7 "${OUT}"
  out sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py \
    image-status --image-set ax --layout "${LAYOUT}" \
    --tag "${TAG_AX}" "${OLD_KO[@]}" "${BUILDX[@]}"
  expect_complete 4 "${OUT}"
  out sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py \
    image-status --image-set web --layout "${LAYOUT}" \
    --tag "${WEB_TAG}" "${WEB_PIN}"
  expect_complete 1 "${OUT}"
  ```

- Una entrada nueva corrupta en la copia viva. `forget_one` se detiene
  porque `image_state` lee la copia en modo estricto y falla por algo que no
  es «another digest». `forget` no lee los blobs: solo reescribe
  `index.json`, y solo donde el nombre apunta a ese digest exacto. Se
  confirma antes, con `sudo -- jq -r '.manifests[] |
  select(.annotations["org.opencontainers.image.ref.name"] == "<nombre>:<tag>")
  | .digest' "${LAYOUT}/index.json"`, que la entrada nombra el pin nuevo, y se
  llama a `sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py forget
  --image-set <conjunto> --layout "${LAYOUT}" --tag <tag> <pin nuevo>`
  directamente, sin la lectura previa de `forget_one`: es la única orden con
  lock de la ventana que se pide sin ella, y la única razón es que esa lectura
  es la que falla. Si el gestor se niega, deja el marker (ver «Un marker
  retenido»).

Hay dos variantes, según hasta dónde llegó la ventana:

- **Antes del apply** (de los pasos 5 a 10, con nada aplicado): solo la copia
  y la CLI se movieron, y el registro y el nodo siguen con lo anterior. El
  bucle y la restauración de la CLI bastan, y no hace falta ningún apply:
  basta un `--check` desde `${PREV}` que informe `changed=0` (más abajo).
- **Después del apply** (desde el paso 11): además del bucle y de la CLI,
  desde `${PREV}` (el siguiente bloque deja el clon en ese commit), `--check`
  y `--confirm-production` de `ax-lab`, con las mismas lecturas y
  verificaciones que los pasos 10 a 12, más abajo. El apply ve los tags del
  registro movidos a los digests nuevos, importa los anteriores desde la
  copia restaurada y las cargas vuelven a ellos. No se ha comprobado que el
  registro siga sirviendo por digest los manifiestos anteriores una vez
  movidos sus tags: no se usa `export` desde él, la fuente es la copia
  restaurada.

En las dos variantes, al terminar el bucle el clon operativo deja el head del
PR de pines, que no está fusionado o que `main` ya no describe, y vuelve al
commit anterior, limpio:

```bash
must git checkout --detach "${PREV:?}"
must test -z "$(git status --porcelain --untracked-files=normal)"
```

Solo en la variante anterior al apply, con el clon en `${PREV}` y limpio, el
registro del último apply tiene que seguir nombrándolo, y un `--check` lo
comprueba, con la copia ya restaurada y nada aplicado: tiene que informar
`changed=0` (el registro nombra `${PREV}` y los dos ficheros de
`deployment_metadata` no cambian), o `changed=1` si en el paso 0 se aceptó
con `HALT=` que `DEPLOYED_VERSION.yml` nombraba a otro playbook: ese fichero
es el único que cambia. `deployed_last` lo vuelve a leer antes (si se acepta,
se levanta con `HALT=` y se pega solo la última orden del bloque):

```bash
must test -z "$(compgen -G '/run/lock/dockerswarm-*.marker')"
record_is "${PREV:?}"
deployed_last
must ./scripts/deploy-ansible.sh --playbook ax-lab --local --check
```

Solo en la variante posterior al apply, con el clon en `${PREV}`, limpio, y
la cola de la Oficina todavía en pausa. Primero las guardas, que son las del
paso 11 con dos diferencias. El registro: tras un apply correcto nombra el
commit de fusión (`MERGE`), y tras uno que falló antes de registrarse,
`PREV`; cualquier otro es un apply ajeno. Y no leen las Tasks (`lab_scopes` en
lugar de `lab_idle`): `ax get tasks` depende de `ax-server`, que puede ser lo
que falló y lo que obliga a volver atrás, y la marcha atrás no puede
depender de él; los scopes son locales y no ejecutan la CLI de AX, que tras
el paso 11 es la nueva, y sin ellos nada de la sesión del operador corre.
`host_memory` sí se mantiene: si se detiene, se
liberan las otras sesiones del host y se repite la lectura, sin un `HALT=`
a ciegas (el OOM del 2026-10-09 mató servicios dentro del nodo), y
`no_transient` también, por si el apply fallido dejó su contenedor. Después,
el `--check`:

```bash
must test -z "$(compgen -G '/run/lock/dockerswarm-*.marker')"
lock_free
record_is "${PREV:?}" ${MERGE:+"${MERGE}"}
office_idle
lab_scopes
host_memory
no_transient
must ./scripts/deploy-ansible.sh --playbook ax-lab --local --check
```

Lo que este `--check` imprime depende de cuánto llegó a aplicar el apply de
antes. Tras uno que terminó bien es el del paso 10 al revés: una línea
`restore into the registry: <imagen>` por cada pin de `STALE_SUB` y de
`STALE_KO` (los tags del registro siguen en los digests nuevos y la copia
restaurada tiene los anteriores), `install Substrate with ate-setup: state,
workloads`, las dos líneas de que AX y el panel se leen cuando Substrate y AX
coincidan, `ax-web-edge runs as reviewed` y `changed=3`: la CLI (el instalado
es el nuevo y la copia restaurada, el anterior) y los dos ficheros de
`deployment_metadata`. Tras uno que falló, que es el motivo habitual de
volver atrás, es un subconjunto y algo más: `deployment_metadata` no llegó a
correr, así que el registro sigue nombrando `${PREV}` y los dos ficheros no
cambian (`changed=1`, solo la CLI, si el apply ya la había copiado, que es lo
primero que hace, y uno más si en el paso 0 se aceptó lo de
`DEPLOYED_VERSION.yml`); solo salen `restore into the registry` de las imágenes
que ya se importaron, y si `ate-setup` no llegó a arrancar, Substrate
coincide y AX se lee y puede informar de lo suyo (`apply AX server-side`, el
`--route-timeout`, workers). Lo que nunca es aceptable, en ningún caso, es
`in neither backup nor registry`, `back up from the registry`, una línea
`the apply stops` o una imagen que no sea de `STALE_SUB` ni de `STALE_KO`:
eso es parar. Después, el apply desde `${PREV}`: vuelve a ejecutar
`ate-setup`, el anterior, y
sustituye otra vez el worker (el WorkerPool lleva el digest de
`ateom-gvisor`); un apply fallido retiene el marker y, antes de repetirlo, se
recupera y se pasa la guarda de contenedores transitorios (ver «Un marker
retenido»), y nunca se avanza hacia el head del PR ni hacia el commit de
fusión:

```bash
must ./scripts/deploy-ansible.sh --playbook ax-lab --local \
  --confirm-production
```

Y la verificación, la de los pasos 12 y 13 con los pines anteriores: el
registro con las imágenes anteriores fijadas, las dos CLI iguales a la
anterior, el worker, el `--check` final y la línea base:

```bash
out sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py \
  image-status --image-set substrate --registry 127.0.0.1:5001 \
  --layout "${LAYOUT}" --tag "${TAG_SUB}" "${OLD_SUB[@]}"
expect_pinned 7 "${OUT}"
out sudo -- /usr/bin/python3 scripts/manage-ax-lab-substrate.py \
  image-status --image-set ax --registry 127.0.0.1:5001 \
  --layout "${LAYOUT}" --tag "${TAG_AX}" "${OLD_KO[@]}" "${BUILDX[@]}"
expect_pinned 4 "${OUT}"
for cli in /opt/dockerswarm/ax-lab/bin/ax "${CLI_BACKUP}"; do
  [[ "$(sudo -- sha256sum "${cli}" | cut -d' ' -f1)" == "${CLI_OLD}" ]] \
    || stop "${cli} no es la CLI anterior"
done
worker_gate "${WORKER_OLD}"
must ./scripts/deploy-ansible.sh --playbook ax-lab --local --check
must diff <(sudo -- cat "${RELOCK}/baseline/memory.txt" | oom_lines) \
  <(lab_memory | oom_lines)
must diff <(sudo -- cat "${RELOCK}/baseline/restarts.txt") \
  <(pod_table unreplaced)
```

Este último `--check`, desde `${PREV}`, tiene que informar `changed=0`: ni
el instalado ni los registros difieren ya de lo aplicado. La prueba de humo
del paso 12 se repite, con el mismo comando, si el propietario quiere cerrar
la marcha atrás con ella. Si el apply desde `${PREV}` falla, no se avanza: se
recupera el marker, se pasa la guarda de contenedores transitorios y se
repite el `--check` y el apply; si vuelve a fallar, es un `STOP` y decide el
propietario, con el laboratorio en lo que dejó el último rollout correcto.

Después de una marcha atrás no puede correr ningún apply desde el head del PR
de pines ni desde el commit de fusión, en ninguna de las dos variantes: solo
`${PREV}` describe lo que la copia guarda, y solo la variante posterior al
apply aplica algo, desde ahí. En las dos variantes, desde el `forget` hasta
que la marcha atrás termina no corre ningún apply de `ax-lab` ni de `ax-web`
desde `main`. Si el PR de pines ya estaba fusionado (pasos 9 en adelante), se
fusiona ese mismo día un PR que lo revierta (`R`), y hasta entonces `main`
sigue sin servir para aplicar nada, porque pide digests que la copia ya no
guarda. La congelación termina con un apply desde `R`, cuyos pines son los de
`${PREV}`, y un `--check` posterior que informe `changed=0`. El `--check`
desde `R` que va antes de ese apply informa `changed=2`, solo los dos
ficheros de `deployment_metadata`, que llevan el commit y por eso difieren
tras la marcha atrás (el registro nombra `${PREV}`, y `main`, `R`): es lo
esperado y se acepta, y cualquier otro `changed` no. Si el PR de pines no
llegó a fusionarse, se queda sin fusionar hasta repetir la ventana, y la
congelación termina con el `--check` desde `${PREV}` que informa
`changed=0` (o `changed=1`, lo de `DEPLOYED_VERSION.yml` aceptado en el
paso 0). La cola de la Oficina se reanuda solo entonces, con la marcha
atrás verificada (ver el paso 13).

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
   - los manifiestos de atestación de los índices del runner y de agentes
     (`ax-task-runner@sha256:622a63a6…` y `ax-agents@sha256:a20f5b9f…`),
     su única procedencia de compilación;
   - copias de `/opt/ax-lab/SPIKE-LOG.txt`,
     `/opt/ax-lab/substrate/bin/kind-config.yaml`,
     `/opt/ax-lab/manifests/*.yaml`, `/opt/ax-lab/bin/ax-tarea` y
     `/usr/local/sbin/ax`, que no guardan ninguna credencial.
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
   en el host antes de borrarlas una a una, y las órdenes
   `/usr/local/sbin/ax` y `/usr/local/sbin/ax-tarea`, que es un enlace a
   `/opt/ax-lab` y sobre el que el rol se niega a escribir. Se conserva la
   imagen `golang@sha256:3680233e…` de `images.toolbox`, la de la
   compilación de reserva. Antes de este paso y del 3, la copia de
   Substrate y la de AX tienen que estar sembradas (ver «Ventana del
   laboratorio»).
7. Comprobar que no queda nada: ni contenedores `kind-control-plane` y
   `kind-registry`, ni red `kind`, ni `/opt/ax-lab`. Después, con los cambios
   2 y 3 fusionados (ver «Ventana del laboratorio»), aplicar `ax-lab` con
   `--check` y de verdad (ver «Aplicación»); el cambio 4 los sigue en su
   propia ventana.

## Recrear el clúster

Cuando cambian la configuración de kind, la imagen del nodo, la versión de
kind o la versión de Substrate, o el rol se detiene porque una prueba de
propiedad ya no coincide (la del nodo o la de los objetos de Substrate), el
clúster se recrea en una ventana exclusiva, borrando antes las Tasks de AX
que queden (ver «Redis») y con el mismo lock:

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
compilar nada, y reinstala AX. Los ficheros `state/substrate.json` y
`state/ax.json` del nodo anterior no se borran: se sobrescriben con los del
nuevo.

El panel web también se pierde con el clúster, junto con sus Secrets de
Kubernetes y el estado de su Oficina (`ax-web-state`). El apply siguiente
crea su espacio de nombres y se detiene pidiendo
`sudo -- ./scripts/ax-web-bootstrap.sh k8s`, que los crea de nuevo desde
`/etc/dockerswarm/ax/web-tls` y el token; un segundo apply termina, y
`ax-web-bootstrap.sh office` vuelve a crear el Secret opcional (ver
[AX_WEB.md](AX_WEB.md), «Recrear el clúster»). El reenviador y los Docker
Secrets de Traefik no cambian.

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
| AX | `f009cc8` (main, 2026-09-24) con google/ax#375 | Incluye `e6211f8` (google/ax#390). v0.3.1 (`e70162a`, 2026-09-25) ya lo incluye, pero ni v0.3.1 ni `main` (`c5c1ac5`) arreglan google/ax#375, y subir exige recompilar las cuatro imágenes con pins nuevos (ver «Parche local») |
| Claude Code y Codex | 2.1.274 y 0.156.1 | Los de `images/ax-agents/package-lock.json`; 2.1.274 era la etiqueta `stable` de npm y 2.1.282 la `latest` |
| Agent Substrate | `67253354` (2026-09-11) | El commit que AX fija en su `go.mod` (línea 7) |
| kind | v0.33.0 | El que fija Substrate en `hack/tools/kind/go.mod` (línea 19) |
| Kubernetes | v1.37.0 | La imagen por defecto de kind v0.33.0, con el mismo digest (`pkg/apis/config/defaults/image.go`, línea 21) |
| gVisor | nightly 2026-09-02 | El que fija Substrate en `manifests/ate-install/sandboxconfig-gvisor.yaml` (línea 34) |
| Go | 1.27.1 (`golang@sha256:3680233e…`) | Con el que el laboratorio manual compiló las imágenes; `go.mod` de Substrate pide `go 1.27.0` |
| ko | v0.19.1 | El que fija Substrate en `hack/tools/ko/go.mod` (línea 89), compilado con `go tool` desde su `go.sum` |

<!-- markdownlint-enable MD013 -->

`config/ax-lab.yml` fija además por tag y digest las imágenes upstream que
usa el laboratorio (`kindest/node`, `registry`, `redis` y `golang`), y las
de Substrate y AX por digest (ver «Imágenes fijadas» e «Imágenes de AX»).
Subir una versión es un cambio revisado de `config/ax-lab.yml` y de su
prueba de contrato, que fija los valores revisados, nunca una edición en el
host.

## Parche local

Las cuatro imágenes de AX se compilaron con
`images/ax/google-ax-375.patch`, byte a byte el diff que el laboratorio
manual dejó preparado (`git apply --index`, sin commit) sobre `f009cc8`:
sha256 `7b8bff50…9d72`, que `sources.ax.patch` fija y el validador
comprueba. Es la rama `local-models` de `arkady-emelyanov/ax` (commits
`c7fd10b` y `89fa366`, sobre `d8ed0fe`) propuesta en google/ax#375, sin el
`__pycache__` que esa rama versiona; ver `images/ax/README.md`. El parche:

- separa `/readyz`, la sonda de Substrate que abre el egress, de
  `/readyz?check=workspace`, la pregunta del controlador, y ejecuta los
  `goal` en segundo plano cuando el sandbox ya está listo. Es la parte que
  importa: el proxy de egress de Substrate solo deja pasar a un actor en
  `ACTOR_STATE_RUNNING`, y sin el parche el runner espera a clonar para
  responder a la sonda que le daría red, así que nunca clona;
- sube a 300 s el tiempo de la sonda de preparación en la `ActorTemplate`
  (y deja un comentario de `internal/substrate/client.go` desfasado, que no
  se toca para no cambiar el sha256);
- añade el proveedor `openai` al cliente de modelos, que en el commit
  fijado nadie llama, y la vía `AX_MODEL_BASE_URL` del bootstrap de
  Antigravity, que solo usaba el proxy de OpenAI retirado;
- corrige `.dockerignore` para `Dockerfile.task-runner` (google/ax#364, ya
  arreglado en v0.3.1 por google/ax#403).

google/ax#375 sigue abierto y sin respuesta, y los ficheros de la parte que
importa no han cambiado en upstream desde `dc4f36c` (2026-09-20). Subir AX
es un cambio revisado: quitar la parte de `.dockerignore`, rehacer la de
`internal/substrate/client.go` sobre google/ax#395, recompilar las cuatro
imágenes fuera del host y fijarlas de nuevo. Solo la CI aplica el parche;
el host nunca compila AX.

Cuando `sources.ax.modules_patch` está fijado, la CI aplica además, encima y
también con `git apply --index`, un segundo parche,
`images/ax/google-ax-modules.patch`, que solo toca `go.mod` y `go.sum` y no
cambia el primero: el sha256 de `google-ax-375.patch` sigue siendo el que fija
`sources.ax.patch`. Sirve a la CLI y a las dos imágenes de ko; el binario del
runner se compila sin él (ver «Reproducibilidad de AX», paso 5).

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
| `codex/auth.json`, `codex/config.toml` | `root:root 0600` | Sesión y configuración de Codex CLI; `auth.json` es también la semilla de la sesión de Codex de la Oficina |
| `github-token` | `root:root 0600` | Opcional: token de GitHub de la Oficina, uno o líneas `organizacion=token` (ver [AX_WEB.md](AX_WEB.md), «Oficina») |
| `openai-api-key` | `root:root 0600` | Sin uso: la clave del proxy de OpenAI, que no se codifica |
| `web-tls/` | `root:root 0700` | Certificado, clave y CA cliente del panel web (`tls.crt`, `tls.key`, `client-ca.crt`, `0600`), que crea `ax-web-bootstrap.sh init` |

<!-- markdownlint-enable MD013 -->

El rol nunca lee, copia ni imprime esos ficheros, ni comprueba su
existencia: solo `ax-tarea`, que ejecuta el propietario, lee
`claude-oauth-token` o `codex/auth.json`, y escribe de vuelta este último
cuando Codex lo renueva (ver «Herramientas del operador»), y solo
`scripts/ax-web-bootstrap.sh`, que también ejecuta el propietario, crea
`web-tls/` y lleva `claude-oauth-token` y `web-tls/` a los Secrets
`ax-web/ax-web-agent` y `ax-web/ax-web-tls`, y con `office`,
`codex/auth.json` y `github-token`, los que existan, al Secret opcional
`ax-web/ax-web-office`, siempre con
`kubectl create secret generic --from-file`. El panel no pone ninguna
credencial de agente en `Task.spec.env`: la lee del volumen del Secret al
arrancar cada ejecución y solo viaja en `StartProcess.env`. La sesión de
Codex que la Oficina renueva queda en su volumen `ax-web-state`, y desde
entonces la del host deja de valer para `ax-tarea` (su refresh token es de
un solo uso). El token de GitHub nunca entra en un sandbox: solo lo usa el
panel contra `api.github.com`. Queda además en claro en el etcd del nodo
(Kubernetes no cifra los Secrets en reposo por defecto), cruza el Envoy de
atenet-router sin cifrar dentro del nodo y lo puede leer cualquier
controlador que lea Secrets en todo el clúster: hoy `ate-controller` de
Substrate, cuyo ClusterRole tiene `get`, `list` y `watch` sobre `secrets`.
De los Secrets el rol solo lee
el nombre, el tipo y el número de claves (ver [AX_WEB.md](AX_WEB.md),
«Propiedad»). El proxy de
OpenAI del laboratorio manual no se codifica: `ax-tarea` nunca usa `goal`,
no atendió ninguna petición desde el reinicio del 2026-09-25 y cualquier pod
o sandbox podía gastar su clave; su fichero queda sin uso.

AX solo entrega variables de entorno a una Task: `EnvVar` no tiene
`valueFrom` (google/ax#348), `ax ssh` no tiene canal de entrada ni de
entorno (`cmd/ax/main.go`, líneas 1216 a 1220) y la única vía con Secret,
`gemini-api-secret`, también acaba en claro en la plantilla. Así que
`ax-tarea` pasa `CLAUDE_CODE_OAUTH_TOKEN` o `CODEX_AUTH_JSON_B64` en
`spec.env`, por la entrada estándar de `ax apply`, y la credencial queda en
claro en estos sitios mientras existe la Task, con esta mitigación:

<!-- markdownlint-disable MD013 -->

| Dónde | Mitigación |
| --- | --- |
| Clave `ax:task:default:<nombre>` de Redis, su RDB y la API de `ax-server`, sin autenticación (google/ax#376) | NetworkPolicies de `ax-server` y `ax-redis`; los túneles de la CLI solo viven lo que dura cada orden; `ax-tarea` borra la Task y espera a que desaparezca |
| `ActorTemplate` de Substrate, con el entorno y `AX_TASK_YAML`, legible con cualquier token de cuenta de servicio con la audiencia de `ate-api` | AX borra las plantillas al borrar la Task; ningún pod de AX monta un token de la API |
| Entorno del sandbox, su servidor de metadatos y el `exec` de invitado de `debug: true` | Solo el router llega a los workers (NetworkPolicy de Substrate); una Task a la vez |
| Snapshots dorados de rustfs, que siguen tras borrar la Task y se leen con las claves estáticas de upstream | Evitar `CONSERVAR=1`; desaparecen al recrear el clúster; la ventana del cambio 4 anota si un sandbox llega a rustfs |

<!-- markdownlint-enable MD013 -->

`ax-agent` borra `CODEX_AUTH_JSON_B64` de su propio entorno, pero la
variable sigue en `AX_TASK_YAML` y en el servidor de metadatos del sandbox.
Una vía de entorno para `ax ssh`, que la API de invitado ya admite
(`internal/guest/client.go`, líneas 84 a 103), sacaría los tokens de todos
esos sitios: queda para upstream.

El kubeconfig de administrador del clúster,
`/opt/dockerswarm/ax-lab/home/.kube/config`, es otra credencial del host:
`root:root 0600`, lo escribe kind y el rol solo comprueba sus metadatos.

## Exposición

Del laboratorio solo se publica el panel web, por decisión del propietario
del 2026-09-25, sin Cloudflare Access ni lista de IP permitidas. Traefik
(playbook `edge`, ver [EDGE.md](EDGE.md), «Ruta de AX») lo sirve en
`https://ax.apptolast.com` con `basicAuth` y límites de peticiones y llega a
él por el reenviador `ax-web-edge` y el NodePort 30843 del panel, que solo
existe en el puente `kind`; el panel solo acepta el certificado cliente de
Traefik (ver [AX_WEB.md](AX_WEB.md)). El registro DNS se creó a mano y es
deriva (EDGE.md, «Registro DNS»). Todo lo demás sigue sin publicarse:

- `ax-server` no tiene autenticación ni autorización (google/ax#376). No se
  publica nunca: ni ruta de Traefik, ni NodePort, ni LoadBalancer, ni
  Ingress, ni `hostPort`, y el validador lo rechaza. Sus Services escriben
  `type: ClusterIP`, así que el diff de cada apply ve que otra mano los
  publique (ver «Plano de control de AX»). El único NodePort del
  laboratorio es el del panel, y el validador exige que sea el único puerto
  de su Service. La NetworkPolicy `ax-web-to-ax-server` solo deja llegar a
  `ax-server` a los pods del panel. Solo se alcanza con
  `kubectl port-forward` desde el propio host, que la CLI abre y las órdenes
  `ax` y `ax-tarea` cierran al terminar (ver «Herramientas del operador»);
  mientras una corre, cualquier proceso local puede usar ese puerto.
- Dos NetworkPolicies de entrada en `ax-system`: a `ax-server` no llega
  ningún pod, y a `ax-redis` solo `ax-server` y `ax-controller`, en el
  6379. kindnet aplica NetworkPolicies (`kube-network-policies`), el
  tráfico desde el propio nodo, como las sondas del kubelet, siempre pasa, y
  `kubectl port-forward` entra en el pod sin pasar por ellas. No se ha
  comprobado desde un sandbox, cuyo tráfico sale por el egress de
  Substrate: la ventana del cambio 4 lo prueba con un control positivo.
- Ningún pod de AX monta un token de la API de Kubernetes, un Secret ni
  nada en `/var/run/secrets/kubernetes.io`, ni tiene permisos RBAC: el
  validador solo admite el token proyectado del controlador para Substrate,
  y el ClusterRole de upstream, que leería todos los Secrets del clúster, no
  se instala.
- La API de Kubernetes solo escucha en `127.0.0.1:6443` y el registro local
  en `127.0.0.1:5001` y `[::1]:5001`. El validador rechaza cualquier otra
  dirección, y el rol se detiene si el contenedor vivo publica otra cosa.
- El registro local no tiene autenticación. Desde el host, cualquier
  proceso de cualquier usuario puede subir, sobrescribir o leer imágenes en
  `127.0.0.1:5001` y `[::1]:5001`, y desde el clúster cualquier carga puede
  hacerlo en `kind-registry:5000`, porque está en la red `kind` con el nodo
  y nada filtra el tráfico de los pods (ver «Límites conocidos»). También
  puede llenar el volumen `ax-lab-registry` en el disco del host. Es un
  riesgo aceptado del laboratorio. Cada carga de Substrate y de AX, el
  WorkerPool y la imagen de cada Task de `ax-tarea` referencian su imagen
  por digest (`@sha256:`), el validador y el rol exigen esas referencias
  exactas y cada apply restaura en el registro cualquier tag movido desde la
  copia de seguridad. Así, sobrescribir un tag en el registro no cambia lo
  que ejecuta el clúster.
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
(ver «Ventana del laboratorio»). El de AX se hace en su propia ventana (ver
«Ventana del cambio 4»). No toca Swarm. Mientras exista el
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
- `image-status --image-set ax` con `--tag f009cc8-issue375` y las cuatro
  `--image` de `ax.images` dice `pinned` y `complete` en todas, y
  `sudo -- sha256sum /opt/dockerswarm/ax-lab/bin/ax
  /var/backups/dockerswarm/ax-lab/binaries/ax` da `ax.cli_sha256` en los
  dos.
- `/usr/local/sbin/ax` y `/usr/local/sbin/ax-tarea` son ficheros regulares
  `root:root 0750`, y `sudo ax get tasks -a default` responde sin dejar
  ningún `kubectl port-forward` después (`pgrep -af port-forward`).
- `/opt/dockerswarm/ax-lab/state/ax.json` es `root:root 0600`, dice
  `installed` y guarda el ID del nodo vivo y los UID de `ax-system` y
  `ax-workers`.
- Los Deployments de `ax-system` corren con sus imágenes exactas, sin
  ClusterRole `ax-controller`, el WorkerPool `ax-workers/ax` está listo con
  una réplica y `atenet-router` lleva un solo `--route-timeout=1h`.
- `memory.events.local` del nodo dice `oom 0`.
- `/opt/dockerswarm/deployments/ax-lab.yml` registra el commit aplicado.
- Un segundo apply informa `changed=0`.

### Si se descarta el laboratorio

Se retira a mano, en una ventana exclusiva y con el mismo lock que en
«Recrear el clúster», y en este orden:

1. Borrar las Tasks de AX con `sudo ax delete task <nombre> -a default`
   (ver «Redis»).
2. Borrar el clúster con la orden de «Recrear el clúster», sin volver a
   aplicar.
3. Borrar el registro y su volumen:
   `/usr/bin/docker rm --force kind-registry` y
   `/usr/bin/docker volume rm ax-lab-registry`, y el reenviador del panel
   web, que también está en la red `kind`:
   `/usr/bin/docker rm --force ax-web-edge`.
4. Borrar la red `kind`, que `kind delete cluster` no borra, después de
   comprobar que no le queda ningún contenedor: `/usr/bin/docker network rm
   kind`.
5. Borrar `/etc/sysctl.d/99-z-dockerswarm-ax-lab.conf`,
   `/usr/local/sbin/ax`, `/usr/local/sbin/ax-tarea`,
   `/opt/dockerswarm/ax-lab` (con el checkout, las cachés, la CLI y los
   ficheros de estado), `/var/backups/dockerswarm/ax-lab` (con las
   imágenes y la CLI), `/etc/dockerswarm/ax/web-tls` (el material del
   panel que creó `ax-web-bootstrap.sh init`) y, si nada más lo usa,
   `/etc/dockerswarm/ax/github-token`, y las imágenes
   que Docker guardó en el host: `localhost:5001/ate-setup@<digest>`, que
   descargó para ejecutarlo, `localhost:5001/ax-web@<digest>`, que
   descargó para el reenviador, y `golang@<digest>` de `images.toolbox`.
   Los límites de inotify vuelven a sus valores previos en el siguiente
   arranque. La ruta de Traefik del panel se retira con el playbook `edge`
   (ver [EDGE.md](EDGE.md)) y, solo después, porque Traefik los monta
   mientras tiene la ruta, los Docker Secrets `edge-ax-upstream-client-v1`
   y `edge-ax-upstream-ca-v1` que creó el arranque del panel, con
   `docker secret rm` y el mismo lock.
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
  y AX lo compensan las referencias por digest y la restauración de cada
  apply.
- El laboratorio no guarda estado que haya que conservar: no entra en el
  backup, igual que RacingGame. De un laboratorio perdido, el clúster, el
  registro, Substrate y AX se recrean desde este repositorio y su copia de
  imágenes; las Tasks que existieran se pierden.
- La copia de seguridad de las imágenes de Substrate y de AX y de la CLI
  vive en el mismo host. En un host reconstruido no existe: vuelven con una
  copia de `/var/backups/dockerswarm/ax-lab` llevada a mano al nuevo host y
  restaurada con `import`. Si no la hay, las de AX y la CLI solo vuelven
  recompilándolas en un cambio revisado con pins nuevos (ver «Lo que no se
  puede codificar fielmente»), y las de Substrate con la compilación de
  reserva autorizada imagen a imagen, que depende de que
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
- Un `ate-setup` en marcha ocupa hasta 192 MiB y 0,5 CPU que ningún plan
  declara, dentro de los 192 MiB y 500m de límites que el plan activo deja
  libres bajo su presupuesto; el margen operativo no se toca (ver
  [CAPACITY.md](CAPACITY.md)).
- Si se interrumpe el controlador durante una compilación de reserva o una
  instalación, el contenedor sigue en marcha fuera del lock host-global
  hasta su tiempo máximo (3 600 o 6 480 s), y el preflight de capacidad de
  otro playbook no lo ve; solo el siguiente `ax-lab` se detiene ante él
  (ver «Contenedores transitorios»).
- El nodo del laboratorio manual llegó a un `memory.peak` de 3 494 457 344
  bytes (3,25 GiB), el 93 % de su límite de 3 584 MiB, con Substrate, AX y
  dos workers, casi todo caché de páginas (`memory.events` en `max 0` y
  `oom_kill 0`). El cambio 4 deja un solo worker, acota los límites de los
  workers con la reserva de la plataforma y exige `oom 0` en
  `memory.events.local` del nodo tras cada apply (ver «WorkerPool y
  capacidad dentro del nodo»); el pico con AX se anota en la ventana del
  cambio 4.
- Las credenciales de las Tasks quedan en claro en Redis y su RDB, en las
  `ActorTemplate` y en los snapshots dorados de rustfs, que siguen tras
  borrar la Task hasta que se recrea el clúster (ver «Credenciales»). No
  hay referencia a secretos en AX (google/ax#348).
- Mientras corre una orden `ax` o un `ax-tarea`, su túnel a `ax-server`, sin
  autenticación, está abierto en la loopback para cualquier proceso local.
- La copia de vuelta de la sesión de Codex no distingue unos tokens de la
  misma cuenta de los de otra: nada local puede. Una tarea de Codex cuyo
  sandbox ejecute código hostil puede dejar al host una sesión elegida por
  ese código, que usarían las tareas de Codex siguientes (ver «Herramientas
  del operador»).
- Tras un reinicio inesperado, una Task que estaba en marcha muestra un
  estado viejo: se borra (ver «Redis»).
- Las NetworkPolicies de AX no son control de salida, que sigue sin
  aplicarse, y su efecto sobre el tráfico de un sandbox está por comprobar
  (ver «Exposición»).
