# Adopción de TemplateSSDUncleBob en este repositorio

> Nota de nombre de fichero: el resto de `docs/` usa MAYÚSCULAS
> (`ARCHITECTURE.md`, `OPERATIONS.md`...). Este fichero usa minúsculas y
> guiones a propósito, con el nombre exacto que se pidió para esta
> adopción. Si se prefiere consistencia con el resto de `docs/`, renombrar
> a `docs/TEMPLATESSD_ADOPTION.md` es un cambio de una línea que no toca
> contenido.

Este documento explica, sin adornos, qué se ha adoptado de
`TemplateSSDUncleBob` (el arnés SDD estilo Uncle Bob de Cénit Digital) en
este repositorio, qué correspondencia real tiene cada pieza con lo que este
repositorio ya tenía, y qué partes **no** encajan limpiamente y por qué. El
criterio en todo momento ha sido: si algo no tiene equivalente honesto
aquí, decirlo, no inventarlo.

## 0. Límite de seguridad (no negociable)

Esta adopción **no crea ni activa ningún workflow de autonomía**. No hay
ningún `.github/workflows/autonomous-evolve.yml` ni equivalente en este
cambio. `terraform apply` y `ansible-playbook` contra el host/clúster real
siguen siendo, hoy y siempre, una acción 100% manual de Pablo.

Si en el futuro se quisiera adaptar el patrón "bot que abre PR, nunca
fusiona" de `docs/autonomous.md` (la plantilla), su alcance tendría que
quedar limitado, sin excepción, a proponer documentación o planificación
— como mucho un `terraform plan` de solo lectura embebido en la
descripción del PR. Nunca un `apply`, nunca un cambio que mute el VPS o el
clúster real, nunca fusión ni auto-merge. Eso sería una decisión nueva y
explícita de Pablo, con su propio PR revisado por `CODEOWNERS` — no un
efecto colateral de esta tarea de documentación.

## 1. Qué se adopta y qué se deja exactamente igual

**Se añade** (los cinco artefactos pedidos, más las integraciones mínimas
que los hacen funcionar de verdad):

- `AGENTS.md` — mapa de navegación, nuevo.
- `CHECKPOINTS.md` — checklist de estado final, nuevo.
- `harness.config.json` — comandos reales de este stack, nuevo, con
  alcance deliberadamente acotado (§4).
- `scripts/sync-memoria.sh` y `scripts/sync-memoria.ps1` — copiados de la
  plantilla.
- Este documento.
- Una sección nueva «Session startup» en `CLAUDE.md` que integra el paso
  2bis (sync de memoria) en el protocolo de arranque ya existente, sin
  tocar ninguna otra sección de ese fichero.
- Una entrada en `.gitignore` para `.memoria-cache/` — sin ella, el propio
  paso 2bis dejaría el árbol de trabajo sucio y rompería el hook `Stop` de
  `.claude/settings.json` y la disciplina de "los writers rechazan un
  worktree sucio" que este repositorio ya exige. No añadirla habría hecho
  la integración deshonesta en la práctica, aunque el documento dijera lo
  contrario.
- Dos líneas nuevas en el `allow` de `.claude/settings.json` para que
  `./scripts/sync-memoria.sh` (y su gemelo `pwsh`) no interrumpan cada
  sesión pidiendo permiso — es un `git clone` de solo lectura contra un
  repo fijo, con el mismo perfil de riesgo que las entradas de solo
  lectura que ya están en esa lista.
- Una entrada en `CHANGELOG.md` bajo `[Unreleased] / Added`, porque
  `CLAUDE.md` y el propio `judge.md` ya exigen esa entrada para cualquier
  cambio, y esta adopción no es una excepción.

**Se deja exactamente igual, sin tocar un carácter**:

- Los siete subagentes de `.claude/agents/` (`ansible-operator`,
  `terraform-operator`, `iac-validator`, `judge`, `security-reviewer`,
  `guardrail-adversary`, `mentor`).
- `.github/workflows/validate.yml` y
  `.github/workflows/guard-sensitive-paths.yml`.
- `.github/CODEOWNERS`, `.github/pull_request_template.md`,
  `.github/actionlint.yaml`, `.github/dependabot.yml`.
- `.gitleaks.toml`, `.ansible-lint`, `.hadolint.yaml`, `ruff.toml`.
- Las nueve puertas STOP de `CLAUDE.md` (contenido, numeración, fechas):
  no se revalida ni se toca su redacción, solo se añade una sección nueva
  en otro punto del fichero.
