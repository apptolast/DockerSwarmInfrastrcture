# Changelog

Los cambios relevantes de la plataforma se documentan aquí. El formato sigue
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y las versiones
siguen [Semantic Versioning](https://semver.org/lang/es/).

## [Unreleased]

### Added

- [`docs/DEPLOYMENT_STATUS.md`](docs/DEPLOYMENT_STATUS.md) recoge el estado real
  del host tras el primer despliegue productivo de este árbol: lo aplicado y
  verificado, los servicios que aún no convergen con su diagnóstico, el bloqueo
  circular que impide redesplegar `workloads` y las compuertas externas que
  siguen cerradas.
- Roots Terraform aislados para bootstrap R2, diez A de Cloudflare y perímetro
  Netcup, con imports/adopción, identidades de backend, locking proof,
  migración y tests offline.
- Wrappers Terraform ligados a commit/state/backend, planes firmados,
  snapshots cifrados y evidencia de locking.
- Bootstrap fresco de Ubuntu 26.04 para `admin`, claves SSH externas, política
  staged/final y rollback automático de acceso.
- Gestión reproducible de paquetes, repositorios, CrowdSec Hub, Fail2ban,
  PSAD, Chrony, rsyslog, UFW, SSH y firewall Docker.
- Mutex host-global compartido por bootstrap y todos los playbooks Ansible,
  markers fail-closed, recuperación con evidencia y supervisor de procesos.
- Catálogo cerrado de servicios y denylist, stacks de workloads, restore
  markers, secrets por identidad, preflight de imágenes y capacidad.
- Kropia, Minecraft Stats, Minecraft, n8n, OpenClaw limpio, Passbolt, webs de
  Alberto/Pablo y Shlink fijados por digest.
- Traefik file-provider sin socket Docker, rutas explícitas y redes aisladas
  por workload.
- Stack interno de Prometheus, Alertmanager, Loki, Grafana, Alloy, exporters,
  blackbox y acceso mediante túneles.
- Backup restic de aplicaciones/ACME/observabilidad/Minecraft, copia fría de
  Raft, retención, métricas y ensayos de restore.
- CI, validadores reales de OpenSSH/Traefik, tests adversariales, lint,
  escaneo de secretos y documentación de reconstrucción/cutover.
- Cuatro subagentes de revisión en `.claude/agents/`, ninguno con `Write`
  ni `Edit`: `judge` (veredicto contra `CLAUDE.md` y la plantilla de PR),
  `security-reviewer` (frontera de confianza: firewall, grupo `docker`,
  política SSH, secretos, rotación, atestación), `guardrail-adversary`
  (¿existe test negativo que demuestre que cada gate rechaza?) y
  `mentor` (explica el porqué de los invariantes no obvios). Hasta ahora
  había tres operadores y ningún revisor.
- [`.github/workflows/guard-sensitive-paths.yml`](.github/workflows/guard-sensitive-paths.yml)
  etiqueta las PR que tocan rutas sensibles. Usa `pull_request_target`
  sin checkout y sin ejecutar código de la PR: solo lista nombres por
  API. Incluye `previous_filename`, para que un rename no lo esquive, y
  cubre además los ficheros que deciden **qué** verifica la CI
  (`scripts/bootstrap-tooling.sh`, `scripts/lint.sh`,
  `scripts/validate-iac.sh`, `ansible/requirements-dev.*`,
  `.ansible-lint`), no solo los que la CI verifica.
- [`.gitattributes`](.gitattributes) fija LF en el índice para todo el árbol.
  Los seis entrypoints de `stacks/workloads/config/` son el PID 1 de sus
  servicios y se entregan al contenedor como Docker Config byte a byte: un
  CRLF en la línea del shebang haría que el kernel buscase un intérprete
  `/bin/sh\r` inexistente y el servicio no arrancaría. El índice ya estaba
  limpio (`git ls-files --eol` devuelve `i/lf` en los 336 ficheros); el
  fichero garantiza que siga estándolo desde cualquier checkout.
- Adopción fiel de `TemplateSSDUncleBob` (arnés SDD estilo Uncle Bob):
  [`AGENTS.md`](AGENTS.md) (mapa de navegación) y
  [`CHECKPOINTS.md`](CHECKPOINTS.md) (checklist de estado final, con las
  C6/C7 de la plantilla marcadas explícitamente como sin equivalente
  honesto en este dominio en vez de forzarlas). `harness.config.json`
  declara los comandos reales de este stack sin enganchar `bin/harness`:
  la cadena real sigue siendo `scripts/validate-iac.sh` y
  `scripts/lint.sh`. `scripts/sync-memoria.sh`/`.ps1`, copiados de la
  plantilla, con su paso 2bis integrado en el protocolo de arranque de
  `CLAUDE.md`.
  [`docs/adopcion-templatessd.md`](docs/adopcion-templatessd.md)
  documenta la correspondencia exacta entre las nueve puertas STOP y los
  siete subagentes ya existentes y los roles de la plantilla, incluidas
  las partes sin equivalente limpio. No introduce ningún workflow de
  autonomía: `apply`/`ansible-playbook` contra el host real siguen siendo
  100% manuales.

### Changed

- `personal-website-alberto` queda fijado al artefacto inmutable
  `sha256:34c6854a3d7ff179e8fee8207696b1940747e84e9782d2133417f17b60602f8d`
  publicado desde el commit `6878ff6` de `PersonalWebsite`, sin depender de
  la etiqueta mutable `latest` en producción.
- Este repositorio pasa a ser el único propietario de toda la IaC, incluidos
  stacks, rutas, migración, observabilidad y backup.
- El daemon Docker declara `no-new-privileges`, `userland-proxy: false` y
  logging `local` acotado.
- La exposición coherente incluye `80/443` y `25565/TCP`. El gate de Minecraft
  se abre únicamente porque el propietario registró su aceptación explícita en
  `platform_minecraft_offline_public_accepted`; sin esa bandera el puerto
  sigue cerrado con `online-mode=false`.
- `platform_dns_cutover` declara el corte que ya había ocurrido en las diez
  etiquetas. Todas resolvían ya a `platform_public_ipv4` y el servidor legado
  `138.199.157.58` fue eliminado, pero nueve banderas seguían en `false` y
  `dns.tf` calcula `cutover ? public : legacy`: un `terraform apply` sobre la
  raíz habría reapuntado nueve registros vivos a una máquina inexistente.
  Declarar el estado real convierte ese plan en un no-op. La rama
  `adoption_only` se conserva solo como prueba unitaria y queda marcada como
  inaplicable.
- Las herramientas de seguridad heredadas se preservan e inventarían; no se
  purgan ni se reescribe PAM de forma destructiva.
- Backup queda codificado pero fail-closed hasta R2, restic, autolock y escrow
  externo probados.
- El catálogo de servicios excluye explícitamente `uptime-kuma` (alias
  `kuma`, hostname legacy `kuma.apptolast.com`), confirmado por el
  propietario el 2026-07-27.
- `scripts/lint.sh` y `scripts/validate-iac.sh` extienden `bash -n` y
  ShellCheck a `stacks/workloads/config/*.sh`. Los seis entrypoints de
  producción quedaban fuera del gate —lo único que recibían era el SHA-256
  con el que se nombra su Docker Config—, así que un error de sintaxis en
  ellos atravesaba la CI entera y solo se manifestaba al no arrancar el
  contenedor en el host. El barrido pasa de 41 a 47 scripts y los seis
  entran sin ningún hallazgo con la configuración vigente.
- `yamllint` se invoca con `--strict` en `scripts/validate-iac.sh`, de modo
  que un hallazgo de nivel `warning` deja de pasar en silencio y devuelve
  código distinto de cero. Medido antes de fijarlo: los 86 ficheros YAML
  del repositorio —que son todos los que la lista de rutas ya cubría— dan
  cero hallazgos incluso contando los de nivel `warning`, así que el cambio
  no exige corregir nada y solo impide una regresión futura.
- [`.ansible-lint`](.ansible-lint) declara `profile: production` con
  `strict: true` y `warn_list: []`. Hasta ahora no existía fichero de
  configuración, así que se aplicaba el perfil por defecto y nada dejaba
  constancia de qué escalón se cumplía. Medido antes de declararlo, en el
  runner de CI: los seis perfiles pasan, y la combinación más estricta da
  «0 failure(s), 0 warning(s) in 110 files processed of 121 encountered.
  Profile 'production' was required, and it passed». El `warn_list` vacío
  convierte `experimental`, `jinja[spacing]` y `fqcn[deep]` en errores
  duros. `skip_list` queda vacío porque la documentación oficial lo
  desaconseja: oculta las violaciones en vez de mostrarlas.

### Security

- Se retira formalmente el MFA de SSH por `pam_google_authenticator`
  (`host_security_ssh_mfa_policy: retired`). Estaba instalado desde el
  2026-07-21 pero era inerte: ningún usuario tenía `~/.google_authenticator`
  y el modificador `nullok` da por buena la autenticación cuando no hay
  secreto, así que el acceso ya era sólo por clave pública. Retirarlo no
  cambia cómo se entra al host; deja de aparentar un segundo factor que nunca
  existió y permite que el endurecimiento SSH deje de tratarlo como estado a
  preservar. Decisión explícita del propietario, registrada en el contrato.
- Queda documentado que `PasswordAuthentication no` no bastaba para cerrar la
  autenticación por contraseña. Con `UsePAM yes` y
  `KbdInteractiveAuthentication yes`, el método `keyboard-interactive` recorre
  la pila PAM de `/etc/pam.d/sshd`, que incluye `common-auth` y por tanto
  `pam_unix.so`; `admin` y `root` tienen hash de contraseña real, de modo que
  el servidor anunciaba `publickey,keyboard-interactive` y una contraseña
  válida bastaba para entrar. La plantilla de endurecimiento de
  `host_baseline` ya fija `KbdInteractiveAuthentication no` y
  `AuthenticationMethods publickey`, que es exactamente lo que cierra esa vía;
  hasta ahora no llegaba a aplicarse porque el playbook abortaba antes.
- El endurecimiento de `/proc` pasa a estar codificado en el repositorio
  (`host_security_proc_mount_options`). `hidepid=2`, que el núcleo representa
  como `invisible`, estaba aplicado a mano en `/etc/fstab` desde el
  2026-07-21 y no lo recogía ningún commit: una reconstrucción desde un commit
  revisado habría devuelto el host con los `/proc/<pid>` ajenos visibles. La
  línea manual usaba además `defaults`, que implica `suid,dev,exec` y por
  tanto retiraba `nosuid,nodev,noexec`, las opciones con las que systemd monta
  `/proc`; se restituyen. `scripts/validate-host-security.py` rechaza el
  contrato si alguna de ellas falta o si `hidepid` queda permisivo.
- `platform_minecraft_offline_public_accepted` codifica de forma auditable la
  aceptación explícita del propietario para publicar Minecraft con
  `online_mode: false`, tras habérsele expuesto la consecuencia exacta:
  cualquiera que alcance el 25565 puede conectarse con cualquier nombre de
  usuario, incluido el de un operador. La compuerta no se elimina ni se
  relaja; sigue fallando en cerrado por defecto y ahora se comprueba en el rol
  `platform`, en `minecraft_preflight`, en `scripts/validate-contract.py` y en
  el bloque `check` de la raíz Terraform de DNS.
- `ansible/playbooks/platform.yml` carga `config/minecraft.yml` porque el rol
  `platform` es quien abre el 25565 en UFW y en la cadena de Docker: sin el
  contrato no podía comprobar `online_mode` y un `--playbook platform` aislado
  habría publicado el puerto sin ver la compuerta de aceptación.
- Los usuarios humanos dejan de pertenecer al grupo root-equivalent `docker`.
- Los despliegues rechazan worktrees sucios, holders perdidos, descendientes
  huérfanos, markers alterados y estado externo no probado.
- La configuración SSH se valida con el parser real y solo deshabilita root
  tras reconexión independiente y rollback armado.
- Tokens, claves, passwords, states y backups permanecen fuera de Git.

### Fixed

- `backup/backupctl.py` deja de tragarse en silencio el fallo de la única
  escritura del estado y de las métricas del backup. `record_failure()`
  envolvía esa escritura en `except Exception: pass`, de modo que un
  `OSError` por disco lleno o permisos, o un fichero de estado corrupto,
  la hacían fracasar sin dejar rastro. El efecto no era cosmético: el
  gauge `dockerswarm_backup_last_run_success` conservaba el `1` del run
  anterior, así que la alerta `BackupLastRunFailed`
  (`severity: critical`) nunca disparaba. Y el único respaldo de
  frescura, `ApplicationBackupStale`, filtra `kind="application"`,
  mientras que se instalan cuatro tipos —`application`, `swarm-state`,
  `verify` y `rehearsal`—, con lo que tres quedaban sin ninguna alerta.
  La captura sigue siendo ancha a propósito para no enmascarar el error
  primario, pero ahora avisa por `stderr`.
- `dockerswarm-docker-firewall.service` fallaba al final de `host-baseline`
  con `iptables: Can't open socket to ipset`. systemd da por arrancado el
  bouncer de CrowdSec antes de que éste haya creado sus ipsets, y el handler
  que reordena `DOCKER-USER` se ejecutaba acto seguido insertando reglas que
  referencian `CROWDSEC_CHAIN`, que a su vez usa `-m set --match-set`. Se
  intercala una espera por los ipsets entre ambos handlers.
- Un simple ensayo `--playbook host-baseline --check` dejaba el host sin
  filtrado de CrowdSec. Las dos tareas que validan la configuración del
  bouncer llevaban `check_mode: false`, de modo que se ejecutaban también en
  modo comprobación, y su `-t` retira `CROWDSEC_CHAIN` de ambas familias al
  salir: se observó cómo las referencias pasaban de 3 a 0 durante un ensayo
  que por definición no debe cambiar nada. Ahora se omiten en modo
  comprobación, y tras las pruebas el rol reconcilia el estado real y espera a
  que la cadena vuelva antes de comprobar el orden de `DOCKER-USER`, porque
  los handlers sólo se disparaban cuando el fichero cambiaba y en un host ya
  convergido nadie reponía las cadenas.
- La comprobación de configuración del bouncer de CrowdSec dejaba el host sin
  filtrado. `crowdsec-firewall-bouncer -t` está documentado como «test config
  and exit», pero inicializa el backend real y al salir retira las cadenas;
  como las cadenas de iptables son globales y no por proceso, la instancia de
  prueba desmontaba las de la instancia viva y nadie las reponía. El servicio
  seguía `active` mientras `CROWDSEC_CHAIN` había desaparecido de ambas
  familias, de modo que el host estuvo unas 20 horas sin bloquear nada y sin
  que ningún control lo señalase. El rol `host_security` reconcilia ahora ese
  estado justo después de la prueba y falla en cerrado si la cadena no acaba
  presente en IPv4 e IPv6.
- `host_baseline` abortaba siempre en `crowdsec-docker.yml`, su última tarea,
  porque la comprobación de presencia del gancho `DOCKER-USER` era por
  subcadena mientras la aserción posterior exigía una línea exacta. El fichero
  del bouncer trae `#  - DOCKER-USER` comentado, que contiene la subcadena
  pero no es la línea, así que se tomaba la rama «ya está puesto», el
  candidato salía sin el gancho y la aserción fallaba. Como `any_errors_fatal`
  corta el play, el host quedaba a medio converger —con `sshd_config`, APT,
  journald y sysctl ya reescritos— y sin metadatos de despliegue. La
  comprobación pasa a ser por línea exacta, igual que la aserción.
- Los tres despliegues de pila (`edge`, `workloads` y `observability`) pedían
  a `community.docker.docker_stack` que esperase con `detach: false`, lo que
  añade `--detach=false`. Eso no se colgaba: el cliente de Docker recorre los
  servicios de uno en uno (defecto reconocido, docker/cli #4907) y por cada
  uno sólo sale cuando `converged && time.Since(convergedAt) >= monitor`, con
  `monitor` leído de `Spec.UpdateConfig.Monitor`. Esta pila declara
  `monitor: 120s`, así que quince servicios suponen unos treinta minutos de
  sondeo en vacío incluso cuando todo está ya en `1/1`. Se pasa a
  `detach: true`, que es el valor por defecto del módulo, y la convergencia
  queda en manos de la comprobación propia de cada rol, acotada en el tiempo.
  A cambio, el cliente ya no observa la ventana `monitor`, de modo que una
  actualización revertida por swarmkit podría verse como `1/1` con el spec
  anterior; para no perder esa señal, cada rol rechaza ahora explícitamente
  los estados `rollback_started`, `rollback_paused`, `rollback_completed` y
  `paused`. La espera ilimitada que sí existió fue la de `selenium` con la
  interfaz vxlan huérfana, que corresponde al fallo abierto docker/cli #5299.
- `inspect_process_references` abortaba con `PromotionError` en cuanto
  encontraba un proceso visible pero ilegible. En este host no se notaba
  porque `/proc` va montado con `hidepid=invisible` y los procesos ajenos ni
  siquiera aparecen, de modo que el error se resolvía como
  `FileNotFoundError`; en el runner de CI, con un `/proc` normal, PID 1 es
  visible e ilegible y el recorrido entero estallaba. Ahora se registra como
  referencia bloqueante, con lo que la promoción sigue fallando en cerrado
  —`promote_runtime_generation.py` ya rechaza cualquier referencia— pero
  enumerando el motivo en vez de morir con un error opaco.
- La verificación de identidad de imagen comparaba la referencia declarada
  con la que Docker guarda en el spec, y Docker normaliza el registro por
  defecto: `docker.io/library/postgres@sha256:X` se almacena como
  `postgres@sha256:X`. La comprobación no podía pasar para ninguna imagen de
  Docker Hub —sólo la superaban `n8n-runners`, declarada sin prefijo, y
  `openclaw`, alojada en `ghcr.io`—, y quedaba enmascarada porque la tarea
  anterior fallaba antes. Se normaliza el lado esperado, sin relajar el
  contrato: el digest sigue comparándose exacto.
- La sonda de salud de Traefik hacia `minecraft-stats` reutilizaba el host de
  la URL del servidor como cabecera `Host`, es decir
  `workloads_minecraft-stats`. El Tomcat embebido de Spring Boot aplica
  RFC 1123 y rechaza con 400 cualquier `Host` con guion bajo, así que el único
  backend quedaba marcado como caído y `minecraft-stats.apptolast.com`
  respondía 503 `no available server` pese a que el contenedor estaba sano y
  su propia sonda, que usa `127.0.0.1`, pasaba. Se fija `hostname` en el
  `healthCheck`. Es el único backend afectado: el resto responde 2xx/3xx con
  el `Host` con guion bajo.
- `.gitignore` excluía
  `ansible/roles/edge/files/letsencrypt-staging-roots.pem` mediante el patrón
  `*.pem`, de modo que el bundle anclado nunca viajó en ningún commit:
  `tests/test_edge_state_safety.py` fija su sha256, CI fallaba con
  `FileNotFoundError` y una reconstrucción desde commit habría perdido el
  ancla de confianza. Son cuatro raíces públicas autofirmadas de Let's Encrypt
  staging, sin material privado, así que se versionan con una negación
  explícita y acotada a ese fichero.
- `scripts/provision-observability-db-users.py` ejecutaba `psql` sin
  `--username`, por lo que tomaba el nombre de la cuenta del sistema y trataba
  de autenticarse con el rol `postgres`, inexistente en las tres instancias:
  sus superusuarios son `n8n`, `passbolt` y `shlink`. Se nombra explícitamente
  el superusuario real de cada base.
- Traefik declaraba `ping.entryPoint` sin `manualRouting`, de modo que el
  router `edge-health` no podía usar `ping@internal`; se activa
  `manualRouting` y se añade el router interno `edge-ping-internal`, sin el
  cual `traefik healthcheck` recibía 404 y Swarm habría matado el edge.
- Passbolt comprobaba su salud buscando la palabra `database` en
  `/healthcheck/status.json`, que responde `{"header":{...},"body":"OK"}`; el
  predicado fallaba siempre y Swarm terminaba la tarea tras los reintentos.
- Shlink heredaba `num_workers: 0` de RoadRunner, es decir un worker por
  CPU: 16 procesos PHP en un cgroup de 256 MiB que el kernel mataba. Se
  fijan `WEB_WORKER_NUM` y `TASK_WORKER_NUM`.
- Minecraft Stats dimensionaba el heap al 25 % del cgroup y dejaba el
  metaspace sin tope, superando su límite de memoria; se acotan con
  `JAVA_TOOL_OPTIONS` y se amplía su reserva revisada.
- El gateway de OpenClaw abortaba con «Missing config» (exit 78); su
  entrypoint declara ahora `gateway.mode=local` de forma idempotente en
  lugar de recurrir al bypass `--allow-unconfigured`.
- Reajustados los límites de memoria de `workloads` contra el consumo
  medido, manteniendo intacto el total revisado en `config/capacity.yml`.
- Eliminadas carreras entre bootstrap y despliegues Ansible mediante un único
  inode de flock para ambos scopes.
- El modo Ansible local eleva supervisor y comando juntos, permitiendo terminar
  también descendientes root.
- Los checks post-state incompatibles con `--check` quedan correctamente
  condicionados.
- El despliegue edge usa `prune: true` y verifica un inventario exacto sin
  borrar Configs históricos referenciados.
- El auto-desbloqueo del Swarm comparaba contra un estado `docker info`
  imposible (`locked true`) que nunca ocurre en la realidad
  (`ControlAvailable` es `false` mientras el manager está bloqueado); el
  desbloqueo automático era inalcanzable.
- El firewall Docker degrada de nuevo a solo IPv4 si falta la cadena
  `DOCKER-USER` de `ip6tables`, en vez de abortar `ExecStartPost` y tumbar
  `docker.service`.
- `terraform-safety.py` abre los ficheros de firma/lock con `O_NOFOLLOW` y
  verifica el descriptor ya abierto, cerrando una ventana TOCTOU de symlink
  entre la comprobación y la lectura.
- `site.yml` ejecuta `capacity_preflight` antes de `host_security`, igual que
  el resto de playbooks, para que el gate de capacidad corra antes de
  cualquier mutación real del host.
- El helper de lock host-global clasifica el estado del Swarm con
  `/usr/bin/docker` explícito en vez de resolver `docker` por `PATH`.
- El timer de rollback SSH puede limpiar su marker de confirmación bajo
  `ProtectSystem=strict` y detiene (no solo deshabilita) el timer/path ya
  armados.
- `restore_databases.sh` ya no adopta una base de datos no vacía sin marker
  propio solo porque exista el marker de fase; exige evidencia verificada.
- `terraform-safety.py` reconoce ahora, de forma estrecha y verificada
  contra un `terraform plan -json` real, el warning permanente e
  inevitable `Resource Destruction Considerations` de
  `cloudflare_r2_managed_domain` — solo para `plan` en `cloudflare/state-bootstrap`,
  nunca para `apply` ni para ningún otro root o diagnóstico.
- El `become` de Ansible fallaba con `Timeout waiting for privilege escalation
  prompt` en Ubuntu 26.04: `sudo-rs` reescribe el indicador de `-p` como
  `[sudo: <prompt>] Password:` y Ansible solo reconoce una línea que empiece
  por su indicador exacto. `ansible/ansible.cfg` fija ahora
  `become_exe = /usr/bin/sudo.ws` (sudo clásico setuid, alternativa de
  prioridad 40) y `config/host-security.yml` bloquea `sudo=1.9.17p2-1ubuntu3`
  para que la reconstrucción disponga del binario. No se altera la alternativa
  del sistema, no se concede `NOPASSWD` y no se almacena ninguna contraseña.
- El assert del conjunto exacto de acquisitions de CrowdSec no podía pasar
  bajo `--check`: `ansible.builtin.find` sí se ejecuta en check mode y lee el
  estado vivo, mientras la plantilla y los borrados solo se simulan, de modo
  que comparaba el estado real contra el deseado. Queda condicionado con
  `when: not ansible_check_mode`, igual que el par equivalente de grupos del
  administrador. El apply sigue enforzándolo sin cambios.
- El apply de `host_security` fallaba de forma reproducible en `Enforce
  versioned-only CrowdSec Hub promotion`: al enmascarar
  `crowdsec-hubupdate.service` desaparece la unidad que dispara su timer y
  systemd deja `crowdsec-hubupdate.timer` en `ActiveState=failed`
  (`Result=resources`) aunque ya esté enmascarado. La propia tarea de
  enmascarado creaba el estado que el gate siguiente rechazaba. Ahora se
  limpia ese residuo con `systemctl reset-failed` antes de leer el estado;
  el assert de promoción no se relaja.
- `docs/KNOWN_ISSUES.md` describía el prefijo de `sudo-rs` con un espacio
  final dentro de un code span, que markdownlint MD038 rechaza y hacía fallar
  `scripts/lint.sh`. El texto se reformula sin alterar el hecho descrito.
- Las cinco sustituciones `regex_replace` que construyen argumentos dentro de
  escalares YAML plegados usaban `\\1`, que llega literal a `re.sub` y no se
  expande: `validate-ufw-contract.py` recibía `--public-port=\1`, y el
  candidato del bouncer CrowdSec habría reemplazado su línea `- INPUT` por
  `\1`. Ahora usan `\g<1>`, que sí expande en ese contexto.
- El contrato UFW de egress solo añadía sus reglas cualificadas por protocolo,
  dejando vivas las reglas legacy sin protocolo del host previo (25, 53, 123,
  443 y 587). `validate-ufw-contract.py` las contaba como drift y abortaba
  `platform`. Ahora se borran antes de aplicar el contrato, derivando la lista
  de los propios puertos revisados.
- Los stacks de workloads y observabilidad arrastraban el mismo
  `security_opt: no-new-privileges:true` que Swarm ignora y por el que avisa en
  cada `docker stack deploy`. Se retira de ambos por la razón ya aplicada al
  edge: la garantía la impone `config/daemon.json` para todo el daemon.
- `workloads_db_mount_contract` declaraba sus rutas como claves de un mapping
  con sintaxis Jinja, pero Ansible plantilla los valores y nunca las claves: el
  gate de contenedores recibía la cadena literal `{{ workloads_paths... }}` como
  ruta de montaje y abortaba el despliegue. El mapping se construye ahora en una
  única expresión. `--check` no lo detectaba porque el assert solo contaba tres
  claves, y literales eran tres igualmente.
- `finalize_restore.py` exigía el directorio `binaryData` de n8n, que es el
  layout anterior: n8n resuelve el almacén de binarios a `~/.n8n/storage` salvo
  que se fije `N8N_BINARY_DATA_STORAGE_PATH` o `N8N_STORAGE_PATH`, y el stack
  activa `N8N_MIGRATE_FS_STORAGE_PATH`. `prepare_runtime.py` ya materializaba
  `storage`, de modo que las dos mitades del utillaje se contradecían y el gate
  no podía emitirse. Ningún dato estaba ausente: los 258 ficheros conservan sus
  rutas relativas.
- El restore vectorial exigía al menos una tabla en las dos bases con pgvector,
  pero `vectors.dump` contiene únicamente la extensión y su comentario: el
  almacén vectorial de n8n nunca llegó a usarse, de modo que la fase no podía
  completarse contra el backup real. `vectors` admite ahora cero tablas y `rag`
  conserva el mínimo; el contenido sigue ligado por `dumpSha256`,
  `schemaSha256` y `vectorExtensionVersion`.
- La redirección HTTP→HTTPS apuntaba al entrypoint `websecure`, cuya dirección
  interna es `:8443`, de modo que Traefik redirigía a un puerto que no está
  publicado: `http://<host>/` acababa en `https://<host>:8443/`. Se redirige al
  puerto público `:443`, que es la otra forma admitida por `to`.
- Declarar dos resolvers para DNS-01 exige que ambos vean el TXT, y Quad9
  cachea el NXDOMAIN más tiempo que Cloudflare, así que la comprobación
  expiraba en cinco de los nueve dominios. Se deja solo `1.1.1.1`, el resolver
  del proveedor que hospeda la zona.
- El reto DNS-01 no declaraba `resolvers`, así que la comprobación de
  propagación usaba el resolver embebido de Docker (`127.0.0.11`), que devuelve
  NXDOMAIN para `_acme-challenge.<host>` y agotaba el plazo pese a haberse
  creado el TXT. Se fijan resolvers públicos, como indica la documentación de
  Traefik para ese caso.
- El assert de identidad del servicio Traefik leía
  `ContainerSpec.ReadonlyRootfs`, clave que la API de Docker no expone para
  servicios Swarm; la real es `ContainerSpec.ReadOnly`. La condición abortaba
  con `object of type 'dict' has no attribute`.
- El mismo assert comprobaba `EndpointSpec.Ports` en la raíz del documento de
  `docker service inspect`, donde solo existe `Endpoint`; la especificación
  declarada vive en `Spec.EndpointSpec`.
- El stack declaraba `security_opt: no-new-privileges:true`, que
  `docker stack deploy` ignora en Swarm y por el que emite un warning en cada
  despliegue; el assert por servicio exigía después ese valor y nunca podía
  cumplirse. Se retiran ambos: la garantía sigue vigente porque
  `config/daemon.json` fija `no-new-privileges` para todo el daemon y tanto
  `validate-iac.sh` como el rol `platform` lo verifican de forma exacta.
- `ping.manualRouting: true` desactiva el router interno que sirve `/ping`,
  pero el CLI `traefik healthcheck` del healthcheck del contenedor consulta esa
  ruta con el path fijo `/ping` sobre el entrypoint `traefik`. Recibía 404, el
  contenedor se declaraba enfermo y Swarm lo reiniciaba cada 60 s
  indefinidamente. Se vuelve al enrutado interno por defecto; el entrypoint
  `traefik` no se publica, y el router `edge-health` sobre TLS se conserva.
- El Docker secret `cloudflare_dns_api_token_v2` no contenía un token válido:
  ACME fallaba con `failed to find zone apptolast.com: 403 9109 Invalid access
  token`, mientras el token de `/etc/dockerswarm/terraform` verificaba
  correctamente contra la API. Se instala su valor como
  `cloudflare_dns_api_token_v3` y el contrato apunta ahí; v1 y v2 se conservan
  para revocarlos por separado.
- El rol instala fail2ban con `banaction = ufw`, de modo que cada baneo añade
  una regla a la misma cadena que `validate-ufw-contract.py` exige exacta:
  cualquier baneo activo hacía fallar `platform` y `host-baseline`. En un host
  con SSH público eso convierte cada ejecución en una lotería. El validador
  ahora tolera denegaciones acotadas por origen (`REJECT`/`DROP`), que solo
  pueden estrechar el ingress; todo `ACCEPT` se sigue verificando igual.
- `docker node update` no acepta el alias `self`, que sí resuelve
  `docker node inspect`: el daemon responde `node self not found`. Las
  etiquetas de colocación de `platform` y `observability` nunca llegaban a
  aplicarse. Ahora ambas usan el ID real del nodo, ya disponible en el
  `inspect` previo.

Nada de esta sección afirma que los stacks, DNS, Terraform remoto o backup ya
estén aplicados en producción.

## [0.1.0] - Pendiente de despliegue

- Primera versión declarativa de la plataforma; no se etiqueta ni se considera
  desplegada hasta completar las compuertas externas y verificar producción.

[Unreleased]: https://github.com/apptolast/DockerSwarmInfrastrcture/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/apptolast/DockerSwarmInfrastrcture/releases/tag/v0.1.0
