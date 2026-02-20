# OpsRunbook Copilot v1 — Technical Documentation

## Executive Summary

OpsRunbook Copilot is an automated incident analysis and response system for AWS workloads. When an incident is reported, it collects evidence from CloudWatch Logs, CloudWatch Metrics, and Step Functions execution history, analyzes the evidence to produce structured findings, and automatically creates a Jira ticket, Teams notification, and GitHub Pull Request with the analysis.

The system is designed to be **deterministic**, **idempotent**, and **evidence-grounded** — every finding references specific evidence objects, and no action is taken without verifiable justification.

## Scope Boundaries

### What it does
- Collects bounded evidence from AWS observability sources
- Produces structured `IncidentPacketV1` with findings, hypotheses, and evidence refs
- Creates Jira tickets, MS Teams notifications, and GitHub PRs
- Deterministic repo resolution with confidence gating
- Receives GitHub webhook events for PR review cycle automation

### What it does NOT do
- No dashboards or UI
- No approval workflows (GitHub branch protection handles merge control)
- No RAG or vector search
- No multi-tenant support (single AWS account)
- No real-time alerting (triggered by API call)
- No LLM in production path (stub analyzer; LLM scaffolded but gated behind `LLM_PROVIDER=stub`)

---

## Architecture Overview

### AWS Services Used

| Service | Purpose |
|---------|---------|
| **FastAPI** (local/Lambda) | API endpoints for incident creation, status, packet/actions retrieval |
| **AWS Step Functions** | Orchestrates parallel evidence collection + snapshot persistence |
| **AWS Lambda** (×8) | Collectors (logs, metrics, stepfn), snapshot persist, analyzer, actions runner, loggen, PR review cycle |
| **Amazon S3** | Evidence blobs, manifest JSONs, packet JSONs |
| **Amazon DynamoDB** (×3 tables) | Incidents metadata, snapshots metadata, packets metadata |
| **Amazon EventBridge** | Async event routing: `evidence.snapshot.persisted` → analyzer, `incident.analyzed` → actions runner |
| **AWS SSM Parameter Store** | Secrets: Jira token, Teams webhook, GitHub App credentials |

### Repository Structure

```
opsrunbook-copilot/
├── services/
│   ├── api/                     # FastAPI application
│   │   ├── src/
│   │   │   ├── app.py           # FastAPI app + router registration
│   │   │   ├── settings.py      # Centralized config from env vars
│   │   │   ├── models.py        # Pydantic request/response models
│   │   │   ├── sanitize.py      # Control-char sanitizer for JSON safety
│   │   │   ├── routers/
│   │   │   │   ├── incidents.py  # /v1/incidents endpoints
│   │   │   │   ├── webhooks.py   # /v1/webhooks/github endpoint
│   │   │   │   └── debug.py      # Health/debug endpoints
│   │   │   ├── stores/           # DynamoDB + S3 persistence
│   │   │   └── evidence/         # Budget, time window, redaction utilities
│   │   └── .env.example          # Template for local dev
│   └── collectors/               # Shared collector library code
│       └── src/collectors/
│           ├── cloudwatch/       # Insights + Metrics clients
│           └── stepfunctions/    # SFN execution history client
├── packages/
│   └── contracts/                # Pydantic v2 schemas (versioned)
│       └── src/contracts/
│           ├── incident_event_v1.py
│           ├── incident_packet_v1.py
│           ├── evidence_snapshot_v1.py
│           ├── action_plan_v1.py
│           ├── github_pr_review_event_v1.py
│           └── pr_fix_plan_v1.py
├── infra/terraform/
│   ├── envs/dev/main.tf          # Dev environment wiring
│   └── modules/
│       ├── storage/               # S3 + DynamoDB tables
│       ├── collector_logs/        # Lambda: CW Logs Insights
│       ├── collector_metrics/     # Lambda: CW Metrics
│       ├── collector_stepfn/      # Lambda: SFN execution history
│       ├── snapshot_persist/      # Lambda: aggregate + persist snapshot
│       ├── orchestrator/          # Step Functions state machine
│       ├── analyzer/              # Lambda: produce IncidentPacket
│       ├── actions_runner/        # Lambda: Jira + Teams + GitHub PR
│       ├── pr_review_cycle/       # Step Functions + Lambda: webhook PR handling
│       └── loggen/                # Test Lambda for generating sample logs
├── tests/                         # Root-level unit tests
├── scripts/                       # Smoke tests + utility scripts
├── docs/                          # This document + architecture diagrams
├── config/
│   └── resource_repo_map.json     # Legacy service→repo mapping
├── pyproject.toml                 # Ruff + pytest config
└── Makefile                       # Dev/test/deploy commands
```

