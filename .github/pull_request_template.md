# Cambio de plataforma

## Objetivo

<!-- Qué problema resuelve y qué queda deliberadamente fuera. -->

## Riesgo y rollback

<!-- Blast radius, indisponibilidad posible y procedimiento de vuelta atrás. -->

## Evidencia

- [ ] `./scripts/validate-iac.sh`
- [ ] `./scripts/lint.sh`
- [ ] No contiene secretos, state, planes ni inventarios sensibles
- [ ] El changelog y la versión se actualizaron o se justificó `N/A`
- [ ] Se documentaron cambios de propiedad entre repositorios
- [ ] Se revisó un plan Terraform completo o se marcó `N/A`
- [ ] Se ensayó Ansible con `--check` o se justificó `N/A`
- [ ] Se definieron validación posterior y disparadores de rollback
- [ ] El backup/restore requerido está probado o el cambio no toca estado

## Aplicación

<!-- Ventana, responsable, commit/tag exacto y orden de ejecución. -->
