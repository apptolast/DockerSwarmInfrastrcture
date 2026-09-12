# Guía práctica para actualizar una imagen de workload

Esta guía explica cómo cambia la imagen que ejecuta un servicio. Está escrita
para alguien que sabe programar, pero aún no conoce Docker Swarm, Ansible o la
política de este repositorio.

Desde la decisión del owner del 2026-09-11, el objeto revisado en Git es el
**canal** de cada imagen, no su digest. Las imágenes propias siguen `:latest`;
las bases de datos de terceros siguen un canal de versión mayor. El modelo
completo, con interruptor y rollback, está en [AUTOUPDATE.md](AUTOUPDATE.md).

## Ideas mínimas

Una imagen Docker tiene dos identificadores que cumplen funciones distintas:

- Una etiqueta, como `personal-website:latest`, es un nombre cómodo que el
  editor puede mover para señalar otra versión.
- Un digest, como `sha256:...`, identifica exactamente el contenido. Si el
  contenido cambia, también cambia el digest.

Un canal es una etiqueta revisada (`repo:tag`). Swarm la convierte en un
digest al desplegar, y el servicio conserva ese digest hasta el siguiente
cambio. Un hold (`repo:tag@sha256:...`) fija un digest concreto y nunca se
actualiza solo.

En este repositorio hay dos ficheros relevantes:

- `config/services.yml` conserva el digest histórico de la migración. No se
  cambia para una actualización normal porque su hash está ligado a evidencia
  de restauración.
- `config/image-channels.yml` declara, para cada servicio renderizado, su
  canal o su hold, su clase y si se actualiza automáticamente.

## Actualizar un servicio paso a paso

Haz todo en una rama y desde una copia limpia de este repositorio. No ejecutes
un apply para «probar»: el dry-run es el primer control real.

1. Comprueba que el canal existe y publica `linux/amd64`, sin descargarlo:

   ```bash
   docker buildx imagetools inspect docker.io/apptolast/kropia-web:latest
   ```

2. Revisa el cambio de la aplicación o de la imagen de terceros: repositorio
   de origen, dependencias, notas de seguridad y, si tiene estado, sus
   migraciones. Este juicio humano no lo sustituye ningún script.

3. En `config/image-channels.yml`, cambia solo la entrada del servicio:
   `reference` al canal (`repo:tag`) o a un hold (`repo:tag@sha256:...`), y
   `autoupdate` a `true` únicamente si debe seguir el canal por sí solo. El
   validador rechaza otro repositorio, otro tag para imágenes propias, un
   tag distinto del de la tabla de versiones mayores y un digest sin tag que
   no sea exactamente el baseline.

4. Si el servicio tiene datos, documenta antes un volcado lógico: no hay
   copia fuera del host (STOP gate 5).

5. Describe el motivo en `CHANGELOG.md`, bajo `[Unreleased]`.

6. Ejecuta las compuertas locales, en este orden:

   ```bash
   ./scripts/bootstrap-tooling.sh
   ./scripts/validate-iac.sh
   ./scripts/lint.sh
   ```

   No inventes flags para ignorar un fallo.

7. Revisa el diff, crea el commit y abre un PR.

8. Tras el merge y con el worktree limpio, ejecuta primero el dry-run humano:

   ```bash
   ./scripts/deploy-ansible.sh \
     --playbook workloads \
     --check \
     --ask-become-pass
   ```

   El preflight consulta el registro y muestra el digest actual de cada canal
   del stack. No descarga imágenes ni modifica Swarm.

9. Una persona ejecuta el apply con los demás STOP gates satisfechos:

   ```bash
   ./scripts/deploy-ansible.sh \
     --playbook workloads \
     --confirm-production \
     --ask-become-pass
   ```

   El stack se despliega con `resolve_image: changed`: solo los servicios
   cuya entrada cambió consultan el registro; los demás conservan su digest.

10. Comprueba el servicio, sus réplicas, la ruta HTTPS, los logs y
    `observed-images.yml`. Después repite el playbook: un segundo apply debe
    converger sin cambios.

## Qué sigue sin automatizarse

Un push a Docker Hub puede cambiar el digest de un servicio con
`autoupdate: true`, pero solo a través del vigilante de canales revisado en
este repositorio, filtrado por la etiqueta `apptolast.autoupdate=true`. Nada
externo ejecuta Ansible: `ansible-playbook` contra el clúster real sigue
siendo una acción humana, y ningún webhook, cron o pipeline puede iniciarlo.

Tampoco modifiques `config/services.yml` para esquivar el contrato. Ese
catálogo pertenece a la evidencia de migración.

## Añadir un servicio nuevo

1. Añade su baseline revisado al catálogo que le corresponda.
2. Añade una entrada en `config/image-channels.yml`. Sin ella,
   `scripts/validate-image-channels.py` falla porque el stack renderizado
   tiene un servicio no listado.
3. Si es una base de datos, una cola o el ingress, añade antes su canal
   mayor a la tabla del validador, con prueba negativa.
4. Renderiza `apptolast.autoupdate` en `deploy.labels` desde la entrada y
   exige `failure_action: rollback`, `monitor` y healthcheck si va a
   actualizarse solo.

## Problemas frecuentes

<!-- markdownlint-disable MD013 -->

| Señal | Significado | Acción correcta |
| --- | --- | --- |
| `repository differs from its baseline` | La entrada apunta a otra imagen | Corrige la entrada; el baseline no se toca para una actualización. |
| `not the reviewed major channel` | Tag de base de datos distinto del revisado | Usa el tag de la tabla; cambiar de mayor exige migración y revisión de la tabla. |
| `rendered services differ from the channel map` | Servicio renderizado sin entrada | Añade su entrada o su exclusión revisada. |
| El spec vivo no coincide tras el apply | Un hold fue cambiado fuera de Ansible | Investiga quién lo cambió; no relajes la comprobación. |
| El marker de restore no coincide con el catálogo | Se alteró el baseline histórico | Restaura el baseline; nunca edites ni reemitas el marker para una actualización de imagen. |
| Falta `docker buildx` | Falta el plugin fijado por el host | Revisa el contrato de paquetes; no sustituyas el comando. |
| El writer rechaza el worktree | El estado no está revisado/commiteado | Revisa y crea el commit; no uses bypasses. |

<!-- markdownlint-enable MD013 -->

## Checklist de un cambio seguro

- [ ] La entrada cambia solo el servicio previsto.
- [ ] El canal existe con `linux/amd64` y la versión fue revisada.
- [ ] Si hay datos, el volcado lógico está documentado.
- [ ] Bootstrap, validación y lint pasan sin omitir compuertas.
- [ ] El dry-run muestra el digest del canal y ningún cambio inesperado.
- [ ] La verificación posterior confirma el servicio y un segundo apply
      converge.
