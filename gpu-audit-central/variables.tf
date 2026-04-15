variable "aws_region" {
  description = "AWS region where the central audit stack is deployed."
  type        = string
  default     = "eu-west-1"
}

variable "google_writer_secret_name" {
  description = "Secrets Manager secret name containing the Google service account JSON."
  type        = string
  default     = "gpu-google-sheets-writer"
}

variable "google_writer_secret_string" {
  description = "Raw JSON for the Google service account with write access to the spreadsheet."
  type        = string
  sensitive   = true
}

variable "google_sheets_spreadsheet_id_parameter_name" {
  description = "SSM parameter used to store the central spreadsheet id."
  type        = string
  default     = "/gpu-test/google-sheets/spreadsheet-id"
}

variable "google_sheets_spreadsheet_id" {
  description = "Google Sheets spreadsheet id for the central audit workbook."
  type        = string
  sensitive   = true
}

variable "exchange_rate_parameter_name" {
  description = "Shared SSM parameter used to cache the USD/EUR exchange rate."
  type        = string
  default     = "/gpu-test/shared/exchange-rate/usd-to-eur"
}

variable "running_costs_sheet_range" {
  description = "A1 range used for the hourly per-VM accumulated cost sheet."
  type        = string
  default     = "VM Running Costs!A:Q"
}

variable "running_costs_schedule_expression" {
  description = "EventBridge schedule expression for updating accumulated VM cost rows."
  type        = string
  default     = "rate(1 hour)"
}

variable "lambda_log_retention_days" {
  description = "CloudWatch Logs retention for central Lambda functions."
  type        = number
  default     = 14
}

variable "admin_principal_arns" {
  description = "Admin IAM principal ARNs that should retain access to the central secret and user state buckets."
  type        = list(string)
  default     = []
}

variable "user_state_bucket_access" {
  description = "Optional map of user state bucket names to the SSO role ARN that should be allowed to access each bucket."
  type = map(object({
    bucket_name   = string
    principal_arn = string
  }))
  default = {}
}
