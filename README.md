# OpsRunbook Copilot

An open-source agentic incident-response pipeline that monitors your AWS services,
analyses errors with an LLM, and automatically:

- Creates a **Jira ticket** with structured findings
- Sends a **Teams / Slack notification**
- Opens a **GitHub Pull Request** with a proposed code fix

Everything runs in your own AWS account. No data leaves your infrastructure except
for the LLM API call (Groq or Google Gemini — swappable).

---

## How It Works

```
Alert / manual trigger
        │
        ▼
┌─────────────────────┐
│   FastAPI  (local   │  POST /v1/incidents
│   or Lambda+APIGW)  │
└──────────┬──────────┘
           │ starts
           ▼
┌─────────────────────────────────────────────────────┐
│            Step Functions Orchestrator              │
│  ┌──────────────┐ ┌───────────┐ ┌───────────────┐  │
│  │ collector_   │ │collector_ │ │ collector_    │  │
│  │ logs         │ │ metrics   │ │ stepfn        │  │
│  └──────────────┘ └───────────┘ └───────────────┘  │
│              └──────────┬──────────┘               │
│                         ▼                           │
│               snapshot_persist Lambda               │
└─────────────────────────┬───────────────────────────┘
                          │ EventBridge
                          ▼
              ┌───────────────────────┐
              │   Analyzer Lambda     │  LLM → IncidentPacket
              │   (Groq / Gemini)     │  findings, hypotheses,
              └───────────┬───────────┘  suspected_owners
                          │ EventBridge
                          ▼
              ┌───────────────────────┐
              │  Actions Runner Lambda│  → Jira ticket
              │                       │  → Teams notification
              └───────────┬───────────┘  → GitHub PR (simple)
                          │ EventBridge
                          ▼
              ┌───────────────────────┐
              │  Coding Agent Lambda  │  LangGraph ReAct agent
              │  (LangGraph + Groq)   │  → GitHub PR (code fix)
              └───────────────────────┘

GitHub PR review comment → pr_review_cycle → LLM fix → commit
```

---

## Prerequisites

