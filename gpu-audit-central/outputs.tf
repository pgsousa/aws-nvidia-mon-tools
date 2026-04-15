output "google_writer_secret_name" {
  description = "Secrets Manager secret name used by the audit lambda."
  value       = aws_secretsmanager_secret.google_writer.name
}

output "google_sheets_spreadsheet_id_parameter_name" {
  description = "SSM parameter name storing the Google Sheets spreadsheet id."
  value       = aws_ssm_parameter.google_sheets_spreadsheet_id.name
}

output "exchange_rate_parameter_name" {
  description = "Shared SSM parameter used to cache the USD/EUR exchange rate."
  value       = aws_ssm_parameter.exchange_rate.name
}