---

## Components and Responsibilities

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/incidents` | POST | Create incident → start orchestration |
| `/v1/incidents/{id}/runs/{run_id}` | GET | Poll orchestrator execution status |
| `/v1/incidents/{id}/snapshot/latest` | GET | Latest evidence snapshot metadata |
| `/v1/incidents/{id}/evidence` | GET | Latest evidence payload from S3 |
| `/v1/incidents/{id}/packet/latest` | GET | Latest IncidentPacket (findings + analysis) |
| `/v1/incidents/{id}/packet/{run_id}` | GET | Packet for specific collector run |
| `/v1/incidents/{id}/actions/latest` | GET | Latest action plan + execution results |
| `/v1/incidents/{id}/actions` | GET | List all action results |
| `/v1/incidents/{id}/replay` | POST | Replay harness: re-generate plan, compare with stored |
| `/v1/webhooks/github` | POST | GitHub webhook ingestion (signature-verified) |
| `/health` | GET | Health check with config summary |

### Orchestrator Step Function

Runs collectors in parallel, then persists the aggregated snapshot:

```
Parallel:
  ├── CollectLogs → logs.json in S3
  ├── CollectMetrics → metrics.json in S3
  └── CollectStepFn → stepfn.json in S3
↓
PersistSnapshot → manifest.json + SNAPSHOT# in DDB
↓
EventBridge: evidence.snapshot.persisted
```

### Analyzer Lambda

Triggered by `evidence.snapshot.persisted` event. Loads manifest + all evidence objects from S3. Produces `IncidentPacketV1` with:
- **Findings**: grounded in evidence_refs, with confidence scores
- **Hypotheses**: lower-confidence observations
- **Next Actions**: suggested investigation steps (runbook-style)
- **Suspected Owners**: repo candidates from resource name heuristics
- **Limits**: what could not be determined

Persists packet to S3 + DynamoDB. Emits `incident.analyzed`.

### Actions Runner Lambda

Triggered by `incident.analyzed` event. For each action:

1. **Idempotency check**: query DynamoDB for prior successful result; skip if found
2. **Kill switch**: if `AUTOMATION_ENABLED=false`, return immediately
3. **Generate action plan** from packet (deterministic)
4. **Execute Jira** → create ticket with findings + evidence summary
5. **Execute Teams** → send notification with findings + Jira link
6. **Execute GitHub PR** → confidence-gated repo resolution → create/update PR

**Confidence Gate**: If repo confidence < 0.7 (configurable via `PR_CONFIDENCE_THRESHOLD`), the PR action is skipped with a logged reason.

**Repo Resolution Priority**:
1. Mapping rules (`repo_mapping.json`) → 0.95 confidence
2. Trace-driven verification (file_exists on GitHub) → 0.85 confidence
3. Heuristic fallback (suspected_owners) → 0.5 confidence (below gate)

### Storage Model

**S3 Key Layout**:
```
evidence/{incident_id}/{collector_run_id}/logs.json
evidence/{incident_id}/{collector_run_id}/metrics.json
evidence/{incident_id}/{collector_run_id}/stepfn.json
evidence/{incident_id}/{collector_run_id}.json          ← manifest
packets/{incident_id}/{collector_run_id}.json            ← IncidentPacket
```

**DynamoDB Key Design**:

| Table | PK | SK Pattern | Purpose |
|-------|-----|------------|---------|
| incidents | `INCIDENT#{id}` | `META` | Incident metadata |
| incidents | `INCIDENT#{id}` | `ACTIONPLAN#{ts}` | Action plan |
| incidents | `INCIDENT#{id}` | `ACTION#{ts}#{aid}` | Individual action result |
| incidents | `INCIDENT#{id}` | `ACTIONS#LATEST` | Latest pointer |
| snapshots | `INCIDENT#{id}` | `SNAPSHOT#{ts}#{run}` | Snapshot metadata |
| snapshots | `INCIDENT#{id}` | `RUN#{run_id}` | Execution tracking |
| packets | `INCIDENT#{id}` | `PACKET#{ts}#{run}` | Packet metadata |

---

## Data Flow: End-to-End

