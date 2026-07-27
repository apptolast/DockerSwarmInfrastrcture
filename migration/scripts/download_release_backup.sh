#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
SAFETY_TOOL="${SCRIPT_DIR}/backup_safety.py"

if [[ "$#" -ne 2 ]]; then
  printf 'Uso: %s TAG DIRECTORIO_PRIVADO\n' "$0" >&2
  exit 2
fi

tag=$1
destination=${2%/}
repository='apptolast/MigracionNetCup'

[[ "$tag" =~ ^backup-[0-9]{8}T[0-9]{6}Z$ ]] || {
  printf 'Tag inválido: %s\n' "$tag" >&2
  exit 1
}
for command_name in gh python3; do
  command -v "$command_name" >/dev/null 2>&1 || {
    printf 'Falta el comando requerido: %s\n' "$command_name" >&2
    exit 1
  }
done
[[ -f "$SAFETY_TOOL" ]] || {
  printf 'Falta la utilidad de seguridad: %s\n' "$SAFETY_TOOL" >&2
  exit 1
}

[[ ! -L "$destination" ]] || {
  printf 'El directorio no puede ser un enlace simbólico: %s\n' "$destination" >&2
  exit 1
}
install -d -m 0700 "$destination"
if find "$destination" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
  printf 'El directorio debe estar vacío: %s\n' "$destination" >&2
  exit 1
fi

gh release download "$tag" \
  --repo "$repository" \
  --dir "$destination"

python3 "$SAFETY_TOOL" validate-release "$destination" --tag "$tag" >/dev/null
package_name=$(
  python3 "$SAFETY_TOOL" reassemble-release "$destination"
)

printf 'OK: %s reconstruido y verificado en %s\n' \
  "$package_name" "$destination"
