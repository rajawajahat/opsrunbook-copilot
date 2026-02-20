variable "name" {
  type = string
}

variable "logs_collector_arn" {
  type        = string
  description = "ARN of the logs collector Lambda function"
}

variable "metrics_collector_arn" {
  type        = string
  description = "ARN of the metrics collector Lambda function"
}

variable "stepfn_collector_arn" {
  type        = string
  description = "ARN of the step functions collector Lambda function"
}

variable "collector_lambda_arns" {
  type        = list(string)
  description = "List of all collector Lambda ARNs for IAM"
}

variable "event_bus_name" {
  type = string
}

variable "event_bus_arn" {
  type = string
}

variable "snapshot_persist_arn" {
  type        = string
  description = "ARN of the snapshot persist Lambda function"
}
