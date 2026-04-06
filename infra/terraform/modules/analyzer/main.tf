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
  llm_client_src = "${path.module}/../../../../packages/llm/llm_client.py"
}

resource "null_resource" "pip_install" {
  triggers = {
    requirements = filesha256("${path.module}/src/requirements.txt")
    src_hash     = sha256(join(",", [for f in fileset("${path.module}/src", "**/*.py") : filesha256("${path.module}/src/${f}")]))
    llm_client   = filesha256(local.llm_client_src)
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      rm -rf "${path.module}/build"
      mkdir -p "${path.module}/build"
      cp "${path.module}/src/"*.py "${path.module}/build/"
      cp "${path.module}/src/"*.json "${path.module}/build/" 2>/dev/null || true
      cp "${local.llm_client_src}" "${path.module}/build/llm_client.py"
      pip install -r "${path.module}/src/requirements.txt" \
        -t "${path.module}/build" \
        --platform manylinux2014_x86_64 \
        --implementation cp \
        --python-version 3.12 \
        --only-binary :all: \
        --quiet 2>/dev/null || \
      pip install -r "${path.module}/src/requirements.txt" \
        -t "${path.module}/build" \
        --upgrade --quiet
    EOT
  }
}

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/build"
  output_path = "${path.module}/dist/analyzer.zip"
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

resource "aws_iam_role_policy" "analyzer_policy" {
  name = "OpsRunbookDevAnalyzerLambdaPolicy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "S3Read"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${var.evidence_bucket_arn}/*"
      },
      {
        Sid      = "S3WritePackets"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${var.evidence_bucket_arn}/packets/*"
      },
      {
        Sid    = "DynamoDBPackets"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem",
          "dynamodb:Query",
        ]
        Resource = var.packets_table_arn
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
resource "aws_lambda_function" "analyzer" {
  function_name = var.name
  role          = aws_iam_role.lambda_role.arn
  runtime       = "python3.12"
  handler       = "handler.lambda_handler"

  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  timeout     = 120
  memory_size = 256

  environment {
    variables = {
      PACKETS_TABLE      = var.packets_table_name
      EVENT_BUS_NAME     = var.event_bus_name
      LLM_PROVIDER       = var.llm_provider
      SSM_GROQ_API_KEY   = "/opsrunbook/dev/groq/api_key"
      SSM_GOOGLE_API_KEY = "/opsrunbook/dev/google/api_key"
    }
  }
}

# ──────────────────────────────────────────────────────────────────
# EventBridge rule: evidence.snapshot.persisted -> analyzer
# ──────────────────────────────────────────────────────────────────
resource "aws_cloudwatch_event_rule" "snapshot_persisted" {
  name           = "${var.name}-on-snapshot-persisted"
  event_bus_name = var.event_bus_name
  description    = "Route evidence.snapshot.persisted to analyzer Lambda"

  event_pattern = jsonencode({
    source      = ["opsrunbook-copilot"]
    detail-type = ["evidence.snapshot.persisted"]
  })
}

resource "aws_cloudwatch_event_target" "analyzer_target" {
  rule           = aws_cloudwatch_event_rule.snapshot_persisted.name
  event_bus_name = var.event_bus_name
  arn            = aws_lambda_function.analyzer.arn
  target_id      = "analyzer-lambda"
}

resource "aws_lambda_permission" "eventbridge_invoke" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.analyzer.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.snapshot_persisted.arn
}
