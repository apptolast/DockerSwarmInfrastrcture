# Acceso SSH por NetBird Cloud

## Estado aplicado el 5 de octubre de 2026

El propietario autorizó limitar el acceso de su PC al VPS a TCP/22.
La política se aplicó en NetBird Cloud y se comprobó por lectura posterior de
la API y por las reglas nftables recibidas por el host. No es un despliegue de
Ansible: el cliente y la configuración de la cuenta aún se gestionan fuera
del repositorio.

| Elemento | Contrato aplicado |
| --- | --- |
| Grupo origen | `owner-ssh-clients`: un único PC del propietario |
| Grupo destino | `apptolast-vps-ssh`: únicamente este VPS |
| Política | `owner-ssh-to-apptolast-vps`, habilitada |
| Acción y dirección | `accept`, origen hacia destino, no bidireccional |
| Protocolo y puertos | `tcp`, únicamente `22` |
| Política `Default` | Deshabilitada, conservada para diagnóstico |
| Otras políticas habilitadas | Ninguna en la lectura posterior |

En `ip netbird` e `ip6 netbird` se comprobó una única aceptación para
conexiones nuevas desde el PC permitido a TCP/22, aceptación de tráfico
establecido/relacionado y descarte del resto por `wt0`. La gestión y la
señalización permanecían conectadas; el servidor SSH integrado de NetBird
seguía deshabilitado. El SSH usa el `sshd` del host y sus claves existentes.

La prueba desde Windows abrió una conexión SSH nueva el 2026-10-05 a las
21:19:52 UTC. Usó la clave existente, `BatchMode=yes`,
`IdentitiesOnly=yes`, `StrictHostKeyChecking=yes`, ED25519 y
`ControlMaster=no`/`ControlPath=none`; no reutilizó un canal multiplexado.
El host respondió con el usuario y nombre esperados. La huella guardada por
el cliente coincide con la clave pública ED25519 leída en la VPS.
El [informe publicado](https://github.com/PabloHurtadoGonzalo86/satisfactory-server/blob/8913fc384a2f86f96f066e017790e6bfcff92ac5/docs/companions/windows-manual-20261005/README.md)
conserva el resultado y sus límites. No acredita una prueba de conexión
rechazada a otro puerto; esa restricción se comprobó mediante API y nftables.

Las lecturas completas de peers, grupos, políticas y nftables, con las copias
anteriores, quedan en el directorio privado del operador. Ni sus direcciones
ni IDs, ni el PAT de gestión, entran en Git. El token se lee desde un fichero
privado 0600, sin imprimirlo ni pasarlo por argumentos o entorno exportado.

## Repetir la configuración o reconstruir el host

1. Mantener disponible el SSH público por clave o la consola de Netcup.
   Comprobar el acceso de reserva antes de cambiar políticas de la cuenta.
2. Instalar y registrar el cliente del host siguiendo
   [OPERATIONS.md](OPERATIONS.md), «SSH por NetBird». No activar el servidor
   SSH integrado, rutas de red ni un exit node para esta operación.
3. En el panel, crear o revisar `owner-ssh-clients` con únicamente el PC
   autorizado y `apptolast-vps-ssh` con únicamente el VPS. Verificar los peers
   concretos: sus nombres por sí solos no prueban su identidad. No usar `All`.
4. Crear o actualizar `owner-ssh-to-apptolast-vps`: habilitada, acción
   `accept`, protocolo TCP, puerto `22`, origen y destino de esos grupos y
   dirección sólo origen hacia destino. Las respuestas SSH se admiten por
   seguimiento de conexiones; no hace falta una regla inversa amplia.
5. Guardar y releer esa política antes de deshabilitar `Default`. Revisar
   también otras políticas habilitadas: los permisos se acumulan, por lo que
   otra regla amplia anularía el objetivo de TCP/22.
6. Comprobar en el host las tablas `ip netbird` e `ip6 netbird`, el descarte
   por defecto, la gestión/señalización y el orden actual de las cadenas del
   cortafuegos. La política de Cloud no sustituye esa comprobación del host.
7. Desde el PC, abrir una conexión SSH nueva a la dirección NetBird del VPS
   como `admin`, con la clave y la huella del host verificadas. Comprobar que
   no se admite otro puerto de aplicación por NetBird. Registrar el resultado
   de la nueva conexión, sin publicarlo como hecho hasta ejecutarlo.

Un host reconstruido tiene otro peer. Antes de retirar el anterior, revisar
su identidad y añadir el nuevo al grupo destino; después comprobar el SSH
nuevo y retirar el peer sustituido. Un PC nuevo requiere actualizar el grupo
origen deliberadamente. No ampliar el grupo para resolver un nombre o una
sesión SSO sin comprobar.

## Validación posterior y rollback

Una modificación de peers, grupos, políticas o del cliente requiere volver a
comprobar API, reglas efectivas IPv4/IPv6 y una conexión nueva desde el PC.
Una regla diferente de TCP/22, otro peer autorizado o una aceptación por
defecto son deriva. La pérdida de gestión/señalización o un SSH nuevo fallido
requiere diagnóstico mediante la vía de reserva.

Desde SSH público o Netcup, corregir primero la pertenencia de los grupos y
la regla restringida. La copia anterior de `Default` permite conocer el estado
previo, pero habilitarla de nuevo abre todos los puertos entre peers: no es
un rollback automático ni una recuperación de mínimo acceso. Cualquier
restauración amplia requiere decisión expresa del propietario y debe quedar
registrada; no cambiar UFW, CrowdSec ni `DOCKER-USER` para compensarla.

No se cambiaron `sshd`, UFW, CrowdSec, puertos publicados, redes de Swarm,
allowed-signers ni credenciales de Terraform/Ansible. La codificación del
cliente NetBird en `host_security` sigue pendiente en
[DEPLOYMENT_STATUS.md](DEPLOYMENT_STATUS.md).

## Referencias

- [Control de acceso](https://docs.netbird.io/manage/access-control).
- [API de políticas](https://docs.netbird.io/api/resources/policies).
- [API de grupos](https://docs.netbird.io/api/resources/groups).
- [Autenticación de la API](https://docs.netbird.io/api/guides/authentication).