```
POST /v1/incidents (service, time_window, hints)
  │
  ├── Store incident metadata → DDB incidents
  ├── Start Step Functions execution
  │
  ▼
Orchestrator (parallel)
  ├── Logs Collector → CW Logs Insights → S3
  ├── Metrics Collector → CW GetMetricData → S3
  └── StepFn Collector → DescribeExecution → S3
  │
  ▼
Snapshot Persist → manifest in S3 + SNAPSHOT# in DDB
  │
  ▼ EventBridge: evidence.snapshot.persisted
  │
Analyzer → IncidentPacketV1 → S3 + DDB
  │
  ▼ EventBridge: incident.analyzed
  │
Actions Runner
  ├── [idempotent] Jira → create ticket → external_refs.jira_issue_key
  ├── [idempotent] Teams → send notification
  └── [idempotent + gated] GitHub PR → deterministic branch + PR
  │
  ▼
DDB: ACTIONPLAN# + ACTION#... + ACTIONS#LATEST
```

---

## Security

### Secrets Handling

| Secret | Storage | Access |
|--------|---------|--------|
| GitHub App Private Key | SSM `/opsrunbook/dev/github/app_private_key_pem` (SecureString) | Actions Runner Lambda IAM role |
| GitHub App ID | SSM `/opsrunbook/dev/github/app_id` | Actions Runner Lambda IAM role |
| GitHub App Installation ID | SSM `/opsrunbook/dev/github/app_installation_id` | Actions Runner Lambda IAM role |
| Jira API Token | SSM `/opsrunbook/dev/jira/api_token` (SecureString) | Actions Runner Lambda IAM role |
| Jira Email | SSM `/opsrunbook/dev/jira/email` | Actions Runner Lambda IAM role |
| Teams Webhook URL | SSM `/opsrunbook/dev/teams/webhook_url` (SecureString) | Actions Runner Lambda IAM role |
| GitHub Webhook Secret | SSM `/opsrunbook/dev/github/webhook_secret` (SecureString) | API service env var |

**Rules**:
- Terraform creates SSM parameter containers with `REPLACE_ME`; actual values are set manually or via CI
- `lifecycle { ignore_changes = [value] }` prevents Terraform from overwriting rotated secrets
- Lambda IAM roles have `ssm:GetParameter` scoped to `/opsrunbook/dev/*`
- No secrets in Terraform state, code, or logs

### IAM Least Privilege

Each Lambda role has a specific policy scoped to the resources it needs:
- Collectors: `logs:StartQuery/GetQueryResults`, `cloudwatch:GetMetricData`, `states:DescribeExecution`, `s3:PutObject` on evidence prefix
- Analyzer: `s3:GetObject` on evidence prefix, `s3:PutObject` on packets prefix, `dynamodb:PutItem/GetItem/Query` on packets table
- Actions Runner: `s3:GetObject`, `dynamodb:PutItem/GetItem/Query` on incidents table, `ssm:GetParameter`, `events:PutEvents`

### Redaction

- Evidence payloads are bounded (max rows, max bytes) before storage
- Log content is truncated, not filtered for PII in v1 (placeholder `redaction: {enabled: false}`)
- Sensitive field masking is applied before DynamoDB persistence

---

## Operational Runbook

### Running Smoke Tests

```bash
# Full pipeline smoke (requires running API + deployed AWS resources)
./scripts/smoke_it5.sh

# Unit tests only (no AWS needed)
make test

# Lint check
make lint
```

### Replay / Evaluation

```bash
# Replay an existing incident to verify deterministic plan generation
curl -X POST http://localhost:8000/v1/incidents/<incident_id>/replay | jq .

# Response includes:
# - match: true/false
# - diffs: list of differences
# - new_plan_preview: re-generated plan summary
```

### Common Failures

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Orchestrator not configured` | `STATE_MACHINE_ARN` missing in `.env` | Run `terraform output state_machine_arn` and set in `.env` |
| `packet not found` after incident | Analyzer not triggered | Check EventBridge rule, analyzer Lambda logs |
| `actions not found` | Actions runner not triggered | Check EventBridge rule for `incident.analyzed` |
| PR action `skipped: repo_confidence` | Repo confidence < 0.7 | Add mapping rule to `repo_mapping.json` or verify file exists |
| `github_client_error` | SSM params missing/invalid | Verify SSM values: `aws ssm get-parameter --name /opsrunbook/dev/github/app_id` |
| Jira `401` | Token expired | Rotate in SSM: `/opsrunbook/dev/jira/api_token` |

---

## Deployment

### Prerequisites

- AWS account with permissions for Lambda, Step Functions, S3, DynamoDB, EventBridge, SSM, IAM
- Terraform >= 1.5.0
- `aws-vault` configured with profile `opsrunbook-dev`

### Deploy Steps

```bash
# 1. Initialize and apply Terraform
make tf-dev

