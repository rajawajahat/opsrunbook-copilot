terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      env = "dev"
      app = "opsrunbook-copilot"
    }
  }
}

data "aws_caller_identity" "current" {}

# Variables are declared in variables.tf

locals {
  prefix     = "opsrunbook-copilot-dev"
  account_id = data.aws_caller_identity.current.account_id
}

# ──────────────────────────────────────────────────────────────────
# Storage (S3 + DynamoDB)  – unchanged from iter-1
# ──────────────────────────────────────────────────────────────────
module "storage" {
  source = "../../modules/storage"

  project                 = "opsrunbook-copilot"
  env                     = "dev"
  aws_region              = var.aws_region
  evidence_retention_days = var.evidence_retention_days
}

output "evidence_bucket" { value = module.storage.evidence_bucket }
output "incidents_table" { value = module.storage.incidents_table }
output "snapshots_table" { value = module.storage.snapshots_table }
output "packets_table" { value = module.storage.packets_table }
output "aws_region" { value = var.aws_region }
output "aws_profile" { value = var.aws_profile }

# ──────────────────────────────────────────────────────────────────
# Log generator Lambda – unchanged from iter-1
# ──────────────────────────────────────────────────────────────────
module "loggen" {
  source = "../../modules/loggen"

  name               = "${local.prefix}-loggen"
  app_name           = "opsrunbook-copilot"
  env                = "dev"
  log_retention_days = 7
}

output "loggen_log_group" {
  value = module.loggen.log_group_name
}

# ──────────────────────────────────────────────────────────────────
# EventBridge custom bus (Iteration 2)
# ──────────────────────────────────────────────────────────────────
resource "aws_cloudwatch_event_bus" "copilot" {
  name = "${local.prefix}-events"
}

output "event_bus_name" { value = aws_cloudwatch_event_bus.copilot.name }
output "event_bus_arn" { value = aws_cloudwatch_event_bus.copilot.arn }

# ──────────────────────────────────────────────────────────────────
# Collector Lambdas (Iteration 2)
# ──────────────────────────────────────────────────────────────────
module "collector_logs" {
  source = "../../modules/collector_logs"

  name            = "${local.prefix}-collector-logs"
  aws_region      = var.aws_region
  account_id      = local.account_id
  evidence_bucket = module.storage.evidence_bucket
  event_bus_name  = aws_cloudwatch_event_bus.copilot.name
  event_bus_arn   = aws_cloudwatch_event_bus.copilot.arn
}

module "collector_metrics" {
  source = "../../modules/collector_metrics"

  name            = "${local.prefix}-collector-metrics"
  aws_region      = var.aws_region
  account_id      = local.account_id
  evidence_bucket = module.storage.evidence_bucket
  event_bus_name  = aws_cloudwatch_event_bus.copilot.name
  event_bus_arn   = aws_cloudwatch_event_bus.copilot.arn
}

module "collector_stepfn" {
  source = "../../modules/collector_stepfn"

  name            = "${local.prefix}-collector-stepfn"
  aws_region      = var.aws_region
  account_id      = local.account_id
  evidence_bucket = module.storage.evidence_bucket
  event_bus_name  = aws_cloudwatch_event_bus.copilot.name
  event_bus_arn   = aws_cloudwatch_event_bus.copilot.arn
}

output "collector_logs_arn" { value = module.collector_logs.lambda_arn }
output "collector_metrics_arn" { value = module.collector_metrics.lambda_arn }
output "collector_stepfn_arn" { value = module.collector_stepfn.lambda_arn }

# ──────────────────────────────────────────────────────────────────
# Snapshot persist Lambda (writes SNAPSHOT# + aggregated evidence)
# ──────────────────────────────────────────────────────────────────
module "snapshot_persist" {
  source = "../../modules/snapshot_persist"

  name                 = "${local.prefix}-snapshot-persist"
  evidence_bucket      = module.storage.evidence_bucket
  snapshots_table_name = module.storage.snapshots_table
  snapshots_table_arn  = module.storage.snapshots_table_arn
  event_bus_name       = aws_cloudwatch_event_bus.copilot.name
  event_bus_arn        = aws_cloudwatch_event_bus.copilot.arn
}

output "snapshot_persist_arn" { value = module.snapshot_persist.lambda_arn }

# ──────────────────────────────────────────────────────────────────
# Orchestrator Step Functions (Iteration 2)
# ──────────────────────────────────────────────────────────────────
module "orchestrator" {
  source = "../../modules/orchestrator"

  name                  = "${local.prefix}-orchestrator"
  logs_collector_arn    = module.collector_logs.lambda_arn
  metrics_collector_arn = module.collector_metrics.lambda_arn
  stepfn_collector_arn  = module.collector_stepfn.lambda_arn
  snapshot_persist_arn  = module.snapshot_persist.lambda_arn
  collector_lambda_arns = [
    module.collector_logs.lambda_arn,
    module.collector_metrics.lambda_arn,
    module.collector_stepfn.lambda_arn,
  ]
  event_bus_name = aws_cloudwatch_event_bus.copilot.name
  event_bus_arn  = aws_cloudwatch_event_bus.copilot.arn
}

output "state_machine_arn" { value = module.orchestrator.state_machine_arn }

# ──────────────────────────────────────────────────────────────────
# Analyzer Lambda (Iteration 3)
# ──────────────────────────────────────────────────────────────────
module "analyzer" {
  source = "../../modules/analyzer"

