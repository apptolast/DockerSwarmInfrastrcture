# Reconstrucción completa y cobertura declarativa

## Objetivo

La plataforma se considera reproducible cuando un VPS limpio y los backups
offsite permiten reconstruir host, Swarm, edge y aplicaciones sin depender de
una configuración manual desconocida.

“Está instalado ahora” no significa “se puede reconstruir”. Cada elemento debe
estar en una de estas categorías:

- configuración no sensible declarada en Terraform, Ansible o Git;
- secreto referenciado por identidad y recuperable desde un gestor externo;
- dato durable cubierto por backup y restore probado;
- bootstrap de proveedor inevitable, documentado con entradas y responsable.

## Cobertura actual

| Componente | Cobertura | Situación |
| --- | --- | --- |
| Compra/reinstalación del VPS | Bootstrap externo | No soportado por provider |
| Identidad del VPS y NIC | Terraform con gate | Faltan IDs reales/import |
| Buckets R2 de state | Terraform | Declarados, aún no aplicados |
| DNS Cloudflare | Terraform | Solo `edge`; falta import/credencial |
| Firewall y claves SCP | Terraform con gate | Falta inventario/import |
| Paquetes Docker y repositorio APT | Ansible | Declarados y fijados |
| Daemon, forwarding, Swarm y edge | Ansible | Declarados, pendientes de apply |
| SSH, sysctl, journald y updates | Adopción Ansible | Tienen preflight |
| Usuario admin y claves autorizadas | Bootstrap externo | Solo se validan |
| UFW completo y egress | Adopción Ansible | Falta reconstrucción |
| Seguridad host y tiempo | Adopción Ansible | Falta configuración completa |
| State Terraform | R2 y snapshot `age` | Faltan buckets/destino/restore |
| Raft Swarm y ACME | Runbook | Falta destino offsite y restore |
| Aplicaciones y datos | Repo de workloads | Falta inventario y migración |
| Monitorización y alertas externas | Sin propietario | Diseño pendiente |

El provider comunitario Netcup `1.0.0` no crea ni compra servidores. Expone
inventario de servidores y recursos de firewall, claves, reverse DNS,
snapshots y failover. La instalación de imagen desde SCP sigue siendo un
bootstrap externo:
[recursos del provider fijado](https://github.com/hornc-greedy/terraform-provider-netcup/tree/v1.0.0/docs/resources).

## Por qué el baseline aún adopta

El servidor contiene controles de seguridad ya activos. Reemplazar su
configuración sin inventario privilegiado podría:

- bloquear SSH o egress necesario;
- perder credenciales del bouncer CrowdSec;
- desactivar jails o reglas que no aparecen en este checkout;
- cambiar la fuente de tiempo;
- romper el orden entre CrowdSec, UFW y Docker.

Por eso el rol actual administra solo las partes observadas y verificables, y
falla si los controles heredados no cumplen. Esa decisión protege el servidor
actual, pero no cierra todavía una reconstrucción desde cero.

## Trabajo necesario para cerrar la reconstrucción

### 1. Inventario privilegiado y saneado

El operador ejecutará una auditoría con `sudo` y guardará el resultado cifrado
fuera de Git. Debe incluir:

- versión de imagen, particiones, mounts y opciones de filesystem;
- usuarios, grupos y fingerprints de claves, nunca claves privadas;
- repositorios APT, fingerprints y versiones de paquetes;
- reglas UFW, iptables/ip6tables, CrowdSec, Fail2ban y PSAD;
- Chrony, rsyslog, journald, logrotate, sysctl y unidades/timers;
- referencias de secretos y custodios, sin volcar sus valores;
- puertos, procesos, cron y automatizaciones fuera de Git.

Los ficheros con tokens o claves se inventarían por checksum, owner, mode,
origen y procedimiento de recuperación; su contenido no se copia a este repo.

### 2. Rol de bootstrap de host

Después del inventario y en un VPS de ensayo se añadirá un rol separado que:

- crea la identidad administrativa y el grupo SSH;
- instala exclusivamente claves públicas aprobadas desde un vault o gestor;
- configura repositorios y paquetes con fingerprints revisados;
- establece UFW fail-closed, SSH restringido y egress completo;
- demuestra reconexión antes de cerrar el acceso anterior;
- no inicializa Swarm hasta superar el baseline.

No se prueba por primera vez en este manager mononodo.

### 3. Roles de controles de seguridad

CrowdSec, bouncer, Fail2ban, PSAD, Chrony y rsyslog se trasladarán de
“verificados” a “gestionados” uno por uno. Cada rol necesita:

- template no sensible y validación nativa previa;
- entrada secreta externa si aplica;
- backup del original y rollback;
- prueba de servicio, integración con firewall y segunda ejecución idempotente.

### 4. Backup automatizado

Solo después de elegir proveedor offsite, recipients `age`, retención y RPO/RTO
se instalarán mediante Ansible unidades/timers de backup. La automatización
debe:

- detener Docker para el backup coherente de Raft;
- preservar owner/mode de ACME;
- congelar o dumpear cada aplicación según su tecnología;
- cifrar antes de transmitir;
- emitir métricas/alertas y no borrar la última copia válida;
- ejecutar restauraciones periódicas en otro host.

Un snapshot Netcup puede complementar, pero nunca sustituir, una copia cifrada
fuera de la cuenta y una restauración probada.

### 5. Workloads y observabilidad

El repositorio de workloads declarará stacks, rutas, healthchecks, migraciones,
dependencias y restore por aplicación. La monitorización externa necesita un
propietario, canal de alerta y prueba desde fuera del VPS.

## Orden de una futura reconstrucción

1. Crear o reinstalar VPS desde la imagen aprobada en SCP.
2. Verificar identidad, consola, discos, NIC, IP y host keys.
3. Ejecutar el rol de bootstrap con claves públicas desde el gestor.
4. Aplicar y repetir baseline/controles del host.
5. Importar/aplicar perímetro sin abrir puertos de aplicaciones.
6. Restaurar o inicializar Swarm conforme a la causa del incidente.
7. Restaurar ACME o emitir de nuevo tras validar DNS-01 staging.
8. Desplegar edge y validar TLS desde fuera.
9. Restaurar datos y desplegar una aplicación cada vez.
10. Cambiar DNS únicamente tras pruebas y conservar rollback.

## Entradas que debe preparar el propietario

No deben pegarse en chat ni commit:

- catálogo de claves SSH públicas, fingerprints, propietarios y expiración;
- CIDR administrativos y mecanismo alternativo de acceso;
- export privilegiado saneado de UFW y controles de seguridad;
- proveedor/destino offsite, recipients `age`, retención, RPO y RTO;
- inventario del repositorio anterior, datos y pruebas de cada aplicación;
- canal y responsable de alertas, incidentes y rollback;
- decisión sobre `apptolast-workloads` y custodios GitHub.

Hasta disponer de esas entradas, la cobertura parcial se mantiene explícita y
ningún documento afirma que una máquina vacía sea todavía reconstruible.
