# Observabilidad interna

## Alcance

Esta capa es plataforma nueva. No restaura ni reutiliza
`monitoring-dozzle`, Kubernetes, `kube-system`, Longhorn ni ningún servicio de
la denylist. El contrato canónico está en `config/services.yml` y fija diez
componentes por digest:

La capa está declarada y validada, pero todavía no existe ningún servicio de
observabilidad desplegado en el Swarm.

| Componente | Función |
| --- | --- |
| Prometheus | Métricas, reglas y estado de alertas |
| Alertmanager | Agrupación y silencios internos |
| Blackbox Exporter | Pruebas HTTPS externas y TCP de Minecraft |
| Loki | Almacenamiento local de logs con retención de 14 días |
| Alloy | Descubrimiento y envío de logs de servicios Swarm gestionados |
| Grafana | Dashboards y exploración de métricas/logs |
| Node Exporter | Host y métricas textfile de backup |
| cAdvisor | Recursos de contenedores |
| PostgreSQL Exporter | Métricas de las tres instancias PostgreSQL |
| Redis Exporter | Métricas del coordinador Redis de n8n |

El stack contiene doce servicios porque PostgreSQL Exporter se instancia una
vez por base de datos. Así cada proceso recibe únicamente la red y el secreto
de su base. Las tres instancias usan la misma imagen inmutable del catálogo.

No se migra estado de observabilidad antiguo. La primera instalación crea
rutas vacías bajo `/srv/dockerswarm/observability`.

## Red y exposición

Ningún servicio declara `ports:` y no existe hostname de observabilidad. La
red `apptolast-observability` es un overlay cifrado, interno y no attachable.
Solo estos enlaces adicionales están permitidos:

- Prometheus y Blackbox Exporter acceden a
  `apptolast-edge-monitoring`;
- Blackbox Exporter y Minecraft comparten únicamente la overlay cifrada,
  interna y dedicada `apptolast-minecraft-monitoring`;
- cada PostgreSQL Exporter accede a un único backend de workloads;
- Redis Exporter accede solo a `workloads_n8n-coordination`.

`workloads` es el único owner de `apptolast-minecraft-monitoring` y conecta
solo `workloads_minecraft`. Observabilidad la consume como red externa y
conecta solo Blackbox Exporter. Ningún tercer consumidor está admitido por los
contratos negativos.

