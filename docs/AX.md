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

Codificado ya, por el playbook `ax-lab` (solo prerrequisitos del host):

- los límites de inotify que kind necesita, persistentes en
  `/etc/sysctl.d/99-z-dockerswarm-ax-lab.conf` y convergidos en vivo;
- el directorio `/opt/dockerswarm/ax-lab` y los binarios `kind` y `kubectl`
  fijados por versión y sha256 en `/opt/dockerswarm/ax-lab/bin`;
- el contrato `config/ax-lab.yml`, que además fija ya los commits de AX y
  Substrate y las imágenes upstream por digest para los cambios siguientes,
  y la bandera de aceptación del nodo privilegiado.

Sigue siendo manual, fuera del estado reconstruible:

- el clúster (`kind-control-plane`) y el registro local (`kind-registry`),
  con el límite de memoria aplicado con `docker update`;
- la instalación de Substrate, el plano de control de AX, sus imágenes
  compiladas en el host y el parche local;
- todo `/opt/ax-lab`, incluidas las compilaciones y la configuración de kind;
- los ficheros de credenciales de `/etc/dockerswarm/ax`, que por diseño
  siempre los provisiona el propietario (ver «Credenciales»).

Cambios previstos, en este orden:

1. Este cambio: contrato, validador, playbook y prerrequisitos del host. Se
   puede aplicar solo y deja inotify persistente.
2. El ciclo de vida del clúster: configuración de kind, registro, límites y
   política de reinicio de los contenedores, comprobación de propiedad, la
   declaración `host_containers` en `config/capacity-profiles.yml` junto con
   `capacity_preflight` en este playbook, y el procedimiento para retirar el
   laboratorio manual. Solo se fusiona en la ventana del laboratorio.
3. La imagen de herramientas, la compilación acotada y la instalación de
   Substrate, que solo se ejecuta cuando detecta deriva.
4. El plano de control de AX, el parche versionado con su sha256, las
   imágenes del runner y de agentes, el WorkerPool y el proxy de OpenAI con
   su secreto.
5. La prueba de humo, la prueba de reinicio y la versión final de este
   documento.

Después de esos cambios, en una ventana exclusiva: retirar el laboratorio
manual, aplicar con `--check` y después de verdad, pasar la prueba de humo y
mover el laboratorio en `DEPLOYMENT_STATUS.md` a «Aplicado y verificado».

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
| `config/ax-lab.yml` | Raíz de instalación, ruta de credenciales, sysctl, commits, binarios, imágenes y bandera de aceptación |
| `scripts/validate-ax-lab.py` | Valida el contrato sin red y renderiza el fichero de sysctl |
| `ansible/playbooks/ax-lab.yml` | Lock host-global, rol `ax_lab` y metadatos de despliegue |
| `ansible/roles/ax_lab/` | Prerrequisitos del host: sysctl, directorios y binarios |

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
- cualquier clave o valor con forma de credencial, en cualquier parte del
  fichero.

## Prerrequisitos del host

El playbook es una capa del host, como `host-baseline`: toma el lock
host-global con `operation_lock_guard`, ejecuta `ax_lab` y registra el
componente `ax-lab` con `deployment_metadata`. No lleva
`capacity_preflight` porque no arranca ningún contenedor; lo incorporará el
cambio que codifique el clúster, junto con su declaración de capacidad.

El rol comprueba primero que sus entradas son exactamente las de
`config/ax-lab.yml`, ejecuta el validador en el controlador, exige que la
raíz de instalación cuelgue de `platform_install_root` y que el host sea
`x86_64`, y revisa sin seguir enlaces que ninguna ruta que va a escribir sea
un enlace simbólico, tenga otro tipo, otro dueño que `root:root` o permiso de
escritura para grupo u otros.

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
claves no se añaden a `host_baseline_sysctl` porque aplicar `host-baseline`
hoy aplicaría también el snapshot de paquetes pendiente (ver
`DEPLOYMENT_STATUS.md`).

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

## Aceptación del nodo privilegiado

