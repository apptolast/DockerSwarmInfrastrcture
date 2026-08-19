# Guía práctica para actualizar una imagen de workload

Esta guía explica el camino completo para promover una imagen Docker de forma
repetible. Está escrita para alguien que sabe programar, pero aún no conoce
Docker Swarm, Ansible o la política de este repositorio.

El objetivo no es desplegar «lo último» a ciegas. El objetivo es que el
servidor ejecute un contenido exacto que una persona haya inspeccionado y que
pueda reconstruirse desde un commit revisado.

## Ideas mínimas

Una imagen Docker tiene dos identificadores que cumplen funciones distintas:

- Una etiqueta, como `personal-website:latest`, es un nombre cómodo que el
  editor puede mover para señalar otra versión.
- Un digest, como `sha256:...`, identifica exactamente el contenido. Si el
  contenido cambia, también cambia el digest.

Por eso una etiqueta no es evidencia suficiente para producción. Puede apuntar
a una imagen distinta entre la revisión y el despliegue. Un digest sí permite
repetir y auditar el despliegue.

En este repositorio hay dos contratos para Alberto:

- `config/services.yml` conserva el digest histórico de la migración. No se
  cambia para una actualización normal porque su hash está ligado a evidencia
  de restauración.
- `config/workload-image-updates.yml` contiene la excepción operativa. Su
  `tracked_reference` indica qué etiqueta se consulta y
  `approved_runtime_reference` registra el digest concreto aprobado.

El preflight consulta Docker Hub, convierte la etiqueta en
`latest@sha256:...` y exige que coincida byte por byte con el digest aprobado.
Si la etiqueta se movió, el proceso se detiene antes de descargar una imagen o
de modificar el stack.

## Actualizar Alberto paso a paso

Haz todo en una rama y desde una copia limpia de este repositorio. No ejecutes
un apply para «probar»: el dry-run es el primer control real.

1. Averigua qué contenido publica actualmente la etiqueta, sin descargarlo:

   ```bash
   docker buildx imagetools inspect \
     --format '{{json .Manifest}}' \
     docker.io/hgarciaalberto/personal-website:latest
   ```

   Guarda el valor `digest` de la salida. Debe tener el formato
   `sha256:` seguido de 64 caracteres hexadecimales.

2. Revisa el cambio de la aplicación antes de aprobarlo. Comprueba el
   repositorio de origen, las dependencias y las notas de seguridad. Este es
   un juicio humano deliberado: ningún script puede determinar si una nueva
   versión cumple la intención del servicio.

3. En `config/workload-image-updates.yml`, reemplaza únicamente el digest de
   `approved_runtime_reference`. Conserva el prefijo exacto
   `docker.io/hgarciaalberto/personal-website:latest@sha256:`. No cambies
   `tracked_reference`, el servicio, el componente ni el digest histórico de
   `config/services.yml` durante una actualización normal.

4. Describe el motivo y el digest nuevo en `CHANGELOG.md`, bajo
   `[Unreleased]`. El diff debe permitir a otra persona saber qué se aprobó y
   por qué.

5. Ejecuta las compuertas locales, en este orden:

   ```bash
   ./scripts/bootstrap-tooling.sh
   ./scripts/validate-iac.sh
   ./scripts/lint.sh
   ```

   No inventes flags para ignorar un fallo. Si el contrato de la imagen falla,
   el digest, el tag o el alcance de la excepción ya no coinciden. Corrige el
   cambio o vuelve a revisar la versión antes de continuar.

6. Revisa el diff, crea el commit y abre un PR. La revisión debe confirmar que
   solo se autorizó el servicio previsto y que el digest aprobado es el que
   inspeccionaste.

7. Tras el merge y con el worktree limpio, ejecuta primero el dry-run humano:

   ```bash
   ./scripts/deploy-ansible.sh \
     --playbook workloads \
     --check \
     --ask-become-pass
   ```

   El dry-run consulta el registro y muestra el digest aprobado. No descarga
   imágenes ni modifica Swarm. Si Docker Hub ya movió `latest`, fallará: vuelve
   al paso 1 y prepara una aprobación nueva; nunca despliegues el digest que
   aparezca por sorpresa.