- Todo el código de infraestructura: `ansible/`, `infra/terraform/`,
  `stacks/`, `config/`, `migration/`, `backup/`, y los scripts existentes
  bajo `scripts/`.
- `README.md`: se deja fuera de esta adopción a propósito. Ya no lista
  todos los ficheros de `docs/` (le faltan `DEPLOYMENT_STATUS.md` y
  `KNOWN_ISSUES.md` desde antes de esta tarea), y añadir una entrada más a
  una lista ya incompleta no es lo que se pidió; el mapa completo y
  actualizado vive en `AGENTS.md`.

## 2. Correspondencia: las puertas STOP existentes ↔ la puerta humana del arnés

La plantilla tiene **una sola** puerta humana, situada en un punto muy
concreto: el humano aprueba el contrato Gherkin (`features/<name>.feature`)
**antes** de que exista una sola línea de código de producción. Es el
punto de máximo apalancamiento porque ocurre antes de tallar nada.

Este repositorio no tiene ese punto porque no tiene Gherkin, y esta
adopción no lo inventa. Lo que sí tiene, desde antes de esta tarea, son
puertas humanas en otros puntos del ciclo de vida, ya más mecánicas que la
propia puerta de la plantilla:

- **Terraform.** El punto de máximo apalancamiento es el plan firmado:
  `scripts/plan-terraform.sh` genera un plan inspeccionable y firmado
  (`--signing-key`), y `scripts/apply-terraform.sh` verifica esa firma,
  el commit, la identidad del backend, el lineage/serial del state y el
  proof de locking **antes de hacer nada** — no es una convención, es un
  wrapper que se niega a continuar si algo no cuadra.
- **Ansible.** El punto equivalente es el `--check --diff` revisado en la
  misma sesión antes del `--confirm-production`
  (`.claude/agents/ansible-operator.md`, secuencia obligatoria
  check-then-apply).
- **Las nueve puertas STOP numeradas de `CLAUDE.md`.** Son de una
  naturaleza distinta a las dos anteriores, y distinta también de la
  puerta Gherkin de la plantilla: no son "el humano aprueba el contrato
  antes de programar", son "el humano aporta una credencial, una firma o
  una decisión de negocio externa que no puede fabricarse ni inferirse
  del repositorio" — por ejemplo, registrar el hash de una credencial R2
  real, aceptar el riesgo de `online-mode=false` en Minecraft, o dar la
  aceptación OAuth de n8n. Ninguna de las nueve se cierra con una revisión
  de código: todas exigen una acción del propietario fuera de este
  repositorio (o una evidencia que solo él puede producir).

En resumen: donde la plantilla tiene una puerta (antes del código), este
dominio tiene varias, en puntos distintos y con artefactos distintos
(plan firmado, diff revisado, evidencia externa registrada) — pero la
misma idea de fondo se conserva: nada mutante se ejecuta sin que un
humano haya revisado el contrato exacto de lo que va a pasar. `C6` de
`CHECKPOINTS.md` deja esto por escrito con más detalle.

## 3. Correspondencia: subagentes existentes ↔ roles del arnés

<!-- markdownlint-disable MD013 -->