  name                = "${local.prefix}-analyzer"
  evidence_bucket     = module.storage.evidence_bucket
  evidence_bucket_arn = module.storage.evidence_bucket_arn
  packets_table_name  = module.storage.packets_table
  packets_table_arn   = module.storage.packets_table_arn
  event_bus_name      = aws_cloudwatch_event_bus.copilot.name
  event_bus_arn       = aws_cloudwatch_event_bus.copilot.arn
  llm_provider        = "groq"
  aws_region          = var.aws_region
  account_id          = local.account_id
}

output "analyzer_lambda_arn" { value = module.analyzer.lambda_arn }
output "packets_table_arn" { value = module.storage.packets_table_arn }

# ──────────────────────────────────────────────────────────────────
# Actions Runner Lambda (Iteration 4)
# ──────────────────────────────────────────────────────────────────
module "actions_runner" {
  source = "../../modules/actions_runner"

  name                  = "${local.prefix}-actions-runner"
  incidents_table_name  = module.storage.incidents_table
  incidents_table_arn   = module.storage.incidents_table_arn
  evidence_bucket_arn   = module.storage.evidence_bucket_arn
  event_bus_name        = aws_cloudwatch_event_bus.copilot.name
  event_bus_arn         = aws_cloudwatch_event_bus.copilot.arn
  aws_region            = var.aws_region
  account_id            = local.account_id
  dry_run               = false
  enable_github_pr      = true
  github_owner          = var.github_owner
  github_default_branch = var.github_default_branch
  llm_provider          = var.llm_provider
}

output "actions_runner_arn" { value = module.actions_runner.lambda_arn }

# ──────────────────────────────────────────────────────────────────
# Coding Agent Lambda — auto-fix via LLM agent after Actions Runner
# ──────────────────────────────────────────────────────────────────
module "coding_agent" {
  source = "../../modules/coding_agent"

  name                 = "${local.prefix}-coding-agent"
  evidence_bucket      = module.storage.evidence_bucket
  evidence_bucket_arn  = module.storage.evidence_bucket_arn
  packets_table_name   = module.storage.packets_table
  packets_table_arn    = module.storage.packets_table_arn
  incidents_table_name = module.storage.incidents_table
  incidents_table_arn  = module.storage.incidents_table_arn
  event_bus_name       = aws_cloudwatch_event_bus.copilot.name
  event_bus_arn        = aws_cloudwatch_event_bus.copilot.arn
  github_owner         = var.github_owner
  llm_provider         = var.llm_provider
  aws_region           = var.aws_region
  account_id           = local.account_id
}

output "coding_agent_arn" { value = module.coding_agent.lambda_arn }

# ──────────────────────────────────────────────────────────────────
# PR Review Cycle (Iteration 6)
# ──────────────────────────────────────────────────────────────────
module "pr_review_cycle" {
  source = "../../modules/pr_review_cycle"

  name                = local.prefix
  evidence_bucket     = module.storage.evidence_bucket
  evidence_bucket_arn = module.storage.evidence_bucket_arn
  incidents_table     = module.storage.incidents_table
  incidents_table_arn = module.storage.incidents_table_arn
  event_bus_name      = aws_cloudwatch_event_bus.copilot.name
  event_bus_arn       = aws_cloudwatch_event_bus.copilot.arn
  github_owner        = var.github_owner
  github_app_slug     = var.github_app_slug
  llm_provider        = var.llm_provider
}

output "pr_review_state_machine_arn" { value = module.pr_review_cycle.state_machine_arn }
output "pr_review_lambda_arn" { value = module.pr_review_cycle.lambda_function_arn }

# Google API key for Gemini LLM (placeholder – set value manually)
resource "aws_ssm_parameter" "google_api_key" {
  name  = "/opsrunbook/dev/google/api_key"
  type  = "SecureString"
  value = "REPLACE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}

# Groq API key for Llama LLM (placeholder – set value manually)
resource "aws_ssm_parameter" "groq_api_key" {
  name  = "/opsrunbook/dev/groq/api_key"
  type  = "SecureString"
  value = "REPLACE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}

# GitHub webhook secret SSM parameter (placeholder – set value manually)
resource "aws_ssm_parameter" "github_webhook_secret" {
  name  = "/opsrunbook/dev/github/webhook_secret"
  type  = "SecureString"
  value = "REPLACE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}

# GitHub PAT for actions_runner and coding_agent (placeholder – set value manually)
resource "aws_ssm_parameter" "github_token" {
  name  = "/opsrunbook/dev/github/token"
  type  = "SecureString"
  value = "REPLACE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}

# Jira credentials (placeholders – set values manually)
resource "aws_ssm_parameter" "jira_base_url" {
  name  = "/opsrunbook/dev/jira/base_url"
  type  = "String"
  value = "REPLACE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "jira_email" {
  name  = "/opsrunbook/dev/jira/email"
  type  = "String"
  value = "REPLACE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "jira_api_token" {
  name  = "/opsrunbook/dev/jira/api_token"
  type  = "SecureString"
  value = "REPLACE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "jira_project_key" {
  name  = "/opsrunbook/dev/jira/project_key"
  type  = "String"
  value = "REPLACE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "jira_issue_type" {
  name  = "/opsrunbook/dev/jira/issue_type"
  type  = "String"
  value = "Bug"

  lifecycle {
    ignore_changes = [value]
  }
}

# Teams webhook URL (placeholder – set value manually)
resource "aws_ssm_parameter" "teams_webhook_url" {
  name  = "/opsrunbook/dev/teams/webhook_url"
  type  = "SecureString"
  value = "REPLACE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}