| Requirement | Notes |
|---|---|
| AWS account | IAM user/role with the permissions listed below |
| Terraform ≥ 1.5 | [Install](https://developer.hashicorp.com/terraform/install) |
| Python 3.11+ | For running the API and tests locally |
| Groq API key **or** Google Gemini API key | Free tier works. Or use `llm_provider = "stub"` for deterministic heuristics without an LLM |
| Jira Cloud account | For ticket creation (optional — disable via `ACTIONS_DRY_RUN=true`) |
| GitHub App or PAT | For opening fix PRs (optional) |
| Teams incoming webhook | For notifications (optional) |

---

## Quick Start (5 steps)

### 1. Clone and set up Python environment

```bash
git clone https://github.com/rajawajahat/opsrunbook-copilot.git
cd opsrunbook-copilot

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e "packages/contracts"
pip install -r services/api/src/../../../requirements.txt 2>/dev/null || true
pip install fastapi uvicorn boto3 pydantic python-dotenv
```

### 2. Configure Terraform

```bash
cd infra/terraform/envs/dev

# Copy the example and fill in your values
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars — set aws_profile, github_owner, etc.
```

### 3. Deploy AWS infrastructure

```bash
# From infra/terraform/envs/dev/
terraform init
terraform plan   # review what will be created
terraform apply
```

This creates: S3 bucket, DynamoDB tables, Lambda functions, Step Functions state machine, EventBridge bus, IAM roles, SSM parameter containers.

### 4. Set secret values in SSM

Terraform creates SSM parameter **containers** with placeholder values.
You must set the real values manually (never via Terraform):

```bash
# LLM API key (choose one)
aws ssm put-parameter --name /opsrunbook/dev/groq/api_key \
  --value "YOUR_GROQ_KEY" --type SecureString --overwrite

# OR for Gemini
aws ssm put-parameter --name /opsrunbook/dev/google/api_key \
  --value "YOUR_GOOGLE_KEY" --type SecureString --overwrite

# GitHub PAT (needs repo + pull_requests scopes)
aws ssm put-parameter --name /opsrunbook/dev/github/token \
  --value "ghp_yourtoken" --type SecureString --overwrite

# Jira credentials
aws ssm put-parameter --name /opsrunbook/dev/jira/base_url \
  --value "https://your-org.atlassian.net" --type String --overwrite

aws ssm put-parameter --name /opsrunbook/dev/jira/email \
  --value "your@email.com" --type String --overwrite

aws ssm put-parameter --name /opsrunbook/dev/jira/api_token \
  --value "YOUR_JIRA_TOKEN" --type SecureString --overwrite

aws ssm put-parameter --name /opsrunbook/dev/jira/project_key \
  --value "OPS" --type String --overwrite

# Teams webhook (optional)
aws ssm put-parameter --name /opsrunbook/dev/teams/webhook_url \
  --value "https://your-org.webhook.office.com/..." --type SecureString --overwrite

# GitHub webhook secret (for PR review cycle)
aws ssm put-parameter --name /opsrunbook/dev/github/webhook_secret \
  --value "$(openssl rand -hex 32)" --type SecureString --overwrite
```

### 5. Configure and start the API

```bash
cd services/api

# Copy .env.example and fill in values from terraform output
cp .env.example .env

# Get Terraform outputs
cd ../../infra/terraform/envs/dev
terraform output

# Fill these into services/api/.env:
#   EVIDENCE_BUCKET, INCIDENTS_TABLE, SNAPSHOTS_TABLE, PACKETS_TABLE
#   STATE_MACHINE_ARN, EVENT_BUS_NAME

# Generate an API key and add to .env
python -c "import secrets; print(secrets.token_urlsafe(32))"
# Add:  API_KEY=<generated value>

# Start the API
cd ../../services/api
aws-vault exec <your-aws-profile> -- bash -c \
  'PYTHONPATH=src:../../packages/contracts/src \
   PLAN_GENERATOR_PATH=$(pwd)/../../infra/terraform/modules/actions_runner/src \
   ../../.venv/bin/uvicorn src.app:app --port 8100 --log-level info'
```

### Run the smoke test

```bash
cd /path/to/opsrunbook-copilot

export GITHUB_OWNER=your-github-org
API_URL=http://127.0.0.1:8100 bash scripts/smoke_it5.sh
```

---

## AWS IAM Permissions Required

The IAM principal (user or role) used by Terraform and the running Lambdas needs the
following permissions. Use separate roles for Terraform deployment vs Lambda execution
(principle of least privilege).

### Terraform deployment role

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": ["s3:*"],           "Resource": "arn:aws:s3:::opsrunbook-copilot-*" },
    { "Effect": "Allow", "Action": ["dynamodb:*"],      "Resource": "arn:aws:dynamodb:*:*:table/opsrunbook-copilot-*" },
    { "Effect": "Allow", "Action": ["lambda:*"],        "Resource": "arn:aws:lambda:*:*:function:opsrunbook-copilot-*" },
    { "Effect": "Allow", "Action": ["states:*"],        "Resource": "arn:aws:states:*:*:stateMachine:opsrunbook-copilot-*" },
    { "Effect": "Allow", "Action": ["events:*"],        "Resource": "*" },
    { "Effect": "Allow", "Action": ["iam:*"],           "Resource": "arn:aws:iam::*:role/opsrunbook-copilot-*" },
    { "Effect": "Allow", "Action": ["ssm:PutParameter","ssm:GetParameter","ssm:DeleteParameter","ssm:DescribeParameters"], "Resource": "arn:aws:ssm:*:*:parameter/opsrunbook/*" },
    { "Effect": "Allow", "Action": ["logs:*"],          "Resource": "arn:aws:logs:*:*:log-group:/aws/lambda/opsrunbook-copilot-*" }
  ]
}
```

### Lambda execution roles (created automatically by Terraform)

Each Lambda is granted only what it needs:

| Lambda | Key permissions |
|---|---|
| `collector_logs` | `logs:StartQuery`, `logs:GetQueryResults`, `s3:PutObject`, `events:PutEvents` |
| `collector_metrics` | `cloudwatch:GetMetricData`, `s3:PutObject`, `events:PutEvents` |
| `collector_stepfn` | `states:DescribeExecution`, `states:ListExecutions`, `s3:PutObject`, `events:PutEvents` |
| `snapshot_persist` | `s3:GetObject`, `s3:PutObject`, `dynamodb:PutItem`, `events:PutEvents` |
| `analyzer` | `s3:GetObject`, `s3:PutObject`, `dynamodb:PutItem`, `events:PutEvents`, `ssm:GetParameter` |
| `actions_runner` | `s3:GetObject`, `dynamodb:PutItem`, `dynamodb:Query`, `events:PutEvents`, `ssm:GetParameter` |
| `coding_agent` | `s3:GetObject`, `dynamodb:PutItem`, `events:PutEvents`, `ssm:GetParameter` |
| `pr_review_cycle` | `s3:GetObject`, `s3:PutObject`, `dynamodb:PutItem`, `ssm:GetParameter` |

---

## Configuration Reference

### `services/api/.env`

| Variable | Required | Description |
|---|---|---|
| `API_KEY` | Yes | Random secret for `X-API-Key` header auth. Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `AWS_REGION` | Yes | AWS region (e.g. `us-east-1`) |
| `EVIDENCE_BUCKET` | Yes | S3 bucket name (from `terraform output evidence_bucket`) |
| `INCIDENTS_TABLE` | Yes | DynamoDB table (from `terraform output incidents_table`) |
| `SNAPSHOTS_TABLE` | Yes | DynamoDB table (from `terraform output snapshots_table`) |
| `PACKETS_TABLE` | Yes | DynamoDB table (from `terraform output packets_table`) |
| `STATE_MACHINE_ARN` | Yes | Step Functions ARN (from `terraform output state_machine_arn`) |
| `EVENT_BUS_NAME` | Yes | EventBridge bus name (from `terraform output event_bus_name`) |
| `GITHUB_WEBHOOK_SECRET` | For PR review | Must match SSM `/opsrunbook/dev/github/webhook_secret` |
| `GITHUB_APP_SLUG` | For PR review | Your GitHub App slug (e.g. `opsrunbook-copilot-bot`) |
| `PR_REVIEW_STATE_MACHINE_ARN` | For PR review | From `terraform output pr_review_state_machine_arn` |
| `PLAN_GENERATOR_PATH` | Local dev | Path to `infra/terraform/modules/actions_runner/src` for the replay endpoint |
| `MAX_ROWS_PER_QUERY` | No | CloudWatch Insights row budget (default: `100`) |
| `MAX_BYTES_TOTAL` | No | Evidence byte budget (default: `200000`) |
| `MAX_TIME_WINDOW_MINUTES` | No | Max analysis window (default: `15`) |

### SSM Parameters (set manually — never via Terraform)

| SSM Path | Type | Description |
|---|---|---|
| `/opsrunbook/dev/groq/api_key` | SecureString | Groq API key |
| `/opsrunbook/dev/google/api_key` | SecureString | Google Gemini API key |
| `/opsrunbook/dev/github/token` | SecureString | GitHub PAT (`repo` + `pull_requests` scopes) |
| `/opsrunbook/dev/github/webhook_secret` | SecureString | HMAC secret registered on your GitHub App |
| `/opsrunbook/dev/jira/base_url` | String | `https://your-org.atlassian.net` |
| `/opsrunbook/dev/jira/email` | String | Jira user email |
| `/opsrunbook/dev/jira/api_token` | SecureString | Jira API token |
| `/opsrunbook/dev/jira/project_key` | String | Jira project key (e.g. `OPS`) |
| `/opsrunbook/dev/jira/issue_type` | String | Issue type (default: `Bug`) |
| `/opsrunbook/dev/teams/webhook_url` | SecureString | Teams incoming webhook URL |

