variable "name" {
  description = "Name prefix for PR review cycle resources"
  type        = string
}

variable "evidence_bucket" {
  type = string
}

variable "evidence_bucket_arn" {
  type = string
}

variable "incidents_table" {
  type = string
}

variable "incidents_table_arn" {
  type = string
}

variable "event_bus_name" {
  type = string
}

variable "event_bus_arn" {
  type = string
}

variable "github_owner" {
  type    = string
  default = ""
}

variable "github_app_slug" {
  type    = string
  default = "opsrunbook-copilot-bot"
}

variable "github_allowed_paths" {
  type    = string
  default = ".opsrunbook/,src/,config/"
}

variable "llm_provider" {
  type    = string
  default = "stub"
}
