resource "aws_kms_key" "trimcp" {
  description             = "TriMCP data encryption (${var.deployment_name})"
  deletion_window_in_days = var.environment == "prod" ? 30 : 7
  enable_key_rotation     = true
}

resource "aws_kms_alias" "trimcp" {
  name          = "alias/trimcp-${var.deployment_name}"
  target_key_id = aws_kms_key.trimcp.key_id
}