# 2. Get outputs for .env
make tf-output-dev
# Copy values into services/api/.env

# 3. Set secrets in SSM (one-time, manual)
aws ssm put-parameter --name /opsrunbook/dev/jira/api_token --type SecureString --value "..."
aws ssm put-parameter --name /opsrunbook/dev/teams/webhook_url --type SecureString --value "..."
aws ssm put-parameter --name /opsrunbook/dev/github/app_private_key_pem --type SecureString --value "$(base64 -i key.pem | tr -d '\n')"
aws ssm put-parameter --name /opsrunbook/dev/github/app_id --type String --value "..."
aws ssm put-parameter --name /opsrunbook/dev/github/app_installation_id --type String --value "..."
aws ssm put-parameter --name /opsrunbook/dev/github/webhook_secret --type SecureString --value "..."

# 4. Run API locally
make api-dev

# 5. Smoke test
make smoke
```

### Rotating GitHub App Private Key

1. Generate new key in GitHub App settings
2. Base64 encode: `base64 -i new-key.pem | tr -d '\n'`
3. Update SSM: `aws ssm put-parameter --name /opsrunbook/dev/github/app_private_key_pem --type SecureString --value "..." --overwrite`
4. Lambda will pick up new value on next cold start (no redeploy needed)

### Environment Variables

| Variable | Required | Source | Description |
|----------|----------|--------|-------------|
| `AWS_REGION` | Yes | Static | AWS region (default: us-east-1) |
| `EVIDENCE_BUCKET` | Yes | Terraform | S3 bucket for evidence |
| `INCIDENTS_TABLE` | Yes | Terraform | DynamoDB incidents table |
| `SNAPSHOTS_TABLE` | Yes | Terraform | DynamoDB snapshots table |
| `PACKETS_TABLE` | Yes | Terraform | DynamoDB packets table |
| `STATE_MACHINE_ARN` | Yes | Terraform | Orchestrator Step Function ARN |
| `EVENT_BUS_NAME` | Yes | Terraform | EventBridge bus name |
| `AUTOMATION_ENABLED` | No | Config | Kill switch (default: true) |
| `PR_CONFIDENCE_THRESHOLD` | No | Config | Min confidence for PR creation (default: 0.7) |
| `ACTIONS_DRY_RUN` | No | Config | Dry run mode (default: true) |
| `ENABLE_GITHUB_PR_ACTION` | No | Config | Enable GitHub PR action (default: false) |
| `GITHUB_OWNER` | No | Config | GitHub org/user for PR creation |
| `GITHUB_WEBHOOK_SECRET` | No | SSM | Webhook signature verification |

---

## Testing Strategy

### Unit Tests (138 tests)

```bash
make test
```

| Test File | Count | Covers |
|-----------|-------|--------|
| `test_actions.py` | 45 | Plan generation, Jira/Teams/GitHub dry-run, idempotency, confidence gate, kill switch, PR template, logging, evidence refs |
| `test_repo_resolver.py` | 24 | Mapping rules, trace parsing, path normalization, verification, skip logic |
| `test_webhook.py` | 69 | Webhook ingestion, signature verification, dedup, schema normalization, code context, patch planning |

### Smoke Tests

| Script | Purpose |
|--------|---------|
| `scripts/smoke_it5.sh` | Full v1 pipeline: create incident → poll → validate packet → validate actions → verify PR → replay |
| `scripts/smoke_it4.sh` | Actions-only smoke (Jira + Teams) |
| `scripts/smoke_it3.sh` | Analyzer/packet smoke |

### Integration Testing

Integration tests require live AWS resources and are not run in CI. Use smoke scripts with a deployed dev environment.

---

## Deployment Recommendation

For **v1 production**, the recommended deployment model is:

1. **API**: Deploy as AWS Lambda behind API Gateway (HTTP API). The FastAPI app is already compatible via Mangum adapter. This eliminates the need for a dedicated server.

2. **Alternative (dev)**: Continue running FastAPI locally with `uvicorn` + `aws-vault` for development and testing.

3. **Infrastructure**: All resources are managed by Terraform. No manual AWS Console steps except initial SSM secret seeding.

4. **CI/CD**: The `make ci` target (lint + test) can be integrated into any CI system. Terraform plan/apply can be added as a deployment stage.
