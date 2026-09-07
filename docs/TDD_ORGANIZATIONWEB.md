# Evidencia local: stack independiente OrganizationWeb

Trabajo en rama aislada `codex/organizationweb-deploy`; ninguna operación
remota ni modificación del catálogo legacy. Runtime Linux en Docker Desktop,
Python 3.14.7 y tooling bloqueado del proyecto. Las pruebas operativas usan
contenedores propios sin puertos publicados y secretos aleatorios temporales.

## Prerrequisitos

Bootstrap EXIT 0 `4576dd`. Promoción legítima de snapshot documentada en
SNAPSHOT_20260906, commits `c1f0a5e` y `2045dd2` revisados por root. Baseline
completo EXIT 0 `e86562`, lint EXIT 0 `097ef5`. Los fallos anteriores de runtime
se conservaron: dependencias, passwd, /tmp OverlayFS y tmpfs noexec. No se
relajó ningún guard, requisito de Python, firma APT ni SLO.

## Ciclos del contrato y runtime

| Oráculo | RED | GREEN | Límite |
| --- | --- | --- | --- |
| Render cuatro servicios/redes/secrets | 113fc3 | 405078 | Sin deploy |
| Variable real RABBITMQ_VHOST | 7b0985 | 1298cb | Configuración |
| Formato Docker stack | Inicialmente GREEN | 215ef4 | No acredita salud |
| Puerto 8080 web | 42e425 | 18ba65 | Hallazgo de root |
| APP_PUBLIC_ORIGIN HTTPS | 326e6b | 84b08e | Configuración de sesión |
| Health web, user101/hardening | Inicialmente GREEN | c562ec | Sin proxy/TLS |
| Presupuesto explícito alternativo | 1056e1 | e026d5 | v1 intacto |
| Servicio extra no contabilizado | d8e77b | 097864 | Rechazo |
| Omitir edge aunque cuadre la suma | 5a4aca | 9a1ad4 | Rechazo |
| Coexistencia simétrica de perfiles | 9749b8 | d93f65 | Sin apply |
| Identidad nombre/stack cruzada | 39649c | 2207b9 | Rechazo |
| CLI rechaza stack fuera de perfil | 6d7e44 | 7b2a47 | Antes de mutación |
| Preflight común check/apply | 14d8d1 | 002375 | Wiring bajo lock |
| Catálogo fuera de raíz independiente | 8e2dd7 | fad47f | Rechazo |
| Playbook aislado y orden de roles | 01eafc | 953d4a | Contrato estático |
| Configs vacío para validador heredado | e40329 | a77a7d | Compatibilidad |
| Dispatcher/identidad/hash contratos | 516370 | 1a0ba5 | Allowlist exacta |
| Edge adicional conserva ocho legacy | 3fc642 | 54ad20 | Render real Ansible |
| Mapas edge separados | Refactor sin cambio visible | d5f7f1 | Legacy sigue 8 |
| OCI coincide con release | Inicialmente GREEN | 51bc63 | Dos imágenes |
| Guard OCI en apply | 863b9a | 215c48 | Contrato estático |
| Guard nodo único local | 2736a1 | c3266e | Sin consultar servidor |
| PG70/ALL/0700 + SQL/reinicio | Inicialmente GREEN | 1cedd8 | Docker local |
| Rabbit100/capDropALL/0700 + reinicio | e2ee7c | cb641f | check_running |
| Python sin privilegios PG/Rabbit | 777e8e | 896aed | Dos servicios reales |
| Gate integra nuevos validadores | 6cec3a | adcdf9 | Sin bypass |
| Padres y destinos /srv+/opt nofollow | 63a40d | 014a06 | Contrato estático |

La primera suma nominal omitió por error 64 MiB de reserva de edge; `ecdcf2`
la rechazó. Se corrigió el fixture a 6784 MiB, sin reducir reservas. El comando
`739313` apuntó a un nombre de contenedor inexistente: no ejecutó un test y no
cuenta como RED. `c2f7ff` encontró que PyYAML no entiende el tag `!unsafe` en el
fixture; se usó el patrón raw de Jinja ya existente, sin omitir el oráculo.

El healthcheck Rabbit inicial `ping` sólo probaba Erlang; la app aún no estaba
lista y list_vhosts fallaba. El mismo caso pasa con `check_running`, crea un
vhost y lo recupera después de reiniciar. PostgreSQL crea una fila y la lee
tras reinicio. No se modifican datos de otros contenedores.

Las primeras pruebas root no acreditaban portabilidad de `os.chown` en CI.
El RED sin privilegios demostró EPERM. La preparación y retirada usa ahora un
helper Docker efímero, red none y sólo el mount del fixture propio verificado
sin enlaces. Python no exige sudo; se verificaron UID1000 y socket Docker
permitido. El helper no monta /srv ni /opt del host.

Regresión focal de cuatro suites: 56/56, EXIT 0 `256a90`, ejecutada como UID1000.
Incluye las suites heredadas de capacidad y operation lock. Ansible-lint
production configurado EXIT 0 `f62556`. Primer intento había omitido
ANSIBLE_CONFIG y dos variables no seguían el prefijo del role; ambos corregidos.

## Gates finales en curso

`validate-iac` inicial del paquete `7830b1` encontró sólo líneas YAML largas;
se formatearon. El siguiente `57204d` llegó a visudo pero el UID numérico aún
no tenía registro passwd. Se creó el usuario local del runtime y el foco
visudo pasó (`ba7efd`). También se incorporaron los dos nuevos catálogos al
listado explícito de yamllint. Digests preservados mediante continuación de
string YAML, siguiendo la convención existente.

La sesión 72525 terminó EXIT 1 (`35599f`): 277/278 casos de la suite principal
pasaron, incluidos los 21 nuevos. El único fallo fue el fixture histórico
de authorized_keys, cuyo propietario esperado es 1001:1001, frente al
usuario 1000 del runtime. No se modificó el guard ni el fixture.

Se preparó usuario 1001:1001 con acceso al socket Docker existente y al clon
dedicado. El foco histórico pasó 12/12 (`313c2b`); el preflight `6a838d`
confirmó escritura en .build/.tools/.venv y los ejecutables requeridos.
Nuevo pase completo: sesión 80163, log `organizationweb-validation-uid1001.log`.
Terminó EXIT 0 (`c151ca`): 278/278 tests principales, 93/93 de migración,
Terraform, Ansible, contratos y Traefik verdes. Lint `b83ab8` encontró cinco
líneas largas sólo en los dos documentos nuevos; se ajustó su formato.
Lint final EXIT 0 (`9b6d3b`), incluido escaneo de secretos y diff-check.
No se declara deployment listo sin revisión y controles remotos.
Logs anteriores se conservan
en el directorio hermano `deployment-preparation`.

Seguridad independiente: revisión estática de A favorable, con límites
operativos explícitos. Tras formato cambian hashes de los tasks, por lo que
el manifiesto final debe ratificarse. Datos, secrets, TLS real y convergencia
del host siguen siendo evidencia del operador posterior al check/apply.
