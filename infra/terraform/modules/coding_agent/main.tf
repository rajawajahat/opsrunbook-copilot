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

locals {
  agent_pkg_src = "${path.module}/../../../../packages/agent"
}

resource "null_resource" "pip_install" {
  triggers = {
    requirements = filesha256("${path.module}/requirements.txt")
    src_hash     = sha256(join(",", [for f in fileset("${path.module}/src", "**/*.py") : filesha256("${path.module}/src/${f}")]))
    agent_hash   = sha256(join(",", [for f in fileset(local.agent_pkg_src, "*.py") : filesha256("${local.agent_pkg_src}/${f}")]))
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      rm -rf "${path.module}/build"
      mkdir -p "${path.module}/build/agent"

      # Copy Lambda handler
      cp "${path.module}/src/handler.py" "${path.module}/build/"

      # Copy agent package (preserving package structure for imports)
      cp "${local.agent_pkg_src}/"*.py "${path.module}/build/agent/"

      # Install Python dependencies
      pip install -r "${path.module}/requirements.txt" \
        -t "${path.module}/build" \
        --platform manylinux2014_x86_64 \
        --implementation cp \
        --python-version 3.12 \
        --only-binary :all: \
        --quiet 2>/dev/null || \
      pip install -r "${path.module}/requirements.txt" \
        -t "${path.module}/build" \
        --upgrade --quiet
    EOT
  }
}

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/build"
  output_path = "${path.module}/dist/coding_agent.zip"
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

resource "aws_iam_role_policy" "coding_agent_policy" {
  name = "${var.name}-policy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
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
# Lambda
# ──────────────────────────────────────────────────────────────────
resource "aws_lambda_function" "coding_agent" {
  function_name = var.name
  role          = aws_iam_role.lambda_role.arn
  runtime       = "python3.12"
  handler       = "handler.lambda_handler"

  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  timeout     = 300
  memory_size = 512

  environment {
    variables = {
      EVIDENCE_BUCKET  = var.evidence_bucket
      PACKETS_TABLE    = var.packets_table_name
      INCIDENTS_TABLE  = var.incidents_table_name
      EVENT_BUS_NAME   = var.event_bus_name
      GITHUB_OWNER     = var.github_owner
      LLM_PROVIDER     = var.llm_provider
      LLM_MODEL        = var.llm_model
      GROQ_API_KEY_SSM = "/opsrunbook/dev/groq/api_key"
      GITHUB_TOKEN_SSM = "/opsrunbook/dev/github/token"
    }
  }
}

# ──────────────────────────────────────────────────────────────────
# EventBridge rule: actions_runner.completed -> coding-agent
# ──────────────────────────────────────────────────────────────────
resource "aws_cloudwatch_event_rule" "actions_completed" {
  name           = "${var.name}-on-actions-completed"
  event_bus_name = var.event_bus_name
  description    = "Route actions_runner.completed to coding-agent Lambda"

  event_pattern = jsonencode({
    source      = ["opsrunbook-copilot"]
    detail-type = ["actions_runner.completed"]
  })
}

resource "aws_cloudwatch_event_target" "coding_agent_target" {
  rule           = aws_cloudwatch_event_rule.actions_completed.name
  event_bus_name = var.event_bus_name
  arn            = aws_lambda_function.coding_agent.arn
  target_id      = "coding-agent-lambda"
}

resource "aws_lambda_permission" "eventbridge_invoke" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.coding_agent.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.actions_completed.arn
}
