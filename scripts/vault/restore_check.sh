#!/usr/bin/env bash
# Decrypt a vault archive with ONE private key and run its verify.py.
# Run this yourself in a plain terminal — never through Claude.
#
#   restore_check.sh primary  <archive.tar.gz.age>   # asks for the passphrase
#   restore_check.sh recovery <archive.tar.gz.age>   # paste the AGE-SECRET-KEY-1… line, Enter, Ctrl-D
#
# Decrypts into a temporary directory, verifies, then deletes it. Nothing secret
# is written to disk.
set -euo pipefail
which="${1:?usage: restore_check.sh primary|recovery <archive>}"
archive="$(readlink -f "${2:?usage: restore_check.sh primary|recovery <archive>}")"
scratch="$(mktemp -d)"
trap 'chmod -R u+w "$scratch" 2>/dev/null; rm -rf "$scratch"' EXIT

case "$which" in
  primary)  identity="$HOME/vault-keys/primary.key.age" ;;
  recovery) identity="-"; echo "Paste the recovery key line, press Enter, then Ctrl-D:" ;;
  *) echo "first argument must be primary or recovery" >&2; exit 2 ;;
esac

age -d -i "$identity" -o "$scratch/archive.tar.gz" "$archive"
tar -xzf "$scratch/archive.tar.gz" -C "$scratch"
rm "$scratch/archive.tar.gz"
dir="$(find "$scratch" -mindepth 1 -maxdepth 1 -type d)"
python3 -I "$dir/verify.py" "$dir"
echo
echo "RESTORE CHECK ($which key): PASSED — $(basename "$archive")"
