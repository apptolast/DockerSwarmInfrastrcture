#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
SAFETY_TOOL="${SCRIPT_DIR}/backup_safety.py"

if [[ "$#" -ne 3 ]]; then
  printf 'Uso: %s PAQUETE.gpg RECOVERY-KEY.txt DESTINO_VACIO\n' "$0" >&2
  exit 2
fi

package=$1
recovery_key=$2
destination=${3%/}

[[ -f "$package" ]] || {
  printf 'No existe el paquete: %s\n' "$package" >&2
  exit 1
}
[[ -f "$recovery_key" ]] || {
  printf 'No existe la clave: %s\n' "$recovery_key" >&2
  exit 1
}

for command_name in gpg zstd python3; do
  command -v "$command_name" >/dev/null 2>&1 || {
    printf 'Falta el comando requerido: %s\n' "$command_name" >&2
    exit 1
  }
done
[[ -f "$SAFETY_TOOL" ]] || {
  printf 'Falta la utilidad de seguridad: %s\n' "$SAFETY_TOOL" >&2
  exit 1
}
[[ -n "$destination" && "$destination" != "/" ]] || {
  printf 'Destino de extracción no permitido: %s\n' "$3" >&2
  exit 1
}
if [[ -L "$destination" ]]; then
  printf 'El destino no puede ser un enlace simbólico: %s\n' "$destination" >&2
  exit 1
fi
if [[ -e "$destination" && ! -d "$destination" ]]; then
  printf 'El destino debe ser un directorio: %s\n' "$destination" >&2
  exit 1
fi
if [[ -d "$destination" ]] \
  && find "$destination" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
  printf 'El destino debe estar vacío: %s\n' "$destination" >&2
  exit 1
fi

destination_parent=$(dirname -- "$destination")
destination_name=$(basename -- "$destination")
install -d -m 0700 "$destination_parent"
work_dir=$(mktemp -d \
  --tmpdir="$destination_parent" \
  ".${destination_name}.extracting.XXXXXX")
committed=0

cleanup() {
  local status=$?
  if [[ "$committed" -eq 0 && -d "$work_dir" ]]; then
    rm -rf -- "$work_dir"
  fi
  exit "$status"
}
trap cleanup EXIT

stage_name=$(
  gpg --batch --quiet --no-symkey-cache --pinentry-mode loopback \
    --passphrase-file "$recovery_key" \
    --decrypt "$package" \
    | zstd -dc \
    | python3 "$SAFETY_TOOL" safe-extract-tar-stream "$work_dir"
)

[[ "$stage_name" =~ ^apptolast-data-[0-9]{8}T[0-9]{6}Z$ ]] || {
  printf 'La raíz extraída no tiene el formato esperado.\n' >&2
  exit 1
}
stage="${work_dir}/${stage_name}"
[[ -d "$stage" && -f "$stage/SHA256SUMS" ]] || {
  printf 'El paquete no contiene SHA256SUMS en la raíz esperada.\n' >&2
  exit 1
}

python3 "$SAFETY_TOOL" verify-tree "$stage" "$stage/SHA256SUMS" >/dev/null

if [[ -d "$destination" ]]; then
  rmdir -- "$destination"
fi
mv -- "$work_dir" "$destination"
committed=1
trap - EXIT

printf 'OK: paquete extraído y checksums internos verificados en %s\n' \
  "$destination/$stage_name"
