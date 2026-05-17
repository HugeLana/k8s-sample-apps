terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "origination_number" {
  type        = string
  description = "AWS End User Messaging origination phone number in E.164 (e.g. +15551234567)"
}

variable "destination_number" {
  type        = string
  description = "Phone number to call/text in E.164 format"
}

variable "tock_business" {
  type    = string
  default = "tfl"
}

variable "party_size" {
  type    = number
  default = 2
}

variable "days_ahead" {
  type    = number
  default = 60
}

variable "voice_id" {
  type    = string
  default = "Joanna"
}

variable "target_date" {
  type        = string
  description = "Target reservation date (YYYY-MM-DD). Voice calls begin once this date enters the rolling DAYS_AHEAD window."
}

variable "dedup_ttl_hours" {
  type        = number
  default     = 24
  description = "How long a notified slot is suppressed before it can re-alert."
}

provider "aws" {
  region = var.region
}

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda"
  output_path = "${path.module}/build/lambda.zip"
}

resource "aws_iam_role" "lambda" {
  name = "french-laundry-watcher"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "sms_voice" {
  name = "send-sms-voice"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "sms-voice:SendVoiceMessage",
        "sms-voice:SendTextMessage",
      ]
      Resource = "*"
    }]
  })
}

resource "aws_dynamodb_table" "dedup" {
  name         = "french-laundry-notified-slots"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "slot_key"

  attribute {
    name = "slot_key"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

resource "aws_iam_role_policy" "ddb_dedup" {
  name = "ddb-dedup"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:PutItem"]
      Resource = aws_dynamodb_table.dedup.arn
    }]
  })
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/french-laundry-watcher"
  retention_in_days = 14
}

resource "aws_lambda_function" "watcher" {
  function_name    = "french-laundry-watcher"
  role             = aws_iam_role.lambda.arn
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 120
  memory_size      = 256

  environment {
    variables = {
      TOCK_BUSINESS      = var.tock_business
      PARTY_SIZE         = tostring(var.party_size)
      DAYS_AHEAD         = tostring(var.days_ahead)
      ORIGINATION_NUMBER = var.origination_number
      DESTINATION_NUMBER = var.destination_number
      VOICE_ID           = var.voice_id
      TARGET_DATE        = var.target_date
      DEDUP_TABLE        = aws_dynamodb_table.dedup.name
      DEDUP_TTL_HOURS    = tostring(var.dedup_ttl_hours)
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

resource "aws_iam_role" "scheduler" {
  name = "french-laundry-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler_invoke" {
  name = "invoke-watcher"
  role = aws_iam_role.scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.watcher.arn
    }]
  })
}

resource "aws_scheduler_schedule" "hourly_edge" {
  name = "french-laundry-hourly-edge"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = "cron(1 * * * ? *)"
  schedule_expression_timezone = "America/Los_Angeles"

  target {
    arn      = aws_lambda_function.watcher.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ mode = "edge" })
  }
}

resource "aws_scheduler_schedule" "daily_rolling" {
  name = "french-laundry-daily-rolling"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = "cron(30 8 * * ? *)"
  schedule_expression_timezone = "America/Los_Angeles"

  target {
    arn      = aws_lambda_function.watcher.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ mode = "rolling" })
  }
}

output "lambda_name" {
  value = aws_lambda_function.watcher.function_name
}

output "hourly_schedule" {
  value = aws_scheduler_schedule.hourly_edge.name
}

output "daily_schedule" {
  value = aws_scheduler_schedule.daily_rolling.name
}
