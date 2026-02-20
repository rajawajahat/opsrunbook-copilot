terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0"
    }
  }
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# ── Build: pip install deps ──────────────────────────────────────

resource "null_resource" "pip_install" {
  triggers = {
    requirements = filemd5("${path.module}/requirements.txt")
    src_hash     = sha256(join("", [for f in fileset("${path.module}/src", "**") : filemd5("${path.module}/src/${f}")]))
  }

  provisioner "local-exec" {
    command = <<-EOT
      rm -rf ${path.module}/build
      mkdir -p ${path.module}/build
      cp ${path.module}/src/*.py ${path.module}/build/
      pip install -r ${path.module}/requirements.txt \
        --target ${path.module}/build \
        --platform manylinux2014_x86_64 \
        --implementation cp \
        --python-version 3.12 \
        --only-binary=:all: \
        --upgrade --quiet 2>/dev/null || \
      pip install -r ${path.module}/requirements.txt \
        --target ${path.module}/build \
        --upgrade --quiet
    EOT
  }
}

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/build"
  output_path = "${path.module}/lambda.zip"
  depends_on  = [null_resource.pip_install]
}

# ── Lambda ───────────────────────────────────────────────────────

resource "aws_lambda_function" "pr_review_handler" {
  function_name = "${var.name}-pr-review-handler"
  role          = aws_iam_role.lambda_role.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 120
  memory_size   = 256

  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  environment {
    variables = {
      EVIDENCE_BUCKET           = var.evidence_bucket
      INCIDENTS_TABLE           = var.incidents_table
      GITHUB_OWNER              = var.github_owner
      GITHUB_APP_SLUG           = var.github_app_slug
      GITHUB_ALLOWED_PATHS      = var.github_allowed_paths
      LLM_PROVIDER              = var.llm_provider
      SSM_GITHUB_TOKEN          = "/opsrunbook/dev/github/token"
      SSM_GITHUB_APP_ID         = "/opsrunbook/dev/github/app_id"
      SSM_GITHUB_APP_INSTALL_ID = "/opsrunbook/dev/github/app_installation_id"
      SSM_GITHUB_APP_PEM        = "/opsrunbook/dev/github/app_private_key_pem"
    }
  }
}

# ── IAM ──────────────────────────────────────────────────────────

resource "aws_iam_role" "lambda_role" {
  name = "${var.name}-pr-review-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda_policy" {
  name = "OpsRunbookDevPRReviewHandlerPolicy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "S3ReadWrite"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${var.evidence_bucket_arn}/*"
      },
      {
        Sid      = "DynamoDB"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        Resource = var.incidents_table_arn
      },
      {
        Sid      = "SSM"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter/opsrunbook/dev/*"
      },
      {
        Sid      = "KMSDecrypt"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "*"
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
    ]
  })
}

# ── Step Function ────────────────────────────────────────────────

resource "aws_iam_role" "sfn_role" {
  name = "${var.name}-pr-review-sfn-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "sfn_policy" {
  name = "${var.name}-pr-review-sfn-policy"
  role = aws_iam_role.sfn_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InvokeLambda"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = aws_lambda_function.pr_review_handler.arn
      },
      {
        Sid      = "EventBridge"
        Effect   = "Allow"
        Action   = ["events:PutEvents"]
        Resource = var.event_bus_arn
      },
    ]
  })
}

resource "aws_sfn_state_machine" "pr_review_cycle" {
  name     = "${var.name}-pr-review-cycle"
  role_arn = aws_iam_role.sfn_role.arn

  definition = templatefile("${path.module}/definition.asl.json", {
    handler_arn = aws_lambda_function.pr_review_handler.arn
  })
}
