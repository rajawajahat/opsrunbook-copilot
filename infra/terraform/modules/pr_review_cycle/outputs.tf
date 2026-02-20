output "state_machine_arn" {
  value = aws_sfn_state_machine.pr_review_cycle.arn
}

output "state_machine_name" {
  value = aws_sfn_state_machine.pr_review_cycle.name
}

output "lambda_function_name" {
  value = aws_lambda_function.pr_review_handler.function_name
}

output "lambda_function_arn" {
  value = aws_lambda_function.pr_review_handler.arn
}
