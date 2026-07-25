# Docker Swarm

Base reproducible para un Docker Swarm de producción de un solo nodo.

## Estado objetivo

- Docker Engine administrado por `systemd`.
- Un manager que también ejecuta tareas.
- Logging predeterminado `local`, con rotación y compresión automáticas.
- Ningún puerto interno de Swarm autorizado desde Internet.
- Ningún servicio de aplicación desplegado por este repositorio.

Un único manager no proporciona alta disponibilidad: una caída o un
mantenimiento del VPS interrumpe el plano de control y sus cargas.

## Contenido

- `config/daemon.json`: configuración versionada del daemon.
- `config/logrotate/iptables`: rotación compatible con el servicio systemd de rsyslog.
- `scripts/install-daemon-config.sh`: instalación atómica y validada.
- `scripts/install-logrotate-config.sh`: corrección reproducible de `iptables.log`.
- `scripts/lint.sh`: JSON, Bash, ShellCheck, Markdown y detección de secretos.
- `scripts/validate-logrotate.sh`: comprobación de rotación, rsyslog y systemd.
- `scripts/smoke-test.sh`: prueba efímera del scheduler y del logging.
- `scripts/validate-daemon-journal.sh`: auditoría de la invocación actual del daemon.
- `scripts/validate-firewall.sh`: comprobación de no exposición del plano interno.
- `scripts/validate.sh`: comprobación no destructiva del estado objetivo.
- `docs/OPERATIONS.md`: decisiones, seguridad y procedimientos operativos.
- `docs/KNOWN_ISSUES.md`: diagnósticos de arranque atribuibles a Docker 29.6.2.

## Instalación

```bash
sudo ./scripts/install-daemon-config.sh
sudo ./scripts/install-logrotate-config.sh
docker swarm init \
  --advertise-addr IPV4_ESTABLE \
  --data-path-addr IPV4_ESTABLE
./scripts/smoke-test.sh
sudo ./scripts/validate.sh
./scripts/lint.sh
```

En hosts donde una interfaz tiene varias direcciones, se debe indicar una IP
concreta; el nombre de interfaz puede resultar ambiguo.

La validación requiere privilegios root para auditar también el journal de
Docker, UFW, iptables, logrotate y rsyslog. Sin ellos solo comprueba el daemon y
el estado del Swarm.

El script de instalación reinicia Docker. En un Swarm mononodo eso implica una
interrupción; debe ejecutarse en una ventana de mantenimiento cuando existan
cargas reales.

## Decisión de logging

Docker recomienda el driver `local` para evitar el crecimiento ilimitado del
driver `json-file`. Sus valores predeterminados conservan cinco archivos de
20 MB por contenedor y comprimen los rotados. El cambio solo se aplica a
contenedores creados después del reinicio.

Existe una limitación específica de Swarm: `docker service logs` solo admite
servicios iniciados con `json-file` o `journald`. Un stack que necesite ese
comando debe declarar explícitamente uno de esos drivers y su política de
rotación; el resto puede conservar el valor predeterminado `local`.

## Referencias oficiales

- [Configurar el daemon](https://docs.docker.com/engine/daemon/)
- [Referencia de `dockerd`](https://docs.docker.com/reference/cli/dockerd/)
- [Configurar drivers de logging](https://docs.docker.com/engine/logging/configure/)
- [Driver `local`](https://docs.docker.com/engine/logging/drivers/local/)
- [Inicializar un Swarm](https://docs.docker.com/reference/cli/docker/swarm/init/)
- [Administrar managers y copias de seguridad](https://docs.docker.com/engine/swarm/admin_guide/)
- [Filtrado de paquetes y UFW](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
