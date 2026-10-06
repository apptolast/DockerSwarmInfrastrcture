# Recuperación de AX preservando core_pattern

Runbook del 2026-10-06. La revisión independiente inicial y las 72 pruebas
offline cubren el helper y su conexión al rol. Los resultados de gates,
check/apply y recuperación deben conservarse aparte con el commit y la hora
de ejecución;
las pruebas offline no acreditan la recuperación del laboratorio.

## Problema y alcance

Tras un reinicio, el nodo AX, el registro y el reenviador quedan parados por
su política `no`, como documentan [OPERATIONS.md](OPERATIONS.md#reinicios) y
[AX_WEB.md](AX_WEB.md#aplicación). Antes de volver a arrancar el nodo
privilegiado hay que impedir que su configuración vendor conocida cambie el
`kernel.core_pattern` del host de `|/bin/false` a `core`; el problema figura en
[DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md#kernelcore_pattern-y-el-nodo-kind).

Este cambio recupera únicamente el nodo existente cuya identidad completa,
imagen fijada, versión kind y configuración prueba `state/cluster.json`.
No cubre la creación de un clúster: `kind create cluster` arranca systemd antes
de que una tarea posterior pueda protegerlo. Esa ruta se rechaza antes de las
escrituras del rol hasta disponer de otra solución previa al arranque.

## Protección previa al arranque

El helper del rol lee, con límites, los directorios efectivos de sysctl y los
metadatos del nodo. Exige la configuración vendor exacta y el valor endurecido
del host, y rechaza enlaces, propietarios/permisos distintos, configuraciones
ambiguas y montajes iguales, anteriores o interiores a `/etc/sysctl.d`.
Los datos de montajes conservados son destino, tipo y lectura/escritura;
se comparan en orden canónico y los destinos duplicados se rechazan,
sin descartar entradas ni cambios reales de esos campos.
No se imprimen orígenes, variables de entorno ni contenidos del daemon.

Sobre el nodo probado y parado, el apply añade un único fichero regular
`/etc/sysctl.d/10-coredump-debian.conf`, root:root 0644, con comentarios.
El mismo basename en `/etc` reemplaza el fichero vendor en `/usr/lib`, según
[sysctl.d](https://www.freedesktop.org/software/systemd/man/sysctl.d.html).
Se conserva systemd-sysctl para aplicar la configuración de red del nodo.

La escritura usa la API Docker local de archivo, también disponible con el
contenedor parado ([Docker cp][docker-cp]). No ejecuta el binario del nodo,
extrae datos al host, arranca contenedores ni modifica volúmenes. El helper
prueba la operación Ansible exacta antes de escribir y revalida identidad,
configuración y hardening antes/después. Su presupuesto nominal es de
120 segundos; no garantiza un plazo absoluto ante bloqueos del kernel.
Una petición PUT puede concluir en el daemon después de abortar el cliente.
Un error no declara éxito ni realiza limpieza o rollback automáticos.

La máscara sólo evita el writer vendor identificado. No demuestra que
cualquier otro proceso root o credencial systemd de terceros sea inocuo.
Las asignaciones con globs o escapes se rechazan conservadoramente, también
cuando no mencionen core_pattern: no se emula parcialmente su sintaxis.

## Secuencia de operación

Un único operador, usando un checkout limpio de un commit revisado y el lock
host-global. No compartir la ventana con Docker, Ansible, APT u otro agente.
El wrapper actual requiere `.git` como directorio: usar el clon aislado del
operador; un linked worktree tiene `.git` como fichero y no sirve para apply.

Antes de editar ese clon, ejecutar en orden:

```bash
./scripts/bootstrap-tooling.sh
./scripts/validate-iac.sh
./scripts/lint.sh
```

En este host Swarm, validate/lint requieren la elevación que documenta
[CLAUDE.md](../CLAUDE.md#validation-workflow). Ejecutar después los gates de
la versión final, revisar el diff y dejar el checkout limpio y commiteado.
No reutilizar los resultados del cambio de n8n como gates de este clon.

El primer precode del clon falló por dos scripts heredados con modo 0660.
Se corrigieron sólo esos ficheros regulares del clon mediante FD a 0640,
con bytes y modo Git conservados; las dos pruebas afectadas pasaron después.
El fallo exige repetir la validación completa: no relajar los controles
de seguridad ni aplicar un chmod amplio al checkout compartido para ocultar
el fallo.
El siguiente intento pasó pruebas y Terraform, pero falló en Traefik: una
reproducción confirmó que UID 65532 no podía leer su fixture pública 0660.
Sólo esa fixture se cambió por FD a 0644, conservando bytes y modo Git, y
el gate Traefik dirigido pasó. Cada fallo exige recuperar formalmente su
marker y repetir validate completo; un tramo aprobado no convierte el
validador fallido en éxito. Preparar el clon con permisos adecuados para
los ficheros públicos montados; los secretos y ficheros privados 0600
conservan sus permisos.

La vía normal remota es:

```bash
./scripts/deploy-ansible.sh --playbook ax-lab --profile production \
  --check --ask-become-pass
./scripts/deploy-ansible.sh --playbook ax-lab --profile production \
  --confirm-production --ask-become-pass
```

Para la ejecución local en esta VPS, autorizada en esta sesión, lanzar como
admin UID/GID 1001; el wrapper eleva supervisor y Ansible conjuntamente:

```bash
./scripts/deploy-ansible.sh --playbook ax-lab --profile production \
  --local --check
./scripts/deploy-ansible.sh --playbook ax-lab --profile production \
  --local --confirm-production
```

Antes del apply, medir de nuevo `MemAvailable` y el consumo de AIDE y
comprobar el presupuesto de [CAPACITY.md](CAPACITY.md); una lectura anterior
no acredita margen disponible durante la recuperación.

Leer el check antes del apply. Con el nodo parado, la deriva Kubernetes no
puede observarse aún: el plan debe expresar que esas comprobaciones ocurren
tras arrancar. El apply normal arranca el registro, verifica imágenes,
protege el nodo, lo arranca por ID y espera API/Ready; luego verifica otra vez
la máscara y el hardening, y continúa las comprobaciones del rol existente.
Ese rol puede reconciliar manifiestos y recrear workers antiguos según su
contrato; este cambio no ordena reinstalar Substrate ni recrear PVCs.

## Verificación y fallos

Conservar recap, commit aplicado y diagnósticos saneados. Comprobar identidad
del nodo, límites, `kernel.core_pattern=|/bin/false`, máscara exacta, API/Ready,
Substrate, AX y estado de la Oficina. Comparar PVCs/UIDs e identidades de los
objetos persistentes antes/después; no inferir conservación sólo del HTTP.
Si la API parada impide observar el estado anterior y no existe un recibo
previo, registrar los UID anteriores como desconocidos. Mantener el ID del
nodo o de sus volúmenes tampoco demuestra conservación del contenido PVC.
Verificar `GET /healthz` público y el 401 del acceso sin publicar credenciales;
HTTP sano no demuestra que los proveedores ni una tarea de agentes funcionen.
Seguir [AX_WEB.md](AX_WEB.md#verificación) para esa aceptación aparte.

Repetir check y apply según la disciplina del repositorio e investigar cada
cambio inesperado; no declarar idempotencia sin recap y evidencias actuales.
El primer apply del 2026-10-06 llegó a API/Node Ready y falló después con
`node_changed_during_observation`; el hardening del host siguió en
`|/bin/false`. Doce lecturas posteriores de la API Docker local observaron
el nodo running, dos órdenes distintos de la lista Mounts y una sola
proyección normalizada. Esa evidencia y la reproducción offline sustentan
la sensibilidad al orden; las dos proyecciones exactas del instante del
fallo no se conservaron y no se atribuye retrospectivamente cada campo.
La corrección ordena destino/tipo/readonly sin perder cardinalidad y
rechaza destinos duplicados. Las regresiones también rechazan cambios
reales de permisos, tipo, destino, adición o eliminación de montajes.
No se sortea el guard ni se reinicia el nodo para ocultar este fallo:
conservar el marker, probar la muerte del holder y recuperarlo formalmente
antes de una nueva operación desde código revisado y sus gates.
La recuperación desde el commit fusionado `c158708` completó después un
apply `ok=344 changed=6 failed=0`, seguido de dos checks `changed=0` y dos
applies consecutivos `ok=339 changed=0 failed=0`, el último completado antes
de las 05:17:40.345527 UTC del 2026-10-06. Se acreditan convergencia e
idempotencia de esta ventana; véase el detalle y los límites de PVC,
Oficina y DNS en [DEPLOYMENT_STATUS.md][ax-recovery-window].
La lectura previa al apply corregido de los cuatro PVCs fue posterior al
primer intento fallido: misma identidad y estado Bound después no prueban
contenido histórico ni el estado anterior al arranque inicial.

Una sonda posterior acreditó la lectura interna autenticada de la Oficina;
véase [la aceptación y sus límites][office-read-window]. El fallo previo de
metadatos del Secret, su comprobación `stat` y las recuperaciones formales
se conservaron como evidencia aparte. Esa lectura no prueba proveedores,
trabajos nuevos ni conservación histórica del contenido de los PVCs.

Ante un fallo, el helper puede haber instalado la máscara y el apply puede
haber avanzado: observar el estado exacto y conservar el marker. Su recuperación
sigue el procedimiento de
[CLAUDE.md](../CLAUDE.md#stale-marker-recovery), con prueba de que murió
el holder. No retirar la máscara, degradar core_pattern ni borrar/recrear nodo o
stores para desbloquearse. Un rollback de código también requiere un cambio
revisado que preserve la protección del nodo antes de cualquier arranque.

[docker-cp]: https://docs.docker.com/reference/cli/docker/container/cp/
[ax-recovery-window]: DEPLOYMENT_STATUS.md#recuperación-ax-tras-el-reinicio-2026-10-06
[office-read-window]: DEPLOYMENT_STATUS.md#lectura-autenticada-de-la-oficina-2026-10-06
