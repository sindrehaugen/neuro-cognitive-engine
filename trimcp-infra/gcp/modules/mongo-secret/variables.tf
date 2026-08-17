variable "deployment_name" {
  type = string
}

variable "worker_service_account_email" {
  type        = string
  description = "Grant secretAccessor on the Mongo URI secret only"
}
