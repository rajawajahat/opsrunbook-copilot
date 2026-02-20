output "lambda_name" {
  value = aws_lambda_function.loggen.function_name
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.lg.name
}
