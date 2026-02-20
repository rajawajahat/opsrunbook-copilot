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

variable "aws_region" { type = string }
variable "aws_profile" { type = string }

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
  evidence_retention_days = 7
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
  github_owner          = "rajawajahat"
  github_default_branch = "main"
}

output "actions_runner_arn" { value = module.actions_runner.lambda_arn }

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
  github_owner        = "rajawajahat"
  github_app_slug     = "opsrunbook-copilot-bot"
  llm_provider        = "stub"
}

output "pr_review_state_machine_arn" { value = module.pr_review_cycle.state_machine_arn }
output "pr_review_lambda_arn" { value = module.pr_review_cycle.lambda_function_arn }

# GitHub webhook secret SSM parameter (placeholder – set value manually)
resource "aws_ssm_parameter" "github_webhook_secret" {
  name  = "/opsrunbook/dev/github/webhook_secret"
  type  = "SecureString"
  value = "REPLACE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}
