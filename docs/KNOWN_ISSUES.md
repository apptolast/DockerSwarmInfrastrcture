# Diagnósticos conocidos de Docker 29.6.2, Traefik 3.7.9, sudo-rs y GRUB

La restauración de este Swarm sano genera dos registros de arranque reproducibles.
No se ocultan ni se rebaja globalmente el nivel de logging. El validador solo los
acepta mediante `--allow-known-swarm-startup` y después de comprobar que el nodo
está `active`, `Ready`, `Active` y `Leader`.

## `error creating cluster object`

Registro:

```text
level=error msg="error creating cluster object"
```

El código oficial de Moby 29.6.2 explica que crear el objeto por defecto debe
fallar cuando el clúster ya existe. La condición que decide registrarlo utiliza
`err != ErrExist || err != ErrNameConflict`; esa disyunción también registra los
dos resultados esperados. El mismo código continúa presente en la rama principal.

- [Comentario y condición en Moby 29.6.2](https://github.com/moby/moby/blob/3d80467678f6e36325fa9ae3dd486fe91e5652e3/vendor/github.com/moby/swarmkit/v2/manager/manager.go#L953-L985)

## `MAC address changed`

Registro:

```text
level=warning msg="MAC address changed" iface=br0
```

Durante la restauración de la red ingress, Linux recalcula la MAC del bridge al
incorporar sus interfaces. Moby detecta el cambio mientras prepara anuncios
ARP/NA, lo registra y detiene ese envío con la MAC antigua. En este host aparece
una sola vez por arranque; la red, el nodo y el scheduler convergen correctamente.

- [Detección en Moby 29.6.2](https://github.com/moby/moby/blob/3d80467678f6e36325fa9ae3dd486fe91e5652e3/daemon/libnetwork/osl/interface_linux.go#L723-L733)

## Política de validación

Toda otra entrada de prioridad warning o superior, o con
`level=warning|error|fatal|panic`, hace fallar la validación. También falla si
alguno de estos dos textos aparece más de una vez o si el manager no está sano.

La aceptación es específica de la versión y debe revisarse al actualizar Docker.

## Advertencia de caracteres codificados de Traefik

Traefik 3.7.9 registra una advertencia antes de cargar la configuración para
recordar que la política predeterminada de caracteres codificados cambió. Este
repositorio configura explícitamente a `false` los siete caracteres en los
cuatro entrypoints, pero la advertencia se emite antes de que esos valores sean
leídos.

El validador ejecuta la imagen exacta, exige una sola ocurrencia del texto
conocido y rechaza cualquier otra entrada `warning`, `error`, `fatal` o `panic`.
No se ocultan logs ni se rebaja su nivel para fabricar una salida vacía.

- [Emisión anterior a la carga en Traefik 3.7.9](https://github.com/traefik/traefik/blob/v3.7.9/cmd/traefik/traefik.go#L100-L103)
- [Migración de caracteres codificados](https://doc.traefik.io/traefik/v3.7/migrate/v3/)

## `Timeout waiting for privilege escalation prompt` con sudo-rs

Ubuntu 26.04 instala `sudo-rs` como alternativa preferente (prioridad 50), de
modo que `/usr/bin/sudo` apunta a `/usr/lib/cargo/bin/sudo`. `sudo-rs` no
reproduce el indicador pedido con `-p`: lo envuelve en un formato propio.

```text
[sudo: [sudo via ansible, key=<id>] password:] Password:
```

El complemento `become` de Ansible construye el indicador exacto
`[sudo via ansible, key=<id>] password:` y solo lo reconoce cuando alguna
línea de la salida **empieza** por ese texto (`check_password_prompt`, en
`ansible/plugins/become/__init__.py`). La línea de `sudo-rs` empieza por
`[sudo:` seguido de un espacio, así que la coincidencia nunca ocurre y la
escalada aborta sin haber enviado nunca la contraseña.

```text
Timeout (12s) waiting for privilege escalation prompt
```

El desajuste también rompe la detección de contraseña incorrecta: Ansible
busca el texto `Sorry, try again.` mientras que `sudo-rs` responde con
`sudo: Authentication failed, try again.`.

`requiretty` no interviene en este fallo. `sudo-rs` no implementa ese ajuste
y `visudo` rechaza `Defaults:admin !requiretty` con `unknown setting`, de modo
que un fichero en `/etc/sudoers.d/` no cambia nada.

El paquete `sudo` clásico sigue disponible en Ubuntu 26.04 e instala el
binario setuid `/usr/bin/sudo.ws` (alternativa de prioridad 40), que respeta
`-p` byte a byte. Por eso `ansible/ansible.cfg` fija:

```ini
become_exe = /usr/bin/sudo.ws
```

No se altera la alternativa del sistema, no se concede `NOPASSWD` y no se
almacena ninguna contraseña: el cambio se limita a Ansible.
`config/host-security.yml` bloquea la versión del paquete `sudo` para que
cualquier reconstrucción disponga del binario.

- [Alternativas de sudo en Ubuntu 26.04](https://manpages.ubuntu.com/manpages/resolute/en/man8/update-alternatives.8.html)

## Permisos del checkout durante la validación del 2026-10-05

Un checkout creado con `umask 0007` dejó ficheros públicos con modo 0660.
Dos guardas rechazaron correctamente los scripts Python escribibles por el
grupo. Se quitó únicamente ese permiso a
`scripts/validate-crowdsec-allowlist.py` y
`scripts/traefik-access-log-probes.py`, dejándolos en 0640. La suite de 935
pruebas pasó después de esa corrección, con dos pruebas omitidas.

La siguiente ejecución completa pasó también las 96 pruebas de migración,
las autopruebas de backup y las cuatro raíces Terraform, pero terminó con
un fallo en la comprobación de salud de Traefik. Su contenedor usa UID
65532 y no podía leer el fixture público
`tests/fixtures/traefik-empty-dynamic.yml`, que seguía en 0660. Se cambió
únicamente ese fixture a 0644 y la validación específica de Traefik pasó.
La ejecución completa posterior de `validate-iac.sh` y `lint.sh` también
pasó antes de editar esta documentación.

Antes de repetir una validación, comprobar los permisos de los ficheros
concretos: los scripts que ejecuta root no admiten escritura de grupo; un
fixture público montado en un contenedor sin privilegios debe ser legible
por su UID. No hacer `chmod` recursivo, abrir ficheros privados ni relajar
las guardas para resolver este desajuste. Los cambios anteriores afectan
al checkout local y no producen diferencias de contenido en Git.

Un fallo conserva su marcador global. La recuperación requiere probar que
su proceso terminó y usar `recover` con la confirmación exacta emitida por
el helper, según [CLAUDE.md](../CLAUDE.md). No borrar el lock o marcador ni
considerar que una prueba específica sustituye la validación completa.

## `eth0` pasa a `ens3` tras actualizar el kernel

Cada paquete de kernel ejecuta `update-grub` al instalarse
(`/etc/kernel/postinst.d/zz-update-grub`, de `grub2-common`), que regenera
`/boot/grub/grub.cfg`. `grub-mkconfig` lee `/etc/default/grub` solo si
existe, y después cada `/etc/default/grub.d/*.cfg`. En este host
`/etc/default/grub` no existe, y en `/etc/default/grub.d/` está
`kdump-tools.cfg`, del paquete `kdump-tools`, que añade a
`GRUB_CMDLINE_LINUX_DEFAULT`:

```text
crashkernel=2G-4G:320M,4G-32G:512M,32G-64G:1024M,64G-128G:2048M,128G-:4096M
```

El host arrancaba con `net.ifnames=0`, `console=tty0`, `video=1024x768`,
`autoinstall` y `ds=nocloud-net`. El kernel `7.0.0-34-generic`, el primero
que se instala desde el 2026-07-21 según los `/var/log/dpkg.log*`
conservados, regeneró `grub.cfg` el 2026-10-05 sin esos parámetros y con
`crashkernel=`:

- sin `net.ifnames=0`, systemd-udevd da a las interfaces nombres
  predecibles y la pública arrancó como `ens3`. `config/platform.yml`
  declara `eth0`: las aserciones previas de `platform` y `host-baseline` se
  detienen si no existe, y las reglas `-i eth0` de UFW y de
  `DOCKERSWARM-INGRESS` no casan con otro nombre. El `ExecStartPost` de
  `docker.service` (`/usr/local/sbin/dockerswarm-docker-firewall`, del
  drop-in `20-dockerswarm-firewall.conf` de `platform`) falló con
  `expected default interface eth0, found ens3` y `docker.service` se
  reiniciaba en bucle. Con `net.ifnames=0`, `ens3`, `enp0s3` y `enx…` siguen
  siendo nombres alternativos de `eth0`: `ip link show dev ens3` responde
  igual, y solo la primera columna de `ip -brief link` dice cómo se llama;
- con la memoria de este host, entre 4 y 32 GiB, `crashkernel=` reserva
  512 MiB para el kernel de volcado, que el sistema no usa
  (`USE_KDUMP=0` en `/etc/default/kdump-tools`, que no impide esa línea). El
  host tiene exactamente `minimum_memory_mib` (15 981 MiB) de
  `config/capacity.yml`, y todo playbook con `capacity_preflight` se detiene
  si `MemTotal` baja de ahí.

Ese mismo día se escribió a mano
`/etc/default/grub.d/zz-dockerswarm-boot-cmdline.cfg`, se ejecutó
`update-grub` y se reinició. `host_baseline` gestiona ahora ese drop-in con
los parámetros de `config/host-security.yml` (ver su
[README](../ansible/roles/host_baseline/README.md), «Kernel command line»):
el primer apply reescribe el fichero manual con el contenido revisado y
ejecuta `update-grub` una vez. Se lee después de `kdump-tools.cfg` y asigna
las dos variables sin su valor previo, así que descarta el `crashkernel=` y
kdump queda sin memoria reservada:

```sh
GRUB_CMDLINE_LINUX="net.ifnames=0 console=tty0 video=1024x768"
GRUB_CMDLINE_LINUX_DEFAULT="autoinstall ds=nocloud-net"
```

Cada apply de `host-baseline` comprueba después que la entrada por defecto
de `grub.cfg` lleva esos parámetros, ningún otro valor de `net.ifnames` y
ningún `crashkernel=`, y que `/proc/cmdline` lleva `net.ifnames=0` como
único valor de `net.ifnames` y ningún `crashkernel=`. La
actualización de paquetes no pasa por Ansible: antes de cada reinicio con un
kernel nuevo, «Parcheo del sistema operativo»
([OPERATIONS.md](OPERATIONS.md)) compara la entrada por defecto de
`grub.cfg` con `/proc/cmdline`, y después del reinicio comprueba `eth0`.

Si el host ya arrancó sin `eth0`, la comprobación previa de `host-baseline`
se detiene antes de llegar al drop-in, así que se arregla a mano. Desde la
consola de Netcup, en el menú de GRUB, `e` edita la entrada, se añade
`net.ifnames=0` a la línea `linux` y `Ctrl-x` arranca; ese arranque no se
guarda. Después se corrige el drop-in, se ejecuta `sudo -- update-grub`, se
comprueba `grub.cfg` como en «Parcheo del sistema operativo» y se reinicia.
Con `eth0` de vuelta, un apply de `host-baseline` adopta el fichero.

- [`net.ifnames=` en systemd-udevd](https://manpages.ubuntu.com/manpages/resolute/en/man8/systemd-udevd.service.8.html)
- [`GRUB_CMDLINE_LINUX` y `GRUB_CMDLINE_LINUX_DEFAULT`](https://www.gnu.org/software/grub/manual/grub/html_node/Simple-configuration.html)
- [`crashkernel=` y `kdump-tools.cfg` en Ubuntu Server](https://ubuntu.com/server/docs/how-to/software/kernel-crash-dump/)
- [Editor de entradas de GRUB](https://www.gnu.org/software/grub/manual/grub/html_node/Menu-entry-editor.html)
- [`NamePolicy=` y `AlternativeNamesPolicy=` en systemd.link](https://manpages.ubuntu.com/manpages/resolute/en/man5/systemd.link.5.html)
