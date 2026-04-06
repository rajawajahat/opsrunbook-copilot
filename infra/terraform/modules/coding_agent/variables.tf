variable "name" {
  type = string
}

variable "evidence_bucket" {
  type = string
}

variable "evidence_bucket_arn" {
  type = string
}

variable "packets_table_name" {
  type    = string
  default = ""
}

variable "packets_table_arn" {
  type    = string
  default = ""
}

variable "incidents_table_name" {
  type    = string
  default = ""
}

variable "incidents_table_arn" {
  type    = string
  default = ""
}

variable "event_bus_name" {
  type    = string
  default = ""
}

variable "event_bus_arn" {
  type    = string
  default = ""
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "account_id" {
  type = string
}

variable "github_owner" {
  type    = string
  default = ""
}

variable "llm_provider" {
  type    = string
  default = "groq"
}

variable "llm_model" {
  type    = string
  default = ""
}
