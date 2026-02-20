terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0"
    }
  }
}

resource "null_resource" "pip_install" {
  triggers = {
    requirements = filesha256("${path.module}/requirements.txt")
    src_hash     = sha256(join(",", [for f in fileset("${path.module}/src", "**/*.py") : filesha256("${path.module}/src/${f}")]))
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      rm -rf "${path.module}/build"
      mkdir -p "${path.module}/build"
      cp -r "${path.module}/src/"* "${path.module}/build/"
      pip install -r "${path.module}/requirements.txt" \
        -t "${path.module}/build" \
        --platform manylinux2014_x86_64 \
        --implementation cp \
        --python-version 3.12 \
        --only-binary :all: \
        --quiet
    EOT
  }
}

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/build"
  output_path = "${path.module}/dist/actions_runner.zip"
  depends_on  = [null_resource.pip_install]
}

# ──────────────────────────────────────────────────────────────────
# IAM
# ──────────────────────────────────────────────────────────────────
resource "aws_iam_role" "lambda_role" {
  name = "${var.name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "basic_exec" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "actions_runner_policy" {
  name = "OpsRunbookDevLambdaActionsRunnerPolicy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DynamoDB"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
        ]
        Resource = var.incidents_table_arn
      },
      {
        Sid      = "S3ReadPackets"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${var.evidence_bucket_arn}/*"
      },
      {
        Sid    = "SSMRead"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters",
        ]
        Resource = "arn:aws:ssm:${var.aws_region}:${var.account_id}:parameter/opsrunbook/dev/*"
      },
      {
        Sid      = "KMSDecrypt"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "arn:aws:kms:${var.aws_region}:${var.account_id}:key/alias/aws/ssm"
      },
    ]
  })
}

resource "aws_iam_role_policy" "eventbridge_put" {
  count = var.event_bus_arn != "" ? 1 : 0
  name  = "${var.name}-eventbridge-put"
  role  = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["events:PutEvents"]
      Resource = var.event_bus_arn
    }]
  })
}

# ──────────────────────────────────────────────────────────────────
# SSM Parameters (Jira + Teams config)
# Secret values use ignore_changes — set real values via console/CLI.
# ──────────────────────────────────────────────────────────────────
resource "aws_ssm_parameter" "jira_base_url" {
  name  = "/opsrunbook/dev/jira/base_url"
  type  = "String"
  value = "https://yourorg.atlassian.net"

  lifecycle { ignore_changes = [value] }
}

resource "aws_ssm_parameter" "jira_email" {
  name  = "/opsrunbook/dev/jira/email"
  type  = "String"
  value = "you@example.com"

  lifecycle { ignore_changes = [value] }
}

resource "aws_ssm_parameter" "jira_api_token" {
  name  = "/opsrunbook/dev/jira/api_token"
  type  = "SecureString"
  value = "REPLACE_ME"

  lifecycle { ignore_changes = [value] }
}

resource "aws_ssm_parameter" "jira_project_key" {
  name  = "/opsrunbook/dev/jira/project_key"
  type  = "String"
  value = "OPS"

  lifecycle { ignore_changes = [value] }
}

resource "aws_ssm_parameter" "jira_issue_type" {
  name  = "/opsrunbook/dev/jira/issue_type"
  type  = "String"
  value = "Bug"

  lifecycle { ignore_changes = [value] }
}

resource "aws_ssm_parameter" "teams_webhook_url" {
  name  = "/opsrunbook/dev/teams/webhook_url"
  type  = "SecureString"
  value = "REPLACE_ME"

  lifecycle { ignore_changes = [value] }
}

# GitHub integration (Iteration 5)
resource "aws_ssm_parameter" "github_token" {
  count = var.enable_github_pr ? 1 : 0
  name  = "/opsrunbook/dev/github/token"
  type  = "SecureString"
  value = "REPLACE_ME"

  lifecycle { ignore_changes = [value] }
}

resource "aws_ssm_parameter" "github_app_id" {
  count = var.enable_github_pr ? 1 : 0
  name  = "/opsrunbook/dev/github/app_id"
  type  = "String"
  value = "REPLACE_ME"

  lifecycle { ignore_changes = [value] }
}

resource "aws_ssm_parameter" "github_app_installation_id" {
  count = var.enable_github_pr ? 1 : 0
  name  = "/opsrunbook/dev/github/app_installation_id"
  type  = "String"
  value = "REPLACE_ME"

  lifecycle { ignore_changes = [value] }
}

resource "aws_ssm_parameter" "github_app_private_key_pem" {
  count = var.enable_github_pr ? 1 : 0
  name  = "/opsrunbook/dev/github/app_private_key_pem"
  type  = "SecureString"
  value = "REPLACE_ME"

  lifecycle { ignore_changes = [value] }
}

# ──────────────────────────────────────────────────────────────────
# Lambda
# ──────────────────────────────────────────────────────────────────
resource "aws_lambda_function" "actions_runner" {
  function_name = var.name
  role          = aws_iam_role.lambda_role.arn
  runtime       = "python3.12"
  handler       = "handler.lambda_handler"

  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  timeout     = 60
  memory_size = 256

  environment {
    variables = {
      INCIDENTS_TABLE         = var.incidents_table_name
      EVENT_BUS_NAME          = var.event_bus_name
      ACTIONS_DRY_RUN         = var.dry_run ? "true" : "false"
      ENABLE_GITHUB_PR_ACTION = var.enable_github_pr ? "true" : "false"
      GITHUB_OWNER            = var.github_owner
      GITHUB_DEFAULT_BRANCH   = var.github_default_branch
    }
  }
}

# ──────────────────────────────────────────────────────────────────
# EventBridge rule: incident.analyzed -> actions-runner
# ──────────────────────────────────────────────────────────────────
resource "aws_cloudwatch_event_rule" "incident_analyzed" {
  name           = "${var.name}-on-incident-analyzed"
  event_bus_name = var.event_bus_name
  description    = "Route incident.analyzed to actions-runner Lambda"

  event_pattern = jsonencode({
    source      = ["opsrunbook-copilot"]
    detail-type = ["incident.analyzed"]
  })
}

resource "aws_cloudwatch_event_target" "actions_runner_target" {
  rule           = aws_cloudwatch_event_rule.incident_analyzed.name
  event_bus_name = var.event_bus_name
  arn            = aws_lambda_function.actions_runner.arn
  target_id      = "actions-runner-lambda"
}

resource "aws_lambda_permission" "eventbridge_invoke" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.actions_runner.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.incident_analyzed.arn
}
