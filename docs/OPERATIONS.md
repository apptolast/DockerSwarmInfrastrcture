# Operación y seguridad

## Arquitectura actual

El clúster tiene un único servidor con una única interfaz pública. El nodo es
manager y worker. Esta arquitectura permite usar servicios, stacks, secrets,
configs y redes overlay, pero no tolera el fallo del nodo.

Para tolerar la pérdida de un manager se necesitan al menos tres managers,
preferiblemente un número impar, distribuidos entre dominios de fallo.

## Puertos internos

Swarm utiliza los siguientes puertos entre nodos:

| Tráfico | Uso |
| --- | --- |
| `2377/TCP` | Plano de control |
| `7946/TCP+UDP` | Descubrimiento entre nodos |
| `4789/UDP` | VXLAN de redes overlay |

No se autoriza ninguno desde `Anywhere`. Con un solo nodo no es necesario abrirlos.
Antes de añadir nodos debe existir una red privada o un túnel autenticado y las
reglas deben limitarse a las direcciones de esos nodos. Docker advierte que VXLAN
no autentica por sí mismo el tráfico recibido en `4789/UDP`.

Los puertos publicados por Docker pueden eludir las reglas normales de UFW. Las
restricciones de cargas publicadas deben diseñarse en `DOCKER-USER`, además de
las reglas perimetrales del proveedor.

## Logging

`config/daemon.json` establece `local` como driver predeterminado. No se deben
leer ni modificar directamente sus archivos internos; se accede a ellos con
`docker logs`.

`docker service logs` solo funciona con `json-file` o `journald`. Si un servicio
necesita agregación mediante ese comando, debe sobrescribir el driver en su
definición. `json-file` debe configurarse con rotación para evitar agotar disco.

`config/logrotate/iptables` utiliza el helper del paquete rsyslog para enviar un
HUP después de rotar `/var/log/iptables.log`. El antiguo `invoke-rc.d rsyslog`
no es válido en este host porque Ubuntu 26.04 ya no instala ese script SysV.

## Reinicios

Antes de reiniciar:

1. Confirmar una ventana de mantenimiento.
2. Ejecutar `dockerd --validate --config-file=/etc/docker/daemon.json`.
3. Verificar que existe una copia de seguridad reciente del estado de Swarm.

Después:

1. Confirmar `systemctl is-active docker`.
2. Confirmar `docker node ls` y que el manager está `Ready` y `Leader`.
3. Revisar el journal desde el instante exacto del reinicio.
4. Confirmar réplicas y health checks de cada servicio.

`live-restore` no mantiene servicios Swarm durante el reinicio del daemon.

## Copia de seguridad y recuperación

El estado del manager reside en `/var/lib/docker/swarm`. La copia coherente
requiere detener Docker y copiar el directorio completo. En este mononodo eso
genera indisponibilidad. La copia debe almacenarse fuera del VPS, cifrada y con
pruebas periódicas de restauración.

No se crea una copia local automática porque no protege frente a la pérdida del
servidor y no existe todavía un destino externo autorizado.

## Autolock

Autolock protege las claves del manager en reposo, pero obliga a proporcionar una
unlock key después de cada reinicio de Docker. No debe activarse hasta disponer
de custodia externa, recuperación probada y un procedimiento de arranque. La
clave nunca se guarda en Git ni en logs.

## Ampliación a varios nodos

Antes de unir otro servidor:

1. Crear una red privada o VPN entre nodos.
2. Asignar direcciones estables.
3. Autorizar los puertos internos solo dentro de esa red.
4. Diseñar pools overlay que no solapen redes existentes.
5. Decidir el número y la distribución de managers.
6. Probar backup, restauración y pérdida de quorum.

Los pools globales definidos durante `docker swarm init` no pueden modificarse
después sin recrear el Swarm.

## Acceso privilegiado

La pertenencia al grupo `docker` equivale a acceso root. Debe limitarse a
administradores, proteger sus claves SSH y revisarse periódicamente.
