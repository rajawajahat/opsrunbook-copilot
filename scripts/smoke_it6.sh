#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────
#  Iteration 6 – Smoke test: webhook → step function → PR comment
# ─────────────────────────────────────────────────────────────────
#
# Prerequisites:
#   1. API running locally (make api-dev) with GITHUB_WEBHOOK_SECRET set
#   2. PR_REVIEW_STATE_MACHINE_ARN set in .env (from terraform output)
#   3. A PR already created by Iteration 5 in the sandbox repo
#
# Usage:
#   GITHUB_WEBHOOK_SECRET=<secret> PR_NUMBER=<n> ./scripts/smoke_it6.sh
# ─────────────────────────────────────────────────────────────────

API="${API_URL:-http://127.0.0.1:8000}"
SECRET="${GITHUB_WEBHOOK_SECRET:?Set GITHUB_WEBHOOK_SECRET}"
PR_NUMBER="${PR_NUMBER:-1}"
REPO="${GITHUB_REPO:-rajawajahat/opsrunbook-copilot-test}"
DELIVERY_ID="test-dlv-$(uuidgen | tr '[:upper:]' '[:lower:]')"

PASS=0; FAIL=0; WARN=0
pass() { PASS=$((PASS+1)); echo "  ✅ $1"; }
fail() { FAIL=$((FAIL+1)); echo "  ❌ $1"; }
warn() { WARN=$((WARN+1)); echo "  ⚠️  $1"; }

echo "============================================="
echo "  Iteration 6 smoke test (webhook → PR cycle)"
echo "============================================="
echo "delivery_id: $DELIVERY_ID"
echo "repo:        $REPO"
echo "pr_number:   $PR_NUMBER"
echo ""

# ── 1. Webhook with valid signature ──────────────────────────────
echo "--- 1. POST /v1/webhooks/github (valid signature) ---"

PAYLOAD=$(cat <<PAYLOAD_EOF
{
  "action": "created",
  "issue": {
    "number": $PR_NUMBER,
    "html_url": "https://github.com/$REPO/pull/$PR_NUMBER",
    "pull_request": {
      "html_url": "https://github.com/$REPO/pull/$PR_NUMBER"
    }
  },
  "comment": {
    "body": "Please fix spelling in opsrunbook/KAN-4.md",
    "html_url": "https://github.com/$REPO/pull/$PR_NUMBER#issuecomment-test123"
  },
  "repository": {
    "full_name": "$REPO"
  },
  "installation": {"id": 12345},
  "sender": {"login": "human-reviewer"}
}
PAYLOAD_EOF
)

SIG=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')

RESP=$(curl -s -w "\n%{http_code}" -X POST "$API/v1/webhooks/github" \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: sha256=$SIG" \
  -H "X-GitHub-Event: issue_comment" \
  -H "X-GitHub-Delivery: $DELIVERY_ID" \
  -d "$PAYLOAD")

HTTP_CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')

echo "  HTTP: $HTTP_CODE"
echo "  Body: $BODY"

if [ "$HTTP_CODE" = "202" ]; then
  pass "Webhook accepted (202)"
else
  fail "Webhook not accepted (HTTP $HTTP_CODE)"
fi

STATUS=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "?")
if [ "$STATUS" = "accepted" ]; then
  pass "Status = accepted"
elif [ "$STATUS" = "skipped" ]; then
  warn "Status = skipped (check PR_REVIEW_STATE_MACHINE_ARN)"
else
  warn "Status = $STATUS"
fi
echo ""

# ── 2. Dedupe: replay same delivery_id ───────────────────────────
echo "--- 2. Dedupe test (replay same delivery_id) ---"