Un puerto de Swarm sin `host_ip` se publica en todas las interfaces. Por ello
el catálogo no representa como «loopback» algo que Swarm no puede garantizar.
Esta decisión sigue la semántica documentada por
[Docker para `ports`](https://docs.docker.com/reference/compose-file/services/#ports).

## Configuración y secretos

Las configuraciones se almacenan en Git y Ansible las convierte en Docker
Configs inmutables con el SHA-256 en el nombre y las labels. Docker Configs son
inmutables; la rotación mediante nombres nuevos es el flujo recomendado por
[Docker](https://docs.docker.com/engine/swarm/configs/).

Los valores secretos nunca se renderizan en el stack. El catálogo
`stacks/observability/secrets.yml` contiene solo identidades, consumidores,
versión y entropía requerida. La primera instalación ejecuta:

```bash
sudo -- ./scripts/install-observability-secrets.py
```

El instalador:

- crea cinco fuentes aleatorias con modo `0600` dentro de un directorio
  `0700`;
- crea Docker Secrets externos versionados;
- etiqueta cada secret con identidad, versión y fingerprint de su fuente;
- rechaza un secret existente si su fuente se ha perdido o difiere;
- nunca imprime valores.

Antes de provisionar PostgreSQL o desplegar, el rol instala el mismo
verificador y ejecuta `--verify-only`. Así compara el SHA-256 de cada fuente
privada actual con la label del Docker Secret; no basta con que la label tenga
forma de hash. Una fuente ausente, rotada o incoherente detiene el playbook.

Tres secretos pertenecen a roles PostgreSQL de monitorización. El playbook
crea o rota `apptolast_monitor`, le concede `pg_monitor` y `CONNECT`, y no
reutiliza las credenciales administrativas de las aplicaciones. El SQL viaja
por stdin a `psql`; el secreto no aparece en argumentos, entorno ni salida.

Para rotar, se incrementan `observability_secret_version` y los cinco
`external_name`, se ejecuta de nuevo el instalador y después el playbook. No se
sobrescribe un Docker Secret: su inmutabilidad forma parte del contrato.

## Render, validación e instalación

La validación no muta el Swarm:

```bash
./scripts/validate-observability.sh
```

Además del contrato Python y sus pruebas negativas, ejecuta las herramientas
de las imágenes exactas: `promtool`, `amtool`, el check de Blackbox Exporter,
la verificación de Loki y `alloy validate`. Un smoke sin puertos publicados
arranca contenedores efímeros fijados por digest y comprueba:

- carga y evaluación sana de las trece reglas de Prometheus;
- readiness HTTP real de Loki;
- las dos datasources y los dos dashboards provisionados en Grafana;
- discovery de Alloy contra el proxy Docker restringido real;
- limpieza de todos los contenedores y sockets de validación.

Orden de instalación:

1. desplegar edge y workloads, restaurar datos y verificar las cinco bases
   lógicas en tres instancias;
2. ejecutar el instalador de secretos;
3. aplicar `ansible/playbooks/observability.yml`;
4. esperar `1/1` y health sano para los doce servicios;
5. comprobar targets, reglas, logs, dashboards y una alerta controlada.

El rol:

- deriva imágenes y rutas exclusivamente del catálogo;
- valida las cinco redes externas antes de utilizarlas;
- crea rutas persistentes con los UID revisados;
- crea Docker Configs inmutables;
- reconcilia `platform.observability=true`;
- provisiona roles PostgreSQL de mínimo privilegio;
- aplica rollback automático ante una actualización fallida;
- verifica imagen, placement y ausencia de puertos en el estado desplegado.

## Acceso administrativo

Grafana y las UIs auxiliares no tienen listener en el servidor. El script abre
un listener temporal exclusivamente en el loopback del equipo administrador:

```bash
./scripts/observability-tunnel.sh admin@SERVIDOR grafana
```

Después se abre `http://127.0.0.1:3000`. También se admiten `prometheus`,
`alertmanager`, `loki` y `alloy`; el tercer argumento permite cambiar el
puerto local. Cada conexión local crea un comando `ssh -T` y transporta los
bytes por stdin/stdout. No usa `ssh -L`, por lo que es compatible con
`AllowTcpForwarding no` y no requiere debilitar el baseline OpenSSH.

El helper remoto:

- se instala por Ansible en
  `/usr/local/libexec/dockerswarm-observability-forward`;
- requiere `sudo` para entrar en el namespace de red de la tarea;
- solo acepta una allowlist fija de servicios/puertos;
- conecta al loopback dentro de ese namespace sin abrir ningún listener;
- vuelve a resolver la tarea para cada conexión;
- termina al cerrar el stdio de esa orden SSH.

Ansible instala un sudoers validado con `visudo` que concede al grupo `sudo`
únicamente las cinco órdenes exactas `--stdio`; no acepta argumentos
arbitrarios. No se crea listener remoto, red attachable, ruta Traefik ni regla
de firewall.

## Métricas, logs y alertas

Prometheus obtiene:

- métricas propias de los diez componentes;
- Traefik y n8n;
- host y contenedores;
- PostgreSQL y Redis;
- métricas textfile del sistema de backup;
- ocho probes HTTPS derivados de la allowlist y con paths de salud concretos;
- un probe TCP interno de Minecraft.

Los paths son `/health`, `/actuator/health`, `/healthz`,
`/healthcheck/status.json`, `/robots.txt`, `/placeholder-logo.svg` y
`/rest/health`, según cada imagen fijada. Blackbox exige exactamente HTTP 200,
sin aceptar redirects como sustituto de salud.

Las reglas cubren targets, endpoints, capacidad de host, memoria de
contenedores, backups obsoletos/fallidos y fallos internos de Prometheus/Loki.
Prometheus envía alertas a Alertmanager según la
[configuración oficial](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/).

Alertmanager enruta a `notifications-disabled`, un receptor deliberadamente
sin integración externa. La regla siempre activa e informativa
`ExternalAlertDeliveryDisabled` hace visible esa limitación; no se presenta un
canal vacío como si notificase. Las alertas son visibles en Grafana,
Prometheus y Alertmanager. Para cerrar el canal de incidentes debe codificarse
un destino real con secreto externo, owner y prueba controlada.

Alloy usa `discovery.docker` y `loki.source.docker`, documentados por
[Grafana](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.docker/).
Conserva solo servicios cuyos nombres pertenecen a los stacks `edge`,
`workloads` u `observability`. No recopila contenedores legacy.

Ningún contenedor recibe `/var/run/docker.sock`. Un servicio systemd root-only
expone a Alloy un socket Unix `0600` distinto y permite solo `GET`/`HEAD` para
ping/version, listado de contenedores/redes, inspect y logs. Rechaza bodies,
upgrade, pipelining, métodos mutantes y cualquier endpoint restante; además
fuerza `Connection: close`. La unidad restringe familias a `AF_UNIX`, elimina
capabilities y aplica `ProtectSystem=strict`. El socket proxy se monta
read-only solo en Alloy.

cAdvisor tampoco usa el daemon ni `/dev/kmsg`: lee rootfs, cgroups y el store
Docker en modo read-only y expone métricas raw por `id` de cgroup. Las reglas y
dashboards agregan esos IDs, sin fingir labels de servicio que no existen sin
el API Docker.

Loki usa TSDB `v13`, filesystem mononodo y Compactor con 14 días. TSDB es el
índice recomendado desde Loki 2.8 y el período de índice debe ser 24 horas
para retención, según la documentación de
[storage](https://grafana.com/docs/loki/latest/configure/storage/) y
[retención](https://grafana.com/docs/loki/latest/operations/storage/retention/).

Grafana provisiona Prometheus, Loki y dos dashboards desde Git conforme al
[provisioning oficial](https://grafana.com/docs/grafana/latest/administration/provisioning/).
SQLite permanece en una sola réplica porque el servidor es mononodo. Antes de
añadir réplicas Grafana debe migrarse a PostgreSQL; compartir SQLite no es un
diseño HA soportado.

## Persistencia y recuperación

Paths exactos para el sistema de backup:

<!-- markdownlint-disable MD013 -->

| Path | Consistencia | Prioridad |
| --- | --- | --- |
| `/srv/dockerswarm/observability/secrets/files` | immutable/quiesced | crítica |
| `/srv/dockerswarm/observability/grafana` | quiesced | crítica |
| `/srv/dockerswarm/observability/prometheus` | quiesced | alta |
| `/srv/dockerswarm/observability/alertmanager` | quiesced | media |
| `/srv/dockerswarm/observability/loki` | quiesced | alta |
| `/srv/dockerswarm/observability/alloy` | quiesced | baja |

<!-- markdownlint-enable MD013 -->

Grafana, Prometheus, Alertmanager y Loki deben detenerse o quiescerse antes de
copiar sus stores locales. Alloy solo conserva posiciones de lectura y puede
reinicializarse; la duplicación limitada de logs tras una restauración es
aceptable. Los dashboards, datasources, reglas y pipelines no necesitan
backup porque se regeneran desde Git.

Una restauración crea primero los paths/owners, restaura secretos y stores,
recrea Docker Secrets, despliega workloads y finalmente observabilidad. La
recuperación no importa ningún estado legacy.

## Fuentes primarias

- [Servicios y modo global de Swarm](https://docs.docker.com/reference/compose-file/deploy/)
- [Docker service discovery](https://docs.docker.com/engine/swarm/networking/)
- [Prometheus configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/)
- [Blackbox Exporter configuration](https://github.com/prometheus/blackbox_exporter/blob/master/CONFIGURATION.md)
- [Node Exporter en contenedor](https://github.com/prometheus/node_exporter/blob/master/README.md)
- [cAdvisor con Docker](https://github.com/google/cadvisor/blob/master/docs/running.md)
- [Docker Engine API](https://docs.docker.com/reference/api/engine/)
- [Sandboxing de unidades systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)
- [Cliente OpenSSH](https://man.openbsd.org/ssh.1)
- [PostgreSQL Exporter](https://github.com/prometheus-community/postgres_exporter)
- [Redis Exporter](https://github.com/oliver006/redis_exporter)
- [Alloy en Docker](https://grafana.com/docs/alloy/latest/set-up/install/docker/)
- [Endpoints HTTP de Alloy](https://grafana.com/docs/alloy/latest/reference/http/)
- [Grafana en Docker](https://grafana.com/docs/grafana/latest/setup-grafana/installation/docker/)
