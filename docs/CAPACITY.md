# Contrato de capacidad

## Objetivo

`config/capacity.yml` es el contrato fail-closed de capacidad para la
topología mononodo actual. Une los recursos de los cuatro stacks (`edge`,
`workloads`, `observability` y `autoupdater`) con el tamaño mínimo del
servidor y evita que un cambio aparentemente local produzca un plan global
imposible. `config/capacity-profiles.yml` define el perfil activo
`organizationweb`, que sustituye `observability` por `organizationweb`, y el
perfil `observability`; ambos perfiles incluyen `autoupdater`.

Docker advierte que agotar la memoria puede activar el OOM killer contra un
contenedor, el daemon u otros procesos importantes del host. También distingue
la reserva blanda del límite duro. Por eso el contrato valida ambos valores y
no trata una reserva Swarm como si fuera el consumo máximo:

- [restricciones de recursos de Docker](https://docs.docker.com/engine/containers/resource_constraints/);
- [reservas y placement de servicios Swarm](https://docs.docker.com/engine/swarm/services/).

## Evidencia del host

La revisión del 26 de julio de 2026 registró estos valores sin modificar el
servidor:

| Recurso | Valor observado | Fuente |
| --- | ---: | --- |
| CPU | 8 CPU / 8 000 millicores | Docker node y `/proc` |
| RAM | 16 757 469 184 bytes / 15 981 MiB completos | Docker node |
| Swap | 0 bytes | `/proc/meminfo` |

El backup del origen registra 16 CPU, 30,6 GiB de RAM y 4 GiB de swap, de los
que 3,7 GiB estaban ocupados en la captura. El destino tiene aproximadamente
la mitad de CPU/RAM y no tiene swap. Por eso copiar sin revisión todos los
límites Kubernetes del origen no produciría un contrato seguro. Esos límites
históricos son techos configurados, no métricas de consumo, y el backup no
contiene una serie de working set/picos que permita presentarlos como demanda
real.

Se reservan 3 072 MiB y 1 000 millicores exclusivamente para kernel, Docker,
containerd y servicios del host. La inspección previa mostró además un
ClamAV residente cercano a 1 GiB y otros daemons de seguridad y sistema. La
reserva no se entrega a los stacks ni se cuenta como caché recuperable.

El servidor no tiene swap. Por tanto, el máximo agregado de los límites duros
de memoria es la RAM física menos la reserva del sistema y 512 MiB adicionales
de headroom operativo: 12 397 MiB, sin overcommit. La CPU sí es un recurso
compresible; se permite como máximo `2.50x` sobre los 7 000 millicores
asignables, manteniendo un core para el host.

## Presupuesto revisado

Cada servicio declara reserva y límite explícitos. Los servicios `global` se
cuentan una vez porque el esquema v1 solo admite un nodo elegible.

| Capa | RAM reservada | RAM límite | CPU reservada | CPU límite |
| --- | ---: | ---: | ---: | ---: |
| edge | 128 MiB | 256 MiB | 100m | 500m |
| workloads | 2 656 MiB | 5 248 MiB | 1 600m | 8 100m |
| observability | 1 248 MiB | 2 496 MiB | 1 070m | 5 100m |
| autoupdater | 18 MiB | 45 MiB | 100m | 250m |
| **Total** | **4 050 MiB** | **8 045 MiB** | **2 870m** | **13 950m** |

Las cifras de `workloads` excluyen Minecraft y OpenClaw, aparcados por
`platform_parked_workloads` en `config/platform.yml` (ver
[OPERATIONS.md](OPERATIONS.md), «Aparcar un servicio»). Un servicio aparcado
renderiza `replicas: 0` y el validador no lo cuenta (`PARKABLE_SERVICES`), así
que libera su presupuesto: 3 328 MiB y 700m de CPU reservados y 4 608 MiB y
3 500m de límite (3 072/4 096 MiB y 500m/2 500m de Minecraft; 256/512 MiB y
200m/1 000m de OpenClaw). Desaparcar uno vuelve a sumar su reserva y su límite
en `config/capacity.yml` y `config/capacity-profiles.yml` dentro del mismo
cambio revisado, y el validador exige que siga cabiendo en el presupuesto.
Con los stacks externos declarados (ver «Stacks externos») ya no cabe sin
más: Minecraft llevaría el plan `observability` a 13 037 MiB de límite. En el
activo, con el laboratorio AX (ver «Contenedores del host»), no cabe ninguno
de los dos: OpenClaw lo llevaría a 12 653 MiB, Minecraft a 16 237 y los dos
juntos a 16 749, por encima de 12 397; sin el laboratorio, los dos juntos
serían 12 909. Volver a arrancarlos exige antes una decisión de capacidad
del propietario.
Con los dos en marcha, `workloads` suma 5 984/9 856 MiB y 2 300m/11 600m, y el
total de la plataforma completa llegaría a 12 653 MiB de límite, 256 MiB por
encima del techo.

Esos 256 MiB son el aumento del 2026-09-25, pagado con el presupuesto que
liberan los aparcados: el límite de Traefik pasó de 128 a 256 MiB, porque su
binario ocupa unos 185 MB. Con 128 MiB, el kernel reclamaba sin parar sus
páginas, `traefik healthcheck` agotaba su tiempo y Swarm reemplazaba la
tarea cada pocos minutos, dejando 80/443 sin servicio durante el relevo. El
de `portfolio-alberto` también pasó de 128 a 256 MiB, tras un OOM y dos
reinicios por healthcheck en una semana. Sus reservas pasaron de 64 a
128 MiB para respetar la proporción máxima límite/reserva de 2,50
(`service_memory_limit_to_reservation_ratio`).

Ese techo no es el margen total del host: se siguen preservando por separado
3 GiB para el host y 512 MiB de headroom operativo (3 584 MiB protegidos).
Mientras Minecraft y OpenClaw sigan aparcados, un aumento puede salir del
presupuesto que liberan, pero cada MiB así gastado se suma a la decisión de
capacidad para desaparcarlos. Fuera de eso, cualquier aumento de un límite
exige reducir otro en la misma revisión. El vigilante `autoupdater` es
distinto: su interruptor `enabled: false` renderiza `replicas: 0` sin liberar
el presupuesto (`SUSPENDABLE_SERVICES`). En el perfil activo
`organizationweb`, con los stacks externos y el laboratorio AX, las sumas
son 5 666 MiB reservados y 12 141 MiB de límite, con 3 100m y 16 650m de
CPU: 256 MiB por debajo del techo de memoria. Sin el laboratorio serían
3 746 y 8 301 MiB, con 2 550m y 14 150m.

El límite de Minecraft es 4 096 MiB y su heap inicial/máximo es 3 GiB; el
validador exige al menos 1 GiB para metaspace, stacks, buffers directos y
proceso nativo. `MEMORY`, `INIT_MEMORY` y `MAX_MEMORY` se mantienen iguales,
de acuerdo con la semántica documentada por
[itzg](https://docker-minecraft-server.readthedocs.io/en/latest/configuration/jvm-options/).
n8n limita un resultado descomprimido a 256 MiB y dispone de 1 024 MiB; el
input admitido no puede superar un cuarto del límite del proceso.

Redis usa `maxmemory 32mb` dentro de un límite duro de 64 MiB. El contrato
reserva los otros 32 MiB para el proceso, allocator, buffers y memoria que
Redis no contabiliza como dataset; también exige `allkeys-lru` y mantiene RDB
y AOF desactivados porque este servicio solo coordina workers de n8n y no es
el sistema de registro. Redis documenta que `maxmemory` no equivale al máximo
RSS del proceso y que ciertos buffers quedan fuera del cálculo de expulsión:
[eviction](https://redis.io/docs/latest/develop/reference/eviction/) y
[FAQ de maxmemory](https://redis.io/faq/doc/1jbxid5qq7/is-maxmemory-the-maximum-value-of-used-memory).
Esta corrección no cambia la tabla: conserva la reserva/límite de 32/64 MiB y
reduce el dataset configurado de 128 a 32 MiB, eliminando el OOM inevitable
que existía antes del margen explícito. Tras arrancar se debe revisar
`INFO memory`, incluidos `used_memory_peak` y `mem_not_counted_for_evict`,
antes de ampliar el dataset o su límite:
[INFO](https://redis.io/docs/latest/commands/info/).

Selenium conserva el `tmpfs` `/dev/shm` de 1 GiB de la migración, dispone de
1 536 MiB, ejecuta una única sesión y desactiva tracing y VNC. El upstream de
Selenium recomienda dimensionar y ajustar `/dev/shm` según el caso y considera
2 GiB un valor conocido, pero arbitrario:
[docker-selenium](https://github.com/SeleniumHQ/docker-selenium#--shm-size2g).

Los valores `M` de los stacks se contabilizan como MiB binarios. Una prueba
ejecuta `docker stack config` y exige que, por ejemplo, `4096M` se normalice a
`4294967296` bytes; así el cálculo Python no depende solo de una interpretación
textual de la unidad de
[Compose](https://docs.docker.com/reference/compose-file/extension/#specifying-byte-values).

Antes de este ajuste, la suma de límites era 16 320 MiB, superior incluso a
la RAM física total y sin reservar memoria para el host. Se redujeron los
límites generales a `2x` la reserva y se ampliaron n8n y Selenium junto con sus
reservas porque sus propios parámetros demostraban que el límite anterior era
incoherente. Minecraft conserva un GiB completo fuera de heap. Son límites
iniciales de arranque, no una afirmación inventada sobre el consumo real.
Después de obtener métricas de producción, cualquier ajuste debe modificar
juntos:

1. el template del stack;
2. los totales revisados de `config/capacity.yml`;
3. las pruebas y esta tabla;
4. la fecha y evidencia de la revisión.

No se debe elevar un límite basándose solo en memoria libre puntual o caché de
página. Hay que revisar al menos el máximo sostenido, picos, OOM/throttling y
la ventana de retención correspondiente.

## Stacks externos

Tres stacks Swarm corren en este host sin estar definidos en este
repositorio: `satisfactory-companions` (`web`, `worker`, `radio`),
`satisfactory-events` (`audit`, `publisher`) y `sftp` (`downloads`). El
preflight de capacidad de cada playbook exige que todo servicio vivo
pertenezca al plan activo, así que, sin declararlos, ningún apply podía
pasar.

`config/capacity-profiles.yml` los declara en `external_stacks`, con las
réplicas y los recursos medidos en vivo el 2026-09-25: 50m y 16 MiB
reservados y 2 050m y 896 MiB de límite en total. Todos los planes los suman
en su agregado. En cada apply, el preflight inspecciona modo y recursos de
cada servicio vivo y exige, solo para estos stacks, que coincidan
exactamente con lo declarado y que no falte ni sobre ningún servicio. Un
cambio en cualquiera de los dos lados detiene el siguiente apply hasta que el
contrato vuelva a coincidir.

Cada servicio externo debe declarar límites de CPU y memoria de al menos 1,
porque Docker lee un límite 0 como ilimitado y ningún presupuesto puede
contarlo. Quedan fuera de las reglas por servicio de los stacks propios: la
relación límite/reserva y la reserva explícita, que Swarm no les exige.

El preflight también impide que un servicio aparcado corra fuera de
presupuesto. Si `config/platform.yml` lo aparca y el Swarm vivo le da
réplicas, cualquier playbook salvo `workloads` y `site`, los que lo llevan a
`0/0`, falla con «parked live service runs over the budget».

Declararlos no los convierte en estado reconstruible: sus ficheros de stack,
imágenes y datos siguen fuera del repositorio (ver
[DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md)), y en un Swarm reconstruido hay
que retirarlos de `external_stacks` antes de aplicar (ver
[REBUILD.md](REBUILD.md), paso 8). Caben porque Minecraft y OpenClaw están
aparcados.

Los proyectos Compose `satisfactory` y `monitor-production` siguen
consumiendo memoria fuera del contrato, así que, con ellos en marcha, un
preflight en verde no garantiza margen real en el host. El nodo kind y el
registro del laboratorio AX sí están declarados, como contenedores del host
(ver «Contenedores del host»).

## Contenedores del host

Además de los servicios Swarm, el host puede ejecutar contenedores Docker
sueltos, fuera de Swarm, como el nodo `kind-control-plane` y el registro
`kind-registry` del laboratorio AX (ver
[DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md)). `config/capacity-profiles.yml`
los presupuesta en `host_containers`, agrupados por lo que se ejecuta junto:
cada grupo declara, por nombre de contenedor, `reservations` y `limits`
(`cpu_millicores` y `memory_mib`) y `pids_limit`. Cada plan enumera en su
propio `host_containers` los grupos que ejecuta. Solo esos grupos se suman a
su agregado, que se sigue comprobando contra el presupuesto del host igual
que el resto.

Hoy hay un grupo declarado, `ax-lab`, que solo ejecuta el plan activo
`organizationweb` (ver [AX.md](AX.md), «Capacidad»):

<!-- markdownlint-disable MD013 -->

| Contenedor | RAM reservada | RAM límite | CPU reservada | CPU límite | PIDs |
| --- | ---: | ---: | ---: | ---: | ---: |
| `kind-control-plane` | 1 792 MiB | 3 584 MiB | 500m | 2 000m | 4 096 |
| `kind-registry` | 128 MiB | 256 MiB | 50m | 500m | 256 |
| **Total** | **1 920 MiB** | **3 840 MiB** | **550m** | **2 500m** | |

<!-- markdownlint-enable MD013 -->

El nodo usó entre 1,62 y 1,70 GiB en régimen y un pico de 3 082 MiB al
arrancar, y el registro entre 17 y 29 MiB sin contar la caché de páginas
(laboratorio manual, 2026-09-25). El `memory.peak` del registro manual es
de 376,8 MiB, por encima de su límite de 256 MiB, pero es sobre todo caché
de ficheros de los blobs de las imágenes que el ensayo le subió: su memoria
anónima era de 12,6 MiB y su `memory.events` marca `oom_kill 0`. Con
`memory.max`, el kernel recupera esa caché antes de recurrir al OOM killer
([documentación de cgroup
v2](https://docs.kernel.org/admin-guide/cgroup-v2.html)), así que el límite
deja más de 200 MiB de caché a un proceso que usa menos de 30 MiB. Los
cambios del laboratorio que suben imágenes lo comprobarán: las subirán con
el límite ya aplicado y exigirán `oom_kill 0` (ver [AX.md](AX.md), «Límites
y política de reinicio»).

El playbook `ax-lab` lanza además dos contenedores transitorios, que no se
declaran: los ejecuta bajo el lock host-global y los borra al terminar
(ver [AX.md](AX.md), «Substrate»), así que otro playbook no los encuentra
en marcha salvo que se interrumpa el controlador de `ax-lab`. En ese caso
la tarea asíncrona sigue en el host, fuera del lock, hasta que termina o
vence su tiempo máximo (3 600 s una compilación, 6 480 s la instalación),
y el preflight de capacidad no la ve; el siguiente `ax-lab` se detiene
mientras exista (ver [AX.md](AX.md), «Contenedores transitorios»). La
compilación de reserva de una imagen de Substrate (3 072 MiB sin swap,
1 536 MiB reservados, 2 CPU, 1 024 PIDs) solo corre con el nodo parado o
ausente, así que usa su presupuesto: registro y compilación suman
3 328 MiB, por debajo de los 3 840 MiB del grupo. `ate-setup` (256 MiB sin
swap, 128 MiB reservados, 0,5 CPU, 256 PIDs) corre junto al nodo y cabe en
los 256 MiB y 850m de límites que el plan activo deja libres bajo el
presupuesto (12 397 MiB y 17 500m); el margen operativo de 512 MiB sigue
aparte. `scripts/validate-ax-lab.py` calcula ese margen libre de cada plan
que ejecuta el grupo `ax-lab` y lo exige como techo de sus límites. Los dos
se niegan a arrancar sin su `MemAvailable` mínimo y se matan si baja del
margen operativo. Ninguna cifra de los planes cambia.

El plan `observability` no incluye el grupo, así que exige los dos
contenedores ausentes o parados. `scripts/validate-ax-lab.py` exige que los
límites de `config/ax-lab.yml`, los que aplica el rol, sean exactamente los
declarados aquí.

El esquema es tan estricto como el de `external_stacks`, y rechaza cualquier
clave desconocida:

- el nombre de grupo usa minúsculas, dígitos y guiones, sin guion al
  principio ni al final (el mismo formato que los identificadores de
  `config/capacity.yml`), y el de contenedor es un nombre válido para Docker
  (un carácter alfanumérico y al menos otro alfanumérico, `_`, `.` o `-`),
  único entre todos los grupos;
- los límites de CPU y memoria son de al menos 1, porque Docker lee un 0 como
  ilimitado, y ninguna reserva supera su límite;
- la relación límite/reserva de memoria no supera
  `service_memory_limit_to_reservation_ratio` de `config/capacity.yml`
  (2,50), así que la reserva de memoria tampoco puede ser 0;
- `pids_limit` es de al menos 1;
- un plan solo enumera grupos declarados y sin repetir.

La reserva de CPU solo cuenta en el presupuesto: Docker no reserva CPU para
un contenedor suelto, así que el preflight no puede comprobarla en vivo.

En cada apply, `capacity_preflight` pide al validador los nombres declarados
(`scripts/validate-capacity-profiles.py --host-container-names`, que antes
valida el contrato) y lee cada uno con `docker container inspect`: el
nombre, el estado y solo los cuatro campos de `HostConfig` que compara. Un
contenedor que no existe no detiene esa lectura.
Después, el validador exige:

- que cada contenedor de un grupo del plan activo esté ausente, parado
  (`created`, `exited` o `dead`) o `running`: cualquier otro estado
  (`paused`, `restarting` o `removing`) detiene el apply;
- que cada contenedor de un grupo del plan activo que exista, en marcha o
  parado, tenga exactamente los `Memory`, `MemoryReservation`, `NanoCpus` y
  `PidsLimit` que corresponden a su límite y reserva de memoria, su límite
  de CPU y su `pids_limit`;
- que cada contenedor de un grupo que el plan activo no ejecuta esté
  ausente o parado: cualquier otro estado (`running`, `paused`, `restarting`
  o `removing`) detiene el apply. Sus límites no se comparan.

Un contenedor del plan activo ausente o parado no ejecuta ningún proceso y
su presupuesto sigue reservado en el plan, así que ningún playbook depende
de que esté en marcha. Uno que existe tiene que llevar sus límites exactos
aunque esté parado, porque al volver a arrancar correría con ellos sin que
nada lo convergiera antes. Para el laboratorio AX, cuya política de
reinicio es `no`, eso significa que tras un reinicio del host o de Docker
sus contenedores quedan parados, los despliegues de producción siguen
adelante y el laboratorio no vuelve hasta que se aplica `ax-lab` (ver
[AX.md](AX.md), «Límites y política de reinicio»).

La única excepción es el playbook que converge un grupo en lugar de un
stack Swarm: hoy `ax-lab`, para el grupo `ax-lab`
(`HOST_CONTAINER_PLAYBOOKS`). `--requested-stack ax-lab` solo se acepta si
el plan activo ejecuta ese grupo, y entonces sus contenedores pueden tener
además otros límites, en marcha o parados, porque ese playbook los crea,
los arranca y los converge, y después los comprueba él mismo. Sus lecturas
tienen que ser igual de completas y válidas, y el resto del preflight no
cambia: servicios Swarm, aparcados, estados que no son en marcha ni parado
y cualquier otro grupo del plan. Un contenedor del laboratorio con otros
límites, como el nodo que kind crea sin ellos si el apply se interrumpe
antes del `docker update`, detiene cualquier otro playbook hasta que `ax-lab`
lo converge o, si le falta la prueba de propiedad, hasta que se borra (ver
[AX.md](AX.md), «Recrear el clúster»).

Los contenedores no declarados no se leen ni se comprueban. Un dato vivo que
falte, sobre o no tenga la forma esperada también detiene el apply: solo
cuenta como ausente un contenedor del que Docker responde que no existe
(`No such container`), y cualquier otro error de lectura falla.

Con `--live`, el validador lee de la entrada estándar
`{"services": [...], "host_containers": [...]}`. `services` es la lista de
servicios de siempre, y `host_containers` guarda, por cada contenedor
declarado, los campos `item`, `rc`, `stdout` y `stderr` de su lectura. Es el
único formato que acepta: una lista de servicios sola, sin las lecturas de
los contenedores, no prueba su estado y se rechaza.

Declarar un grupo no lo convierte en estado reconstruible, igual que ocurre
con los stacks externos. En un host reconstruido, sus contenedores no
existen; el preflight lo acepta y su presupuesto sigue reservado en el plan
activo. Para `ax-lab`, el playbook `ax-lab` los recrea desde este
repositorio, al final y solo si se quiere el laboratorio (ver
[REBUILD.md](REBUILD.md), «Orden de reconstrucción»).

## Gates

La validación completa, sin mutar Docker Swarm, es:

```bash
./scripts/validate-capacity.sh
```

Para comparar además el host local con el mínimo versionado y exigir cero
swap:

```bash
./scripts/validate-capacity.sh --verify-host
```

El validador:

- exige exactamente los 29 servicios revisados en los cuatro renders;
- cuenta réplicas y los tres servicios globales de observabilidad: cero
  instancias para un servicio aparcado y una para el vigilante suspendido;
- rechaza recursos ausentes, unidades ambiguas y reservas mayores que límites;
- compara los totales renderizados con los totales revisados;
- conserva 3 GiB, 512 MiB de headroom y 1 CPU fuera de los stacks;
- rechaza overcommit de memoria y más de `2.50x` de CPU;
- rechaza una relación límite/reserva de memoria superior a `2.50x`;
- relaciona heap de Minecraft, input descomprimido de n8n, `maxmemory` más
  overhead de Redis y `tmpfs` de Selenium con sus límites reales;
- ejecuta pruebas negativas de omisión, servicio inesperado, drift, host menor,
  swap inesperada y presupuesto excedido.

Los playbooks `site`, `edge`, `workloads`, `observability`,
`organizationweb`, `racinggame`, `autoupdater` y `ax-lab` ejecutan
`capacity_preflight` antes de cualquier rol que muta el servidor. El preflight
recopila los facts de hardware aunque el playbook parcial desactive el
gathering general y detiene la ejecución si el host o el plan global no
cumplen.

## Cambio de servidor o topología

Un servidor nuevo puede ser mayor, pero no menor que el mínimo versionado. Se
deben capturar de nuevo los valores de Docker y `/proc`, validar el margen real
del sistema y actualizar el contrato mediante revisión. Añadir nodos cambia
el número de tareas `global` y la colocación de recursos; el esquema v1 lo
rechaza deliberadamente. Una topología multinodo requiere un esquema nuevo
que modele capacidad y placement por clase de nodo, no solo un total global.
