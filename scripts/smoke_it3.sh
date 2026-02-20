#!/usr/bin/env bash
set -euo pipefail

API="${API_URL:-http://127.0.0.1:8000}"
EVENT_ID="test-it3-$(uuidgen | tr '[:upper:]' '[:lower:]')"
END="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
START="$(date -u -v-15M +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u -d '15 minutes ago' +"%Y-%m-%dT%H:%M:%SZ")"

echo "=== Iteration 3 smoke test ==="
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
    \"title\":\"Iteration3 smoke\",
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

echo "--- 3. GET /snapshot/latest ---"
curl -s "$API/v1/incidents/$INCIDENT_ID/snapshot/latest" | python3 -m json.tool 2>/dev/null || echo "(no snapshot yet)"
echo ""

echo "--- 4. Wait for analyzer (20s extra) then GET /packet/latest ---"
sleep 20
PACKET=$(curl -s "$API/v1/incidents/$INCIDENT_ID/packet/latest")
echo "$PACKET" | python3 -m json.tool 2>/dev/null || echo "$PACKET"
echo ""

echo "--- 5. Suspected owners ---"
echo "$PACKET" | python3 -c "
import sys, json
try:
    p = json.load(sys.stdin)
    pkt = p.get('packet', {})
    for o in pkt.get('suspected_owners', []):
        print(f\"  repo={o['repo']}  confidence={o['confidence']}  reasons={o['reasons']}\")
except Exception as e:
    print(f'  (could not parse: {e})')
"
echo ""

echo "--- 6. GET /packet/$RUN_ID ---"
curl -s "$API/v1/incidents/$INCIDENT_ID/packet/$RUN_ID" | python3 -m json.tool 2>/dev/null || echo "(not found)"
echo ""
echo "=== Done ==="
