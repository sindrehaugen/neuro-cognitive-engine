variable "deployment_name" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "kms_key_arn" {
  type = string
}

data "aws_caller_identity" "current" {}

locals {
  bucket = lower("trimcp-${var.deployment_name}-${data.aws_caller_identity.current.account_id}")
}

resource "aws_s3_bucket" "blobs" {
  bucket = substr(replace(local.bucket, "_", "-"), 0, 63)
}

resource "aws_s3_bucket_public_access_block" "blobs" {
  bucket = aws_s3_bucket.blobs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "blobs" {
  bucket = aws_s3_bucket.blobs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "blobs" {
  bucket = aws_s3_bucket.blobs.id
  versioning_configuration {
    status = "Enabled"
  }
}

output "bucket_id" {
  value = aws_s3_bucket.blobs.id
}

output "bucket_arn" {
  value = aws_s3_bucket.blobs.arn
}