RESP2=$(curl -s -w "\n%{http_code}" -X POST "$API/v1/webhooks/github" \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: sha256=$SIG" \
  -H "X-GitHub-Event: issue_comment" \
  -H "X-GitHub-Delivery: $DELIVERY_ID" \
  -d "$PAYLOAD")

HTTP_CODE2=$(echo "$RESP2" | tail -1)
BODY2=$(echo "$RESP2" | sed '$d')
STATUS2=$(echo "$BODY2" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "?")

echo "  HTTP: $HTTP_CODE2  Status: $STATUS2"
if [ "$STATUS2" = "already_processed" ]; then
  pass "Dedupe working: already_processed"
else
  fail "Dedupe not working: status=$STATUS2"
fi
echo ""

# ── 3. Invalid signature ─────────────────────────────────────────
echo "--- 3. Invalid signature test ---"

RESP3=$(curl -s -w "\n%{http_code}" -X POST "$API/v1/webhooks/github" \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: sha256=bad_signature" \
  -H "X-GitHub-Event: issue_comment" \
  -H "X-GitHub-Delivery: test-invalid-sig" \
  -d "$PAYLOAD")

HTTP_CODE3=$(echo "$RESP3" | tail -1)
echo "  HTTP: $HTTP_CODE3"
if [ "$HTTP_CODE3" = "401" ]; then
  pass "Invalid signature rejected (401)"
else
  fail "Invalid signature not rejected (HTTP $HTTP_CODE3)"
fi
echo ""

# ── 4. Bot self-event skip ───────────────────────────────────────
echo "--- 4. Bot self-event skip test ---"

BOT_DELIVERY="test-bot-$(uuidgen | tr '[:upper:]' '[:lower:]')"
BOT_PAYLOAD=$(echo "$PAYLOAD" | python3 -c "
import sys, json
p = json.load(sys.stdin)
p['sender']['login'] = 'opsrunbook-copilot-bot[bot]'
print(json.dumps(p))
")
BOT_SIG=$(echo -n "$BOT_PAYLOAD" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')

RESP4=$(curl -s -w "\n%{http_code}" -X POST "$API/v1/webhooks/github" \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: sha256=$BOT_SIG" \
  -H "X-GitHub-Event: issue_comment" \
  -H "X-GitHub-Delivery: $BOT_DELIVERY" \
  -d "$BOT_PAYLOAD")

HTTP_CODE4=$(echo "$RESP4" | tail -1)
BODY4=$(echo "$RESP4" | sed '$d')
STATUS4=$(echo "$BODY4" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('reason', d.get('status','')))" 2>/dev/null || echo "?")

echo "  HTTP: $HTTP_CODE4  Status: $STATUS4"
if [ "$STATUS4" = "self_event" ]; then
  pass "Bot self-event skipped"
else
  warn "Bot self-event not skipped: status=$STATUS4"
fi
echo ""

# ── 5. /copilot stop command ─────────────────────────────────────
echo "--- 5. /copilot stop command test ---"

STOP_DELIVERY="test-stop-$(uuidgen | tr '[:upper:]' '[:lower:]')"
STOP_PAYLOAD=$(echo "$PAYLOAD" | python3 -c "
import sys, json
p = json.load(sys.stdin)
p['comment']['body'] = '/copilot stop'
print(json.dumps(p))
")
STOP_SIG=$(echo -n "$STOP_PAYLOAD" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')

RESP5=$(curl -s -w "\n%{http_code}" -X POST "$API/v1/webhooks/github" \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: sha256=$STOP_SIG" \
  -H "X-GitHub-Event: issue_comment" \
  -H "X-GitHub-Delivery: $STOP_DELIVERY" \
  -d "$STOP_PAYLOAD")

HTTP_CODE5=$(echo "$RESP5" | tail -1)
BODY5=$(echo "$RESP5" | sed '$d')
STATUS5=$(echo "$BODY5" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "?")

echo "  HTTP: $HTTP_CODE5  Status: $STATUS5"
if [ "$STATUS5" = "paused" ]; then
  pass "/copilot stop paused the PR"
else
  fail "/copilot stop did not pause: status=$STATUS5"
fi
echo ""

# ── Summary ──────────────────────────────────────────────────────
echo "============================================="
echo "  RESULTS: $PASS passed, $FAIL failed, $WARN warnings"
echo "============================================="
echo ""
if [ "$FAIL" -gt 0 ]; then
  echo "  ❌ SMOKE TEST FAILED"
  exit 1
else
  echo "  ✅ SMOKE TEST PASSED"
  exit 0
fi
