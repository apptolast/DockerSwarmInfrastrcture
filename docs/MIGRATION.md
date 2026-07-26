# Migración desde `MigracionNetCup`

## Regla principal

La migración no empieza con un `apply` ni con un cambio DNS. Empieza con un
inventario reproducible, backups restaurables y la asignación de un único
propietario a cada recurso.

Este runbook se redactó comparando este repositorio con `MigracionNetCup` en el
commit `79e9c79e5c9ca9f71a7f9011cde9a1bb3a1f8e42`. No se dispone aquí de
credenciales de producción, state remoto, contenido de secretos ni evidencia de
qué commit está desplegado. Por tanto, todos los pasos son compuertas pendientes;
ninguno se considera ejecutado por estar documentado.

## Prohibiciones durante la transición

Hasta completar las compuertas correspondientes:

1. No ejecutar en paralelo el Terraform antiguo y los roots nuevos sobre los
   mismos recursos.
2. No volver a aplicar `host_baseline` del repositorio antiguo después de que
   este repositorio asuma Docker, daemon, `sysctl`, UFW o `DOCKER-USER`.
3. No arrancar dos Traefik que intenten publicar `80/443`.
4. No crear la overlay `apptolast-edge` mientras exista el bridge homónimo.
5. No modificar registros A/AAAA/CNAME/SRV de aplicaciones antes de migrar y
   verificar sus datos.
6. No publicar ni autorizar `25565/TCP` antes de migrar y verificar Minecraft.
7. No usar `docker compose down -v`, borrar volúmenes, vaciar buckets ni ejecutar
   un destroy como parte del corte.
8. No mantener dos escritores sobre una base de datos durante cutover o
   rollback.
9. No adivinar IDs de import, MAC, server ID, policy IDs, zone ID, rutas de
   datos, TTL ni credenciales.