### `infra/terraform/envs/dev/terraform.tfvars`

| Variable | Description |
|---|---|
| `aws_region` | AWS region |
| `aws_profile` | AWS CLI profile name |
| `github_owner` | GitHub org or username owning the target repos |
| `github_default_branch` | Default branch for fix PRs (default: `main`) |
| `github_app_slug` | GitHub App slug for self-event filtering |
| `llm_provider` | `groq` \| `gemini` \| `stub` |
| `evidence_retention_days` | S3 object retention (default: `7`) |

---

## Calling the API

All endpoints (except `GET /health`) require the `X-API-Key` header.

```bash
export API=http://127.0.0.1:8100
export API_KEY=your-api-key-here

# Trigger incident analysis
curl -X POST "$API/v1/incidents" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "schema_version": "incident_event.v1",
    "event_id": "evt-unique-id-001",
    "service": "payment-service",
    "environment": "prod",
    "severity": "critical",
    "time_window": {
      "start": "2026-04-06T20:00:00Z",
      "end":   "2026-04-06T20:15:00Z"
    },
    "hints": {
      "log_groups": ["/aws/lambda/payment-service"]
    }
  }'

# Poll orchestrator status
curl -H "X-API-Key: $API_KEY" \
  "$API/v1/incidents/{incident_id}/runs/{collector_run_id}"

# Get LLM analysis
curl -H "X-API-Key: $API_KEY" \
  "$API/v1/incidents/{incident_id}/packet/latest"

# Get Jira/Teams/PR action results
curl -H "X-API-Key: $API_KEY" \
  "$API/v1/incidents/{incident_id}/actions/latest"
```

