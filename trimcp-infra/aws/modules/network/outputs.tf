output "vpc_id" {
  value = aws_vpc.this.id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "private_app_subnet_ids" {
  value = aws_subnet.private_app[*].id
}

output "private_db_subnet_ids" {
  value = aws_subnet.private_db[*].id
}

output "app_security_group_id" {
  value = aws_security_group.app.id
}

output "data_security_group_id" {
  value = aws_security_group.data.id
}
