output "lambda_arn" {
  value = aws_lambda_function.analyzer.arn
}

output "lambda_name" {
  value = aws_lambda_function.analyzer.function_name
}
