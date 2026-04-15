data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      identifiers = ["lambda.amazonaws.com"]
      type        = "Service"
    }
  }
}

locals {
  lambda_runtime = "python3.12"
  managed_instance_filters = [
    {
      Name   = "tag:ManagedBy"
      Values = ["terraform"]
    },
    {
      Name   = "tag:Stack"
      Values = ["aws-gpu-test"]
    },
    {
      Name   = "tag:Role"
      Values = ["gpu-vm"]
    },
  ]
}

resource "aws_secretsmanager_secret" "google_writer" {
  name = var.google_writer_secret_name
}

resource "aws_secretsmanager_secret_version" "google_writer" {
  secret_id     = aws_secretsmanager_secret.google_writer.id
  secret_string = var.google_writer_secret_string
}

resource "aws_ssm_parameter" "google_sheets_spreadsheet_id" {
  name      = var.google_sheets_spreadsheet_id_parameter_name
  overwrite = true
  type      = "String"
  value     = var.google_sheets_spreadsheet_id
}

resource "aws_ssm_parameter" "exchange_rate" {
  name      = var.exchange_rate_parameter_name
  overwrite = true
  type      = "String"
  value     = "0.92"
}

resource "terraform_data" "package_audit_lambda" {
  triggers_replace = {
    source_hash = filesha256("${path.module}/lambdas/audit/lambda_function.py")
    script_hash = filesha256("${path.module}/scripts/package_lambda.sh")
  }

  provisioner "local-exec" {
    command = "${path.module}/scripts/package_lambda.sh ${path.module}/lambdas/audit ${path.module}/build/audit ${local.lambda_runtime}"
  }
}

resource "terraform_data" "package_exchange_rate_lambda" {
  triggers_replace = {
    source_hash = filesha256("${path.module}/lambdas/exchange_rate/lambda_function.py")
    script_hash = filesha256("${path.module}/scripts/package_lambda.sh")
  }

  provisioner "local-exec" {
    command = "${path.module}/scripts/package_lambda.sh ${path.module}/lambdas/exchange_rate ${path.module}/build/exchange_rate ${local.lambda_runtime}"
  }
}

data "archive_file" "audit_lambda" {
  depends_on  = [terraform_data.package_audit_lambda]
  output_path = "${path.module}/build/audit.zip"
  source_dir  = "${path.module}/build/audit"
  type        = "zip"
}

data "archive_file" "exchange_rate_lambda" {
  depends_on  = [terraform_data.package_exchange_rate_lambda]
  output_path = "${path.module}/build/exchange_rate.zip"
  source_dir  = "${path.module}/build/exchange_rate"
  type        = "zip"
}

resource "aws_iam_role" "audit_lambda" {
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  name               = "gpu-audit-central"
}

resource "aws_iam_role" "exchange_rate_lambda" {
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  name               = "gpu-exchange-rate-central"
}

resource "aws_cloudwatch_log_group" "audit_lambda" {
  name              = "/aws/lambda/gpu-audit-central"
  retention_in_days = var.lambda_log_retention_days
}

resource "aws_cloudwatch_log_group" "exchange_rate_lambda" {
  name              = "/aws/lambda/gpu-exchange-rate-central"
  retention_in_days = var.lambda_log_retention_days
}

resource "aws_iam_role_policy" "audit_lambda" {
  name = "gpu-audit-central"
  role = aws_iam_role.audit_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid = "AllowInstanceInspection"
        Action = [
          "ec2:DescribeInstances",
          "ec2:DescribeVolumes",
          "ec2:DescribeSpotPriceHistory",
          "pricing:GetProducts",
        ]
        Effect   = "Allow"
        Resource = "*"
      },
      {
        Sid = "AllowGoogleWriterSecretRead"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
        ]
        Effect   = "Allow"
        Resource = aws_secretsmanager_secret.google_writer.arn
      },
      {
        Sid = "AllowExchangeRateRead"
        Action = [
          "ssm:GetParameter",
        ]
        Effect   = "Allow"
        Resource = aws_ssm_parameter.exchange_rate.arn
      },
      {
        Sid = "AllowCloudWatchLogs"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Effect   = "Allow"
        Resource = "${aws_cloudwatch_log_group.audit_lambda.arn}:*"
      },
    ]
  })
}