| Rol de la plantilla | Agente de este repo | Correspondencia |
| --- | --- | --- |
| `craftsman_lead` (orquesta, nunca implementa) | Ninguno dedicado | **No hay equivalente 1:1.** Aquí orquesta `CLAUDE.md` más Pablo decidiendo qué operador o revisor invocar; ningún agente de este repo se llama "lead" porque no hay un pipeline de features que orquestar de esa forma. Si se quisiera nombrar el rol, hoy lo cumple el propio Pablo. |
| `spec_partner` (debate la spec con el humano) | Ninguno | **No aplica, dicho explícitamente.** La "spec" de este repositorio no se conversa por feature: ya vive, versionada y declarativa, repartida entre `CLAUDE.md`, `docs/*.md` y los contratos de `config/*.yml`. Cambios grandes de contrato (nuevo servicio, nuevo puerto, cambio de DNS) sí se debaten y se aprueban, pero por PR + `CODEOWNERS` + los propios docs, no por un agente conversacional dedicado. |
| `gherkin_author` (destila Gherkin) | Ninguno | **No aplica, dicho explícitamente.** No hay lenguaje Gherkin en este dominio y esta adopción no lo introduce. El contrato ejecutable más parecido ya existe y es más estricto: los propios validadores fail-closed de `scripts/validate-*.py` y los ficheros `config/*.yml`, verificados por `assert` de Ansible y por los validadores, no por escenarios `Given/When/Then`. |
| `tdd_craftsman` (Rojo-Verde-Refactor, un test a la vez) | `ansible-operator` / `terraform-operator`, parcialmente | **Analogía parcial, no literal.** Para Ansible/Terraform el ciclo real no es "test rojo, código, verde": es "dry-run (`--check`/plan), revisar, aplicar de verdad". Para el código Python de `scripts/*.py` la analogía sí es razonablemente fiel: `tests/` (17 suites) y `migration/tests/` (9 suites) ya cultivan el hábito de test negativo explícito antes de este cambio (`tests/test_ufw_contract.py`, `tests/test_terraform_safety.py`). |
| `judge` (revisor, no edita, veredicto en fichero) | `judge` | **Casi 1:1**, incluso el nombre coincide. El `judge` de este repo va más allá que el de la plantilla: además de cobertura y disciplina, verifica el idiom TOCTOU, la ausencia de flags de bypass, la entrada en `CHANGELOG.md`, las nueve puertas STOP y las nueve casillas de `pull_request_template.md`. |
| `mutation_tester` (mide que los tests muerden) | `guardrail-adversary` | **Equivalente conceptual, no literal.** Ver §4 de este documento y `CHECKPOINTS.md` C7: no hay puntuación de mutación, hay un veredicto `PASS`/`SUPERVIVIENTES(N)` sobre si cada gate tocado rechaza de verdad lo que dice rechazar. El propio `guardrail-adversary.md` ya se autodescribe como "no soy un mutation tester" y explica por qué, de forma independiente a esta adopción. |
| `security_reviewer` (opcional, bajo demanda) | `security-reviewer` | **Coincide el nombre, cambia la criticidad.** En la plantilla es una puerta opcional que el orquestador convoca si aporta valor. Aquí es **obligatoria** para todo diff que toque `ansible/`, `config/`, `scripts/`, `stacks/`, `infra/terraform/`, `backup/`, `migration/` o `.claude/` — el propio fichero lo declara así. Esta adopción no relaja ese criterio; lo señala como una decisión ya tomada, más estricta que la plantilla. |
| `a11y_seo_auditor` (opcional, UI web) | Ninguno | **No aplica, dicho explícitamente**, igual que la propia plantilla admite ("bórralo si tu proyecto no tiene UI web"). Este repositorio no tiene UI web propia que auditar: Traefik enruta aplicaciones de terceros cuyo código no vive aquí. |
| `mentor` (enseña el porqué, solo lectura, bajo demanda) | `mentor` | **Casi 1:1.** Mismo nombre, mismo espíritu, mismo formato de respuesta ("Concept/Concepto: [nombre]"), adaptado al hecho de cero alta disponibilidad en vez de a un dominio de aplicación genérico. |
| _(sin rol equivalente en la plantilla)_ | `iac-validator` | **Ampliación, no carencia.** La plantilla no separa "quien corre la validación" de "quien implementa" porque tiene un único `tdd_craftsman`. Aquí, por tratarse de infraestructura real con dos stacks (Terraform y Ansible) más un mutex host-global con markers y recuperación propia, ese trabajo se separó en su propio agente. |
| _(sin rol equivalente en la plantilla)_ | `terraform-operator` | **Ampliación, no carencia**, por el mismo motivo: la plantilla es agnóstica a un solo `tdd_craftsman`; aquí Terraform tiene su propia disciplina check/plan-then-apply lo bastante distinta de Ansible como para merecer un operador dedicado. |

<!-- markdownlint-enable MD013 -->

## 4. Por qué no hay prueba de mutación aquí (y qué se adoptó en su lugar)

Esto ya está registrado, de forma independiente a esta adopción, en
`.claude/agents/guardrail-adversary.md` (creado en el commit `8a76620`,
antes de esta tarea). Se resume aquí porque `CHECKPOINTS.md` y
`harness.config.json` lo citan como la razón de no rellenar C7 ni
`mutation.threshold`:

- La suite es `unittest` puro
  (`python -m unittest discover -s tests -v`, invocado por
  `scripts/validate-iac.sh`). No hay `pytest` en el repo.
- El entorno exige Python 3.14 de forma dura
  (`scripts/bootstrap-tooling.sh`, `.github/workflows/validate.yml`).
