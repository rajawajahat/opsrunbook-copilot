#!/usr/bin/env bash
set -euo pipefail

API="${API_URL:-http://127.0.0.1:8000}"
GITHUB_OWNER="${GITHUB_OWNER:-rajawajahat}"
EVENT_ID="test-v1-$(uuidgen | tr '[:upper:]' '[:lower:]')"
END="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
START="$(date -u -v-15M +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u -d '15 minutes ago' +"%Y-%m-%dT%H:%M:%SZ")"

PASS=0; FAIL=0; WARN=0
pass() { PASS=$((PASS+1)); echo "  ✅ $1"; }
fail() { FAIL=$((FAIL+1)); echo "  ❌ $1"; }
warn() { WARN=$((WARN+1)); echo "  ⚠️  $1"; }

echo "============================================="
echo "  OpsRunbook Copilot v1 – Full Pipeline Smoke"
echo "============================================="
echo "event_id:    $EVENT_ID"
echo "time_window: $START -> $END"
echo ""

# ── 1. Create incident ───────────────────────────────────────────
echo "--- 1. POST /v1/incidents ---"
CREATE=$(curl -s -X POST "$API/v1/incidents" \
  -H "Content-Type: application/json" \
  -d "{
    \"schema_version\":\"incident_event.v1\",
    \"event_id\":\"$EVENT_ID\",
    \"service\":\"loggen\",
    \"title\":\"v1 full pipeline smoke\",
    \"time_window\":{\"start\":\"$START\",\"end\":\"$END\"},
    \"hints\":{\"log_groups\":[\"/aws/lambda/opsrunbook-copilot-dev-loggen\"]}
  }")

INCIDENT_ID=$(echo "$CREATE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('incident_id',''))" 2>/dev/null || true)
RUN_ID=$(echo "$CREATE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('collector_run_id',''))" 2>/dev/null || true)

if [ -z "$INCIDENT_ID" ]; then
  echo "$CREATE"
  fail "POST /v1/incidents did not return incident_id"
  echo ""; echo "=== ABORT: $PASS passed, $FAIL failed, $WARN warnings ==="; exit 1
fi
pass "Incident created: $INCIDENT_ID (run: $RUN_ID)"
echo ""

# ── 2. Poll orchestrator ─────────────────────────────────────────
echo "--- 2. Poll orchestrator (max 120s) ---"
ORCH_OK=false
for i in $(seq 1 24); do
  sleep 5
  STATUS=$(curl -s "$API/v1/incidents/$INCIDENT_ID/runs/$RUN_ID")
  ST=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo "?")
  echo "  [$((i*5))s] status=$ST"
  if [ "$ST" = "SUCCEEDED" ]; then ORCH_OK=true; break; fi
  if [ "$ST" = "FAILED" ] || [ "$ST" = "TIMED_OUT" ] || [ "$ST" = "ABORTED" ]; then break; fi
done
if $ORCH_OK; then pass "Orchestrator SUCCEEDED"; else fail "Orchestrator did not succeed (last=$ST)"; fi
echo ""

