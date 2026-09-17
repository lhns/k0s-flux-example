#!/usr/bin/env bash
# Really decrypt every SOPS file, which is the only way to catch a broken MAC.
#
# NOT part of scripts/validate.sh and never will be: it needs the age private key, and the
# key does not belong in CI. scripts/check-sops.py covers everything checkable without it
# and says so; this covers the rest.
#
# Run it before trusting any change to an encrypted file. A file re-encrypted through a
# pipe (`sops -d | sed | sops -e /dev/stdin`) can come out with a MAC that does not
# validate; nothing else in this repo notices, and kustomize-controller then stops
# reconciling the whole Kustomization while git and every lint stay green.
#
# The key lives on the jumphost, with a clone at ~/k0s-flux:
#
#     ssh admin@10.20.5.15 'cd k0s-flux && git pull --ff-only && bash scripts/check-sops-mac.sh'
#
# Decrypted output is discarded, never printed and never written to disk.
set -uo pipefail
cd "$(dirname "$0")/.."

command -v sops >/dev/null || { echo "sops not on PATH -- run this on the jumphost" >&2; exit 1; }

fail=0
n=0
while IFS= read -r f; do
  n=$((n + 1))
  if err="$(sops -d "$f" 2>&1 >/dev/null)"; then
    printf '  ok    %s\n' "$f"
  else
    # The error text is sops's own (e.g. "cipher: message authentication failed") and
    # carries no plaintext; the decrypted document is discarded above either way.
    printf '  FAIL  %s: %s\n' "$f" "$(printf '%s' "$err" | tr '\n' ' ')" >&2
    fail=$((fail + 1))
  fi
done < <(grep -rl 'ENC\[AES256_GCM' kube-cluster/ --include='*.yaml' --include='*.yml' | sort)

[ "$n" -gt 0 ] || { echo "no encrypted files found -- wrong directory?" >&2; exit 1; }

echo
if [ "$fail" -gt 0 ]; then
  echo "$fail of $n file(s) do not decrypt" >&2
  exit 1
fi
echo "$n file(s) decrypt and their MACs validate"
