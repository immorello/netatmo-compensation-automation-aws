#!/usr/bin/env bash
# Create / overwrite the SSM parameters used by the Netatmo TRV compensator.
#
# Usage: scripts/bootstrap_tokens.sh [<client_id> <client_secret> <refresh_token> [prefix] [region]]
#
# Without arguments every value is read from .env:
#   CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, PARAMETER_PREFIX (default /netatmo-trv),
#   REGION, AWS_PROFILE
# The target account is shown and must be confirmed before anything is written.
#
# SecureString parameters use the AWS managed key aws/ssm.
# Values are read through a temporary file so they never show up in `ps` output.
set -euo pipefail

source "$(dirname "$0")/_env.sh"

if [[ $# -ne 0 && ( $# -lt 3 || $# -gt 5 ) ]]; then
  echo "Usage: $0 [<client_id> <client_secret> <refresh_token> [prefix] [region]]" >&2
  exit 1
fi

CLIENT_ID="${1:-${CLIENT_ID:-}}"
CLIENT_SECRET="${2:-${CLIENT_SECRET:-}}"
REFRESH_TOKEN="${3:-${REFRESH_TOKEN:-}}"
PREFIX="${4:-${PARAMETER_PREFIX:-/netatmo-trv}}"
PREFIX="${PREFIX%/}"
REGION="${5:-${REGION:-${AWS_REGION:-}}}"

for v in CLIENT_ID CLIENT_SECRET REFRESH_TOKEN; do
  if [[ -z "${!v}" ]]; then
    echo "$v is missing: pass it as an argument or set it in .env" >&2
    exit 1
  fi
done
if [[ "$PREFIX" != /* ]]; then
  echo "Prefix must start with '/': $PREFIX" >&2
  exit 1
fi

AWS_ARGS=()
[[ -n "${AWS_PROFILE:-}" ]] && AWS_ARGS+=(--profile "$AWS_PROFILE")
[[ -n "$REGION" ]] && AWS_ARGS+=(--region "$REGION")

ACCOUNT="$(aws sts get-caller-identity "${AWS_ARGS[@]+"${AWS_ARGS[@]}"}" --query Arn --output text)"
echo "Target: $ACCOUNT"
echo "Profile: ${AWS_PROFILE:-<default>} · region: ${REGION:-<default>} · prefix: $PREFIX"
read -r -p "Write the parameters here? [y/N] " answer
[[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "Aborted."; exit 1; }

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
chmod 600 "$TMP"

put() { # name type value
  printf '%s' "$3" > "$TMP"
  aws ssm put-parameter "${AWS_ARGS[@]+"${AWS_ARGS[@]}"}" \
    --name "$PREFIX/$1" --type "$2" --value "file://$TMP" --overwrite >/dev/null
  echo "ok  $PREFIX/$1 ($2)"
}

put client_id String "$CLIENT_ID"
put client_secret SecureString "$CLIENT_SECRET"
put refresh_token SecureString "$REFRESH_TOKEN"

# /state is created by the Lambda on its first real write. If an old state
# exists from a previous installation, reset it so it is not mistaken for ours.
put state String '{}'

echo "Done. Parameters stored under $PREFIX/"
echo "Note: the Lambda rotates the refresh token on every run; the one in .env is now stale."