# ── 3. Poll for actions ──────────────────────────────────────────
echo "--- 3. Poll /actions/latest (max 90s) ---"
ACTIONS=""
ACTIONS_OK=false
for i in $(seq 1 18); do
  sleep 5
  ACTIONS=$(curl -s "$API/v1/incidents/$INCIDENT_ID/actions/latest")
  HAS_RESULTS=$(echo "$ACTIONS" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    results = d.get('results', [])
    print(len(results))
except: print(0)
" 2>/dev/null || echo "0")
  echo "  [$((i*5))s] results=$HAS_RESULTS"
  if [ "$HAS_RESULTS" -ge 2 ] 2>/dev/null; then ACTIONS_OK=true; break; fi
done
if $ACTIONS_OK; then pass "Actions available ($HAS_RESULTS results)"; else fail "Actions not available after 90s"; fi
echo ""

# ── 4. Validate packet ───────────────────────────────────────────
echo "--- 4. Validate packet ---"
PACKET=$(curl -s "$API/v1/incidents/$INCIDENT_ID/packet/latest")
echo "$PACKET" | python3 -c "
import sys, json
p = json.load(sys.stdin)
pkt = p.get('packet', {})
findings = len(pkt.get('findings', []))
erefs = len(pkt.get('all_evidence_refs', []))
owners = [o.get('repo') for o in pkt.get('suspected_owners', [])]
print(f'  findings={findings}  evidence_refs={erefs}  owners={owners}')
" 2>/dev/null || warn "Could not parse packet"
echo ""

# ── 5. Validate action results ───────────────────────────────────
echo "--- 5. Validate action results ---"

eval "$(echo "$ACTIONS" | python3 -c "
import sys, json

def q(v):
    s = str(v).replace(\"'\", \"'\\\"'\\\"'\")
    return f\"'{s}'\"

d = json.load(sys.stdin)
plan = d.get('action_plan', {})
results = d.get('results', [])

plan_count = len(plan.get('actions', []))
print(f'PLAN_COUNT={plan_count}')

for r in results:
    atype = r.get('action_type', '')
    status = r.get('status', '')
    refs = r.get('external_refs', {})
    if isinstance(refs, str):
        refs = json.loads(refs)
    error = (r.get('error') or '')[:200]

    if atype == 'create_jira_ticket':
        print(f'JIRA_STATUS={q(status)}')
        print(f'JIRA_KEY={q(refs.get(\"jira_issue_key\", \"\"))}')
        print(f'JIRA_URL={q(refs.get(\"jira_url\", \"\"))}')
        print(f'JIRA_ERROR={q(error)}')
    elif atype == 'notify_teams':
        print(f'TEAMS_STATUS={q(status)}')
        print(f'TEAMS_ERROR={q(error)}')
    elif atype == 'create_github_pr':
        print(f'GH_STATUS={q(status)}')
        print(f'GH_PR_URL={q(refs.get(\"pr_url\", \"\"))}')
        print(f'GH_PR_NUMBER={q(refs.get(\"pr_number\", \"\"))}')
        print(f'GH_BRANCH={q(refs.get(\"branch\", \"\"))}')
        print(f'GH_DEFAULT_BRANCH={q(refs.get(\"default_branch\", \"\"))}')
        print(f'GH_REPO={q(refs.get(\"github_repo\", \"\"))}')
        print(f'GH_OWNER={q(refs.get(\"github_owner\", \"\"))}')
        print(f'GH_COMMIT_SHA={q(refs.get(\"commit_sha\", \"\"))}')
        print(f'GH_ERROR={q(error)}')
" 2>/dev/null || echo "PLAN_COUNT=0")"

echo "  Plan: $PLAN_COUNT actions"
if [ "${PLAN_COUNT:-0}" -ge 3 ]; then pass "Plan has >= 3 actions"; else fail "Plan has $PLAN_COUNT actions (expected >= 3)"; fi

# -- Jira
echo ""
echo "  [Jira]"
echo "    status=$JIRA_STATUS  key=$JIRA_KEY"
echo "    url=$JIRA_URL"
if [ "${JIRA_STATUS:-}" = "success" ]; then
  pass "Jira ticket created: $JIRA_KEY"
  if [ -n "${JIRA_URL:-}" ] && [ "$JIRA_URL" != "?" ]; then pass "Jira URL present"; else fail "Jira URL missing"; fi
  if [ -n "${JIRA_KEY:-}" ] && [ "$JIRA_KEY" != "?" ]; then pass "Jira key present"; else fail "Jira key missing"; fi
else
  fail "Jira status=$JIRA_STATUS error=${JIRA_ERROR:-}"
fi

# -- Teams
echo ""
echo "  [Teams]"
echo "    status=$TEAMS_STATUS"
if [ "${TEAMS_STATUS:-}" = "success" ]; then
  pass "Teams notification sent"
elif [ "${TEAMS_STATUS:-}" = "skipped" ]; then
  warn "Teams skipped: ${TEAMS_ERROR:-}"
else
  fail "Teams status=$TEAMS_STATUS error=${TEAMS_ERROR:-}"
fi

# -- GitHub PR
echo ""
echo "  [GitHub PR]"
if [ -n "${GH_STATUS:-}" ]; then
  echo "    status=$GH_STATUS"
  if [ "${GH_STATUS:-}" = "success" ]; then
    echo "    pr_url=$GH_PR_URL"
    echo "    branch=$GH_BRANCH -> $GH_DEFAULT_BRANCH"
    echo "    repo=$GH_OWNER/$GH_REPO"
    pass "GitHub PR created"
    if echo "$GH_BRANCH" | grep -q "opsrunbook/"; then pass "Deterministic branch naming"; else fail "Branch not deterministic: $GH_BRANCH"; fi
  elif [ "${GH_STATUS:-}" = "skipped" ]; then
    echo "    reason=${GH_ERROR:-}"
    if echo "${GH_ERROR:-}" | grep -q "confidence"; then
      pass "PR skipped due to confidence gate"
    else
      warn "PR skipped: ${GH_ERROR:-}"
    fi
  else
    fail "GitHub PR status=$GH_STATUS error=${GH_ERROR:-}"
  fi
else
  echo "    (not in results — ENABLE_GITHUB_PR_ACTION may be false)"
  warn "GitHub PR action not found in results"
fi
echo ""

# ── 6. Validate PR body contains marker (if PR was created) ──────
echo "--- 6. Validate PR body ---"
if [ "${GH_STATUS:-}" = "success" ] && [ -n "${GH_PR_NUMBER:-}" ] && [ "$GH_PR_NUMBER" != "0" ]; then
  GH_API_OWNER="${GH_OWNER:-$GITHUB_OWNER}"
  GH_API_REPO="${GH_REPO:-opsrunbook-copilot}"

  PR_JSON=""
  if command -v gh &>/dev/null; then
    PR_JSON=$(gh api "repos/$GH_API_OWNER/$GH_API_REPO/pulls/$GH_PR_NUMBER" 2>/dev/null || echo "{}")
  elif [ -n "${GITHUB_TOKEN:-}" ]; then
    PR_JSON=$(curl -s -H "Authorization: Bearer $GITHUB_TOKEN" \
      -H "Accept: application/vnd.github+json" \
      "https://api.github.com/repos/$GH_API_OWNER/$GH_API_REPO/pulls/$GH_PR_NUMBER" 2>/dev/null || echo "{}")
  fi

  if [ -n "$PR_JSON" ] && [ "$PR_JSON" != "{}" ]; then
    eval "$(echo "$PR_JSON" | python3 -c "
import sys, json
try:
    pr = json.load(sys.stdin)
    body = pr.get('body', '')
    has_marker = 'opsrunbook_copilot' in body
    has_incident = '$INCIDENT_ID' in body
    has_evidence = 'Evidence' in body
    has_confidence = 'Confidence' in body
    print(f'HAS_MARKER={str(has_marker).lower()}')
    print(f'HAS_INCIDENT={str(has_incident).lower()}')
    print(f'HAS_EVIDENCE={str(has_evidence).lower()}')
    print(f'HAS_CONFIDENCE={str(has_confidence).lower()}')
except Exception:
    print('HAS_MARKER=false')
    print('HAS_INCIDENT=false')
    print('HAS_EVIDENCE=false')
    print('HAS_CONFIDENCE=false')
" 2>/dev/null || echo "HAS_MARKER=false")"

    if [ "$HAS_MARKER" = "true" ]; then pass "PR body has opsrunbook_copilot marker"; else fail "PR body missing marker"; fi
    if [ "$HAS_INCIDENT" = "true" ]; then pass "PR body has incident_id"; else fail "PR body missing incident_id"; fi
    if [ "$HAS_EVIDENCE" = "true" ]; then pass "PR body has evidence summary"; else fail "PR body missing evidence"; fi
    if [ "$HAS_CONFIDENCE" = "true" ]; then pass "PR body has confidence"; else fail "PR body missing confidence"; fi
  else
    warn "Could not fetch PR from GitHub API"
  fi
else
  echo "  (skipped — PR was not created)"
fi
echo ""

# ── 7. Replay harness ────────────────────────────────────────────
echo "--- 7. Replay harness ---"
REPLAY=$(curl -s -X POST "$API/v1/incidents/$INCIDENT_ID/replay")
MATCH=$(echo "$REPLAY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('match', False))" 2>/dev/null || echo "false")
echo "  match=$MATCH"
if [ "$MATCH" = "True" ]; then
  pass "Replay produces identical plan"
else
  DIFFS=$(echo "$REPLAY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('diffs', []))" 2>/dev/null || echo "[]")
  warn "Replay diverged: $DIFFS"
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