Terraform advierte que cada objeto remoto debe estar asociado a una sola
dirección de recurso; importar el mismo objeto varias veces puede producir
comportamiento no deseado:
[importación oficial](https://developer.hashicorp.com/terraform/cli/import).

## Fase 0: autoridad, ventana y criterios

Antes de tocar producción se registra:

- responsable que autoriza el corte y responsable que decide rollback;
- inicio y fin de la ventana de mantenimiento;
- aplicaciones incluidas y excluidas;
- máximo tiempo de indisponibilidad y de pérdida de datos admisible para cada
  aplicación;
- smoke tests objetivos, incluidos login, lectura, escritura controlada,
  trabajos asíncronos y dependencias externas;
- disparadores de rollback;
- destino offsite, custodio de claves y resultado de una restauración de prueba;
- mecanismo de comunicación con usuarios.

La migración queda bloqueada mientras cualquiera de esos elementos no tenga
dueño. En particular, este repositorio no contiene todavía stacks Swarm ni
routers para las aplicaciones: solo contiene el edge y `/ping`.

## Fase 1: inventario inmutable

### Host y Docker

Guardar en un expediente protegido, con fecha UTC y hostname:

- identidad del VPS, distribución, kernel, discos, mounts, espacio e inodos;
- interfaces, direcciones, rutas y puertos en escucha;
- `docker version`, `docker info`, contexto activo y root directory;
- estado de Swarm, nodo, stacks, servicios, tasks, secrets y configs, sin
  extraer el contenido de los secrets;
- `docker compose ls`, projects, contenedores, imágenes y health;
- redes completas, driver, scope, subnet, opciones y contenedores conectados;
- volúmenes, bind mounts y tamaño de cada dataset;
- unidades systemd/timers/cron que puedan reiniciar Compose o reescribir el
  host;
- reglas Netcup, UFW, nftables/iptables y `DOCKER-USER`;
- configuración efectiva de Docker y hash de los ficheros relevantes.

No basta con listar nombres. Para cada volumen o bind mount se identifica la
aplicación, formato, proceso escritor, método consistente de backup, método de
restore y prueba de integridad.

### Aplicaciones y datos

Por cada servicio de `MigracionNetCup` crear una ficha:

| Campo | Evidencia requerida |
| --- | --- |
| Imagen | Referencia exacta y digest observado |
| Configuración | Ficheros/variables, separando secretos |
| Dependencias | DB, cache, colas, almacenamiento, APIs y orden de arranque |
| Datos | Ruta/volumen, propietario, tamaño, formato y versión |
| Consistencia | Freeze, dump o snapshot soportado por la aplicación |
| Restore | Procedimiento probado en un destino aislado |
| Red | Puertos internos, públicos, hosts y protocolos |
| Salud | Healthcheck y smoke tests de negocio |
| Rollback | Compatibilidad de esquema y reconciliación de escrituras |

Un `tar` de una base activa no se acepta como backup consistente salvo que la
tecnología de esa base lo documente y la restauración se haya probado.

### Proveedores y DNS

Exportar mediante APIs/UI oficiales:

- todos los registros de `apptolast.com`, IDs, tipo, contenido, proxy y TTL;
- zone ID y account ID, sin copiar tokens al expediente;
- firewall policies Netcup, reglas, orden, IDs y policies asignadas al servidor;
- server ID, MAC observada y claves públicas del catálogo SCP;
- backend, workspace, lineage, serial y lista de recursos de cada state
  Terraform encontrado;
- ejecuciones/automatizaciones capaces de aplicar cualquiera de esos states.

Buscar state local, remoto y backups del repositorio antiguo. Si no aparece, se
documenta “no localizado tras estas búsquedas”, no “no existe”.

## Fase 2: backups y restore rehearsal

Antes del primer cambio:

1. Detener o congelar escrituras con el mecanismo de cada aplicación.
2. Crear dumps/snapshots consistentes de datos y configuración.
3. Copiar también los artefactos necesarios para reconstruir el Compose
   antiguo, sin subir secretos a Git.
4. Respaldar por separado el ACME antiguo y el nuevo; no mezclarlos.
5. Obtener snapshots cifrados de todos los states Terraform localizados. Para
   los tres roots nuevos, usar
   [`scripts/snapshot-terraform-state.sh`](../scripts/snapshot-terraform-state.sh),
   que canaliza `state pull` directamente a cifrado y no escribe state en claro
   en disco.
6. Durante una parada controlada de Docker, copiar íntegramente
   `/var/lib/docker/swarm`, tal como exige el
   [procedimiento oficial de Docker](https://docs.docker.com/engine/swarm/admin_guide/#back-up-the-swarm).
7. Cifrar antes de enviar, almacenar fuera del VPS y registrar checksums.
8. Restaurar cada clase de backup en un entorno aislado y ejecutar sus pruebas.

“Backup creado” no abre la compuerta. La abre “restore probado, checksum
verificado, clave disponible desde otro sistema y tiempo de recuperación
aceptado”.

## Fase 3: traspaso de Terraform

Seguir también [`TERRAFORM_STATE.md`](TERRAFORM_STATE.md). El orden por cada
objeto preexistente es:

1. Congelar el pipeline y las credenciales escritoras del state antiguo.
2. Guardar snapshot cifrado del state y export del proveedor.
3. Declarar el objeto en el root nuevo sin cambiar todavía su realidad.
4. Obtener el identificador de import de la UI/API y de la documentación del
   provider fijado; nunca inferir su formato.
5. Importar en la dirección exacta del root nuevo.
6. Revisar state y un plan completo. No se acepta reemplazo, destroy, pérdida de
   reglas ni cambio de DNS no incluido expresamente.
7. Verificar el objeto mediante la API/UI del proveedor.
8. Solo entonces retirar su dirección del state/pipeline antiguo y archivar ese
   state. La retirada no debe destruir el objeto remoto.

Los recursos Netcup que se solapan son la firewall policy, su asignación al VPS
y las claves públicas SCP. `manage_firewall` permanece `false` hasta inventariar
e importar todas las policies existentes y confirmar `server_id`, MAC,
`admin_cidrs` y `preserved_policy_ids`.

El root DNS nuevo empieza únicamente con `edge.apptolast.com`. Los registros de
aplicaciones permanecen bajo su propietario actual hasta que cada carga supere
la Fase 5. Un plan Terraform se considera sensible y se revisa con la
[semántica oficial de `terraform plan`](https://developer.hashicorp.com/terraform/cli/commands/plan).

La versión fijada publica los formatos `<policy_id>` para una policy,
`<server_id>/<mac>` para la asignación y `<scp_key_id>` para una clave. Esos
formatos no proporcionan los valores reales: se obtienen de SCP y se contrastan
antes de ejecutar import. Las direcciones HCL con `count` o `for_each` deben
entrecomillarse y coincidir exactamente con la instancia declarada.

- [Import de firewall policy](https://registry.terraform.io/providers/hornc-greedy/netcup/latest/docs/resources/firewall_policy)
- [Import de asignación al servidor](https://registry.terraform.io/providers/hornc-greedy/netcup/latest/docs/resources/server_firewall)
- [Import de clave SCP](https://registry.terraform.io/providers/hornc-greedy/netcup/latest/docs/resources/ssh_key)

## Fase 4: ensayo previo sin mover tráfico

Antes de la ventana:

- validar este repositorio y renderizar Ansible/stack en un entorno seguro;
- confirmar que el host cumple requisitos sin cambiar puertos o DNS;
- declarar o importar los dos buckets con `cloudflare/state-bootstrap` y
  probar el locking de los roots remotos;
- crear el token ACME separado y el secret versionado siguiendo
  [`EDGE.md`](EDGE.md) y el helper de bootstrap que allí se documenta;
- validar ACME contra Let's Encrypt staging con almacenamiento separado;
- preparar stacks, rutas file-provider y datos restaurados de las aplicaciones
  en un entorno aislado;
- ejecutar migración de esquema y smoke tests con copias de datos;
- capturar respuestas esperadas para comparar durante el corte;
- ensayar el rollback completo y medirlo.

No se puede completar esta fase con el contenido actual de este repositorio:
faltan las definiciones/rutas de aplicaciones, credenciales, inventario vivo y
datasets. Esas ausencias son bloqueos explícitos.

## Fase 5: corte en el mismo host

El bridge antiguo y la overlay nueva comparten nombre, y ambos Traefik compiten
por `80/443`. Por eso el cambio requiere una ventana coordinada:

1. Anunciar mantenimiento y bloquear despliegues/automatizaciones.
2. Tomar snapshot DNS y comprobar acceso alternativo al VPS.
3. Congelar escrituras y ejecutar la sincronización final de cada dataset.
4. Crear un último backup consistente, cifrarlo offsite y verificarlo.
5. Detener el proyecto Compose antiguo. No usar `-v`; confirmar que no quedan
   procesos escritores ni puertos de aplicación publicados.
6. Confirmar que el Traefik antiguo ya no ocupa `80/443`.
7. Retirar el bridge `apptolast-edge` únicamente cuando no tenga endpoints.
8. Ejecutar el workflow revisado de los playbooks `platform` y `edge`.
9. Verificar que `apptolast-edge` es overlay, Swarm, cifrada y no attachable.
10. Verificar nodo `Ready/Active/Leader`, servicio Traefik `1/1`, healthcheck,
    logs, certificado y respuesta HTTPS de `edge.apptolast.com/ping`.
11. Migrar/arrancar una aplicación cada vez y probarla localmente con resolución
    forzada hacia la IP nueva, sin cambiar aún su A/AAAA/CNAME/SRV.
12. Verificar datos, login, lectura, escritura controlada, jobs y observabilidad.
13. Solo tras la aceptación de esa aplicación, cambiar/importar sus registros
    mediante su único root propietario.
14. Observar al menos sus TTL efectivos y métricas antes de continuar.

El registro inicial `edge.apptolast.com` se mantiene DNS-only
(`proxied = false`). Su validación no autoriza cambios en registros de
aplicaciones.

### Compuerta especial de Minecraft

Minecraft sigue cerrado aunque el edge esté sano. Antes de abrir `25565/TCP`:

1. localizar y respaldar mundo, configuración, plugins/mods y versión exacta;
2. restaurarlos en el runtime destino y ejecutar las comprobaciones propias del
   servidor;
3. demostrar que no hay dos servidores escribiendo el mismo mundo;
4. probar conexión en un canal restringido;
5. aprobar un cambio de contrato que añada `25565/TCP` de forma coherente en
   Netcup, host y publicación Docker;
6. aplicar y verificar las tres capas;
7. solo después cambiar registros A/AAAA/SRV relacionados, si realmente existen
   y el inventario confirma que deben cambiar.

La presencia de `25565` en el Compose o Terraform antiguo no es autorización
para abrirlo en la plataforma nueva.

## Verificación de aceptación

El corte solo se declara correcto si existe evidencia de:

- un único listener autorizado para cada puerto público;
- un único Traefik y una única red `apptolast-edge` con el driver correcto;
- ninguna automatización antigua capaz de reescribir host o perímetro;
- nodo y servicios convergidos, sin tasks fallando o reiniciándose;
- certificados de producción válidos y renovación no bloqueada;
- cada aplicación usando el dataset migrado y pasando sus pruebas;
- DNS autoritativo y resolvers externos mostrando los valores aprobados;
- firewall Netcup, UFW y `DOCKER-USER` coincidiendo con la allowlist;
- states nuevos con lineage/serial registrados, locking probado y snapshots
  offsite;
- backups anteriores al corte todavía disponibles;
- logs revisados durante toda la ventana.

Un `/ping` correcto valida el edge, no las aplicaciones ni sus datos.

## Rollback

### Disparadores

Se vuelve atrás si ocurre cualquiera de los siguientes:

- corrupción o pérdida de datos;
- incompatibilidad de esquema sin reversión probada;
- autenticación, escritura o dependencia crítica fallida;
- Traefik, red, certificados o firewall no convergen dentro del tiempo acordado;
- observabilidad insuficiente para demostrar el estado;
- la ventana se agota antes de completar las verificaciones.

### Antes de cambiar DNS de una aplicación

1. Detener sus nuevos escritores.
2. Recoger logs y snapshot para el análisis, sin retrasar el rollback.
3. Restaurar el dataset anterior o descartar la copia fallida según su runbook.
4. Arrancar la carga antigua y ejecutar los smoke tests.
5. Como el DNS no cambió, no hay propagación que revertir.

### Después de cambiar DNS

1. Congelar inmediatamente las escrituras nuevas.
2. Restaurar exactamente los registros del snapshot previo mediante el único
   propietario DNS.
3. Esperar/verificar el comportamiento de caches y TTL; no asumir propagación
   instantánea.
4. Detener el stack nuevo antes de reactivar el antiguo.
5. Reconciliar o restaurar datos con el procedimiento específico. Nunca arrancar
   el antiguo contra un esquema incompatible ni permitir dos primarios.
6. Verificar la aplicación antigua desde fuera y mantener el modo mantenimiento
   hasta asegurar consistencia.

### Rollback global del edge

Para volver al Compose antiguo en el mismo host:

1. detener el stack edge nuevo para liberar `80/443`;
2. detener todas las cargas conectadas a la overlay;
3. retirar la overlay solo cuando no tenga endpoints;
4. recrear el bridge mediante el Compose antiguo y arrancar su Traefik;
5. verificar certificados, routes y aplicaciones antiguas;
6. no copiar automáticamente `acme.json` entre ambas implementaciones.

No se improvisa este procedimiento durante el incidente: debe haberse ensayado
con las versiones y artefactos exactos preservados antes del corte.

## Retirada del repositorio antiguo

Después de una ventana de observación definida por los responsables, con
backups y rollback aún válidos:

- retirar del Compose antiguo el Traefik, sus puertos/configuración y el bridge
  `apptolast-edge`;
- desactivar y después retirar su Terraform de firewall, asignación y claves
  Netcup;
- desactivar y después retirar `host_baseline` para Docker, daemon, `sysctl`,
  UFW y firewall;
- eliminar esos jobs de CI, timers y documentación operativa activa;
- migrar o retirar las aplicaciones restantes una por una;
- revocar credenciales antiguas solo tras demostrar que ningún workflow las usa;
- conservar commits, expediente, snapshots y checksums según la retención
  aprobada.

“Retirado” significa que no puede ejecutarse accidentalmente, no solo que existe
una nota indicando que está obsoleto.

## Referencias oficiales

- [Terraform: importar infraestructura existente](https://developer.hashicorp.com/terraform/cli/import)
- [Terraform: planificación](https://developer.hashicorp.com/terraform/cli/commands/plan)
- [Terraform: refactorizar recursos](https://developer.hashicorp.com/terraform/language/modules/develop/refactoring)
- [Docker: desplegar un stack](https://docs.docker.com/reference/cli/docker/stack/deploy/)
- [Docker: backup y recuperación de Swarm](https://docs.docker.com/engine/swarm/admin_guide/#back-up-the-swarm)
- [Docker: redes overlay](https://docs.docker.com/engine/network/drivers/overlay/)
- [Cloudflare: importar recursos a Terraform](https://developers.cloudflare.com/terraform/advanced-topics/import-cloudflare-resources/)
- [Cloudflare DNS: gestión de registros](https://developers.cloudflare.com/dns/manage-dns-records/)
