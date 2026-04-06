output "lambda_arn" {
  value = aws_lambda_function.coding_agent.arn
}

output "lambda_function_name" {
  value = aws_lambda_function.coding_agent.function_name
}
