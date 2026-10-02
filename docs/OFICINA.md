# La Oficina de agentes

La Oficina es el panel web del laboratorio de AX convertido en una oficina
de agentes de IA: un equipo de agentes con nombre, papel, modelo y forma de
trabajar, que reciben encargos sobre los repositorios de AppToLast, los
ejecutan uno a uno en un sandbox aislado de AX, entregan resultados, parches
y pull requests, y aprenden de cada trabajo con la aprobación de una
persona. Es la versión 1.0.0 de `images/ax-web`; este documento explica qué
hace y cómo se usa. El despliegue está en [AX_WEB.md](AX_WEB.md) y el
laboratorio en [AX.md](AX.md).

Se publica en `https://oficina.apptolast.com` y en
`https://ax.apptolast.com`, con el mismo usuario y contraseña (ver
[EDGE.md](EDGE.md), «Ruta de AX»).

## De dónde sale

- **El concepto** viene de
  [agent-office](https://github.com/AgentSystemLabs/agent-office), de
  WebDevCody (MIT): una oficina 3D donde los agentes se sientan en
  escritorios, con tableros de issues, PR y cola, balizas cuando necesitan a
  una persona y reuniones con patrones fijos. Ese proyecto ejecuta cada
  agente como un proceso local del servidor, sin aislamiento; aquí no se
  despliega tal cual porque daría una shell a los agentes sobre el servidor
  de producción. La Oficina toma sus ideas y las monta sobre AX.
- **La ejecución** es AX (Google Agent Executor) sobre Agent Substrate:
  cada trabajo corre en su propio sandbox gVisor, que se crea y se borra con
  él. AX no tiene cola, ni modelos, ni costes; la Oficina los añade.
- **El bucle de mejora** sigue lo que la investigación práctica sobre
  agentes que se auto-mejoran considera seguro: cada mejora es una propuesta
  que aprueba una persona, se mide y puede deshacerse.

## El equipo

Al arrancar por primera vez, la Oficina crea este equipo. Todo se puede
cambiar desde «Agentes»: nombre, papel, CLI, modelo, esfuerzo, modo,
turnos, tiempo máximo, prompt de sistema y herramientas prohibidas.

<!-- markdownlint-disable MD013 -->

| Agente | Papel | CLI y modelo | Esfuerzo | Modo |
| --- | --- | --- | --- | --- |
| 🏛️ Ada | Arquitecta | Claude · `opus` | alto | lectura |
| 🛠️ Linus | Desarrollador | Claude · `opus` | alto | completo |
| 🔍 Grace | Revisora de código | Claude · `opus` | muy alto (`xhigh`) | lectura |
| 🧪 Kent | QA y tests | Claude · `sonnet` | alto | completo |
| 🛡️ Hedy | Seguridad | Claude · `opus` | alto | lectura |
| 📚 Margaret | Documentación | Claude · `sonnet` | medio | completo |
| 🤖 Guido | Segunda opinión | Codex · el de su configuración | el del modelo | completo |
| ⚡ Haiku | Becario: preguntas rápidas | Claude · `haiku` | bajo | lectura |
| 🧭 Coach | Mejora continua del equipo | Claude · `opus` | alto | lectura |

<!-- markdownlint-enable MD013 -->

### Modelos y esfuerzo

Los catálogos vienen de las CLIs fijadas en la imagen `ax-agents`, leídos
en el servidor el 2026-10-02, no de memoria:

- **Claude Code 2.1.274**: alias `fable`, `opus`, `sonnet` y `haiku` (el
  modelo más reciente de cada familia) o un nombre completo
  (`claude-opus-5-5`, …); esfuerzo `low`, `medium`, `high`, `xhigh` o
  `max`; modelo de respaldo opcional.
- **Codex 0.156.1** (`codex debug models`): `gpt-6-astra`, `gpt-6-sol`,
  `gpt-6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` y `gpt-5.5`,
  cada uno con sus niveles de razonamiento (hasta `ultra` en algunos).

También se acepta cualquier otro identificador de modelo que la CLI
entienda («otro…»).

### Modos

- **Lectura**: Claude corre con `--restricted`: sin herramientas que
  ejecuten órdenes o código y sin WebFetch. Analiza, planifica y revisa.
- **Completo**: Claude corre con `--permission-mode bypassPermissions`
  (dentro del sandbox, con `IS_SANDBOX=1`, porque es root): edita,
  ejecuta tests y builds, usa git. El límite es el sandbox gVisor.
- **Codex** no puede aplicar su propio aislamiento dentro de gVisor, así
  que en lectura solo se le pide que no modifique ficheros.

Por decisión del propietario del 2026-10-02, los agentes tienen el máximo
de autonomía dentro del sandbox.

## Encargos

Un encargo (un «trabajo») es un agente, un proyecto, un tipo, una
instrucción y, opcionalmente, ajustes que solo valen para él. Con token de
GitHub se puede partir de una issue o PR del proyecto: su título, su texto
(y el diff de una PR) llegan al agente como datos no confiables, nunca como
parte de la instrucción.

| Tipo | Qué entrega |
| --- | --- |
| `pregunta` | Una respuesta citando ficheros y líneas |
| `plan` | Un plan numerado con ficheros, riesgos y verificación |
| `cambio` | Cambios en el código, que la Oficina recoge como parche |
| `revision` | Hallazgos con gravedad y un `VEREDICTO: APROBADO` o `CAMBIOS` |

Cada respuesta termina con «Resumen» y «Lecciones», que la Oficina lee.

### La cola

El laboratorio tiene un solo worker gVisor (1 536 MiB): **un trabajo a la
vez**. Los demás esperan en la cola, por prioridad (alta, normal, baja) y
antigüedad. Si alguien lanza `ax-tarea` en el host, la Oficina espera a que
termine. La cola se puede pausar.

Mientras un trabajo corre se ve en directo: cada herramienta que usa el
agente (órdenes, ficheros que lee o edita, búsquedas), su texto y, al
final, el coste, los tokens, los turnos y la duración que informa la CLI.

### Parches y pull requests

En un `cambio`, al terminar el agente, la Oficina ejecuta dentro del
sandbox, sin ninguna credencial, `git add -A` y `git diff` contra el commit
clonado, y guarda el parche y el contenido de los ficheros cambiados. Desde
el trabajo se puede:

- ver el diff y descargar el `.patch`;
- pedir a otro agente que lo revise o lo corrija (el siguiente trabajo
  arranca con el parche aplicado);
- **crear una pull request en borrador**, si hay token de GitHub: la Oficina
  enseña antes el título y el texto exactos (editables), y después crea la
  rama `oficina/<trabajo>`, el commit y la PR con la API de GitHub. El token
  **nunca entra en el sandbox**. Las menciones `@usuario` del texto del
  agente se neutralizan, y no se publica nada que toque `.github/` (la CI,
  que corre con los secretos del repositorio) ni `.gitmodules`: esos
  cambios se aplican a mano tras revisarlos;
- comentar el resumen en una issue o PR, también con vista previa.

## Proyectos

La configuración revisada siembra los proyectos con vínculo demostrado entre
repositorio, servicio y dominio (`config/ax-lab.yml`, `web.office.projects`).
Desde «Proyectos → Importar desde GitHub» se añaden más repositorios
públicos de una organización o usuario; los privados no, porque AX los
clona sin credenciales. Cada proyecto tiene notas y su memoria de
lecciones.

## Equipos

Un equipo es una plantilla de varios pasos, cada uno un trabajo de la cola:

- **Equipo: plan → código → revisión.** Ada planifica, Linus implementa,
  Grace revisa; si pide cambios, Linus corrige y Grace vuelve a revisar,
  hasta el máximo de iteraciones (2 por defecto).
- **Panel de revisión.** Corrección, seguridad y tests revisan el mismo
  parche o PR desde su ángulo; una síntesis da el veredicto.
- **Debate de diseño.** Tres propuestas independientes y una decisión.
- **Rojo / azul.** Hedy busca vulnerabilidades, Linus las corrige, Hedy
  verifica; se repite mientras queden.
- **Auto-mejora de la Oficina.** El equipo trabaja sobre el propio código de
  la Oficina (`images/ax-web` de este repositorio) y propone mejoras como PR
  en borrador. Ningún cambio llega a producción sin revisión humana, la CI
  reproducible y el despliegue del playbook `ax-lab`.

## Turnos

Un turno lanza un trabajo o un equipo los días de la semana y a la hora UTC
elegidos (por ejemplo, una auditoría de seguridad los lunes a las 06:00).

## Evolución: cómo aprende el equipo

1. **Lecciones.** Las «Lecciones» de cada trabajo se convierten en
   propuestas. Las que apruebas pasan a la **memoria del proyecto**, que se
   incluye en los siguientes encargos sobre él (las más recientes, hasta 12).
2. **Valoraciones.** 👍/👎 y una nota en cada trabajo.
3. **Coach.** «Pedir mejora al Coach» le da al Coach el prompt actual de un
   agente, sus métricas y sus últimos trabajos (resultados, veredictos,
   valoraciones). Propone un prompt nuevo; si lo apruebas, el agente sube de
   versión y la anterior queda en su historial, de donde se puede recuperar.
4. **Métricas.** Por agente y versión, y por modelo: trabajos, éxito,
   valoraciones, aprobación de sus cambios en revisión, nota en el banco de
   pruebas, coste y duración medios.
5. **Banco de pruebas.** Encargos fijos con criterios; un juez puntúa de 0
   a 10 la respuesta de un agente. Sirve para comparar versiones o modelos
   antes de quedarse con uno.

Nada de esto cambia el comportamiento sin una persona: las lecciones y los
prompts nuevos son propuestas hasta que se aprueban (la política de
lecciones se puede cambiar en «Ajustes»).

## Credenciales

Viven en el host, `root` y `0600`, bajo `/etc/dockerswarm/ax/`, y llegan al
panel como Secrets de Kubernetes. Nunca se muestran ni se registran.

| Credencial | Fichero en el host | Cómo llega |
| --- | --- | --- |
| Claude Code (OAuth) | `claude-oauth-token` | `ax-web-bootstrap.sh k8s` |
| Codex (sesión ChatGPT) | `codex/auth.json` | `ax-web-bootstrap.sh office` |
| GitHub | `github-token` | `ax-web-bootstrap.sh office` |

- El token de GitHub es un token por línea (`token`) o una línea por
  organización (`organizacion=token`). Se lee en cada uso.
- La sesión de Codex usa un refresh token de un solo uso: cuando Codex la
  renueva, la Oficina guarda la nueva en su volumen y la usa desde entonces,
  solo si es de la misma cuenta (mismo `sub` y cuenta en los JWT) y con un
  formato válido. No conviene usar además `ax-tarea … codex` en el host con
  la misma sesión. «Ajustes → Olvidar la sesión de Codex guardada» vuelve a
  la del Secret.

## Seguridad

- Cada trabajo corre en un sandbox gVisor nuevo que se borra al terminar; la
  credencial del agente solo viaja en el entorno de su proceso, nunca en la
  definición de la tarea de AX, en el navegador ni en los logs.
- **Riesgo aceptado:** en modo completo el agente tiene su credencial (Claude
  o Codex) en su entorno y la salida a Internet del sandbox no se filtra (ver
  [AX.md](AX.md), «Límites conocidos»). Un repositorio o un texto hostil podría
  intentar que el agente la filtre. Los textos de issues y PR se le pasan
  marcados como datos no confiables.
- El panel solo acepta TLS 1.3 con el certificado cliente de Traefik, que
  exige usuario y contraseña, limita las peticiones y deja que CrowdSec
  bloquee las IP que fallan el login.

## Límites conocidos

- Un trabajo a la vez (un worker). Más workers necesitan memoria del host.
- Solo repositorios públicos de `github.com`: AX clona sin credenciales.
- AX sigue fijado en `f009cc8`; el upstream actual cambió su API (tareas
  inmutables, sin controlador, sin salida a Internet por defecto) y
  actualizarlo es otro cambio.
- El estado de la Oficina vive en un volumen del nodo kind: recrear el
  clúster lo borra. «Ajustes → Exportar» descarga una copia. Los trabajos
  terminados más antiguos se borran pasados 3 000 trabajos o 768 MiB.
- Un paso de equipo sigue con el parche del anterior aplicado sobre la punta
  de la rama: si la rama cambió entretanto y el parche ya no aplica, el
  paso falla de forma visible.

## Investigación

Las fuentes de las decisiones (agent-office, AX upstream y su
documentación, Claude Code y Codex, y el panorama de oficinas de agentes y
de auto-mejora) están citadas en la descripción del PR que introdujo la
Oficina.
