# Contrato de capacidad

## Objetivo

`config/capacity.yml` es el contrato fail-closed de capacidad para la
topología mononodo actual. Une los recursos de los tres stacks (`edge`,
`workloads` y `observability`) con el tamaño mínimo del servidor y evita que
un cambio aparentemente local produzca un plan global imposible.

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
| edge | 64 MiB | 128 MiB | 100m | 500m |
| workloads | 5 760 MiB | 9 728 MiB | 2 300m | 11 600m |
| observability | 1 248 MiB | 2 496 MiB | 1 070m | 5 100m |
| **Total** | **7 072 MiB** | **12 352 MiB** | **3 470m** | **17 200m** |

Quedan 45 MiB dentro del presupuesto de stacks después de preservar por
separado 3 GiB para el host y 512 MiB de headroom operativo. Estos 45 MiB no
son el margen total del host: el margen protegido es 3 584 MiB.

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

- exige exactamente los 28 servicios revisados en los tres renders;
- cuenta réplicas y los tres servicios globales de observabilidad;
- rechaza recursos ausentes, unidades ambiguas y reservas mayores que límites;
- compara los totales renderizados con los totales revisados;
- conserva 3 GiB, 512 MiB de headroom y 1 CPU fuera de los stacks;
- rechaza overcommit de memoria y más de `2.50x` de CPU;
- rechaza una relación límite/reserva de memoria superior a `2.50x`;
- relaciona heap de Minecraft, input descomprimido de n8n, `maxmemory` más
  overhead de Redis y `tmpfs` de Selenium con sus límites reales;
- ejecuta pruebas negativas de omisión, servicio inesperado, drift, host menor,
  swap inesperada y presupuesto excedido.

Los playbooks `site`, `edge`, `workloads` y `observability` ejecutan
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