8. Solo después de revisar ese resultado, una persona ejecuta el apply
   manual con los demás STOP gates satisfechos:

   ```bash
   ./scripts/deploy-ansible.sh \
     --playbook workloads \
     --confirm-production \
     --ask-become-pass
   ```

   El preflight descarga el digest aprobado, comprueba que la imagen local
   expone ese mismo digest y entrega exactamente esa referencia a Swarm.

9. Comprueba el servicio, sus réplicas, la ruta HTTPS, los logs y las alertas.
   Después repite el playbook: un segundo apply debe converger sin cambios.

## Qué no automatizar

No añadas un cron, webhook, watcher de Docker Hub ni un pipeline que ejecute
Ansible automáticamente. Un push externo no debe poder iniciar un despliegue.
La etiqueta solo sirve para detectar si el contenido aprobado sigue disponible;
la autorización es el digest versionado y revisado en Git.

Tampoco modifiques `config/services.yml` para esquivar el contrato. Ese
catálogo pertenece a la evidencia de migración. La excepción operativa existe
precisamente para mantener separados el historial y una actualización
revisada.

## Aplicar el patrón a otro servicio

No copies esta excepción sin diseño. Para añadir otro caso, sigue este orden:

1. Decide si realmente necesita una etiqueta mutable. La respuesta normal es
   no: un digest fijado es más sencillo y seguro.
2. Si hay una razón revisada, añade una entrada explícita que ligue servicio,
   componente, etiqueta permitida, digest de base y digest aprobado.
3. Amplía los validadores para que rechacen otra clave, otro repositorio, otro
   tag, un digest mal formado o una segunda entrada sin aprobación explícita.
4. Haz que el dry-run consulte el descriptor y compare el resultado con el
   digest versionado. El apply debe reutilizar exactamente esa comparación y
   verificar la identidad de la imagen local tras el pull.
5. Añade pruebas negativas para cada rechazo nuevo y actualiza la
   documentación antes de pedir revisión.

El principio es general: los datos que vienen de una red son observaciones,
no autorizaciones. La autorización debe vivir en un cambio revisado y el
despliegue debe comprobar que ambas cosas coinciden.

## Problemas frecuentes

<!-- markdownlint-disable MD013 -->

| Señal | Significado | Acción correcta |
| --- | --- | --- |
| El digest remoto no coincide | `latest` cambió tras la revisión | Detén el despliegue y repite la aprobación. |
| El marker de restore no coincide con el catálogo | Se alteró el baseline histórico durante una actualización | Restaura el baseline y actualiza solo `approved_runtime_reference`; nunca edites ni reemitas el marker para una actualización de imagen. |
| Falta `docker buildx` | Falta el plugin fijado por el host | Revisa el contrato de paquetes; no sustituyas el comando. |
| Falla el SLO del snapshot Ubuntu | La revisión de paquetes está vencida | Promueve un snapshot real tras verificar índices y pins. |
| Hay un marker en `/run/lock` | Una operación previa no cerró limpiamente | Sigue el recovery documentado y conserva evidencia. |
| El writer rechaza el worktree | El estado no está revisado/commiteado | Revisa y crea el commit; no uses bypasses. |

<!-- markdownlint-enable MD013 -->

## Checklist de una promoción segura

- [ ] El digest fue observado y revisado por una persona.
- [ ] `approved_runtime_reference` contiene exactamente ese digest.
- [ ] El diff no amplía el servicio, componente, repositorio ni etiqueta.
- [ ] Bootstrap, validación y lint pasan sin omitir compuertas.
- [ ] El dry-run muestra el mismo digest y no reporta cambios inesperados.
- [ ] El commit y la revisión están listos antes del apply manual.
- [ ] La verificación posterior confirma el servicio y un segundo apply
      converge.