resource "aws_iam_role_policy" "exchange_rate_lambda" {
  name = "gpu-exchange-rate-central"
  role = aws_iam_role.exchange_rate_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid = "AllowExchangeRateParameterAccess"
        Action = [
          "ssm:GetParameter",
          "ssm:PutParameter",
        ]
        Effect   = "Allow"
        Resource = aws_ssm_parameter.exchange_rate.arn
      },
      {
        Sid = "AllowCloudWatchLogs"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Effect   = "Allow"
        Resource = "${aws_cloudwatch_log_group.exchange_rate_lambda.arn}:*"
      },
    ]
  })
}

resource "aws_lambda_function" "audit" {
  depends_on = [
    aws_cloudwatch_log_group.audit_lambda,
    terraform_data.package_audit_lambda,
  ]

  architectures    = ["x86_64"]
  filename         = data.archive_file.audit_lambda.output_path
  function_name    = "gpu-audit-central"
  handler          = "lambda_function.handler"
  role             = aws_iam_role.audit_lambda.arn
  runtime          = local.lambda_runtime
  source_code_hash = data.archive_file.audit_lambda.output_base64sha256
  timeout          = 90

  environment {
    variables = {
      GOOGLE_SHEETS_SPREADSHEET_ID = aws_ssm_parameter.google_sheets_spreadsheet_id.value
      GOOGLE_WRITER_SECRET_ARN     = aws_secretsmanager_secret.google_writer.arn
      RUNNING_COSTS_SHEET_RANGE    = var.running_costs_sheet_range
      EXCHANGE_RATE_PARAMETER      = aws_ssm_parameter.exchange_rate.name
      DEFAULT_CREATED_BY           = "unknown"
      ROOT_VOLUME_SIZE_GIB         = "64"
    }
  }
}

resource "aws_lambda_function" "exchange_rate" {
  depends_on = [
    aws_cloudwatch_log_group.exchange_rate_lambda,
    terraform_data.package_exchange_rate_lambda,
  ]

  architectures    = ["x86_64"]
  filename         = data.archive_file.exchange_rate_lambda.output_path
  function_name    = "gpu-exchange-rate-central"
  handler          = "lambda_function.handler"
  role             = aws_iam_role.exchange_rate_lambda.arn
  runtime          = local.lambda_runtime
  source_code_hash = data.archive_file.exchange_rate_lambda.output_base64sha256
  timeout          = 30

  environment {
    variables = {
      EXCHANGE_RATE_PARAMETER = aws_ssm_parameter.exchange_rate.name
    }
  }
}

resource "aws_cloudwatch_event_rule" "instance_launch_success" {
  name        = "gpu-audit-central-instance-launch-success"
  description = "Audit GPU VM launches across the account."
  event_pattern = jsonencode({
    source        = ["aws.autoscaling"]
    "detail-type" = ["EC2 Instance Launch Successful"]
  })
}

resource "aws_cloudwatch_event_rule" "instance_terminated" {
  name        = "gpu-audit-central-instance-terminated"
  description = "Finalize audit rows when a GPU VM is terminated."
  event_pattern = jsonencode({
    source        = ["aws.ec2"]
    "detail-type" = ["EC2 Instance State-change Notification"]
    detail = {
      state = ["terminated"]
    }
  })
}

resource "aws_cloudwatch_event_rule" "running_costs_hourly" {
  name                = "gpu-audit-central-running-costs-hourly"
  description         = "Refresh accumulated cost per GPU VM every hour."
  schedule_expression = var.running_costs_schedule_expression
}

resource "aws_cloudwatch_event_rule" "exchange_rate_daily" {
  name                = "gpu-audit-central-exchange-rate-daily"
  description         = "Refresh the shared USD/EUR exchange rate daily."
  schedule_expression = "cron(0 6 * * ? *)"
}

resource "aws_cloudwatch_event_target" "audit_launch_success" {
  arn  = aws_lambda_function.audit.arn
  rule = aws_cloudwatch_event_rule.instance_launch_success.name
}

