variable "name" {
  type = string
}

variable "incidents_table_name" {
  type = string
}

variable "incidents_table_arn" {
  type = string
}

variable "evidence_bucket_arn" {
  type = string
}

variable "event_bus_name" {
  type    = string
  default = ""
}

variable "event_bus_arn" {
  type    = string
  default = ""
}

variable "dry_run" {
  type    = bool
  default = true
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "account_id" {
  type = string
}

variable "enable_github_pr" {
  type    = bool
  default = false
}

variable "github_owner" {
  type    = string
  default = ""
}

variable "github_default_branch" {
  type    = string
  default = "main"
}
