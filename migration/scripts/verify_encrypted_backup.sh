#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
SAFETY_TOOL="${SCRIPT_DIR}/backup_safety.py"

if [[ "$#" -ne 3 ]]; then
  printf 'Uso: %s PAQUETE.gpg RECOVERY-KEY.txt PAQUETE.gpg.sha256\n' "$0" >&2
  exit 2
fi

package=$1
recovery_key=$2
checksum=$3

for file in "$package" "$recovery_key" "$checksum"; do
  [[ -f "$file" ]] || {
    printf 'No existe: %s\n' "$file" >&2
    exit 1
  }
done

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

python3 "$SAFETY_TOOL" \
  verify-package-checksum "$package" "$checksum" >/dev/null
gpg --batch --quiet --no-symkey-cache --pinentry-mode loopback \
  --passphrase-file "$recovery_key" \
  --decrypt "$package" \
  | zstd -dc \
  | python3 "$SAFETY_TOOL" inspect-tar-stream >/dev/null

printf 'OK: checksum estricto, descifrado, zstd y estructura tar segura válidos.\n'