resource "aws_cloudwatch_event_target" "audit_instance_terminated" {
  arn  = aws_lambda_function.audit.arn
  rule = aws_cloudwatch_event_rule.instance_terminated.name
}

resource "aws_cloudwatch_event_target" "audit_running_costs_hourly" {
  arn  = aws_lambda_function.audit.arn
  rule = aws_cloudwatch_event_rule.running_costs_hourly.name
}

resource "aws_cloudwatch_event_target" "exchange_rate_daily" {
  arn  = aws_lambda_function.exchange_rate.arn
  rule = aws_cloudwatch_event_rule.exchange_rate_daily.name
}

resource "aws_lambda_permission" "eventbridge_audit_launch_success" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.audit.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.instance_launch_success.arn
  statement_id  = "AllowExecutionFromLaunchSuccessRule"
}

resource "aws_lambda_permission" "eventbridge_audit_instance_terminated" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.audit.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.instance_terminated.arn
  statement_id  = "AllowExecutionFromInstanceTerminatedRule"
}

resource "aws_lambda_permission" "eventbridge_audit_running_costs_hourly" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.audit.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.running_costs_hourly.arn
  statement_id  = "AllowExecutionFromRunningCostsHourlyRule"
}

resource "aws_lambda_permission" "eventbridge_exchange_rate_daily" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.exchange_rate.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.exchange_rate_daily.arn
  statement_id  = "AllowExecutionFromExchangeRateDailyRule"
}

data "aws_iam_policy_document" "google_writer_secret" {
  statement {
    sid = "AllowAdminsAndAuditLambdaRead"
    principals {
      type        = "AWS"
      identifiers = concat(var.admin_principal_arns, [aws_iam_role.audit_lambda.arn])
    }
    actions = [
      "secretsmanager:DescribeSecret",
      "secretsmanager:GetSecretValue",
    ]
    resources = [aws_secretsmanager_secret.google_writer.arn]
  }

  statement {
    sid    = "DenyEveryoneElse"
    effect = "Deny"
    not_principals {
      type        = "AWS"
      identifiers = concat(var.admin_principal_arns, [aws_iam_role.audit_lambda.arn])
    }
    actions = [
      "secretsmanager:DescribeSecret",
      "secretsmanager:GetSecretValue",
    ]
    resources = [aws_secretsmanager_secret.google_writer.arn]
  }
}

resource "aws_secretsmanager_secret_policy" "google_writer" {
  secret_arn = aws_secretsmanager_secret.google_writer.arn
  policy     = data.aws_iam_policy_document.google_writer_secret.json
}

data "aws_iam_policy_document" "user_state_bucket" {
  for_each = var.user_state_bucket_access

  statement {
    sid = "AllowAssignedPrincipalBucketRead"
    principals {
      type        = "AWS"
      identifiers = [each.value.principal_arn]
    }
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${each.value.bucket_name}"]
  }

  statement {
    sid = "AllowAssignedPrincipalObjectAccess"
    principals {
      type        = "AWS"
      identifiers = [each.value.principal_arn]
    }
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = ["arn:aws:s3:::${each.value.bucket_name}/*"]
  }

  statement {
    sid    = "AllowAdminsBucketAccess"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = var.admin_principal_arns
    }
    actions = [
      "s3:GetBucketLocation",
      "s3:ListBucket",
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = [
      "arn:aws:s3:::${each.value.bucket_name}",
      "arn:aws:s3:::${each.value.bucket_name}/*",
    ]
  }

  statement {
    sid    = "DenyAllOtherPrincipals"
    effect = "Deny"
    not_principals {
      type        = "AWS"
      identifiers = concat([each.value.principal_arn], var.admin_principal_arns)
    }
    actions = ["s3:*"]
    resources = [
      "arn:aws:s3:::${each.value.bucket_name}",
      "arn:aws:s3:::${each.value.bucket_name}/*",
    ]
  }
}

resource "aws_s3_bucket_policy" "user_state_bucket" {
  for_each = var.user_state_bucket_access

  bucket = each.value.bucket_name
  policy = data.aws_iam_policy_document.user_state_bucket[each.key].json
}
