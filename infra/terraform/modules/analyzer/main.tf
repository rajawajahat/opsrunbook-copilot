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
  }
}

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/src"
  output_path = "${path.module}/dist/analyzer.zip"
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

  timeout     = 60
  memory_size = 256

  environment {
    variables = {
      PACKETS_TABLE  = var.packets_table_name
      EVENT_BUS_NAME = var.event_bus_name
      LLM_PROVIDER   = var.llm_provider
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
