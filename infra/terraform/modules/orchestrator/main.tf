terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

# --- IAM Role for Step Functions ---
resource "aws_iam_role" "sfn_role" {
  name = "${var.name}-sfn-role"

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
  name = "${var.name}-sfn-policy"
  role = aws_iam_role.sfn_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "InvokeCollectorLambdas"
        Effect = "Allow"
        Action = [
          "lambda:InvokeFunction",
        ]
        Resource = concat(var.collector_lambda_arns, [var.snapshot_persist_arn])
      },
      {
        Sid    = "EventBridge"
        Effect = "Allow"
        Action = [
          "events:PutEvents",
        ]
        Resource = var.event_bus_arn
      },
    ]
  })
}

# --- State Machine ---
resource "aws_sfn_state_machine" "orchestrator" {
  name     = var.name
  role_arn = aws_iam_role.sfn_role.arn

  definition = templatefile("${path.module}/definition.asl.json", {
    logs_collector_arn    = var.logs_collector_arn
    metrics_collector_arn = var.metrics_collector_arn
    stepfn_collector_arn  = var.stepfn_collector_arn
    snapshot_persist_arn  = var.snapshot_persist_arn
    event_bus_name        = var.event_bus_name
  })
}

# --- EventBridge rule: emit incident.analyzed on execution success ---
resource "aws_cloudwatch_event_rule" "sfn_success" {
  name        = "${var.name}-success"
  description = "Fires when the orchestrator state machine execution succeeds"

  event_pattern = jsonencode({
    source      = ["aws.states"]
    detail-type = ["Step Functions Execution Status Change"]
    detail = {
      status          = ["SUCCEEDED"]
      stateMachineArn = [aws_sfn_state_machine.orchestrator.arn]
    }
  })
}

resource "aws_cloudwatch_event_target" "sfn_success_to_bus" {
  rule      = aws_cloudwatch_event_rule.sfn_success.name
  target_id = "forward-to-custom-bus"
  arn       = var.event_bus_arn
  role_arn  = aws_iam_role.eventbridge_forward.arn
}

# IAM role allowing the default bus rule to put events on the custom bus
resource "aws_iam_role" "eventbridge_forward" {
  name = "${var.name}-eb-forward"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "eventbridge_forward" {
  name = "${var.name}-eb-forward"
  role = aws_iam_role.eventbridge_forward.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "events:PutEvents"
      Resource = var.event_bus_arn
    }]
  })
}
