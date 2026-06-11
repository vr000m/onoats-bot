#!/bin/sh
# make_cert.sh — create the stable self-signed "Code Signing" identity, scripted.
#
# Equivalent to the Keychain Access GUI steps in native/README.md "Step 0", but
# non-interactive (verified: codesign needs neither keychain trust nor a
# partition-list fix — the import-time ACL pre-authorizing /usr/bin/codesign is
# sufficient). Safe to re-run: exits 0 if the identity already exists.
#
# TCC grant persistence keys on this cert's identity (the designated
# requirement), so it is created ONCE with a long validity and never
# regenerated casually — regenerating it invalidates every existing TCC grant
# for the bundle and macOS will re-prompt.
#
# Usage:  sh make_cert.sh            # creates identity named "Code Signing"
#         IDENTITY="My Cert" sh make_cert.sh
set -eu

IDENTITY="${IDENTITY:-Code Signing}"
DAYS="${DAYS:-3650}"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

if security find-identity -p codesigning 2>/dev/null | grep -q "\"$IDENTITY\""; then
  echo "✓ codesigning identity '$IDENTITY' already exists — nothing to do"
  echo "  (regenerating it would invalidate existing TCC grants; delete it"
  echo "   explicitly in Keychain Access first if you really mean to)"
  exit 0
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

cat > "$tmp/cert.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = v3_codesign
prompt = no
[dn]
CN = $IDENTITY
[v3_codesign]
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
basicConstraints = critical,CA:FALSE
subjectKeyIdentifier = hash
EOF

echo "→ generating self-signed code-signing certificate '$IDENTITY' (${DAYS}d)…"
openssl req -x509 -newkey rsa:2048 -sha256 -days "$DAYS" -nodes \
  -keyout "$tmp/key.pem" -out "$tmp/cert.pem" -config "$tmp/cert.cnf" 2>/dev/null

# Throwaway transport password: random per-run, file-fed to openssl so it
# never hits argv there. `security import` only takes -P on argv — a one-shot
# random secret guarding a file that lives seconds inside $tmp (0700) is an
# acceptable residual exposure window.
transport_pw=$(openssl rand -hex 16)
printf '%s' "$transport_pw" > "$tmp/pw"
openssl pkcs12 -export -name "$IDENTITY" -inkey "$tmp/key.pem" \
  -in "$tmp/cert.pem" -out "$tmp/identity.p12" -passout "file:$tmp/pw"

echo "→ importing into the login keychain (codesign pre-authorized via ACL)…"
security import "$tmp/identity.p12" -k "$KEYCHAIN" \
  -P "$transport_pw" -f pkcs12 -T /usr/bin/codesign

echo
if security find-identity -p codesigning | grep "\"$IDENTITY\""; then
  echo "✓ identity '$IDENTITY' ready. Note: 'find-identity -v' will NOT list it"
  echo "  (self-signed ⇒ fails keychain trust validation) — that is expected;"
  echo "  codesign signs with it regardless, and TCC keys on the cert identity."
  echo "  If a keychain prompt ever appears on first sign, click 'Always Allow'."
else
  echo "✗ identity not visible after import — see native/README.md Step 0 for" >&2
  echo "  the manual Keychain Access fallback." >&2
  exit 1
fi
