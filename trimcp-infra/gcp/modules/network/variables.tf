variable "project_id" {
  type = string
}

variable "deployment_name" {
  type = string
}

variable "region" {
  type = string
}

variable "vpc_cidr" {
  type = string
}

variable "labels" {
  type = map(string)
}
