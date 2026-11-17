#!/usr/bin/env bash
# Upload the static dashboard (dashboard/) to the stack's S3 bucket.
#
# Usage: scripts/deploy_dashboard.sh [stack-name] [profile] [region]
# Missing arguments are read from .env (STACK_NAME, AWS_PROFILE, REGION),
# then fall back to the stack name netatmo-trv-compensator and the default AWS profile/region.
# data/*.json (written by the Lambda) is never touched.
set -euo pipefail

source "$(dirname "$0")/_env.sh"

STACK="${1:-${STACK_NAME:-netatmo-trv-compensator}}"
PROFILE="${2:-${AWS_PROFILE:-}}"
REGION="${3:-${REGION:-${AWS_REGION:-}}}"
DIR="$(cd "$(dirname "$0")/.." && pwd)/dashboard"

AWS_ARGS=()
[[ -n "$PROFILE" ]] && AWS_ARGS+=(--profile "$PROFILE")
[[ -n "$REGION" ]] && AWS_ARGS+=(--region "$REGION")

echo "Stack: $STACK · profile: ${PROFILE:-<default>} · region: ${REGION:-<default>}"

output() {
  aws cloudformation describe-stacks --stack-name "$STACK" "${AWS_ARGS[@]+"${AWS_ARGS[@]}"}" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

BUCKET="$(output DashboardBucketName)"
URL="$(output DashboardUrl)"
if [[ -z "$BUCKET" || "$BUCKET" == "None" ]]; then
  echo "Stack $STACK has no dashboard: set DashboardPassword and run sam deploy first." >&2
  exit 1
fi

aws s3 sync "$DIR" "s3://$BUCKET/" "${AWS_ARGS[@]+"${AWS_ARGS[@]}"}" \
  --exclude "data/*" --delete --cache-control "no-cache"

echo "Dashboard: $URL"
