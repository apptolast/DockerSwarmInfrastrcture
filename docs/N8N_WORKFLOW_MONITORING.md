# Observación del estado de workflows de n8n

## Alcance

El observador compara metadatos de workflows con un baseline privado y
cuenta estados de ejecuciones de las últimas 24 horas. Es una comprobación
manual del estado almacenado; no ejecuta workflows ni confirma sus resultados
de negocio, sus credenciales OAuth o la entrega de notificaciones.

La baja de los ocho workflows desactivados del inventario histórico y las
dos ausencias fue confirmada por el propietario el 6 de octubre de 2026,
hora de Madrid. Esa decisión está registrada en
`docs/solutions/additional-findings-observability-20261006.md` del
repositorio Satisfactory, commit
`2d4067b75e36967520ffbd4be6ecacdfa0e1e752`.
No se reactivan para hacer coincidir el inventario antiguo.

Los workflows activos se registran como estado observado, no como aceptación
OAuth o aprobación de negocio. La puerta de aceptación descrita en
`CLAUDE.md` permanece abierta.

## Baseline privado

El baseline se guarda fuera de Git, bajo un directorio root-only, con owner
root:root y modo `0600`. Nunca contiene nombres, nodos, credenciales ni
payloads; los identificadores y versiones siguen siendo metadatos privados.

Su esquema cerrado distingue:

- `expected_active`: identificador y versión publicada exactos;
- `retired_inactive`: bajas deliberadas que deben continuar sin actividad
  ni publicación; el estado archivado puede ser verdadero o falso;
- `retired_absent`: identificadores cuya ausencia fue deliberada;
- `provenance.observed_at`: instante UTC de la captura inicial;
- `provenance.owner_confirmed_on`: fecha de confirmación del propietario;
- `active_basis: observed_current` y
  `retirement_basis: owner_confirmed`: origen distinto de ambas decisiones.

Los grupos son únicos y disjuntos. La comprobación previa encontró ocho
activos, ocho retirados y dos ausentes; esos conteos deben contrastarse al
preparar el baseline y no son constantes universales del programa.
La fecha de origen no se renueva con cada lectura ni caduca para permitir
adoptar una deriva. Cualquier cambio del baseline necesita revisión explícita
del inventario y de la intención del propietario; el CLI no aprende ni
captura automáticamente un nuevo estado esperado.

### Captura inicial verificada (2026-10-06)

A las 12:00:47 UTC se creó el archivo privado de forma exclusiva, como
root:root `0600`, dentro del nuevo directorio root:root `0700`.
El candidato conservó la instantánea SQL de las 11:55:43 UTC: ocho activos
observados, ocho bajas deliberadas y dos ausencias confirmadas.
Una revisión independiente aprobó sus bytes exactos; antes de escribir,
otra consulta confirmó igualdad de identidad, actividad, archivo y versión
publicada en las 42 filas. Se conservó el instante de origen del candidato.

La clasificación inicial usa los IDs actuales del inventario histórico
privado de 13 entradas: tres siguen activos, ocho están inactivos y dos
están ausentes. La confirmación del propietario se aplica a esos grupos.
No reconstruye la instantánea antigua perdida ni resuelve los tres alias
de nombres históricos que quedaron sin correspondencia.

El observador de `5c1d390`, sin cambios de código desde su incorporación,
completó otra lectura a las 12:01:33 UTC con código 0 y estado `ok`.
No detectó deriva ni reactivaciones. En su ventana de 24 horas contó
433 ejecuciones: 432 correctas, una cancelada y cero errores o crashes.
El baseline y los recibos permanecen privados; no se activaron workflows,
publicaron versiones ni cambiaron credenciales. Este corte no acredita
aceptación OAuth, resultados de negocio ni vigilancia continua.

## Consulta y límites de confianza

La lectura utiliza el Docker local mediante socket Unix, sin contexto remoto,
proxy ni configuración heredados. Verifica servicio Swarm, tarea, contenedor
en ejecución e imagen fijada, y vuelve a comprobar sus identidades al
terminar. Una sustitución durante la consulta produce estado desconocido.

Dentro del contenedor PostgreSQL usa el socket Unix local y una consulta SQL
fija sobre `public.workflow_entity` y `public.execution_entity`.
La transacción es `REPEATABLE READ READ ONLY`; el instante de observación y
los agregados proceden de la misma instantánea. El lector selecciona sólo
identidad, actividad, archivo, versión publicada y conteos por estado.

