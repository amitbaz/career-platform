#!/usr/bin/env bash
# Create the local stack's JWT signing key, if it is not there already.
#
# config.toml sets signing_keys_path, so `supabase start` refuses to run without
# this file. It is generated per machine and git-ignored, which means a fresh
# clone has to make one before the stack will boot.
#
# The key is a throwaway for localhost. Never commit it, and never put the
# hosted project's signing key here: that key can mint a token for any user, and
# a local stack has no business holding it.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -f supabase/signing_keys.json ]; then
  echo "supabase/signing_keys.json exists; leaving it as it is"
  exit 0
fi

# --append writes into the keys file in the array shape the stack expects, and
# prints only a count -- so the key itself never reaches stdout or a CI log.
echo '[]' > supabase/signing_keys.json
supabase gen signing-key --algorithm ES256 --append
