# Storage module variables
variable "project" {
  type = string
}

variable "env" {
  type = string
}

variable "aws_region" {
  type        = string
  description = "Region used to build deterministic bucket name."
}

variable "evidence_retention_days" {
  type        = number
  default     = 30
  description = "Auto-expire evidence objects after N days (set 0 to disable)."
}
