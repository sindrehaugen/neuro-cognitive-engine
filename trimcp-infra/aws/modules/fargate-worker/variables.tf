variable "deployment_name" {
  type = string
}

variable "environment" {
  type        = string
  description = "dev | staging | prod — controls log retention"
}

variable "region" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "app_security_group_id" {
  type = string
}

variable "cluster_name_suffix" {
  type = string
}

variable "service_name" {
  type = string
}

# --- Orchestrator (control plane) ---

variable "container_image" {
  type        = string
  description = "ECR image URI for the orchestrator container"
}

variable "cpu" {
  type        = number
  description = "vCPU units for the orchestrator task (1024 = 1 vCPU)"
}

variable "memory" {
  type        = number
  description = "Memory (MiB) for the orchestrator task"
}

variable "desired_count" {
  type        = number
  description = "Desired orchestrator task count"
}

# --- Worker (restricted — untrusted MCP integrations) ---

variable "worker_container_image" {
  type        = string
  description = "ECR image URI for the restricted worker container"
}

variable "worker_cpu" {
  type        = number
  description = "vCPU units for the worker task"
  default     = 1024
}

variable "worker_memory" {
  type        = number
  description = "Memory (MiB) for the worker task"
  default     = 4096
}

variable "worker_desired_count" {
  type        = number
  description = "Desired worker task count"
  default     = 1
}

variable "worker_s3_prefix" {
  type        = string
  description = "S3 key prefix scoped to worker IAM role (e.g. 'worker/' or 'mcp-integrations/')"
  default     = "worker/"
}

variable "worker_secrets_arns" {
  type        = list(string)
  description = "Secrets Manager ARNs readable by the restricted worker role (scoped — NOT RDS/ElastiCache masters)"
  default     = []
}

# --- Shared ---

variable "secrets_arns" {
  type        = list(string)
  description = "Secrets Manager ARNs readable by the orchestrator task role (full data-plane access)"
}

variable "s3_bucket_arn" {
  type = string
}
