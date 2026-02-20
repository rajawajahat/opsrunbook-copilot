#!/usr/bin/env bash
set -euo pipefail

API="${API_URL:-http://127.0.0.1:8000}"
EVENT_ID="test-it4-$(uuidgen | tr '[:upper:]' '[:lower:]')"
END="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
START="$(date -u -v-15M +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u -d '15 minutes ago' +"%Y-%m-%dT%H:%M:%SZ")"

echo "=== Iteration 4 smoke test ==="
echo "event_id: $EVENT_ID"
echo "time_window: $START -> $END"
echo ""

echo "--- 1. POST /v1/incidents ---"
CREATE=$(curl -s -X POST "$API/v1/incidents" \
  -H "Content-Type: application/json" \
  -d "{
    \"schema_version\":\"incident_event.v1\",
    \"event_id\":\"$EVENT_ID\",
    \"service\":\"loggen\",
    \"title\":\"Iteration4 smoke\",
    \"time_window\":{\"start\":\"$START\",\"end\":\"$END\"},
    \"hints\":{\"log_groups\":[\"/aws/lambda/opsrunbook-copilot-dev-loggen\"]}
  }")
echo "$CREATE" | python3 -m json.tool 2>/dev/null || echo "$CREATE"

INCIDENT_ID=$(echo "$CREATE" | python3 -c "import sys,json; print(json.load(sys.stdin)['incident_id'])")
RUN_ID=$(echo "$CREATE" | python3 -c "import sys,json; print(json.load(sys.stdin)['collector_run_id'])")
echo ""
echo "incident_id: $INCIDENT_ID"
echo "collector_run_id: $RUN_ID"
echo ""

echo "--- 2. Poll /runs/$RUN_ID until SUCCEEDED (max 120s) ---"
for i in $(seq 1 24); do
  sleep 5
  STATUS=$(curl -s "$API/v1/incidents/$INCIDENT_ID/runs/$RUN_ID")
  ST=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo "?")
  echo "  [$((i*5))s] status=$ST"
  if [ "$ST" = "SUCCEEDED" ]; then break; fi
  if [ "$ST" = "FAILED" ] || [ "$ST" = "TIMED_OUT" ] || [ "$ST" = "ABORTED" ]; then
    echo "  Execution ended with status=$ST"
    echo "$STATUS" | python3 -m json.tool 2>/dev/null || echo "$STATUS"
    break
  fi
done
echo ""

echo "--- 3. Wait for analyzer + actions-runner (30s) ---"
sleep 30
echo ""

echo "--- 4. GET /packet/latest ---"
curl -s "$API/v1/incidents/$INCIDENT_ID/packet/latest" | python3 -c "
import sys, json
p = json.load(sys.stdin)
pkt = p.get('packet', {})
print(f\"  findings: {len(pkt.get('findings', []))}\")
print(f\"  evidence_refs: {len(pkt.get('all_evidence_refs', []))}\")
print(f\"  suspected_owners: {[o.get('repo') for o in pkt.get('suspected_owners', [])]}\")
" 2>/dev/null || echo "  (packet not available yet)"
echo ""

echo "--- 5. GET /actions/latest ---"
ACTIONS=$(curl -s "$API/v1/incidents/$INCIDENT_ID/actions/latest")
echo "$ACTIONS" | python3 -m json.tool 2>/dev/null || echo "$ACTIONS"
echo ""

echo "--- 6. Action results summary ---"
echo "$ACTIONS" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    plan = data.get('action_plan', {})
    if plan:
        print(f'  Plan: {len(plan.get(\"actions\", []))} actions planned')
    results = data.get('results', [])
    for r in results:
        print(f'  {r.get(\"action_type\", \"?\")} => status={r.get(\"status\", \"?\")} refs={r.get(\"external_refs\", {})}')
except Exception as e:
    print(f'  (could not parse: {e})')
"
echo ""

echo "--- 7. GET /actions (list) ---"
curl -s "$API/v1/incidents/$INCIDENT_ID/actions" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for a in data.get('actions', []):
        print(f'  {a.get(\"action_type\",\"?\")} status={a.get(\"status\",\"?\")} id={a.get(\"action_id\",\"?\")}')
except Exception as e:
    print(f'  (could not parse: {e})')
" 2>/dev/null || echo "  (no actions)"
echo ""
echo "=== Done ==="
