# Componente de backup

`backupctl.py` es el único motor de copia y recuperación. Los wrappers de
`scripts/backup-*.sh` son puntos de entrada estables para systemd.

El controlador exige:

- ejecución como `root`;
- configuración `root` no escribible por grupo;
- tres credenciales restic/R2 y el escrow de autolock como ficheros regulares
  `root:root 0600`;
- URL R2 HTTPS sin credenciales;
- restic 0.19.1 con el SHA-256 revisado;
- Swarm mononodo activo, servicios exactos y todos los datasets de la
  allowlist;
- cinco bases lógicas (`n8n`, `vectors`, `rag`, `passbolt`, `shlink`) y las
  versiones fijadas de `pgvector`;
- un lock exclusivo común a copia, verificación, ensayo y restore.

No existe ningún modo que reemplace o borre datos vivos. `restore` solo acepta
un ID explícito y un directorio vacío, verifica hashes y, opcionalmente,
materializa los tar en otro directorio vacío.

La activación, procedimientos y recuperación completa se documentan en
[`docs/BACKUP_RECOVERY.md`](../docs/BACKUP_RECOVERY.md).
