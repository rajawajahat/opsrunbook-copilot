output "lambda_arn" {
  value = aws_lambda_function.persist.arn
}

output "lambda_name" {
  value = aws_lambda_function.persist.function_name
}