- Una parte de los tests son **puramente estáticos**: no ejecutan lógica
  de producción, cargan YAML o plantillas renderizadas y afirman sobre su
  estructura. Mutar código Python no mueve ni una de esas aserciones, así
  que cualquier puntuación de mutación saldría falseada hacia abajo y
  castigaría exactamente a los tests que más importan.

La pregunta análoga en infraestructura no es "¿muerden mis tests?" sino
"¿rechaza mi gate el input malo?", y este repositorio ya lo practica y lo
nombra (`tests/test_ufw_contract.py` se autodescribe como "Negative and
positive tests"). El sustituto adoptado es exactamente esa disciplina,
extendida por `guardrail-adversary` a cualquier gate que un cambio toque,
reportada como `PASS` o `SUPERVIVIENTES(N)` — nunca como un porcentaje.

## 5. El motor del arnés: por qué no se introduce aquí

Esta adopción **no** trae `.harness/harness.mjs`, `bin/harness(.ps1)`,
`init.sh`/`init.ps1`, `harness.schema.json`, `feature_list.json`,
`project-spec.md`, `features/`, `progress/`, ni un `src/`/`tests/` al
estilo plantilla. Tres razones concretas:

1. El encargo de esta tarea (paso 3) enumera cinco artefactos concretos;
   el motor y sus ficheros de acompañamiento no están en esa lista.
2. Este repositorio ya tiene una cadena de verificación real, más
   estricta y más específica de dominio que el motor genérico:
   `scripts/validate-iac.sh` y `scripts/lint.sh`, ya conectados a CI
   (`.github/workflows/validate.yml`). Redirigir esa cadena a través de un
   motor Node genérico la duplicaría sin necesidad, o peor, la
   debilitaría si el motor genérico no supiera expresar alguno de sus
   invariantes (el idiom TOCTOU, el mutex host-global, los STOP gates).
3. `tests/` y `migration/tests/` ya existen con un significado propio
   (tests adversariales de infraestructura, no un ciclo TDD de feature
   única) que no debe confundirse con el `tests/` de un arnés de
   plantilla nuevo.

`harness.config.json` se incluye de todas formas porque el paso 3 lo pide
explícitamente, con un alcance limitado y declarado en el propio fichero:
es un artefacto de consistencia entre repositorios de Cénit Digital
(mismo formato que `docs/configuration.md` de la plantilla), no un punto
de entrada ejecutable. Quien quiera los comandos reales de este stack los
lee ahí sin tener que instalar Node ni entender el motor de la plantilla.

## 6. Honestidad sobre lo que no se pudo verificar en este entorno

Esta tarea se ejecutó en un sandbox sin daemon Docker (`docker info`
falla con "no such file or directory" sobre el socket) y sin el
entorno completo de este repositorio (`.venv`, Terraform, Ansible,
colecciones fijadas). Eso significa que **no se pudo ejecutar**
`./scripts/bootstrap-tooling.sh` ni `./scripts/validate-iac.sh` de
verdad contra este cambio.

Lo que sí se verificó de verdad, con herramientas reales, no de forma
especulativa:

- `shellcheck 0.9.0 --severity=style --external-sources` sobre
  `scripts/sync-memoria.sh` en su ruta final: **0 avisos**. No es la
  versión exacta que fija `scripts/lint.sh` por digest, pero es una
  candidata cercana instalada desde los repositorios de Ubuntu para esta
  verificación.
- `bash -n scripts/sync-memoria.sh`: sintaxis válida.
- `markdownlint-cli2@0.23.1` (la versión exacta que fija
  `scripts/lint.sh`) sobre todos los ficheros `.md` nuevos y sobre
  `CLAUDE.md` modificado: cero incidencias, corregido de forma iterativa
  hasta quedar en cero.
- `python3 -m json.tool` tras cada edición sobre `harness.config.json` y
  sobre `.claude/settings.json`: JSON válido en ambos.

Antes de fusionar esta rama, Pablo debería correr la secuencia completa
real:

```bash
./scripts/bootstrap-tooling.sh
./scripts/validate-iac.sh
./scripts/lint.sh
```

Decirlo así, en vez de afirmar sin más que "todo pasa", es exactamente la
disciplina que este mismo repositorio exige
(`docs/verification.md` de la propia plantilla: "el agente no dice
funciona, lo demuestra"; `judge.md` de este repo: "un veredicto que
oculta una ejecución roja es peor que no revisar").
