# OrganizationWeb: despliegue independiente del MVP

Este procedimiento aplica el catálogo `config/organizationweb.yml`, sin
modificar `config/services.yml`, generaciones de datos legacy ni su marcador
de restauración. El release publicado es el commit MVP que declara el catálogo;
las dos imágenes deben conservar esa revisión OCI además de su digest.
No incluye la feature 19 aún en desarrollo.

## Precondiciones y alcance

- Commit revisado, CI verde y checkout limpio en el host. Ejecutar bootstrap,
  validate-iac y lint según AGENTS; conservar sus salidas y el commit.
- Un solo nodo Swarm, manager activo y elegible para workloads. El role lo
  comprueba antes de usar healthchecks locales. No hay puertos nuevos públicos.
- Perfil activo `organizationweb`: edge y workloads conservan su presupuesto;
  observability queda fuera de este perfil y su despliegue se rechaza. `site`
  también se rechaza, porque incluye observability. No es una desinstalación:
  cualquier servicio live incompatible o desconocido bloquea el preflight.
- DNS de `organizacion.apptolast.com` ya verificado por el operador. El certificado
  y la ruta TLS no quedan acreditados hasta su comprobación posterior.
- Backup externo y escrow/autolock continúan con sus límites documentados.
  Este despliegue no cierra el gate de migración legacy ni certifica recuperación
  ante pérdida del host. No ejecutar el playbook backup como atajo.

El plan app suma 6784 MiB de reservas y 11456 MiB de límites de servicios,
manteniendo 3072 MiB de reserva del sistema y 512 MiB de margen. Es un sizing
inicial probado nominalmente, no una garantía de carga sostenida.

## Bootstrap de siete secretos externos

El operador conserva las credenciales en su almacenamiento protegido, fuera
del repositorio, y prepara un script temporal root-only fuera del checkout.
No incluir valores en argumentos, historial, logs, Git, variables exportadas
ni salida de los comandos. Desactivar xtrace (`set +x`) antes de cargarlos.
Los passwords DB/Rabbit deben ser hexadecimales aleatorios de 64 caracteres,
evitando interpretaciones adicionales en `rabbitmq.conf`.

Los nombres exactos y la procedencia exigida son:

| Nombre | Contenido |
| --- | --- |
| organizationweb-db-username-v1 | `organization` |
| organizationweb-db-password-v1 | Password DB conservado por el operador |
| organizationweb-auth-username-v1 | Usuario de acceso a la aplicación |
| organizationweb-auth-password-v1 | Password de acceso conservado |
| organizationweb-rabbitmq-username-v1 | `organization` |
| organizationweb-rabbitmq-password-v1 | Password Rabbit conservado |
| organizationweb-rabbitmq-config-v1 | Configuración con esos mismos datos |

Todos llevan `com.apptolast.managed-by=manual-bootstrap` y
`com.apptolast.purpose=organizationweb`. Son objetos inmutables. Si alguno ya
existe, verificar el contexto y la operación anterior; no sustituir credenciales
ni recrear una mitad del conjunto con passwords nuevos. Un bootstrap parcial
se resuelve con las mismas credenciales conservadas, nunca inventando su valor.

El cuerpo del script protegido, una vez cargadas las variables shell locales
`ORG_DB_PASSWORD`, `ORG_AUTH_USERNAME`, `ORG_AUTH_PASSWORD` y
`ORG_RABBIT_PASSWORD`, crea mediante stdin (sin imprimir valores):

```bash
set +x
set -euo pipefail
create_secret() {
  docker secret create \
    --label com.apptolast.managed-by=manual-bootstrap \
    --label com.apptolast.purpose=organizationweb "$1" - >/dev/null
}
printf %s organization | create_secret organizationweb-db-username-v1
printf %s "$ORG_DB_PASSWORD" | create_secret organizationweb-db-password-v1
printf %s "$ORG_AUTH_USERNAME" | create_secret organizationweb-auth-username-v1
printf %s "$ORG_AUTH_PASSWORD" | create_secret organizationweb-auth-password-v1
printf %s organization | create_secret organizationweb-rabbitmq-username-v1
printf %s "$ORG_RABBIT_PASSWORD" | create_secret organizationweb-rabbitmq-password-v1
printf '%s\n' 'default_user = organization' \
  "default_pass = $ORG_RABBIT_PASSWORD" 'default_vhost = organization' \
  | create_secret organizationweb-rabbitmq-config-v1
unset ORG_DB_PASSWORD ORG_AUTH_USERNAME ORG_AUTH_PASSWORD ORG_RABBIT_PASSWORD
```

Ejecutarlo bajo el lock real, con la ruta absoluta del script root-only revisado:

```bash
sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
  --operation organizationweb-secrets -- \
  /bin/bash /root/organizationweb-secrets-v1.sh
```

El role sólo inspecciona metadatos de secrets, con `no_log`. API monta seis
archivos configtree 0400 para UID999; PostgreSQL usa sus dos `_FILE` para UID70;
RabbitMQ monta configuración 0400 para UID100/GID101. El role crea únicamente
los directorios nuevos: PostgreSQL y RabbitMQ con 0700. Rechaza enlaces y
propiedad/escritura inseguras en las cadenas de instalación y almacenamiento.

## Check, apply y aceptación

Desde el checkout limpio revisado, ejecutar secuencialmente:

```bash
./scripts/deploy-ansible.sh --local --playbook edge --check
./scripts/deploy-ansible.sh --local --playbook edge --confirm-production
./scripts/deploy-ansible.sh --local --playbook organizationweb --check
./scripts/deploy-ansible.sh --local --playbook organizationweb --confirm-production
```

Edge crea sólo una red adicional cifrada y un router file-provider para la
aplicación; conserva los ocho routers y redes legacy. Mantener los Configs
anteriores de Traefik para rollback. OrganizationWeb necesita esa red antes
del check. Cada operación está ligada a commit y contratos, bajo el mismo lock.
Si falla, conservar el marker y usar la recuperación existente descrita en
OPERATIONS; no borrarlo ni saltar su guard.

El apply exige cuatro réplicas, healthchecks y referencias exactas de red y
secrets. El operador verifica después HTTPS sin desactivar TLS, sesión con
cookie Secure, login, lectura, escritura y publicación outbox/broker. También
comprueba que los endpoints legacy siguen accesibles. Repetir el apply y exigir
convergencia; conservar salidas, hashes y límites de estas observaciones.

## Rollback sin destruir datos

En una actualización posterior, mantener el commit de catálogo anterior y sus
secrets; restablecer esos digests en un nuevo commit revisado, pasar gates y
repetir check/apply del role. Antes de bajar una versión, comprobar compatibilidad
de esquema y disponer de backup probado: Swarm rollback no revierte Flyway.

En la primera instalación, no existe versión anterior de la aplicación. Si es
necesario retirarla, el operador puede ejecutar la retirada del stack nuevo
bajo el lock, conservando datos y los siete secrets:

```bash
sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
  --operation organizationweb-initial-rollback -- \
  /usr/bin/docker stack rm organizationweb
```

La retirada es asíncrona: comprobar sólo ese namespace hasta que no tenga
servicios. No borrar `/srv/organizationweb`, redes globales, secretos ni
volúmenes. Para retirar el router nuevo, revertir sus cambios en un commit
revisado compatible con los ocho legacy y ejecutar edge check/apply por el
wrapper; no editar dinámicamente el file-provider ni degradar todo el checkout
a un snapshot de seguridad obsoleto. Una ruta de aplicación sin backend puede
devolver 503 durante esta retirada; el edge compartido debe seguir sano.
