terraform {
  required_version = ">= 1.5.0"
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

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Environment = var.environment
      ManagedBy   = "Terraform"
      Project     = "SecureDataOrchestrator"
    }
  }
}

variable "aws_region" {
  description = "AWS deployment region"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment name"
  type        = string
  default     = "production"
}

# Network Configuration (Uses default VPC for demo simplicity)
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# S3 Ingress Bucket (Encrypted at rest with AES256)
resource "aws_s3_bucket" "ingress_bucket" {
  bucket_prefix = "secure-data-ingress-"
  force_destroy = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ingress_encryption" {
  bucket = aws_s3_bucket.ingress_bucket.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "ingress_block_public" {
  bucket = aws_s3_bucket.ingress_bucket.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# DynamoDB Immutable Audit Log Table
resource "aws_dynamodb_table" "audit_table" {
  name         = "AuditLog"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"
  range_key    = "timestamp"

  attribute {
    name = "job_id"
    type = "S"
  }
  attribute {
    name = "timestamp"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}

# Amazon ECR Repository for Processor Image
resource "aws_ecr_repository" "processor_repo" {
  name                 = "data-processor"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

# ECS Fargate Cluster
resource "aws_ecs_cluster" "main" {
  name = "data-processing-cluster"
}

# IAM Role: Ingress Lambda Orchestrator
resource "aws_iam_role" "lambda_exec" {
  name = "ingress_lambda_orchestrator_role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy" "lambda_least_privilege" {
  name = "lambda_least_privilege_policy"
  role = aws_iam_role.lambda_exec.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Effect   = "Allow"
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Action   = ["s3:GetObject", "s3:GetObjectTagging"]
        Effect   = "Allow"
        Resource = "${aws_s3_bucket.ingress_bucket.arn}/*"
      },
      {
        Action   = ["dynamodb:PutItem"]
        Effect   = "Allow"
        Resource = aws_dynamodb_table.audit_table.arn
      },
      {
        Action   = ["ecs:RunTask"]
        Effect   = "Allow"
        Resource = aws_ecs_task_definition.processor_task.arn
      },
      {
        Action   = "iam:PassRole"
        Effect   = "Allow"
        Resource = [aws_iam_role.ecs_execution_role.arn, aws_iam_role.ecs_task_role.arn]
      }
    ]
  })
}

# Lambda Archive & Function
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../src/lambda"
  output_path = "${path.module}/lambda.zip"
}

resource "aws_lambda_function" "ingress_validator" {
  filename         = data.archive_file.lambda_zip.output_path
  function_name    = "ingress_validator"
  role             = aws_iam_role.lambda_exec.arn
  handler          = "index.lambda_handler"
  runtime          = "python3.11"
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 30

  environment {
    variables = {
      DYNAMODB_TABLE = aws_dynamodb_table.audit_table.name
      ECS_CLUSTER    = aws_ecs_cluster.main.name
      ECS_TASK_DEF   = aws_ecs_task_definition.processor_task.family
      SUBNETS        = join(",", data.aws_subnets.default.ids)
    }
  }
}

# S3 Event Notification to Lambda
resource "aws_lambda_permission" "allow_s3" {
  statement_id  = "AllowExecutionFromS3"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingress_validator.arn
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.ingress_bucket.arn
}

resource "aws_s3_bucket_notification" "bucket_notification" {
  bucket = aws_s3_bucket.ingress_bucket.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.ingress_validator.arn
    events              = ["s3:ObjectCreated:*"]
    filter_suffix       = ".zip"
  }

  depends_on = [aws_lambda_permission.allow_s3]
}

# ECS IAM Roles (Task Execution & Task)
resource "aws_iam_role" "ecs_execution_role" {
  name = "ecs_task_execution_role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ecs-tasks.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_execution_role_policy" {
  role       = aws_iam_role.ecs_execution_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "ecs_task_role" {
  name = "ecs_data_processor_task_role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ecs-tasks.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy" "ecs_task_least_privilege" {
  name = "ecs_processor_least_privilege"
  role = aws_iam_role.ecs_task_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action   = ["s3:GetObject", "s3:GetObjectTagging"]
        Effect   = "Allow"
        Resource = "${aws_s3_bucket.ingress_bucket.arn}/*"
      },
      {
        Action   = ["dynamodb:PutItem"]
        Effect   = "Allow"
        Resource = aws_dynamodb_table.audit_table.arn
      }
    ]
  })
}

# CloudWatch Log Group for ECS
resource "aws_cloudwatch_log_group" "ecs_logs" {
  name              = "/ecs/data-processor"
  retention_in_days = 14
}

# ECS Fargate Task Definition
resource "aws_ecs_task_definition" "processor_task" {
  family                   = "data-processor-task"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.ecs_execution_role.arn
  task_role_arn            = aws_iam_role.ecs_task_role.arn

  container_definitions = jsonencode([{
    name      = "processor"
    image     = "${aws_ecr_repository.processor_repo.repository_url}:latest"
    essential = true
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.ecs_logs.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "ecs"
      }
    }
  }])
}

# Infrastructure Outputs
output "s3_bucket_name" {
  description = "Ingress S3 Bucket name"
  value       = aws_s3_bucket.ingress_bucket.id
}

output "ecr_repository_url" {
  description = "ECR Repository URL for processor container image"
  value       = aws_ecr_repository.processor_repo.repository_url
}

output "dynamodb_audit_table" {
  description = "DynamoDB Audit Log Table name"
  value       = aws_dynamodb_table.audit_table.name
}

output "ecs_cluster_name" {
  description = "ECS Fargate Cluster name"
  value       = aws_ecs_cluster.main.name
}