No lee definiciones de nodos ni datos de ejecución y no descifra credenciales.
Los nombres de usuario y base deben ser identificadores simples antes de
pasarlos a `psql`: no se aceptan cadenas de conexión. El entorno de
`psql` queda reducido, sin password file, y usa
`--no-password`, `--no-psqlrc` y conexión local explícita.

El programa requiere Linux, root, un solo hilo, `SIGCHLD` por defecto y
ningún recolector externo de sus procesos hijos. Limita el baseline y el
stdout agregado de los nueve comandos Docker a 64 KiB cada uno, acepta como
máximo 1000 workflows y rechaza JSON con profundidad superior a 16.
Descarta stderr, sin publicarlo. También limita tiempo de conexión y tiempo
SQL. La observación tiene un presupuesto nominal de
10 segundos y reserva 0,25 segundos para limpiar su proceso local.
El bloqueo del kernel o del arranque de procesos no permite prometer un
límite absoluto. Terminar el cliente Docker tampoco prueba la terminación
inmediata de un proceso remoto `psql`; sus límites de conexión y consulta
son distintos.

## Interpretación

Un resultado sano sólo significa que el estado observado coincide con el
baseline y que no se detectaron errores en la ventana consultada.

Los cambios de identidad, versión publicada, archivo o presencia de un
workflow esperado se informan como deriva. Un workflow nuevo publicado,
una baja reactivada o una ausencia que reaparece requieren revisión.

n8n 2.31.5 declara `active` obsoleto y recomienda consultar
`activeVersionId`. Por eso una discrepancia entre ambas señales produce
estado desconocido; `active: false` por sí solo no demuestra una baja.
Un workflow archivado que conserva actividad o publicación también impide
un resultado sano. Esto es una comprobación del lector, no una supuesta
restricción de la base de datos.

`error` y `crashed` se cuentan como fallos. `canceled` se cuenta por
separado: no se convierte automáticamente en error. `new`, `running`
y `waiting` describen estado, no aceptación ni éxito de negocio.
El estado oficial `unknown` y cualquier estado no reconocido impiden
devolver un resultado sano. Una ventana sin ejecuciones tampoco prueba
que un workflow se ejecuta en su horario.

La salida pública contiene únicamente campos y motivos permitidos y
agregados. No expone identificadores, nombres, versiones, rutas privadas ni
errores originales. Un fallo de consulta se informa como desconocido.

## Operación

La integración inicial es manual. No instala un temporizador, una alerta,
un receptor externo ni un workflow de GitHub que acceda a producción.
Los tests offline forman parte de la validación habitual del repositorio.
Una lectura manual correcta no acredita vigilancia continua.

Desde el checkout revisado y con el baseline ya preparado:

```bash
sudo -- /usr/bin/python3 scripts/observe-n8n-workflows.py \
  --baseline \
  /srv/dockerswarm/services/monitoring-state/n8n-workflows-expected.json
```

El único argumento admitido es `--baseline`. La salida es un objeto JSON
con `scope: n8n_workflow_metadata`, estado, motivo, instante de la
instantánea SQL (`observed_at`), instante de comprobación (`checked_at`) y
conteos. Si no pudo validar la observación, el instante SQL y los conteos
son `null`. Devuelve código 0 únicamente para `ok`; `drift`, `error` y
`unknown` devuelven 1. Si tampoco puede escribir la salida, devuelve 1.

Antes de crear el baseline, contrastar el inventario histórico privado con
una captura actual validada y conservar la procedencia de las bajas. Crear
el directorio como root:root `0700` y el archivo con creación exclusiva,
modo `0600`; no sobrescribir un baseline existente.

Si una lectura detecta deriva, investigar el cambio antes de actualizar el
baseline. Nunca activar workflows, publicar versiones o modificar
credenciales para obtener un resultado verde.

## Fuentes primarias

- [Entidad Workflow de n8n 2.31.5](https://github.com/n8n-io/n8n/blob/n8n%402.31.5/packages/%40n8n/db/src/entities/workflow-entity.ts).
- [Publicación y retirada en n8n 2.31.5](https://github.com/n8n-io/n8n/blob/n8n%402.31.5/packages/cli/src/workflows/workflow.service.ts).
- [Estados de ejecución de n8n 2.31.5](https://github.com/n8n-io/n8n/blob/n8n%402.31.5/packages/workflow/src/execution-status.ts).
