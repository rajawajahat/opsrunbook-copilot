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
  type = string
}

variable "packets_table_arn" {
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

variable "llm_provider" {
  type    = string
  default = "stub"
}
