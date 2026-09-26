# Operación segura del edge

## Contrato declarado

El stack `edge` declara una réplica de Traefik `3.7.9` fijada por digest, como
usuario `65532:65532`, con filesystem raíz de solo lectura, todas las
capabilities eliminadas y `no-new-privileges`.

Publica únicamente `80/TCP` y `443/TCP`. Usa:

- file provider con un fichero dinámico y `watch: false`;
- ningún provider Docker/Swarm y ningún socket Docker;
- DNS-01 de ACME mediante Cloudflare;
- `/srv/dockerswarm/traefik/acme.json` como estado persistente;
- el secret externo `cloudflare_dns_api_token_v2`;
- dos Docker Configs inmutables con nombre derivado de SHA-256;
- ocho overlays de workload aisladas y una overlay de monitorización, todas
  externas, cifradas y no attachable;
- nueve routers explícitos: `/ping` de `edge.apptolast.com` y los ocho
  servicios HTTP aprobados.

Si `config/platform.yml` aparca OpenClaw (`platform_parked_workloads`), su
router y su certificado se mantienen, pero el backend se renderiza sin
servidores ni sonda de salud: Traefik responde `503 no available server` sin
registrar nada, porque un balanceador sin servidores devuelve ese error
(`pkg/server/service/loadbalancer/wrr/wrr.go`). Con la sonda activa y sin
tarea, Traefik registra un WARN `Health check failed.` en cada intervalo de
15 s (`pkg/healthcheck/healthcheck.go`). Ambos ficheros coinciden en las
etiquetas
[v3.7.9](https://github.com/traefik/traefik/blob/v3.7.9/pkg/healthcheck/healthcheck.go),
la base fijada, y
[v3.7.13](https://github.com/traefik/traefik/blob/v3.7.13/pkg/healthcheck/healthcheck.go),
la que ejecuta el canal `traefik:v3` a 2026-09-25. Se comprobó el 2026-09-25
en contenedores aislados con el backend renderizado y ambas imágenes por
digest: `traefik@sha256:652929a140a32d7cafafb13c6cdfab5376cfeff800f51397b87b524501ed02a8`
(3.7.9) y `traefik@sha256:f86a2cab1b5c649070c49f883c743dd32d8485a56e3368c5f93b9e91f1e91259`
(3.7.13). Resultado: `503` y ninguna línea de sonda en 20 s; con la sonda
activa y sin backend, tres WARN en 50 s. La evidencia está en el pull request
apptolast/DockerSwarmInfrastrcture#59.
Aparcar o desaparcar OpenClaw exige aplicar también el playbook `edge`:
después de `workloads` al aparcar y antes al desaparcar (ver
[OPERATIONS.md](OPERATIONS.md), «Aparcar un servicio»). El apply de `edge`
del 2026-09-26 (ver «Ventana de aplicación») retiró la sonda de OpenClaw, y
desde entonces la compuerta STOP 10 de `CLAUDE.md` ya no impide aplicar
`edge`.

Esto es estado declarado, no evidencia de despliegue. El Docker Secret
`cloudflare_dns_api_token_v1` sigue existiendo (rotación pendiente de revocar
hasta verificar el servicio con la v2, según el propio procedimiento de este
documento) pero ya no está referenciado por `edge_traefik_cloudflare_secret_name`.
`cloudflare_dns_api_token_v2` es la versión activa, instalada y verificada
(TXT efímero) el 2026-07-27. No existen todavía certificado, overlays, stack
ni servicios.

Nota del 2026-09-25: desde `1c0a673` (2026-07-28)
`edge_traefik_cloudflare_secret_name` fija `cloudflare_dns_api_token_v3`, y es
el que usa el Traefik vivo (Docker Secret creado el 2026-07-27, etiquetas
`com.apptolast.managed-by=manual-bootstrap` y
`com.apptolast.purpose=traefik-cloudflare-dns`). Las menciones a la v2 como
activa en este documento, incluido el registro de secrets, son anteriores y
falta añadir su fila.

## Separación de credenciales

No se reutiliza un token entre funciones:

- **Token Cloudflare ACME:** crea y elimina TXT de DNS-01 desde Traefik.
  Solo recibe `Zone / Zone / Read` y `Zone / DNS / Edit`, limitados a
  `apptolast.com`.
- **Token Cloudflare Terraform:** gestiona únicamente el DNS declarado.
  Recibe los mismos permisos mínimos sobre la zona, pero es otro token.
- **Credencial R2 DNS state:** accede al backend del root DNS con
  `Object Read & Write` limitado a su bucket.
- **Credencial R2 Netcup state:** accede al backend del root Netcup con
  `Object Read & Write` limitado a un bucket distinto.
- **Token Netcup SCP:** accede al root de perímetro con solo la cuenta y las
  operaciones necesarias según SCP.

Aunque ACME y Terraform requieran permisos DNS parecidos, sus tokens son
distintos. Así se puede revocar o rotar el runtime sin interrumpir Terraform, y
una credencial del CI no se instala en el host.

**Excepción decidida por el propietario (2026-07-27):** para las credenciales
R2, el propietario del servidor autorizó explícitamente usar un token R2 de
cuenta con alcance sobre los tres buckets (`apptolast-tfstate-dns`,
`apptolast-tfstate-netcup`, `apptolast-backups`) en lugar de tres credenciales
`Object Read & Write` separadas y limitadas a un único bucket cada una. Es una
decisión informada de simplicidad operativa frente a aislamiento de blast
radius, tomada tras explicar la separación por defecto descrita arriba; no es
un descuido. `infra/terraform/backend-identities.json` registra el hash
SHA-256 del mismo Access Key ID en las tres identidades por ese motivo.

El cliente ACME usado por Traefik admite variables terminadas en `_FILE`. Su
documentación oficial para Cloudflare exige DNS Edit y Zone Read y permite
separar ambos permisos:
[lego Cloudflare](https://go-acme.github.io/lego/dns/cloudflare/). El stack usa
`CF_DNS_API_TOKEN_FILE`, por lo que el valor se lee desde el fichero del secret,
no desde el YAML.

Las credenciales R2 se crean desde el flujo específico de R2, no desde el
creador de tokens DNS:
[autenticación R2](https://developers.cloudflare.com/r2/api/tokens/).

## Creación inicial del secret Swarm

### Precondiciones

- identidad del manager y contexto Docker comprobados;
- shell administrativa aislada, sin grabación ni tracing;
- token ACME recién creado, verificado mediante la API/UI oficial y limitado a
  la zona;
- nombre versionado confirmado en
  `edge_traefik_cloudflare_secret_name`;
- backup de Swarm y procedimiento de recuperación disponibles.

Docker secrets cifra el material en tránsito y en el Raft log, lo entrega solo
a los servicios autorizados y no permite leer su contenido después:
[modelo de Docker secrets](https://docs.docker.com/engine/swarm/secrets/).

El workflow soportado usa
[`scripts/install-cloudflare-secret.sh`](../scripts/install-cloudflare-secret.sh).
Sin `--token-file`, lee silenciosamente desde el terminal de control. Antes de
crear el secret:

- comprueba que el proceso puede acceder a un manager Swarm;
- rechaza un nombre de secret existente;
- resuelve exactamente una zona activa `apptolast.com`;
- crea y elimina un TXT efímero para comprobar `Zone Read` y `DNS Edit`;
- pasa el token por stdin a `docker secret create`;
- no imprime el valor.

El TXT de verificación es una mutación real y breve en Cloudflare. Se ejecuta
solo después de confirmar la zona y durante una ventana en la que no haya otra
verificación con el mismo propósito.

La invocación interactiva no incluye el token en argumentos, entorno ni
historial:

```bash
sudo -- ./scripts/install-cloudflare-secret.sh \
  --secret-name cloudflare_dns_api_token_v2 \
  --zone apptolast.com
```

Para automatización, `--token-file` solo acepta un fichero regular, no symlink,
sin permisos de grupo/otros. El operador debe retirarlo de forma segura después;
el modo interactivo es preferible para el bootstrap manual.

El operador debe confirmar primero que ese nombre no existe y que no hay una
operación Ansible activa. Los secrets no se actualizan in-place:
`cloudflare_dns_api_token_v1` no se borra ni se reemplaza a ciegas. Se crea una
versión nueva, se cambia la referencia versionada y solo se revoca la anterior
tras verificar el servicio.

No se registra el token ni un hash reutilizable de su valor. Sí se registra el
ID/nombre de Docker, `CreatedAt`, identificador visible del token Cloudflare,
scope, custodio y fecha de próxima revisión.

### Registro de secrets instalados

<!-- markdownlint-disable MD013 -->

| Docker secret | ID Docker | `CreatedAt` | ID visible Cloudflare | Scope | Custodio | Próxima revisión |
| --- | --- | --- | --- | --- | --- | --- |
| `cloudflare_dns_api_token_v2` | `2l8zyn0elq7ir45hm87qx63zv` | 2026-07-27T09:20:10Z | `185f75d78a7b79a5b1d41e595fdaf90f` | Zone Read + DNS Edit sobre `apptolast.com` (uso ACME/Traefik) | Pablo Hurtado Gonzalo | 2026-10-25 |
| `cloudflare_dns_api_token_v1` | `hq1sjhfojnyujxf96ryngdiu0` | 2026-07-26 (aprox.) | desconocido | desconocido; expuesto fuera del gestor previsto | Pablo Hurtado Gonzalo | revocar tras verificar `v2` en servicio |

<!-- markdownlint-enable MD013 -->

El mismo token (`185f75d78a7b79a5b1d41e595fdaf90f`,
`cloudflare_dns_api_token_v2` arriba) se reutiliza también como
`CLOUDFLARE_API_TOKEN` para las operaciones de Terraform sobre
`cloudflare/apptolast-dns`, copiado fuera de Git en
`/etc/dockerswarm/terraform/dns-zone-api-token.txt` (root:root, 0600). No es
un token nuevo ni distinto: es una decisión explícita del propietario del
repositorio de reutilizar el mismo credential entre ACME/Traefik y Terraform
en vez de provisionar uno separado. Ver `docs/TERRAFORM_STATE.md` para el
detalle del gate de cutover DNS que este token desbloquea.

## ACME: staging antes de producción

El valor declarado actualmente para `edge_traefik_acme_ca_server` es el
directorio de producción. Esto no demuestra que exista un ensayo staging. El
primer despliegue de producción queda bloqueado hasta completar este flujo:

1. Configurar temporalmente
   `https://acme-staging-v02.api.letsencrypt.org/directory`.
2. Usar un almacenamiento ACME exclusivo de staging. Nunca reutilizar
   `acme.json` de producción ni mezclar cuentas/certificados de ambos entornos.
3. Desplegar solo `edge.apptolast.com` y comprobar creación/limpieza del TXT,
   emisión staging, healthcheck y renovación controlada.
4. Guardar evidencia y retirar el estado staging del path de producción.
5. Configurar
   `https://acme-v02.api.letsencrypt.org/directory`.
6. Crear un `acme.json` de producción vacío, propietario `65532:65532`, modo
   `0600`, y desplegar una sola réplica.
7. Verificar cadena, SAN, expiración, logs y HTTPS externo.
8. Respaldar inmediatamente el estado ACME de producción de forma cifrada y
   offsite.

Let's Encrypt recomienda su entorno staging para pruebas y advierte que sus
raíces no son de confianza pública:
[entorno staging](https://letsencrypt.org/docs/staging-environment/). Las
pruebas repetidas directamente en producción pueden alcanzar
[límites de emisión](https://letsencrypt.org/docs/rate-limits/).

Traefik documenta `caServer`, DNS challenge, renovación y el fichero de storage
en su
[resolver ACME 3.7](https://doc.traefik.io/traefik/v3.7/reference/install-configuration/tls/certificate-resolvers/acme/).

El wrapper usa los dos overrides revisados y registra el perfil
`acme-staging`, sin tocar los valores productivos:

```bash
./scripts/deploy-ansible.sh \
  --playbook edge \
  --profile acme-staging \
  --confirm-production \
  --ask-become-pass
```

Producción vuelve a los valores versionados
`https://acme-v02.api.letsencrypt.org/directory` y `acme.json`. El certificado
staging no es confiable públicamente; se validan DNS-01, logs, health y cadena
staging sin desactivar permanentemente la verificación TLS.

El despliegue posterior de producción usa el mismo wrapper sin `--profile` y
sobrescribe la huella instalada con `profile: production`.

## Arranque DNS-only

El root DNS gestiona exactamente diez A DNS-only:

- un registro nuevo, `edge.apptolast.com`;
- nueve registros de aplicación existentes que primero se adoptan sin cambiar;
- de esos nueve, ocho HTTP cambian a la IPv4 nueva en el cutover actual;
- Minecraft permanece en la IPv4 legacy porque su gate vale `false`.

El orden es:

1. importar los nueve A existentes mediante el flujo de adopción y comprobar
   que el plan no cambia contenido;
2. confirmar que ninguna otra automatización los gestiona;
3. validar Traefik y los ocho workloads con resolución forzada local;
4. revisar un plan separado que crea `edge` y mueve solo los ocho A HTTP;
5. aplicar y verificar DNS autoritativo/resolvers externos;
6. completar ACME y comprobar `/ping` y cada router;
7. confirmar que Minecraft no cambió.

DNS-01 utiliza un TXT de challenge y no exige mover el A/AAAA de una aplicación
para emitir su certificado:
[challenge types de Let's Encrypt](https://letsencrypt.org/docs/challenge-types/).
Los TXT efímeros de ACME no autorizan modificar los registros que dirigen
tráfico.

Los registros A, AAAA, CNAME o SRV de aplicaciones no se cambian hasta que su
dataset y runtime hayan superado [`MIGRATION.md`](MIGRATION.md). En particular,
el edge inicial no autoriza abrir Minecraft ni publicar `25565/TCP`.

## Despliegue y comprobaciones

Antes de cada despliegue:

- validar templates y stack renderizados;
- confirmar que el secret externo y la overlay cumplen identidad/contrato;
- confirmar que ningún Traefik antiguo ocupa `80/443`;
- comprobar espacio, ownership y modo de `acme.json`;
- conservar los Configs actualmente usados para rollback;
- revisar el diff de configuración y el digest de imagen.

Después:

- nodo `Ready`, `Active` y `Leader`;
- servicio `edge_traefik` exactamente `1/1`;
- task `Running` y healthcheck healthy, sin bucle de reinicios;
- imagen observada igual al digest declarado;
- Config IDs/nombres observados iguales a los renderizados;
- ausencia de mount/endpoint hacia el API de Docker;
- solo los puertos públicos contractuales;
- `https://edge.apptolast.com/ping` válido desde fuera;
- certificado del entorno correcto, SAN y expiración revisados;
- logs y métricas revisados durante los 90 segundos de monitor y después;
- rollback automático no activado y update no pausado.

Con una réplica y `stop-first`, una interrupción breve es esperable durante
actualizaciones. Si se requiere cero downtime hay que rediseñar topología,
publicación y número de nodos; no basta con cambiar `order`.

## Rutas de Satisfactory

Satisfactory se despliega fuera de este repositorio: el proyecto Compose
`satisfactory` (`/srv/satisfactory`, contenedores `satisfactory-web`,
`satisfactory-reverb` y `satisfactory-logs`) y el stack Swarm
`satisfactory-companions`. Su entrada se añadió a mano el 2026-09-22 con la
Docker Config `edge-traefik-dynamic-companions-a0952eace071` y la red
`apptolast-edge-satisfactory`. `stacks/edge/dynamic.yml.j2` la codifica igual
que `monitorizacion`: solo la entrada, tal como corría.

<!-- markdownlint-disable MD013 -->

| Router | Regla | Prioridad | Middlewares | Backend |
| --- | --- | --- | --- | --- |
| `satisfactory-web` | `Host(satisfactory.apptolast.com)` | implícita | `edge-security` | `http://satisfactory-web:80` |
| `satisfactory-ws` | la anterior `&& PathPrefix(/app/)` | 100 | `edge-security` | `http://satisfactory-reverb:8080` |
| `satisfactory-companions` | la anterior `&& (Path(/companions) \|\| PathPrefix(/companions/))` | 120 | `edge-security` | `http://satisfactory-companions_web:8080` |
| `satisfactory-logs` | `Host(logs-satisfactory.apptolast.com)` | implícita | `edge-security`, `satisfactory-log-auth` | `http://satisfactory-logs:8080` |

<!-- markdownlint-enable MD013 -->

Ninguna lleva `edge-rate-limit`, como en vivo. La red es una red adoptada
(`edge_adopted_attachable_networks`): los contenedores Compose solo pueden
unirse a una overlay `attachable`. En un host reconstruido el rol crea las
redes adoptadas ya `attachable` y el resto no.

La única diferencia buscada con la Config viva es el login de los logs. El
middleware `satisfactory-log-auth` ya no lleva `users` en línea sino
`usersFile: /run/secrets/basicauth_satisfactory_logs`, con el mismo realm
(`Satisfactory logs`) y `removeHeader: true`. Traefik da prioridad a `users`
sobre `usersFile`, así que la clave `users` no existe en el render y
`scripts/validate-contract.py` la rechaza, igual que cualquier `$2…$`,
`$apr1$` o `{SHA}` en los ficheros renderizados
([basicAuth v3.7](https://doc.traefik.io/traefik/v3.7/reference/routing-configuration/http/middlewares/basicauth/)).
El fichero es el Docker Secret `edge-basicauth-satisfactory-logs-v1`
(`edge_traefik_basicauth_secrets` en `ansible/group_vars/all.yml`), montado
`0400` para `65532:65532` en un tmpfs del task
([Docker secrets](https://docs.docker.com/engine/swarm/secrets/)). Contiene
una línea `usuario:hash-bcrypt` y solo existe en el host: ningún hash entra
en este repositorio público. El rol solo inspecciona sus metadatos, con
`no_log`, y se detiene antes de mutar nada si falta o no lleva
`com.apptolast.managed-by=manual-bootstrap` y
`com.apptolast.purpose=traefik-basicauth`.

Traefik lee el fichero una vez, al construir el middleware. Si falta, está
vacío o no se puede leer, desactiva solo ese router, que responde `404`, y el
task sigue sano, así que el rollback automático no salta. Por eso el deploy
exige, después de `/ping`, que `logs-satisfactory.apptolast.com` responda
`401` con `WWW-Authenticate: Basic realm="Satisfactory logs"`. La petición va
sin credenciales y no cuesta ningún bcrypt. `EdgeBasicAuthChallengeProbeTests`
deriva del render cada router que pasa por un `basicAuth`, con su realm, y
exige que sea exactamente la lista que sondea el deploy: un router protegido
nuevo necesita su propia sonda.

`EdgeLiveParityTests` (`tests/test_edge_contract.py`) compara el render con
`tests/fixtures/edge-live-dynamic-companions-a0952eace071.masked.json`, la
Config viva con cada usuario enmascarado. Solo admite dos diferencias: el
`usersFile` y el backend sin servidores de OpenClaw mientras siga aparcado
(PR #59), que la Config hecha a mano no tenía. Las Docker Configs son
inmutables, así que el nombre fija el contenido. Este comando, de solo
lectura, regenera la fixture o comprueba que sigue siendo la Config viva; el
hash solo pasa por la tubería:

```bash
sudo -- docker config inspect edge-traefik-dynamic-companions-a0952eace071 \
  --format '{{printf "%s" .Spec.Data}}' |
  /usr/bin/python3 -c 'import json, sys, yaml
d = yaml.safe_load(sys.stdin)
a = d["http"]["middlewares"]["satisfactory-log-auth"]["basicAuth"]
a["users"] = ["<masked>" for _ in a["users"]]
print(json.dumps(d, indent=2, sort_keys=True))' |
  diff -u tests/fixtures/edge-live-dynamic-companions-a0952eace071.masked.json -
```

El spec vivo de `edge_traefik` se comparó campo a campo con el render el
2026-09-25, con `Version.Index` 147489: imagen, etiquetas, usuario, entorno,
healthcheck, montaje, puertos, recursos (`256M`/`128M` de memoria, aplicados
a mano ese día), reinicio, update, rollback y placement coinciden. Un apply
cambia solo tres cosas:

- el nombre de la Config dinámica;
- un secret más, el del fichero de usuarios;
- el alias `traefik` en `apptolast-edge-satisfactory`, que la conexión hecha
  a mano no tenía y que `docker stack deploy` añade en todas las redes.

### Crear el secret desde el hash vivo

El secret se crea una sola vez, antes del primer apply, copiando la línea en
uso sin imprimirla, así que la contraseña no cambia. Precondiciones:

- `edge_traefik` sigue usando `edge-traefik-dynamic-companions-a0952eace071`;
- el secret no existe (`sudo -- docker secret ls`);
- no hay otra operación en curso.

Cuerpo del script root-only revisado, por ejemplo
`/root/edge-basicauth-satisfactory-logs-v1.sh` (`0700`):

```bash
set +x
set -euo pipefail
name=edge-basicauth-satisfactory-logs-v1
source_config=edge-traefik-dynamic-companions-a0952eace071
if docker secret inspect "${name}" >/dev/null 2>&1; then
  printf 'ERROR: %s already exists\n' "${name}" >&2
  exit 1
fi
users_line="$(
  docker config inspect "${source_config}" \
    --format '{{printf "%s" .Spec.Data}}' |
    /usr/bin/python3 -c 'import re, sys, yaml
a = yaml.safe_load(sys.stdin)["http"]["middlewares"]["satisfactory-log-auth"]
u = a["basicAuth"].get("users")
ok = "usersFile" not in a["basicAuth"] and isinstance(u, list) and len(u) == 1
if not ok or not re.fullmatch(r"[^:\s]+:\$2[aby]\$1[0-9]\$[./A-Za-z0-9]{53}", u[0]):
    sys.exit("ERROR: the live users entry has an unexpected shape")
sys.stdout.write(u[0])'
)"
printf '%s\n' "${users_line}" |
  docker secret create \
    --label com.apptolast.managed-by=manual-bootstrap \
    --label com.apptolast.purpose=traefik-basicauth \
    "${name}" - >/dev/null
unset users_line
docker secret inspect "${name}" --format '{{.Spec.Name}} {{json .Spec.Labels}}'
```

`printf` es un builtin: la línea no llega a ningún `argv` ni al historial, y
`docker secret create` la lee de stdin. Ejecutarlo bajo el lock real:

```bash
sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
  --operation edge-basicauth-satisfactory-logs -- \
  /bin/bash /root/edge-basicauth-satisfactory-logs-v1.sh
```

Para cambiar la contraseña más adelante, o para sustituir un fichero de
usuarios que no carga, se crea `edge-basicauth-satisfactory-logs-v2` con una
línea nueva; nunca se reutiliza un nombre. El hash se genera en un prompt
oculto (`mkpasswd -m bcrypt -R 10`, paquete `whois`), nunca en un argumento.
El nombre exacto `…-v1` está fijado en cuatro sitios, así que cambiar solo la
variable detiene el siguiente apply en la validación de entradas del rol. El
nombre nuevo entra en un único PR revisado que cambia a la vez
`edge_traefik_basicauth_secrets` (`ansible/group_vars/all.yml`), la aserción
de valor exacto de `ansible/roles/edge/tasks/main.yml`, el mapa revisado de
`scripts/validate-contract.py` y sus pruebas en
`tests/test_edge_contract.py`. Después se aplica siguiendo la ventana de
abajo.

### Ventana de aplicación

El primer apply de `edge` desde este repositorio reemplaza la única tarea de
Traefik. Con `stop-first`, puertos en modo `host` y el `graceTimeOut` de 10 s,
80/443 rechazan conexiones nuevas unos 13 s en todos los hostnames (medido
el 2026-09-25 en dos relevos) y se cortan los WebSocket y SSE abiertos. Una
sola persona, fuera de 22:30–00:40 UTC (ventana del Observatorio), desde el
clon operativo limpio y en detached HEAD sobre el commit fusionado, tras
`git fetch --all --prune`.

1. Comprobar que el servicio no cambió desde la comparación:

   ```bash
   sudo -- docker service inspect edge_traefik --format \
     '{{.Version.Index}}{{range .Spec.TaskTemplate.ContainerSpec.Configs}} {{.ConfigName}}{{end}}'
   ```

   Debe mostrar `147489 edge-traefik-static-1f7dc2751eef366d
   edge-traefik-dynamic-companions-a0952eace071`. Otro índice significa
   que alguien tocó el servicio: parar y repetir la comparación de esta
   sección antes de seguir.
2. Renderizar (`ansible/playbooks/render-edge.yml`) y guardar el estado de
   cada ruta pública con la función de abajo: `edge_probe >
   /tmp/edge-before.txt`.
3. Crear el secret (sección anterior).
4. `./scripts/deploy-ansible.sh --playbook edge --check --ask-become-pass`
   y, si está limpio, el apply con `--confirm-production`.
5. `edge_probe > /tmp/edge-after.txt` y
   `diff /tmp/edge-before.txt /tmp/edge-after.txt`, sin diferencias. Antes
   de este cambio OpenClaw ya respondía `503` (sonda caída) y sigue así.
   Cualquier otra diferencia es motivo de rollback (ver «Rollback» abajo).
6. `sudo -- docker service inspect edge_traefik`: la Config dinámica nueva,
   los secrets `cloudflare_dns_api_token_v3` y
   `edge-basicauth-satisfactory-logs-v1`, y 13 redes. Los logs se leen solo
   de la tarea nueva: los del servicio incluyen también las tareas paradas
   ([`docker service logs`](https://docs.docker.com/reference/cli/docker/service/logs/)
   acepta un servicio o una tarea), y la anterior registró el WARN de
   OpenClaw cada 15 s hasta el relevo, minutos antes. `${task}` debe ser un
   único ID y la cuenta, `0`: ni `no users found` ni el WARN de la sonda de
   `workloads_openclaw`.

   ```bash
   task="$(sudo -- docker service ps edge_traefik \
     --filter desired-state=running --quiet --no-trunc)"
   sudo -- docker service logs --since 10m "${task}" 2>&1 |
     grep --count -E 'no users found|workloads_openclaw'
   ```

7. El propietario entra una vez en `https://logs-satisfactory.apptolast.com`
   con su contraseña de siempre.
8. Solo si los pasos 5 a 7 salieron bien, repetir el apply: `changed=0` y el
   mismo ID de tarea en `sudo -- docker service ps edge_traefik`. Desde aquí
   `docker service rollback` ya no vuelve a la Config hecha a mano (ver
   «Rollback»).
9. Registrar la evidencia en `docs/DEPLOYMENT_STATUS.md` y actualizar la
   compuerta STOP 10 de `CLAUDE.md` en el mismo cambio.

La función de sondeo solo hace un `GET` sin credenciales a cada URL que sale
de las reglas `Host`/`Path`/`PathPrefix` del render, forzando la IP pública
del contrato:

```bash
edge_routes() {
  .venv/bin/python - <<'PY'
import re
import yaml
routers = yaml.safe_load(open(".build/edge/dynamic.yml"))["http"]["routers"]
urls = set()
for router in routers.values():
    paths = re.findall(r"Path(?:Prefix)?\(`([^`]+)`\)", router["rule"])
    for host in re.findall(r"Host\(`([^`]+)`\)", router["rule"]):
        urls.update(f"https://{host}{path}" for path in paths or ["/"])
print("\n".join(sorted(urls)))
PY
}
edge_probe() {
  local ip url host
  ip="$(.venv/bin/python -c 'import yaml
print(yaml.safe_load(open("config/platform.yml"))["platform_public_ipv4"])')"
  for url in $(edge_routes); do
    host="${url#https://}"
    host="${host%%/*}"
    printf '%s ' "${url}"
    curl --silent --output /dev/null --connect-timeout 5 --max-time 20 \
      --resolve "${host}:443:${ip}" \
      --write-out '%{http_code} verify=%{ssl_verify_result}\n' "${url}" ||
      true
  done
}
```

El 2026-09-25 devolvía `401` en `logs-satisfactory` y en `/companions`,
`503` en OpenClaw, `404` en `/app/` y en `generadorcodigosqr`, `302`/`307`
en Passbolt y Alberto, y `200` en el resto, todas con `verify=0`.

#### Rollback

Si la tarea nueva no queda sana en los 90 s de `monitor`, Swarm vuelve solo
al spec anterior (`failure_action: rollback`), deja el `PreviousSpec` nulo
(ver «Crash tras un rollback» en [AUTOUPDATE.md](AUTOUPDATE.md)) y el apply
falla. A mano, `docker service rollback edge_traefik` vuelve al
`PreviousSpec`, con otro corte de unos 13 s.

Ese `PreviousSpec` es el estado hecho a mano (la Config
`edge-traefik-dynamic-companions-a0952eace071`, solo el secret de Cloudflare
y la red sin alias) únicamente entre el paso 4 y el paso 8. `docker stack
deploy` actualiza cada servicio de la pila aunque no cambie, y cada
actualización guarda el spec en uso como `PreviousSpec`: en vivo, servicios
de `workloads` que el último deploy no cambió tienen un `PreviousSpec` igual
a su spec. Tras el paso 8, un rollback reaplicaría el spec de este cambio y
no haría nada. Por eso toda comprobación que pueda pedir un rollback (pasos
5 a 7) va antes del paso 8. Más tarde, la vuelta atrás es un PR revisado que
revierta o corrija lo que falla y un apply de `edge` en otra ventana como
esta. Revertir este cambio entero no devuelve el estado hecho a mano: quitaría
las rutas de Satisfactory del render.

Cómo volver depende de si Ansible dejó su marker:

- **Falla `deploy-ansible.sh`** (el `--check`, la sonda HTTPS, la prueba
  `401` o un rollback automático). Un fallo de Ansible conserva
  `/run/lock/dockerswarm-ansible.marker` (ver «Bloqueo de cambios Ansible» en
  [OPERATIONS.md](OPERATIONS.md)) y `host_global_operation_lock.py run` se
  niega mientras exista, así que el rollback va sin lock, como la parada de
  emergencia de «Apply rechazado por un update en curso»:
  1. Leer qué spec corre:

     ```bash
     sudo -- docker service inspect edge_traefik --format \
       '{{range .Spec.TaskTemplate.ContainerSpec.Configs}}{{.ConfigName}} {{end}}{{.UpdateStatus.State}}'
     ```

  2. Si la Config dinámica ya es `…companions-a0952eace071`, no se lanza
     ningún rollback: Swarm ya volvió (`rollback_completed`), está volviendo
     (`rollback_started`: esperar y repetir la lectura) o el fallo llegó antes
     del deploy. Si es la Config nueva, rollback de emergencia:
     `sudo -- docker service rollback edge_traefik`.
  3. `edge_probe` igual que en `/tmp/edge-before.txt`.
  4. Recuperar el marker con «Recuperar un marker abandonado» de
     [OPERATIONS.md](OPERATIONS.md): primero el dry-run y después
     `--apply --confirm`. Solo entonces se crea un secret o se repite un apply.
- **El apply terminó bien y falla el paso 5, 6 o 7.** No queda marker, así
  que el rollback va bajo el lock, como los demás rollbacks manuales:

  ```bash
  sudo -- /usr/bin/python3 scripts/host_global_operation_lock.py run \
    --operation edge-rollback -- \
    /usr/bin/docker service rollback edge_traefik
  ```

Un `404` solo en `logs-satisfactory` es el router desactivado por el fichero
de usuarios, y el apply falla en la prueba `401`: primer caso. Se corrige con
un secret `-v2` y su PR revisado (ver «Crear el secret desde el hash vivo») en
otra ventana. Las Configs antiguas nunca se borran en un deploy. Las dos hechas
a mano (`…companions-a0952eace071` y `…satisfactory-2af1d9e1a890`) son el
destino de este rollback. No se retiran hasta que el propietario lo decida con
el edge ya estable, aunque guarden el hash en línea. Un rollback se registra
el mismo día en `docs/DEPLOYMENT_STATUS.md` y la compuerta 10 sigue abierta.

## Ruta de AX

Por decisión del propietario (2026-09-25), el panel web de AX
(`images/ax-web`, que corre dentro del laboratorio kind) se publica en
`https://ax.apptolast.com` detrás del `basicAuth` de Traefik, sin Cloudflare
Access ni lista de IPs permitidas. `ax-server` sigue sin publicarse nunca
(ver [AX.md](AX.md), «Exposición»). Este cambio solo añade la parte de
Traefik. El panel, su reenviador `ax-web-edge` y los dos secrets mTLS llegan
con el despliegue del panel en el laboratorio: su
`scripts/ax-web-bootstrap.sh init` crea esos dos secrets.

**Este cambio se fusiona con el despliegue del panel o después, nunca
antes.** Desde que se fusiona, todo apply de `edge` o de `site`, que incluye
el rol `edge`, exige los tres secrets de «Secrets de la ruta» y se detiene
antes de mutar nada, también en `--check`, si falta uno. Por eso `edge` y
`site` solo se aplican siguiendo «Ventana de aplicación de la ruta», después
de `ax-web-bootstrap.sh init`. Registrada esa ventana, cada apply posterior
de `edge` o `site` sigue la ventana que este documento da para el cambio que
aplica (la de «Log de acceso en fichero» es «Ventana del log de acceso»), con
las reglas comunes de la compuerta STOP 10 de `CLAUDE.md`. Dentro de la
ventana de la ruta, entre el apply de `edge` y el despliegue del panel, la
ruta pide la contraseña y después responde `502`.

<!-- markdownlint-disable MD013 -->

| Objeto | Valor |
| --- | --- |
| Router `ax` | `Host(ax.apptolast.com)`, `websecure`, `certResolver: letsencrypt`; middlewares `edge-security`, `ax-canonical-host`, `ax-rl-ip`, `ax-rl-host`, `ax-inflight`, `ax-auth`, en ese orden |
| Router `ax-health` | la regla anterior `&& Path(/healthz) && Method(GET)`, los mismos middlewares con `ax-strip-authorization` en lugar de `ax-auth` |
| `ax-canonical-host` | `headers` con `customRequestHeaders.Host: ax.apptolast.com`, para que los límites cuenten un solo Host |
| `ax-rl-ip` | `rateLimit` con `average: 30`, `period: 1m` y `burst: 30` por IP, las IPv6 por su `/64`; tope real de unas 30 peticiones cada 2-3 s |
| `ax-rl-host` | `rateLimit` con `average: 2`, `period: 1s` y `burst: 10` para todo el host, sea cual sea la IP; tope real de unas 10 peticiones cada 1-2 s |
| `ax-inflight` | `inFlightReq` de 8 peticiones simultáneas por host, flujos SSE abiertos incluidos |
| `ax-auth` | `basicAuth` con `usersFile: /run/secrets/basicauth_ax`, `realm: AX`, `removeHeader: true` y sin `users` |
| `ax-strip-authorization` | `headers` con `customRequestHeaders.Authorization: ""`, que borra esa cabecera |
| Backend `ax` | `https://ax-web-edge:8443`, `passHostHeader: true`, `serversTransport: ax-web-mtls`, sin `healthCheck` |
| Transporte `ax-web-mtls` | `serverName: ax-web`, CA `/run/secrets/ax_upstream_ca`, certificado cliente y clave en `/run/secrets/ax_upstream_client`, TLS 1.3 como mínimo y máximo, `dialTimeout` 5 s, `responseHeaderTimeout` 60 s, `idleConnTimeout` 180 s |
| Red | `apptolast-edge-ax`, overlay cifrada y adoptada (`attachable`), con la subred fija `10.0.250.0/24` |

<!-- markdownlint-enable MD013 -->

`scripts/validate-contract.py` fija cada valor de la tabla. También exige
que `ax-web-mtls` sea el único transporte y que solo lo use `ax`, rechaza
`insecureSkipVerify` en cualquier punto del render y comprueba que cada
fichero `/run/secrets/…` del render sea un secret montado y al revés.
`tests/test_edge_contract.py` prueba cada una de esas guardas con su
mutación negativa, y `EdgeLiveParityTests` admite sobre la Config viva
exactamente estos objetos y ningún cambio en los existentes. Además,
`scripts/validate-traefik-config.sh` arranca el Traefik fijado con el render
dinámico entero (ver «Validación con Traefik»).

Por qué es así, contra el código de Traefik v3.7.13, la versión que
ejecutaba el canal `traefik:v3` el 2026-09-26. La comparación de versiones
TLS del transporte es la misma en v3.7.9, la base fijada:

- **Host canónico antes de los límites.** El router compara el Host en
  minúsculas, sin puerto y con o sin punto final
  (`pkg/middlewares/requestdecorator/request_decorator.go` y
  `pkg/muxer/http/matcher.go`). En cambio, `rateLimit` e `inFlightReq` con
  `requestHost` agrupan por el Host tal como llega:
  `pkg/middlewares/extractor.go` usa el extractor `request.host` de
  `vulcand/oxy` v2.1.0, la versión que fija el `go.mod` de Traefik, y
  `utils/source.go` de oxy devuelve `req.Host` sin tocarlo. Sin
  `ax-canonical-host`, `AX.apptolast.com`, `ax.apptolast.com:443` o
  `ax.apptolast.com.` entran en `ax` con un contador nuevo cada uno, y el
  tope de host deja de existir. El middleware `headers` asigna `req.Host`
  antes de llamar al siguiente (`pkg/middlewares/headers/header.go`), así
  que desde ahí todas las variantes cuentan como una. Con
  `passHostHeader: true` el panel recibe ese Host, y solo compara `Origin`.
- **Orden de los middlewares.** Se aplican en el orden declarado, así que
  los límites van antes del `basicAuth`: cada petición con credenciales
  cuesta un bcrypt, también con un usuario inexistente
  (`pkg/middlewares/auth/basic_auth.go`). Los dos `rateLimit` van antes que
  `inFlightReq` porque un `rateLimit` retiene hasta 0,5 s la petición que
  admite con retraso (`pkg/middlewares/ratelimiter/rate_limiter.go`), y así
  esa espera no ocupa una de las 8 plazas. Cada router construye sus propios
  middlewares (`pkg/server/middleware/middlewares.go`), así que `ax` y
  `ax-health` no comparten contadores. Quien agote el cupo de host deja al
  propietario con `429`.
- **Topes reales de los `rateLimit`.** Traefik olvida el contador de una
  fuente tras un TTL sin peticiones suyas: `1 + 1/tasa` segundos, en entero,
  con menos de una petición por segundo, y 2 s con una o más
  (`pkg/middlewares/ratelimiter/rate_limiter.go:92-101`), es decir, 3 s en
  `ax-rl-ip` y 2 s en `ax-rl-host`. La siguiente petición crea un contador
  nuevo con la ráfaga entera (`rate.NewLimiter(tasa, ráfaga)` en
  `in_memory_limiter.go:45-58`), y cada petición, también la rechazada,
  renueva el TTL. La caducidad va en segundos Unix enteros
  (`ratelimiter/ttlmap/ttlmap.go`), así que puede llegar tras algo más de
  TTL − 1 s. El tope real es, por tanto, unas 30 peticiones cada 2-3 s por
  IP y unas 10 cada 1-2 s para todo el host, no 30 por minuto ni 2 por
  segundo. Solo una ráfaga que no pase de lo que la tasa recarga en TTL − 1 s
  (1 por IP y 2 para el host) dejaría el tope en el nominal, y con esas
  ráfagas el panel no carga: una página son 7 peticiones en cuatro tandas (el
  `401`, `/`, `app.css` con `app.js` y `favicon.ico`, y `/api/status` con
  `/api/tasks`), y en la prueba de abajo recibieron `429`.
- **Coste del bcrypt.** Una contraseña fallida cuesta en Traefik v3.7.13
  unos 66 ms de CPU (bcrypt de Go, coste 10, medido abajo). Al tope real del
  host, entre 5 y 10 intentos por segundo, son entre 0,33 y 0,66 CPU frente
  a los `cpus: "0.50"` del servicio (`stacks/edge/stack.yml.j2`), que
  comparten todos los hostnames: quien espacie sus ráfagas puede llevar
  Traefik a su límite de CPU y frenar todas las rutas. `ax-inflight` limita
  peticiones simultáneas, no CPU, y `logs-satisfactory` ya hace bcrypt sin
  límite. Solo un bloqueo por IP tras fallos lo reduce, y por eso es una
  precondición de «Ventana de aplicación de la ruta».
- **Sin `users`.** Traefik añade las líneas de `users` detrás de las del
  fichero y la última gana (`pkg/middlewares/auth/auth.go`), y pondrían un
  hash en este repositorio público. El fichero es una sola línea
  `usuario:hash` bcrypt de coste 10; Traefik acepta los prefijos `2a`, `2b`,
  `2x` y `2y` (`basic.go` de `containous/go-http-auth`, la versión que fija
  su `go.mod`). Si el fichero falta o está vacío, Traefik desactiva solo este
  router, que responde `404` (ver «Rutas de Satisfactory»).
- **Sin `edge-compress` ni `edge-default`.** `compress` retiene los primeros
  1024 bytes de cada respuesta (`compression_handler.go`) y solo excluye SSE
  mirando el `Content-Type` de la petición (`compress.go`), que un
  `EventSource` no envía: la salida en directo del panel quedaría retenida.
- **`GET /healthz` sin credenciales.** El panel responde `ok` en su puerto
  mTLS a esa única ruta. Cualquier otro método o ruta cae en `ax` y su
  `basicAuth`. Un `200` en `https://ax.apptolast.com/healthz` prueba la
  cadena entera: Traefik, el reenviador, el NodePort y el mTLS. El navegador
  reenvía por su cuenta las credenciales Basic guardadas a toda ruta del
  mismo espacio de protección
  ([RFC 7617](https://www.rfc-editor.org/rfc/rfc7617), sección 2.2), y este
  router no pasa por el `removeHeader` de `ax-auth`: `ax-strip-authorization`
  borra la cabecera, porque Traefik elimina la de `customRequestHeaders` con
  valor vacío (`header.go`). Así el panel no recibe `Authorization` por
  ninguno de los dos routers.
- **TLS 1.3 como mínimo y como máximo.** El panel solo acepta TLS 1.3.
  Traefik v3.7.13 compara `minVersion` con `maxVersion` aunque `maxVersion`
  falte, y entonces vale 0 (`pkg/server/service/transport.go`): con solo
  `minVersion: VersionTLS13` registra `Could not configure HTTP Transport
  ax-web-mtls@file TLS configuration, fallback on default TLS config` y
  conecta sin certificado cliente ni CA propia. El validador rechaza
  `minVersion` sin `maxVersion`.
- **`serverName`.** Traefik verifica el certificado del panel contra el
  nombre `ax-web` y no contra el host de la URL, que es el del reenviador.
- **`forwardingTimeouts` explícitos.** Un transporte con nombre usa solo su
  propia configuración: no hereda el `serversTransport` estático
  (`createRoundTripper` en `transport.go`). `responseHeaderTimeout` de 60 s
  obliga al panel a contestar las operaciones largas con un `202` y seguir
  por SSE; `idleConnTimeout` iguala el del panel.
- **Sin `healthCheck`.** Con el laboratorio parado (no arranca solo tras un
  reinicio del host) la ruta responde `502` después del login y Traefik no
  registra un WARN en cada intervalo.
- **Red adoptada.** El reenviador es un contenedor suelto, así que la red es
  `attachable`, como `apptolast-edge-observatorio` y
  `apptolast-edge-satisfactory`. Como toda red del edge, es una overlay
  cifrada: el rol la crea con `--opt encrypted` (y `--attachable`) si falta y
  rechaza cualquier red del edge sin cifrar
  (`ansible/roles/edge/tasks/deploy.yml`). Es la única con subred fija,
  `10.0.250.0/24` (`edge_network_subnets` en `ansible/group_vars/all.yml`):
  el reenviador solo admite pares de esa subred (`--allow-cidr`), así que el
  rol la crea con `--subnet` y rechaza la red si Swarm la numeró de otra
  forma. El despliegue del panel solo la inspecciona y se detiene si no es
  esa.

Un certificado cliente que no carga no se ve en los logs: Traefik solo lo
registra en DEBUG (`pkg/tls/certificate.go`), y trata una ruta que no puede
abrir como el propio contenido PEM (`pkg/types/file_or_content.go`). El panel
cierra entonces el TLS con `certificate required`, y Traefik devuelve `502`,
también en DEBUG. Una CA o un `serverName` que no encajan sí dan ERROR
(`pkg/proxy/httputil/proxy.go`). Por eso la prueba del mTLS es el `200` de
`/healthz`, no los logs.

Comprobado el 2026-09-26 con el binario oficial de Traefik v3.7.13 (tarball
de la release con su sha256 verificado contra `traefik_v3.7.13_checksums.txt`),
como proceso local en loopback, sin Docker, con los objetos de esta sección
tomados del render (rutas de secrets cambiadas a un directorio temporal,
certificados y contraseña de prueba). El backend exigía TLS 1.3 y
certificado cliente de la CA:

- `GET /` sin credenciales: `401` y `WWW-Authenticate: Basic realm="AX"`;
- `GET /healthz` sin credenciales: `200 ok`, con TLS 1.3 y el certificado
  cliente `edge-traefik`;
- `POST` o `HEAD /healthz` sin credenciales: `401`;
- con credenciales: `200`, sin cabecera `Authorization` en el backend y con
  el `Host` canónico;
- un login fallido con un usuario inventado: `401`, y el log de acceso sin
  `ClientUsername` ni ese usuario;
- solo `minVersion`: el error de arriba y `500` en `/healthz`;
- un certificado cliente ausente: `401` en `/`, `502` en `/healthz` y los
  errores solo en DEBUG.

Con el mismo binario, el mismo día y el render de este cambio (backend mTLS
local, contraseña de prueba, una IP de loopback distinta por ronda cuando
hacía falta):

- sin `ax-canonical-host`, tras agotar el cupo de host con
  `ax.apptolast.com` (10 `401` y después `429`), `AX.apptolast.com`,
  `Ax.apptolast.com` y `ax.apptolast.com:443` recibieron 5 de 5 `401`, un
  contador nuevo cada una; con él, las mismas variantes, `ax.apptolast.com.`,
  `ax.apptolast.com:1` y `aX.APPTOLAST.com` recibieron `429`;
- tras pausas de 2,2 s, tres rondas de 12 peticiones dieron 10 `401` cada
  una (a 2 por segundo tocaban unas 4); ocho rondas desde IPs distintas, con
  pausas de 1,3 a 1,8 s, pasaron 80 peticiones en 16 s, frente a unas 42 con
  un contador que no se regenerase; tres rondas de 40 peticiones desde una
  sola IP, con pausas de 3,3 s y solo `ax-rl-ip` en un router de prueba,
  dieron 30 `200` cada una (a 30 por minuto tocaban unas 2);
- un login fallido tardó 66 ms de media en Traefik, uno correcto 76 ms
  (incluido el backend) y una petición sin credenciales 1 ms;
- `GET /healthz` con `Authorization` y `Host: AX.apptolast.com:443` llegó al
  panel sin `Authorization` y con `Host: ax.apptolast.com`, igual que un
  `GET /` con credenciales;
- una carga de página completa (las 7 peticiones de arriba) recibió `200` en
  todas; con ráfaga 1 por IP, `429` desde `/`, y con ráfaga 2 para el host,
  `429` en `app.css`, `app.js`, `/api/status` y `/api/tasks`.

### Validación con Traefik

`scripts/validate-traefik-config.sh`, dentro de `scripts/validate-iac.sh`,
arranca dos veces el Traefik fijado con el mismo endurecimiento que el
servicio. La primera, con la configuración estática del render y un fichero
dinámico mínimo. La segunda, con el render dinámico entero, que prepara
`scripts/prepare-traefik-validation.py`:

- quita el resolver ACME y toda referencia a él: pediría a Let's Encrypt
  producción un certificado por hostname con un token de prueba;
- quita el `healthCheck` de los backends: sus nombres de Swarm y de kind no
  resuelven ahí, y cada sonda fallida registraría un WARN;
- monta en `/run/secrets` un sustituto desechable de cada fichero que nombra
  el render, del mismo tipo: un fichero de usuarios con una contraseña
  aleatoria para cada `basicAuth`, una CA para `rootCAs` y un PEM con
  certificado cliente y clave para `certificates`. Un fichero de
  `/run/secrets` usado en cualquier otro sitio detiene la validación.

Cualquier entrada WARN o superior, salvo los dos avisos conocidos de v3.7,
hace fallar la validación. Comprobado con el binario oficial el 2026-09-26:
el render de este cambio arranca solo con esos avisos; `minVersion` sin
`maxVersion` da el ERROR `Could not configure HTTP Transport`; una clave
desconocida detiene el proveedor de ficheros con un ERROR y deja `/ping` sin
router, así que el healthcheck no converge; y `ipStrategy` junto a
`requestHost` desactiva `ax` y `ax-health` con un ERROR.

### Log de acceso

`basicAuth` guarda en `ClientUsername` lo que se escriba como usuario,
también cuando el login falla (`basic_auth.go`). Una contraseña tecleada en
ese campo acabaría en el log de acceso. La configuración estática lo
descarta con `accessLog.fields.names.ClientUsername: drop`: Traefik pasa los
nombres a minúsculas al cargarlos y compara en minúsculas
(`pkg/middlewares/accesslog/logger.go`). Afecta a todas las rutas, pero solo
los `basicAuth` rellenan ese campo.

### Secrets de la ruta

<!-- markdownlint-disable MD013 -->

| Docker Secret | Fichero en `/run/secrets` | Contenido | Lo crea |
| --- | --- | --- | --- |
| `edge-basicauth-ax-v1` | `basicauth_ax` | una línea `usuario:hash` bcrypt de coste 10 | el operador, con la orden de abajo |
| `edge-ax-upstream-ca-v1` | `ax_upstream_ca` | el certificado PEM de la CA privada del panel | `scripts/ax-web-bootstrap.sh init`, del despliegue del panel |
| `edge-ax-upstream-client-v1` | `ax_upstream_client` | un PEM con el certificado cliente (CN `edge-traefik`, uso `clientAuth`, emitido por esa CA) seguido de su clave privada sin cifrar | `scripts/ax-web-bootstrap.sh init`, del despliegue del panel |

<!-- markdownlint-enable MD013 -->

Los tres llevan `com.apptolast.managed-by=manual-bootstrap`. El del login
lleva `com.apptolast.purpose=traefik-basicauth`, como el de Satisfactory, y
los dos mTLS `com.apptolast.purpose=traefik-upstream-mtls`. Se montan `0400`
para `65532:65532`. El rol solo inspecciona sus metadatos, con `no_log`, y se
detiene antes de mutar nada, también en `--check`, si falta uno o no lleva
esas etiquetas. **Desde que este cambio se fusiona, todo apply de `edge` o
de `site` exige los tres secrets** (ver el orden de fusión al principio de
«Ruta de AX»). Traefik lee el mismo fichero como certificado y
como clave: toma los bloques `CERTIFICATE` para el primero y el bloque de
clave para la segunda (así cargó en la prueba de arriba).

Los nombres están fijados en `ansible/group_vars/all.yml`
(`edge_traefik_basicauth_secrets` y `edge_traefik_upstream_mtls_secrets`), en
las aserciones de valor exacto de `ansible/roles/edge/tasks/main.yml`, en
`scripts/validate-contract.py` y en `tests/test_edge_contract.py`. Nunca se
reutiliza un nombre: una contraseña o un certificado nuevos van en un `-v2`
creado antes, cambiado en esos cuatro sitios en un PR revisado y aplicado en
otra ventana.

### Crear el secret del login

El usuario lo decide el propietario y no se escribe en este repositorio
público. La contraseña solo existe fuera del host: al host llega por la
entrada estándar de esta orden y solo sale de ella su hash, hacia
`docker secret create`. Nunca va en un argumento (lo verían `ps`, sudo y el
historial), ni en un `echo`, ni en un fichero, ni con `set -x`.

En el host hay `python3-bcrypt` 5.0.0 (paquete de Ubuntu `5.0.0-3build1`,
para `/usr/bin/python3` 3.14.4) y `mkpasswd` 5.6.6 (paquete `whois`); no hay
`htpasswd` (`apache2-utils` no está instalado). Se usa `python3-bcrypt`,
que rechaza una contraseña de más de 72 bytes en vez de truncarla
(comprobado con un valor de prueba). Python corre con `-I` (modo aislado):
sin el directorio actual en `sys.path` ni las variables `PYTHON*`, ningún
módulo suelto del clon puede suplantar a `bcrypt`, `getpass` o `re` en el
proceso que tiene la contraseña (comprobado con un `bcrypt.py` de prueba en
el directorio de trabajo, que sin `-I` se importa en lugar del paquete).

Esta orden **no** va bajo `host_global_operation_lock.py run`. Ese lock
ejecuta la orden en una pseudoterminal (`pty.fork` en
`scripts/run-locked-command.py`) y le copia la entrada estándar con el eco
de la terminal activo: una línea enviada por la entrada sale por la salida
aunque la orden la descarte (comprobado el 2026-09-26 con un valor de
prueba). `sudo` sin ese lock no la repite: con la entrada redirigida, la
orden no recibe una terminal y los sudoers del host no tienen `log_input`.
`docker secret create` es atómico y rechaza un nombre existente. Antes de
empezar, sin otra operación en curso:

```bash
sudo -- docker secret ls --filter name=edge-basicauth-ax --quiet
ls /run/lock/dockerswarm-*.marker 2>/dev/null
```

Ambas deben salir vacías. Después, con el usuario en lugar de `<usuario>`:

```bash
set +x
set -o pipefail
sudo -v
ax_user='<usuario>'
/usr/bin/python3 -I -c '
import getpass, re, sys
import bcrypt
user = sys.argv[1]
if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", user):
    sys.exit("ERROR: usuario no valido")
if sys.stdin.isatty():
    password = getpass.getpass("Contrasena: ").encode()
    if password != getpass.getpass("Repetir: ").encode():
        sys.exit("ERROR: las contrasenas no coinciden")
else:
    password = sys.stdin.buffer.read()
    if password.endswith(b"\n"):
        password = password[:-1]
if not password or len(password) > 72 or re.search(rb"[\r\n]", password):
    sys.exit("ERROR: la contrasena debe ser una linea de 1 a 72 bytes")
hashed = bcrypt.hashpw(password, bcrypt.gensalt(rounds=10, prefix=b"2b"))
ok = bcrypt.checkpw(password, hashed)
del password
if not ok or len(hashed) != 60 or hashed.split(b"$")[1:3] != [b"2b", b"10"]:
    sys.exit("ERROR: el hash no tiene la forma esperada")
print("hash: prefijo", hashed[:2].decode(), "longitud", len(hashed),
      file=sys.stderr)
sys.stdout.buffer.write(user.encode() + b":" + hashed + b"\n")
' "${ax_user}" |
  sudo -- docker secret create \
    --label com.apptolast.managed-by=manual-bootstrap \
    --label com.apptolast.purpose=traefik-basicauth \
    edge-basicauth-ax-v1 - >/dev/null
```

- Quien automatiza la creación conecta su propia tubería a la entrada
  estándar de la orden y escribe en ella la contraseña y un salto de línea.
  Nada más la toca.
- Una persona la ejecuta en su terminal SSH: con la entrada en la terminal,
  Python la pide dos veces sin eco (`getpass`). `sudo -v` va antes para que
  una petición de contraseña de sudo no se cruce con esa.
- La única salida es `hash: prefijo $2 longitud 60`, que comprueba el hash
  sin mostrarlo. Tras cualquier `ERROR` no se crea nada: Docker rechaza un
  secret vacío (`ValidateSecretPayload` en `api/validation/secrets.go` de
  swarmkit).

Comprobación, solo de metadatos:

```bash
sudo -- docker secret inspect edge-basicauth-ax-v1 \
  --format '{{.Spec.Name}} {{json .Spec.Labels}}'
```

Debe mostrar el nombre y las dos etiquetas. El contenido de un Docker Secret
no se puede leer después ([Docker
secrets](https://docs.docker.com/engine/swarm/secrets/)). La prueba de que
Traefik lo cargó es el `401` con `realm="AX"` del apply (un fichero que no
carga da `404`), y la de la contraseña, el primer login del propietario.

### Registro DNS

`ax.apptolast.com` es un registro A a `159.195.156.57` que el propietario
creó a mano en Cloudflare, DNS-only, igual que los de OrganizationWeb y
RacingGame (ver [RACINGGAME.md](RACINGGAME.md), «Alcance y precondiciones»).
El 2026-09-26 resolvía a esa IP con TTL servido de 300 s y sin AAAA. Es
deriva frente a Terraform (ver
[DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md), «Deriva fuera del
repositorio»):

- No se crea nunca desde Terraform: Cloudflare admite varios A con el mismo
  nombre, así que un `create` añadiría un segundo registro.
- Tampoco se ejecuta el root `cloudflare/apptolast-dns` por él (ver
  «Advertencia sobre Terraform y DNS» en DEPLOYMENT_STATUS.md).
- Se adoptará con la adopción general del DNS, con un bloque `import` como
  los de `infra/terraform/cloudflare/apptolast-dns/imports.tf`
  (`<zone_id>/<dns_record_id>`, provider `5.25.0`), junto con
  `config/platform.yml`, `contract.tf`, `dns.tf`, `terraform-safety.py`,
  sus pruebas y el README del root. El TTL configurado no se ha leído de la
  API; si es «Automático» (la API lo guarda como 1), el plan de adopción
  mostrará el cambio a los 300 s del contrato.
- Sigue DNS-only: coincide con `proxied=false` del contrato, CrowdSec y el
  bouncer ven la IP real y DNS-01 no depende de él.

### Ventana de aplicación de la ruta

Es la parte de `edge` de la ventana del panel. Tiene los mismos riesgos y
reglas que la de Satisfactory (ver «Ventana de aplicación»): unos 13 s sin
conexiones nuevas en 80/443 para todos los hostnames, cortes en los
WebSocket y SSE abiertos, una sola persona, fuera de 22:30–00:40 UTC, desde
el clon operativo limpio en detached HEAD sobre el commit fusionado, tras
`git fetch --all --prune`.

Precondiciones. Si falta una, esta ventana no empieza:

- La ventana de Satisfactory ya se hizo y verificó (su apply repetido dio
  `changed=0`), y `docs/DEPLOYMENT_STATUS.md` registra el `Version.Index` y
  las dos Configs de `edge_traefik` tras ese apply repetido, o tras una
  ventana de `edge` posterior también registrada.
- El despliegue del panel ya está fusionado (este cambio entra con él o
  después) y `scripts/ax-web-bootstrap.sh init` creó los dos secrets mTLS.
  El del login también existe (sección anterior).
- Hay bloqueo tras fallos: CrowdSec lee los logs de Traefik y banea por IP
  los `401` repetidos de `ax@file`, con las IPs del propietario en su lista
  permitida (cambio aparte). Los límites solo frenan, con los topes reales y
  el coste de CPU de arriba (unos 430 000 intentos al día a 5 por segundo).
  Sin ese cambio, la ventana solo se abre si el propietario deja escrito
  antes en `docs/DEPLOYMENT_STATUS.md` que acepta publicar el panel sin
  bloqueo.
- `getent ahostsv4 ax.apptolast.com` devuelve `159.195.156.57`.

Pasos:

1. Comprobar que nadie tocó `edge_traefik` desde la última ventana
   registrada, y los secrets (solo metadatos):

   ```bash
   sudo -- docker service inspect edge_traefik --format \
     '{{.Version.Index}}{{range .Spec.TaskTemplate.ContainerSpec.Configs}} {{.ConfigName}}{{end}}'
   for name in edge-basicauth-ax-v1 edge-ax-upstream-ca-v1 \
     edge-ax-upstream-client-v1; do
     sudo -- docker secret inspect "${name}" \
       --format '{{.Spec.Name}} {{json .Spec.Labels}}'
   done
   ```

   La primera línea debe ser exactamente el índice y las dos Configs que
   registró la última ventana en `docs/DEPLOYMENT_STATUS.md` (la de
   Satisfactory en su paso 9, o una posterior). Otro índice u otra Config
   significa que alguien tocó el servicio a mano: parar, renderizar el
   commit que registró esa ventana (detached HEAD y
   `ansible/playbooks/render-edge.yml`) y compararlo con la Config dinámica
   viva, que ya no guarda ningún hash:

   ```bash
   sudo -- docker config inspect <Config dinámica del paso 1> \
     --format '{{printf "%s" .Spec.Data}}' |
     diff -u .build/edge/dynamic.yml -
   ```

   La ventana no sigue hasta que cada diferencia esté explicada y
   codificada. El spec así verificado es el destino de un rollback. Cada
   secret debe mostrar su nombre y sus dos etiquetas.
2. Renderizar y guardar el estado de cada ruta pública con `edge_probe`
   (ver «Ventana de aplicación»): `edge_probe > /tmp/edge-before-ax.txt`. Las
   dos líneas de `ax.apptolast.com` salen con `000`: aún no hay certificado y
   `sniStrict` rechaza el TLS.
3. `./scripts/deploy-ansible.sh --playbook edge --check --ask-become-pass`.
   `--check` no ejecuta el despliegue, pero sí la comprobación de los tres
   secrets. Si está limpio, el apply con `--confirm-production`. Crea
   `apptolast-edge-ax` si falta (overlay cifrada, `attachable` y con su
   subred fija), cambia las dos Configs, relanza la tarea y Traefik pide el
   certificado de `ax.apptolast.com` por DNS-01. La prueba del apply espera
   hasta 5 minutos el `401` con `realm="AX"`.
4. `edge_probe > /tmp/edge-after-ax.txt` y
   `diff /tmp/edge-before-ax.txt /tmp/edge-after-ax.txt`. Solo cambian las
   dos líneas de `ax.apptolast.com`: `/` pasa a `401 verify=0` y `/healthz`
   a `502 verify=0` (panel aún no desplegado) o `200 verify=0`. Cualquier
   otra diferencia es motivo de rollback.
5. El certificado es de Let's Encrypt producción y cubre el nombre:

   ```bash
   openssl s_client -connect 159.195.156.57:443 \
     -servername ax.apptolast.com </dev/null 2>/dev/null |
     openssl x509 -noout -issuer -subject -ext subjectAltName -enddate
   ```

6. `sudo -- docker service inspect edge_traefik`: las Configs nuevas, cinco
   secrets (el token de Cloudflare, los dos ficheros de usuarios y los dos
   mTLS) y 14 redes. En los logs de la tarea nueva la cuenta es `0`:

   ```bash
   task="$(sudo -- docker service ps edge_traefik \
     --filter desired-state=running --quiet --no-trunc)"
   sudo -- docker service logs --since 10m "${task}" 2>&1 |
     grep --count -e 'no users found' \
       -e 'Could not configure HTTP Transport' \
       -e 'Unable to obtain ACME certificate' -e '"level":"error"'
   ```

7. Solo si los pasos 4 a 6 salieron bien, repetir el apply: `changed=0` y el
   mismo ID de tarea. Desde aquí `docker service rollback` ya no vuelve al
   spec del paso 1.
8. Registrar la evidencia en `docs/DEPLOYMENT_STATUS.md`, con el
   `Version.Index` y las dos Configs tras el apply repetido: son la
   referencia del paso 1 de la siguiente ventana.

Cuando el despliegue del panel esté aplicado, la prueba de extremo a extremo
es `curl --silent --resolve ax.apptolast.com:443:159.195.156.57
https://ax.apptolast.com/healthz`, que devuelve `ok`, y después el primer
login del propietario en el navegador.

#### Rollback de la ruta

Igual que el de Satisfactory (ver «Rollback»), con el spec verificado en el
paso 1 como destino:

- **Falla `deploy-ansible.sh`** (el `--check`, la sonda HTTPS, la prueba
  `401` o un rollback automático): Ansible conserva su marker. Se lee qué
  Config corre y, solo si es la nueva, `sudo -- docker service rollback
  edge_traefik` sin lock. Después `edge_probe` igual que en
  `/tmp/edge-before-ax.txt` y se recupera el marker antes de cualquier otro
  paso.
- **El apply terminó bien y falla el paso 4, 5 o 6**: rollback bajo el lock
  (`--operation edge-rollback`), como en la otra ventana.

Un `404` solo en `ax.apptolast.com` es el `basicAuth` sin fichero de
usuarios, y un fallo solo del certificado deja las demás rutas intactas.
Ambos se corrigen en otra ventana: un secret `-v2` y su PR, o la causa del
DNS-01 que muestre el log. Un `502` en `/healthz` con el panel ya desplegado
se diagnostica desde su despliegue (reenviador, NodePort, certificados): no
afecta a otras rutas ni pide rollback del edge. El rollback no borra la
red `apptolast-edge-ax`: queda vacía, y el siguiente apply la reutiliza.
Después del paso 7, la vuelta atrás es un PR revisado y otro apply de `edge`.

## Log de acceso en fichero

Traefik escribe su log de acceso JSON en un fichero del host y CrowdSec lo
lee como lee `auth.log`: con su fuente `file`, nunca por la API de Docker.
Es la base de los baneos por `401` repetidos en los `basicAuth` (ver
[`host_security/README.md`](../ansible/roles/host_security/README.md)). El
log de aplicación de Traefik sigue en stdout.

Por qué no la API de Docker: en CrowdSec 1.7.8 todas las fuentes comparten
una sola adquisición y la primera que devuelve un error para las demás
(`pkg/acquisition/acquisition.go:652-656`). La fuente `docker` reintenta su
suscripción a los eventos de Docker con un límite total de 15 minutos que no
se puede cambiar (`pkg/acquisition/modules/docker/run.go:28-40` y `:410`,
`cenkalti/backoff` v5.0.3 `retry.go:10`). Tras una caída de Docker de ese
orden devuelve el error (`run.go:462-469`) y CrowdSec deja de leer también
`auth.log` y syslog hasta que alguien lo reinicia: la detección de fuerza
bruta de SSH dependería de Docker. La fuente `file` no tiene ese modo de
fallo; el detalle, con sus líneas, está en el README del rol.

<!-- markdownlint-disable MD013 -->

| Objeto | Valor |
| --- | --- |
| Configuración estática | `accessLog.filePath: /var/log/traefik/access.log`, `format: json`, sin `ClientUsername` (ver «Log de acceso») |
| Montaje | bind de `/var/log/dockerswarm/edge` (`edge_traefik_access_log_dir`) en `/var/log/traefik`, escribible; el otro y único montaje sigue siendo el de `/data` |
| Directorios del host | `/var/log/dockerswarm` y `/var/log/dockerswarm/edge`, `root:root 0755`; los crean `edge` y `host_security` con la misma identidad y rechazan un enlace |
| Fichero | `/var/log/dockerswarm/edge/access.log`, `65532:65532 0600`, un solo enlace; lo crea el rol `edge` antes del despliegue y nunca lo repara |
| Rotación | `/opt/dockerswarm/edge/config/traefik-access.logrotate`: `daily`, `maxsize 100M`, `rotate 14`, `copytruncate`, `compress`, `delaycompress` |
| Temporizador | `dockerswarm-edge-access-log-rotate.timer`, cada 15 minutos, con su propio estado en `/var/lib/logrotate/dockerswarm-edge-access.status` |
| Lector | CrowdSec, `/etc/crowdsec/acquis.d/02-dockerswarm-traefik.yaml` (`config/host-security.yml`, `host_security_crowdsec_traefik_access_log`) |

<!-- markdownlint-enable MD013 -->

Decisiones:

- **Solo un fichero escribible.** El usuario de Traefik puede escribir en
  `access.log` y en nada más de ese directorio: no puede crear, renombrar ni
  enlazar una entrada junto a él. logrotate y CrowdSec, que corren como
  root, nunca siguen un enlace que haya dejado la tarea. Traefik abre el
  fichero con `O_RDWR|O_CREATE|O_APPEND`
  (`pkg/middlewares/accesslog/logger.go:460-470` en v3.7.13), así que le
  basta el `0600` de su dueño.
- **Rotación sin señal.** Traefik reabre sus logs con `USR1`
  (`pkg/server/server_signals.go:13-31`), pero mandarla a una tarea de Swarm
  exige la API de Docker. Como escribe con `O_APPEND`, `copytruncate` lo
  rota sin señal: la siguiente escritura cae al principio del fichero
  truncado, y CrowdSec reabre un fichero truncado desde el principio
  (`nxadm/tail` v1.4.11, `tail.go:403-410`). `copytruncate` puede perder las
  líneas que lleguen entre la copia y el truncado (página de manual de
  logrotate); son unas pocas por rotación y no cambian un baneo.
- **Temporizador propio.** El stdout al que sustituye lo acotaba el driver
  `local` a 5 × 20 MB. El `logrotate.timer` de la distribución es diario y no
  acota una inundación, así que un temporizador dedicado pasa la política
  cada 15 minutos. El techo es unos 100 MB vivos, otros 100 MB en `.1` y
  trece `.gz`, más lo que llegue en 15 minutos.
- **Prueba en el despliegue.** Si Traefik no puede abrir el fichero, arranca
  sin log de acceso y solo deja un WARN (`cmd/traefik/traefik.go:594-606`):
  CrowdSec no vería a nadie. Por eso `scripts/validate-traefik-config.sh`
  arranca Traefik con un `tmpfs` en `/var/log/traefik` y falla con ese WARN,
  y el apply de `edge`, tras las sondas `401` sin credenciales, exige con
  `scripts/traefik-access-log-probes.py` que el fichero tenga esos `401` con
  el `RequestHost` y el `RouterName` que cuenta el escenario. El script solo
  imprime cuentas, nunca una línea del log.

Consecuencias:

- `docker service logs edge_traefik` ya no muestra el log de acceso, solo el
  de aplicación, y el diagnóstico del rol `edge` tras un fallo deja de
  imprimir IPs de clientes. El log de acceso se lee con
  `sudo -- tail /var/log/dockerswarm/edge/access.log`.
- Alloy, en el stack de observabilidad (hoy sin desplegar), solo recoge el
  stdout de los contenedores: el log de acceso no llegaría a Loki. Añadirlo
  sería un `loki.source.file` sobre este fichero, en un cambio aparte.
- Las sondas que se lanzan desde el propio host (`edge_probe`, las del rol)
  llegan a Traefik desde `docker_gwbridge` (`172.18.0.1`), una dirección
  privada que el parser `crowdsecurity/whitelists` ya descarta: nunca banean.

`scripts/validate-contract.py` fija la ruta, el formato y los dos montajes,
y exige que `config/host-security.yml` lea el mismo fichero que Traefik
escribe; el rol comprueba los dos montajes en el servicio desplegado y
`scripts/validate-edge.sh` en el vivo, con la identidad del fichero y el
temporizador. Lo prueban `tests/test_edge_access_log_contract.py` y
`tests/test_edge_contract.py`.

### Ventana del log de acceso

Es la ventana de `edge` de este cambio. La compuerta STOP 10 de `CLAUDE.md`
exige que, registrada «Ventana de aplicación de la ruta», cada apply de
`edge` o `site` siga la ventana de su cambio con las reglas de aquella: unos
13 s sin conexiones nuevas en 80/443 para todos los hostnames, cortes en los
WebSocket y SSE abiertos, una sola persona, fuera de 22:30–00:40 UTC, desde
el clon operativo limpio en detached HEAD sobre el commit fusionado, tras
`git fetch --all --prune`.

Precondiciones. Si falta una, esta ventana no empieza:

- La última ventana de `edge` está registrada en
  `docs/DEPLOYMENT_STATUS.md` con su `Version.Index` y sus dos Configs tras
  el apply repetido. Hoy es la de la ruta de AX, cuyo registro llega con el
  PR #80 («Panel web de AX: ventana 2»). Si ese registro no trae el índice y
  las Configs finales, se completa antes, en un cambio de evidencia, con la
  comparación del paso 1 de «Ventana de aplicación de la ruta»: renderizar
  el commit de esa ventana y compararlo con la Config dinámica viva.
- Recomendado, no obligatorio: `host-baseline` con este cambio ya aplicado
  (ver «CrowdSec y log de acceso de Traefik (2026-09-26)» en
  `docs/DEPLOYMENT_STATUS.md`).
  Así CrowdSec lee el fichero en cuanto Traefik escribe la primera línea. El
  orden no importa: los dos roles crean los directorios y CrowdSec vigila el
  suyo desde que arranca.

Pasos:

1. Igual que el paso 1 de «Ventana de aplicación de la ruta»: el
   `Version.Index` y las dos Configs deben ser los registrados, y cada
   secret de la ruta debe mostrar su nombre y sus dos etiquetas.
2. `edge_probe > /tmp/edge-before-log.txt`.
3. `./scripts/deploy-ansible.sh --playbook edge --check --ask-become-pass`
   y, si está limpio, el apply con `--confirm-production`. Cambios
   esperados: los directorios (si `host-baseline` aún no los creó), el
   fichero `access.log`, la política de rotación, las dos unidades y el
   temporizador, la Config estática nueva y el servicio (montaje nuevo y esa
   Config), que relanza la tarea. El apply termina probando que las sondas
   `401` de los dos logins están en el fichero.
4. `edge_probe > /tmp/edge-after-log.txt` y
   `diff /tmp/edge-before-log.txt /tmp/edge-after-log.txt`: sin
   diferencias. Cualquier diferencia es motivo de rollback.
5. Comprobar el servicio, el fichero y su lector:

   ```bash
   sudo -- docker service inspect edge_traefik \
     --format '{{json .Spec.TaskTemplate.ContainerSpec.Mounts}}'
   sudo -- stat --format '%n %u:%g %a %h' /var/log/dockerswarm/edge \
     /var/log/dockerswarm/edge/access.log
   systemctl list-timers dockerswarm-edge-access-log-rotate.timer
   sudo -- cscli metrics show acquisition
   task="$(sudo -- docker service ps edge_traefik \
     --filter desired-state=running --quiet --no-trunc)"
   sudo -- docker service logs --since 10m "${task}" 2>&1 |
     grep --count -e 'Unable to create access logger' -e '"level":"error"' \
       -e 'no users found' -e 'Could not configure HTTP Transport'
   ```

   Dos montajes `bind` (`/data` y `/var/log/traefik`), el directorio
   `0:0 755` y el fichero `65532:65532 600 1`, el temporizador con su
   próxima ejecución, `file:/var/log/dockerswarm/edge/access.log` con líneas
   leídas en la tabla de CrowdSec (si `host-baseline` ya se aplicó) y la
   cuenta a `0`.
6. Solo si los pasos 4 y 5 salieron bien, repetir el apply: `changed=0` y el
   mismo ID de tarea.
7. Registrar la evidencia en `docs/DEPLOYMENT_STATUS.md`, con el
   `Version.Index` y las dos Configs tras el apply repetido.

#### Rollback del log de acceso

Igual que «Rollback de la ruta», con el spec del paso 1 como destino.
`docker service rollback edge_traefik` devuelve el log de acceso a stdout y
el fichero deja de crecer; CrowdSec no ve líneas nuevas, sin error ni efecto
en SSH. Los directorios, el fichero y el temporizador pueden quedarse: el
siguiente apply los reutiliza. Después del paso 6, la vuelta atrás es un PR
revisado y otro apply de `edge`.

## Rotación del token ACME

Docker recomienda versionar nombres para rotar secrets. El procedimiento es:

1. Crear en Cloudflare un token nuevo con el mismo scope mínimo y verificarlo.
2. Ensayar DNS-01 con staging y storage staging independiente.
3. Crear con `scripts/install-cloudflare-secret.sh` un secret nuevo, por ejemplo
   `cloudflare_dns_api_token_v2`; el helper volverá a comprobar lectura, creación
   y borrado DNS.
4. Cambiar en Git `edge_traefik_cloudflare_secret_name` al nombre nuevo.
5. Validar y desplegar; comprobar task healthy y referencias del servicio.
6. Verificar una operación ACME controlada sin forzar emisiones innecesarias.
7. Revocar el token anterior en Cloudflare.
8. Solo después de confirmar que ningún servicio referencia el secret anterior,
   eliminarlo de Swarm.
9. Actualizar el registro operativo sin guardar el valor.

Si el despliegue falla, el token anterior no se revoca: se vuelve al Config y
secret anteriores, se verifica servicio y se investiga. Docker impide eliminar
un secret usado por un servicio, pero esa protección no sustituye la inspección
previa:
[ejemplo oficial de rotación](https://docs.docker.com/engine/swarm/secrets/#example-rotate-a-secret).

La misma separación se conserva al rotar Terraform DNS y las dos credenciales
R2. Rotar una no debe exigir distribuirla a consumidores de otra.

## Docker Configs inmutables

Ansible calcula SHA-256 de los YAML renderizados y crea nombres:

```text
edge-traefik-static-<16-hex>
edge-traefik-dynamic-<16-hex>
```

Cada objeto lleva labels `com.apptolast.managed-by=ansible`,
`com.apptolast.stack=edge`, `com.apptolast.kind=static|dynamic` y el SHA-256
completo. El contenido y las labels se verifican antes del deploy. El stack se
aplica con `prune: true` y después se exige exactamente el servicio
`edge_traefik`; esto retira servicios huérfanos del stack, no Docker Configs
históricos.

Este comportamiento evita mutaciones invisibles y mantiene material de
rollback. También genera objetos huérfanos con el tiempo; no se borran como
parte automática de un deploy.

### Garbage collection seguro

Una versión solo es candidata si:

- tiene las labels de gestión esperadas;
- su nombre y hash son coherentes;
- ningún servicio de ningún stack referencia su ID;
- no es la versión activa;
- no es la última versión conocida como buena;
- terminó la ventana de rollback y existe el commit que la reproduce;
- el edge actual lleva estable el periodo acordado.

Procedimiento:

1. Ejecutar
   [`scripts/gc-edge-configs.sh`](../scripts/gc-edge-configs.sh) sin argumentos.
   Este es el modo por defecto y solo presenta un dry-run.
2. El helper excluye los Configs referenciados por el spec actual o
   `PreviousSpec` de **todos** los servicios, no solo `edge_traefik`.
3. También conserva las dos generaciones más recientes de cada tipo
   `static|dynamic`. Esa regla evita limpieza agresiva, pero no demuestra por sí
   sola cuál fue la última pareja buena.
4. Revisar manualmente la lista con nombre, ID, fecha, hash y commit. Si la
   ventana acordada exige más de dos generaciones, no ejecutar `--apply` hasta
   que termine esa retención.
5. Solo con la lista y retención aprobadas, ejecutar el modo destructivo. El
   borrado es uno a uno; nunca usar globs, sustituciones no revisadas ni limpieza
   masiva.
6. Volver a inspeccionar servicio, réplicas y `/ping`.

Docker Configs son inmutables y no pueden eliminarse mientras un servicio los
usa:
[Docker Configs](https://docs.docker.com/engine/swarm/configs/). Si Docker
rechaza un borrado por referencia, no se fuerza ni se elimina el servicio para
facilitar el GC.

## Backup y recuperación del edge

Respaldar por separado:

- `/srv/dockerswarm/traefik/acme.json`, preservando owner/mode;
- `/var/lib/docker/swarm` mediante el procedimiento coherente de Docker;
- commit, templates y hashes de Configs desplegados;
- inventario de secret/config IDs, nunca el contenido recuperado desde un task;
- snapshot DNS y del firewall.

Los backups se cifran antes de salir del VPS, se almacenan fuera del VPS y se
restauran periódicamente en un entorno aislado. `acme.json` contiene material de
cuenta y claves privadas; no se imprime, adjunta a incidencias ni sube a Git.

La recuperación no crea un token Cloudflare válido: la custodia externa debe
permitir emitir un token nuevo si el anterior se revoca. La recuperación de
Swarm sigue el
[procedimiento oficial de Docker](https://docs.docker.com/engine/swarm/admin_guide/#recover-from-disaster).

## Bloqueos actuales que requieren evidencia externa

- contenido/validez/scope del token ACME no observable y rotación pendiente;
- secret `cloudflare_dns_api_token_v1` existente pero todavía sin consumidor;
- staging ACME no demostrado;
- estado, permisos y backup de `acme.json` no verificados;
- registro DNS aplicado y delegación autoritativa no verificados;
- stack, red, puertos y logs de producción no observados desde este documento;
- destino offsite y custodio de claves aún no documentados;
- rutas y stacks de aplicaciones declarados, pero todavía no desplegados;
- ocho overlays de workload y la overlay de monitorización aún no creadas.

Ninguno de estos bloqueos se resuelve inventando un valor en Git.

## Referencias oficiales

- [Traefik 3.7: file provider](https://doc.traefik.io/traefik/v3.7/reference/install-configuration/providers/others/file/)
- [Traefik 3.7: provider Swarm y acceso al API](https://doc.traefik.io/traefik/v3.7/reference/install-configuration/providers/swarm/)
- [Traefik 3.7: ACME](https://doc.traefik.io/traefik/v3.7/reference/install-configuration/tls/certificate-resolvers/acme/)
- [lego: credenciales Cloudflare](https://go-acme.github.io/lego/dns/cloudflare/)
- [Cloudflare: crear API tokens](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/)
- [Docker secrets](https://docs.docker.com/engine/swarm/secrets/)
- [Docker Configs](https://docs.docker.com/engine/swarm/configs/)
- [Let's Encrypt staging](https://letsencrypt.org/docs/staging-environment/)
- [Let's Encrypt rate limits](https://letsencrypt.org/docs/rate-limits/)