---

## Adapting to Your Services

### Point it at your own CloudWatch log groups

In the `POST /v1/incidents` body, set `hints.log_groups` to your application's log groups:

```json
"hints": {
  "log_groups": [
    "/aws/lambda/my-payment-service",
    "/aws/ecs/my-payment-processor",
    "/aws/rds/my-db-errors"
  ]
}
```

### Add CloudWatch metric queries

```json
"hints": {
  "log_groups": ["/aws/lambda/my-service"],
  "metric_queries": [
    {
      "namespace": "AWS/Lambda",
      "metric_name": "Errors",
      "dimensions": {"FunctionName": "my-service"},
      "period": 300,
      "stat": "Sum"
    }
  ]
}
```

### Map services to GitHub repos

Create `infra/terraform/modules/analyzer/src/resource_repo_map.json`:

```json
{
  "payment-service":   "your-org/payment-service",
  "auth-service":      "your-org/auth-service",
  "order-processor":   "your-org/order-processor"
}
```

This tells the analyzer which repo owns which service, so the coding agent
opens fix PRs on the correct repository.

### Disable automations you don't need

```bash
# Dry-run mode: analyse and create Jira but don't open GitHub PRs
ACTIONS_DRY_RUN=true

# Kill switch: analyse only, no external actions at all
AUTOMATION_ENABLED=false

# Use stub LLM (no API key needed, deterministic heuristics)
# Set in terraform.tfvars:
llm_provider = "stub"
```

---

## Local Development

```bash
# Run all tests
bash scripts/test.sh

# Lint
bash scripts/lint.sh

# Smoke test: full pipeline (requires AWS + API running)
export GITHUB_OWNER=your-github-org
API_URL=http://127.0.0.1:8100 bash scripts/smoke_it5.sh

# Smoke test: webhook → PR review cycle
GITHUB_WEBHOOK_SECRET=<secret> GITHUB_REPO=owner/repo PR_NUMBER=17 \
  API_URL=http://127.0.0.1:8100 bash scripts/smoke_it6.sh
```

### Running the coding agent locally (without Lambda)

```bash
cd /path/to/opsrunbook-copilot

export GROQ_API_KEY=your-key
export GITHUB_TOKEN=ghp_yourtoken

python -m packages.agent.runner \
  --packet-file tests/fixtures/sample_packet.json \
  --repo your-org/your-repo \
  --dry-run   # omit --dry-run to actually open a PR
```

---

## Security Notes

- **Secrets**: All API keys and tokens are stored in AWS SSM Parameter Store (SecureString). They are never in Terraform state, environment files, or source code.
- **API authentication**: The FastAPI layer requires an `X-API-Key` header on all routes (except `GET /health`). Deploy behind a VPC or API Gateway with additional network controls in production.
- **GitHub webhooks**: Verified with HMAC-SHA256 using `hmac.compare_digest` (timing-safe).
- **Evidence redaction**: Logs are scanned for tokens, API keys, passwords, AWS credentials, and connection strings before persistence.
- **LLM data**: Only redacted evidence text is sent to the LLM provider. Raw log lines with secrets are never forwarded.
- **IAM**: Lambda execution roles follow least privilege (see permissions table above). Broad `*` actions are never granted.

---

## Project Layout

```
opsrunbook-copilot/
├── packages/
│   ├── agent/          # LangGraph ReAct coding agent
│   ├── contracts/      # Pydantic v2 shared schemas (installable package)
│   └── llm/            # Shared LLM client (Groq / Gemini / stub)
├── services/
│   ├── api/            # FastAPI entry point + routers + stores
│   └── collectors/     # CloudWatch / Step Functions collector library
├── infra/terraform/
│   ├── envs/dev/       # Dev environment wiring (main.tf, variables.tf)
│   └── modules/        # One module per Lambda function
│       ├── actions_runner/
│       ├── analyzer/
│       ├── coding_agent/
│       ├── collector_logs/
│       ├── collector_metrics/
│       ├── collector_stepfn/
│       ├── orchestrator/
│       ├── pr_review_cycle/
│       ├── snapshot_persist/
│       └── storage/
├── tests/              # Integration tests + fixtures
└── scripts/            # smoke_it*.sh, lint.sh, test.sh
```

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feat/your-feature`
3. Make changes and add tests
4. Run `bash scripts/lint.sh && bash scripts/test.sh`
5. Open a pull request

Please do not commit `.env` files, `terraform.tfvars`, `*.tfstate`, or any file
containing real credentials. The `.gitignore` is configured to block these.

---

## License

MIT