`ax_lab_privileged_node_accepted` en `config/ax-lab.yml` es la aceptación
explícita del propietario de que el nodo kind corra privilegiado y sin
confinamiento de seccomp ni AppArmor, lo que equivale a root en el host.
Vale `false`, su valor por defecto, y el validador exige un booleano real.
Este playbook todavía no actúa con ella; el cambio que codifique el clúster
debe negarse a crear o arrancar el nodo mientras valga `false`. Cambiarla es
una decisión del propietario, en una línea revisada, con el mismo patrón que
`platform_minecraft_offline_public_accepted` en `config/platform.yml`.

## Versiones revisadas

<!-- markdownlint-disable MD013 -->

| Pieza | Versión | Motivo |
| --- | --- | --- |
| AX | `f009cc8` (main, 2026-09-24) | Incluye `e6211f8` (google/ax#390), que no está en ninguna release: v0.3.0 es anterior |
| Agent Substrate | `67253354` (2026-09-11) | El commit que AX fija en su `go.mod` (línea 7) |
| kind | v0.33.0 | El que fija Substrate en `hack/tools/kind/go.mod` (línea 19) |
| Kubernetes | v1.37.0 | La imagen por defecto de kind v0.33.0, con el mismo digest (`pkg/apis/config/defaults/image.go`, línea 21) |
| gVisor | nightly 2026-09-02 | El que fija Substrate en `manifests/ate-install/sandboxconfig-gvisor.yaml` (línea 34) |

<!-- markdownlint-enable MD013 -->

`config/ax-lab.yml` fija además por tag y digest las imágenes upstream que
usa el laboratorio (`kindest/node`, `registry`, `redis` y
`nginx-unprivileged`). Subir una versión es un cambio revisado de
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

## Exposición

Nada del laboratorio se publica:

- `ax-server` no tiene autenticación ni autorización (google/ax#376). No se
  publica nunca: ni ruta de Traefik, ni NodePort, ni LoadBalancer, ni
  Ingress, ni `hostPort`. Solo se alcanza con `kubectl port-forward` desde el
  propio host.
- En el laboratorio actual, la API de Kubernetes solo escucha en
  `127.0.0.1` (puerto 42803, elegido por kind) y el registro local en
  `127.0.0.1` y `::1` (puerto 5001). El cambio que codifique el clúster debe
  mantenerlos en loopback.
- El laboratorio no publica ningún puerto, no añade nada a
  `platform_public_tcp_ports` y no toca el firewall del host.

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

No reinicia nada ni toca Swarm, el clúster kind o el laboratorio manual. Si
los valores vivos de inotify ya coinciden, como hoy, el apply no los toca:
solo crea el fichero que los hace persistentes, el directorio y los
binarios.

## Verificación

- `sysctl -n fs.inotify.max_user_watches fs.inotify.max_user_instances`
  devuelve `524288` y `512`.
- `/etc/sysctl.d/99-z-dockerswarm-ax-lab.conf` es `root:root 0644` y
  contiene exactamente esas dos claves.
- `sudo -- sha256sum /opt/dockerswarm/ax-lab/bin/kind
  /opt/dockerswarm/ax-lab/bin/kubectl` coincide con `config/ax-lab.yml`.
- `/opt/dockerswarm/deployments/ax-lab.yml` registra el commit aplicado.
- Un segundo apply informa `changed=0`.

Si se descarta el laboratorio, estos prerrequisitos se retiran a mano:
borrar `/etc/sysctl.d/99-z-dockerswarm-ax-lab.conf` y
`/opt/dockerswarm/ax-lab`. Los límites de inotify vuelven a sus valores
previos en el siguiente arranque.

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
- El nodo kind, limitado a 3 584 MiB, sigue fuera del contrato de capacidad
  hasta que el cambio del clúster lo declare en `host_containers` (ver
  [CAPACITY.md](CAPACITY.md), «Contenedores del host»).
- El laboratorio no guarda estado que haya que conservar: no entra en el
  backup, igual que RacingGame. Cuando los cambios siguientes codifiquen el
  clúster, la recuperación será recrearlo desde este repositorio; hasta
  entonces, un laboratorio perdido se reconstruye a mano.
