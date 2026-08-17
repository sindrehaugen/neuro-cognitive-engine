output "kms_key_arn" {
  value = aws_kms_key.trimcp.arn
}

output "kms_key_id" {
  value = aws_kms_key.trimcp.id
}
