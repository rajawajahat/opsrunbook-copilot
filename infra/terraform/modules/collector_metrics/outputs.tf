output "lambda_arn" {
  value = aws_lambda_function.collector.arn
}

output "lambda_name" {
  value = aws_lambda_function.collector.function_name
}

output "role_arn" {
  value = aws_iam_role.lambda_role.arn
}
