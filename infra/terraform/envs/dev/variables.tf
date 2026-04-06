variable "aws_region" {
  type        = string
  description = "AWS region to deploy into"
  default     = "us-east-1"
}

variable "aws_profile" {
  type        = string
  description = "AWS CLI profile to use for authentication"
}

variable "github_owner" {
  type        = string
  description = "GitHub organisation or username that owns the target repositories"
}

variable "github_default_branch" {
  type        = string
  description = "Default branch name used when creating fix PRs"
  default     = "main"
}

variable "github_app_slug" {
  type        = string
  description = "Slug of the GitHub App installed on the target organisation (used to filter self-events)"
  default     = "opsrunbook-copilot-bot"
}

variable "llm_provider" {
  type        = string
  description = "LLM provider for analysis and action generation: groq | gemini | stub"
  default     = "groq"

  validation {
    condition     = contains(["groq", "gemini", "stub"], var.llm_provider)
    error_message = "llm_provider must be one of: groq, gemini, stub"
  }
}

variable "evidence_retention_days" {
  type        = number
  description = "Number of days to retain evidence objects in S3"
  default     = 7
}
