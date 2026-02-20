output "lambda_arn" {
  value = aws_lambda_function.actions_runner.arn
}

output "lambda_name" {
  value = aws_lambda_function.actions_runner.function_name
}
