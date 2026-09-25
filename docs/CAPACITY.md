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
más: Minecraft llevaría el plan `observability` a 13 037 MiB de límite y los
dos juntos llevarían el activo a 12 909 MiB, por encima de 12 397. Volver a
arrancarlos exige antes una decisión de capacidad del propietario.
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
`organizationweb`, con los stacks externos, las sumas son 3 746 MiB
reservados y 8 301 MiB de límite, con 2 550m y 14 150m de CPU.

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

El contrato solo cubre servicios Swarm. Los proyectos Compose `satisfactory`
y `monitor-production` y el nodo kind del laboratorio AX, limitado a
3 584 MiB, consumen memoria fuera de él. Ese nodo equivale a toda la reserva
de 3 GiB más los 512 MiB de headroom, así que, con él en marcha, un
preflight en verde no garantiza margen real en el host.

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
`organizationweb`, `racinggame` y `autoupdater` ejecutan
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
