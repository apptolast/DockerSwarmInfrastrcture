# Estado del despliegue productivo

Instantánea comprobada el 28 de julio de 2026 sobre `159.195.156.57`. Sustituye
a la sección «Estado observado» de [`README.md`](../README.md), que describe el
host antes del primer despliegue real de este árbol.

## Aplicado y verificado

| Capa | Resultado |
| --- | --- |
| `platform` | `ok=156 changed=18 failed=0` |
| `edge` con perfil `acme-staging` | `ok=71 failed=0` |
| `edge` con perfil `production` | `ok=72 changed=6 failed=0` |
| Certificados | 9 de 9 emitidos por Let's Encrypt producción |
| Servicios Swarm | 11 de 16 en `1/1` |

Los nueve nombres del catálogo resuelven por HTTPS con cadena verificada desde
Internet. `n8n`, `pablohurtadohg` y `albertohidalgo` sirven tráfico real.

El nodo declara las etiquetas `platform.edge` y `platform.workloads`, existen
las nueve redes overlay aisladas, UFW mantiene exactamente trece reglas de
egress más `80/tcp` y `443/tcp` de ingress, y `22/tcp` conservó su límite de
tasa durante todo el proceso.

## Runtime regenerado

El árbol anterior quedó apartado como
`/srv/dockerswarm/services.pre-runtime-v4-20260728T002105Z`; revertir es un
`mv`. El actual se reconstruyó desde el backup cifrado y volvió a verificarse:

- `runtime-manifest.json` en `schemaVersion 4`, con clave HMAC de identidad de
  32 bytes y 35 ficheros fuente de secrets.
- Cinco bases restauradas y comprobadas por SQL; `rag` conserva pgvector 0.8.2
  con tres tablas y `vectors` solo la extensión 0.8.1, que es su contenido real.
- `workloads-ready-v2.json` ligado al `catalogSha256` de `config/services.yml`
  en HEAD, resolviendo el desajuste que impedía abrir el gate.
- 35 Docker Secrets de workloads instalados.

## Pendiente

### Servicios que no convergen

`kropia` necesitaba recuperar `CHOWN`, `SETGID`, `SETUID` y `NET_BIND_SERVICE`
tras `cap_drop: ALL`, porque el entrypoint de nginx prepara `/var/cache/nginx`
antes de bajar de privilegios. El arreglo está commiteado pero **no llegó a
aplicarse**: ver el bloqueo descrito más abajo.

`minecraft-stats` y `passbolt` arrancan correctamente y Swarm los detiene; sus
procesos paran con `exit status 0`, señal de terminación limpia por healthcheck.
Necesitan más margen de arranque, no una corrección de código.

`shlink` pierde sus workers de RoadRunner (`WorkerAllocate: EOF`). Causa sin
confirmar.

`openclaw` responde `Missing config. Run 'openclaw setup'`. Es una instalación
limpia esperando su alta inicial, tal y como la declara el catálogo.

### Bloqueo circular al redesplegar

`workloads` falla en «Inspect every running or stopped Docker container»
(`deploy.yml:205`) porque los servicios en bucle de reinicio destruyen
contenedores entre el listado y la inspección. El propio crash-loop impide
desplegar el cambio que lo detendría.

Para romperlo, retirar del stack los servicios que reinician antes de volver a
aplicar, o aplicar el cambio de capacidades directamente sobre el servicio y
reconciliar después.

### Compuertas externas que siguen cerradas

El backup permanece bloqueado: exige un custodio externo para la unlock key del
Swarm y un bucket R2 con credencial propia.
[`docs/BACKUP_RECOVERY.md`](BACKUP_RECOVERY.md) prohíbe reutilizar el token DNS
de Cloudflare o las credenciales del backend Terraform. Mientras siga así, este
host no tiene copias fuera de sí mismo.

Minecraft espera un flag explícito que registre la aceptación de publicar con
`online-mode=false`, en lugar de eliminar el assert que hoy acopla ambas cosas.

## Advertencia sobre Terraform y DNS

**No debe ejecutarse Terraform contra el root `cloudflare/apptolast-dns`.** En
modo `initialize` fuerza `adoption_only=true`, lo que devolvería los nueve
registros A a `138.199.157.58`, un servidor que ya no existe. Los registros
apuntan hoy a la plataforma porque se cambiaron a mano en Cloudflare; adoptar
ese estado en Terraform requiere trabajo previo sobre `imports.tf`, que además
no contempla el registro `edge`.
