output "evidence_bucket" {
  value = aws_s3_bucket.evidence.bucket
}

output "incidents_table" {
  value = aws_dynamodb_table.incidents.name
}

output "incidents_table_arn" {
  value = aws_dynamodb_table.incidents.arn
}

output "snapshots_table" {
  value = aws_dynamodb_table.snapshots.name
}

output "snapshots_table_arn" {
  value = aws_dynamodb_table.snapshots.arn
}

output "packets_table" {
  value = aws_dynamodb_table.packets.name
}

output "packets_table_arn" {
  value = aws_dynamodb_table.packets.arn
}

output "evidence_bucket_arn" {
  value = aws_s3_bucket.evidence.arn
}

output "account_id" {
  value = data.aws_caller_identity.current.account_id
}
