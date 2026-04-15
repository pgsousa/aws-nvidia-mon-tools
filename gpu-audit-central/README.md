# GPU Audit Central

This directory contains the administrator-only Terraform stack for the shared Google Sheets audit pipeline. It is intentionally kept outside the user-facing repo.

## What it creates

- `Secrets Manager` secret containing the Google service account JSON
- `SSM Parameter` containing the Google Sheets spreadsheet id
- Shared `SSM Parameter` for the cached USD/EUR exchange rate
- Central audit Lambda that records VM launches, hourly running costs and terminations
- Central exchange-rate Lambda that refreshes the shared USD/EUR rate daily
- `EventBridge` rules for launch success, termination, hourly cost sync and daily exchange-rate refresh
- Optional `aws_s3_bucket_policy` resources to lock each user's manually-created Terraform state bucket to the intended SSO role ARN

## Usage

1. Copy `terraform.tfvars.example` to `terraform.tfvars`.
2. Fill in:
   - `google_writer_secret_string`
   - `google_sheets_spreadsheet_id`
   - `admin_principal_arns`
   - optional `user_state_bucket_access`
3. Run:

```bash
terraform init
terraform plan
terraform apply
```

## Notes

- This stack is for administrators only.
- The spreadsheet itself is created manually; Terraform only stores its id in SSM.
- `user_state_bucket_access` is optional, but if populated it lets this stack enforce per-user access to manually-created state buckets via bucket policies.
- `gpu-audit-central/` is added to the parent repo's `.git/info/exclude` so it does not show up for commit/push by default.
