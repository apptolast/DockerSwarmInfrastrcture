# Estado del despliegue productivo

Instantánea comprobada el 28 de julio de 2026 sobre `159.195.156.57`. Sustituye
a la sección «Estado observado» de [`README.md`](../README.md), que describe el
host antes del primer despliegue real de este árbol.

## Servicios aparcados

Por decisión del propietario (2026-09-25), `config/platform.yml` declara
Minecraft y OpenClaw aparcados (`platform_parked_workloads`) para liberar RAM
y CPU del host. Es estado declarado: se aplica con `edge` y después
`workloads`, y la evidencia del apply y los SHA-256 de los archivos en frío
bajo `/var/backups/dockerswarm/parked` se añaden aquí cuando se verifican.
Sus datos siguen en `/srv/dockerswarm/services`. Procedimiento en
[OPERATIONS.md](OPERATIONS.md), «Aparcar un servicio».

Antes de aparcar se tomó además un archivo en caliente de Minecraft con el
protocolo RCON del backup (`save-off`, `save-all flush`, `save-on`), sin
jugadores conectados:
`/var/backups/dockerswarm/parked/minecraft-hot-20260925T070501Z.tar.zst`,
2 352 303 106 bytes, 2 601 entradas, SHA-256
`fef4bf8b4675c63ee6445d718ce8967b4ad2d65511d6d2337dc724f3fee9e1c7`.

Seguimientos abiertos del aparcado:

- Cerrar el 25565 en el firewall mientras Minecraft está aparcado. Exige
  aplicar `platform` y `host-baseline`, que aplicarían también el snapshot de
  paquetes 20260924 pendiente (`docs/SNAPSHOT_20260924.md`). Hasta entonces el
  apply de `workloads` exige que ningún proceso del host escuche en ese
  puerto, pero solo en el momento del apply.
- Ninguna alerta avisa si alguien arranca a mano un servicio aparcado; el
  siguiente apply de `workloads` lo detecta y falla.

## Estado temporal fuera del repositorio

- Laboratorio AX (Google Agent Executor sobre Kubernetes kind y Agent
  Substrate) en `/opt/ax-lab`: nodo `kind-control-plane` limitado con
  `docker update` a 3 584 MiB y registro local `kind-registry`, fuera de
  Swarm y del contrato de capacidad. Es un ensayo manual pendiente de
  codificarse en su propio cambio revisado; hasta entonces no forma parte del
  estado reconstruible. Sale de esta lista cuando ese cambio lo codifique o,
  si se descarta, cuando se borren el clúster, el registro y `/opt/ax-lab`.
- Límites `fs.inotify.max_user_watches=524288` y
  `fs.inotify.max_user_instances=512`, aplicados en caliente para kind; se
  pierden al reiniciar hasta que ese cambio los codifique.
- `/swap-ax-build`, swap temporal de 4 GiB creado para compilar el
  laboratorio (fuera de `fstab`). Incumple `required_swap_mib: 0` y se retira
  antes de cualquier apply, porque el preflight de capacidad lo rechaza.

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
