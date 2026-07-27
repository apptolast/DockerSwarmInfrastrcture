# Reconstrucción completa y cobertura declarativa

## Criterio

Una reconstrucción válida parte de un VPS Ubuntu 26.04 limpio, un commit
revisado y material externo recuperable. Termina únicamente cuando host,
servicios, datos, DNS, backups y rollback han sido verificados.

Cada elemento debe ser uno de:

- configuración no sensible declarada aquí;
- secreto identificado y recuperable desde un gestor externo;
- dato durable cubierto por backup/restore probado;
- bootstrap inevitable del proveedor con entradas y responsable explícitos.

## Cobertura actual

<!-- markdownlint-disable MD013 -->

| Componente | Código | Estado productivo |
| --- | --- | --- |
| Compra/reinstalación del VPS | Runbook externo | SCP no tiene provider de compra |
| Identidad/NIC y perímetro Netcup | Terraform con import/gates | No aplicado; credencial real ausente |
| Buckets R2 de state | Terraform | No aplicados; credenciales ausentes |
| DNS Cloudflare | Terraform, 10 A exactos | No importado ni cortado |
| Bootstrap `admin`/SSH | Ansible + wrapper atómico | Código probado; no usado en este host |
| Seguridad, UFW, tiempo y logging | Ansible/Jinja | Código probado; apply pendiente |
| Docker, Swarm y firewall Docker | Ansible | Swarm existe; reconciliación pendiente |
| Traefik y certificados | Stack/Ansible | Secret existe; stack no desplegado |
| Ocho servicios HTTP | Stacks/Ansible | Datos preparados; stacks no desplegados |
| Minecraft | Stack/Ansible + gate triple | Datos preparados; publicación bloqueada |
| Observabilidad | Stack/Ansible | No desplegada |
| Backup/restore | restic, systemd, Ansible | Código probado; activación bloqueada |
| State Terraform | R2 + snapshots `age` | Destinos/identidades reales ausentes |

<!-- markdownlint-enable MD013 -->

“Código probado” no significa “recuperación demostrada”. Hoy siguen sin existir
backends R2 live, escrow externo de autolock ni un ensayo completo en otro VPS.

## Bootstrap fresco

`scripts/bootstrap-host.sh`:

- exige Ubuntu 26.04, root inicial, commit limpio y entradas externas `0600`;
- rechaza identidades reservadas, rutas/plataforma o paquetes Docker/containerd
  preexistentes;
- crea únicamente `admin` UID/GID 1001, `sshusers` y las claves aprobadas;
- conserva el hash de contraseña externo sin reescribirlo en un resume;
- liga todo el flujo a un nonce/commit/contrato y al mutex host-global;
- instala primero una política SSH staged;
- arma rollback automático antes de deshabilitar root;
- solo libera el lock tras una reconexión independiente de `admin`.

Un crash conserva marker. No existe timeout que lo borre: la recuperación exige
inspección, confirmación ligada al contenido y evidencia archivada.

El rol `host_security` fija el snapshot Ubuntu promovido, repositorios/firma de
CrowdSec, paquetes, Hub, UFW fail-closed, Chrony, rsyslog, Fail2ban, PSAD y SSH.
Herramientas de seguridad legacy quedan inventariadas pero no se purgan ni
reescriben sin una migración explícita.

## Servicios y datos

El catálogo aprobado incluye:

- Kropia;
- Traefik;
- Minecraft Stats;
- Minecraft;
- n8n;
- OpenClaw limpio;
- Passbolt;
- webs personales de Alberto y Pablo;
- Shlink.

La denylist se valida en tests y no aparece en stacks. Las definiciones de
workloads, secretos por identidad, restore markers, bases, healthchecks,
redes, rutas y preflight de imágenes pertenecen a este repositorio.

Los datos restaurados bajo `/srv/dockerswarm` no se activan hasta que:

- el marker de migración coincide con catálogo, manifests y checksums;
- todos los secrets esperados existen con la identidad versionada;
- las imágenes exactas están disponibles por digest;
- no hay writers anteriores concurrentes;
- los smoke tests específicos pasan.

OpenClaw se inicializa limpio y rechaza evidencia de import legacy. n8n conserva
workflows no publicados hasta completar la aceptación funcional/OAuth.

## Backup

La capa de backup ya está codificada. Cubre dumps PostgreSQL, datasets, fuentes
de secretos, observabilidad, ACME, Minecraft y una copia fría de Raft. Falla
antes de mutar si faltan:

- bucket/account R2 de backup;
- credenciales R2 limitadas;
- contraseña restic con custodia externa;
- autolock ya activo y unlock key custodiada/probada;
- `backup_activation_enabled: true`.

No se activa autolock con un comando manual que pueda perder la clave entre su
emisión y el escrow. Hasta integrar un destino y custodio externo aprobado, el
runbook marca `STOP`.

## Orden de reconstrucción

1. Crear/reinstalar el VPS desde la imagen aprobada y verificar consola, NIC,
   discos, IP y host keys.
2. Preparar fuera de Git claves públicas y hash de contraseña del administrador.
3. Ejecutar el bootstrap fresco desde un commit limpio.
4. Importar/aplicar proveedor sin abrir todavía puertos de aplicaciones.
5. Aplicar dos veces plataforma y baseline; la segunda debe ser idempotente.
6. Restaurar o inicializar Swarm según la causa del incidente.
7. Restaurar ACME o emitir de nuevo tras validar DNS-01 staging.
8. Aplicar preflight, edge y workloads sin cambiar aún DNS.
9. Probar cada servicio con resolución forzada hacia la IP nueva.
10. Aplicar observabilidad y comprobar alertas/blackbox.
11. Adoptar DNS existente; cortar los ocho A HTTP y crear `edge`.
12. Mantener Minecraft legacy hasta aprobar seguridad/publicación.
13. Con R2 y custodia externa ya probados, aplicar el target separado `backup`.
14. Ejecutar restore de aplicación y recuperación Raft en un host aislado.

`site` termina en observabilidad. Backup es deliberadamente un target separado:
omitirlo tras preparar sus entradas deja la reconstrucción incompleta.

## Entradas externas pendientes

Nunca deben pegarse en chat ni commit:

- claves SSH públicas aprobadas y hash de contraseña inicial;
- credenciales/identidades separadas para Cloudflare, R2 y Netcup;
- recipients `age` e identidades firmantes reales;
- contraseña restic y unlock key en un gestor externo;
- canal/responsable de alertas, incidentes y rollback;
- aceptación OAuth de n8n y decisión de exposición de Minecraft.
- evidencia de snapshot final o tooling revisado de refresh/promoción.

La máquina todavía no puede declararse reconstruible al 100 % mientras falten
esas entradas y un ensayo off-host. Los gates convierten esa ausencia en un
fallo explícito, no en configuración implícita.
