# Revisión del despliegue inicial de OrganizationWeb

2026-09-07. Aprobado para commit, CI y ensayo remoto; la aplicación real
requiere revisar ese ensayo antes de ejecutarla. No acredita TLS ni tráfico.

El catálogo publica el MVP18 a5d1586 mediante cuatro digests inmutables.
El nuevo stack mantiene datos y secrets propios. Los ocho servicios web
legacy conservan sus redes y rutas; el edge compartido añade una ruta.
La revisión de seguridad exige tipos, propietarios y permisos de rutas,
procedencia de secrets, imágenes exactas y el lock operativo existente.

Se contrastaron los 28 hashes del corte final sin diferencias. La validación
local Linux UID1001 pasó 278 pruebas principales y 93 de migración; lint
terminó correctamente. El escaneo adicional del historial real completo
pasó con 71 commits y configuración Gitleaks fijada, sin hallazgos.
El lint portable anterior sólo contenía un commit y no se confunde con él.

La prueba operacional de PostgreSQL restauró un dump custom en una base
vacía. Un proceso API nuevo recuperó los mismos proyectos, tareas, reservas,
recibos de inicio/cierre e historial. Usó las imágenes publicadas y datos
sintéticos; no acredita backup automático, RabbitMQ ni copia externa.
La instalación crea estado. La ausencia de datos anteriores debe verificarse
antes de justificar que no hay copia previa de esta aplicación que realizar.

Evidencia conservada fuera de Git:

- organizationweb-validation-uid1001.log: EXIT 0.
- organizationweb-lint-final.log: EXIT 0.
- organizationweb-full-history-gitleaks.log: EXIT 0, 71 commits.
- organizationweb-pg-restore-attempt6.log: EXIT 0.
- organizationweb-pg-restore-report.md: detalle y límites del ensayo.

Siguen pendientes CI del commit, check remoto, aplicación, TLS, login,
lectura/escritura, publicación de eventos, comprobación de rutas legacy y
convergencia. El rollback inicial retira sólo el nuevo stack y conserva
sus datos; una reversión del edge necesita su propio cambio revisado.
Los gates de escrow, backup externo y migración legacy no quedan cerrados.
