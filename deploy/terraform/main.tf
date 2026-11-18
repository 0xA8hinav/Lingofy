# Lingofy AWS base: ECR repos + S3 buckets + SQS queues + DLQ + IAM.
# ~200 lines per docs/plan.md. App data-plane (SQS/S3 in code) is phase 2;
# this creates the resources + ECR so images ship and k3s-on-EC2 runs today.
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = ">= 5.0" }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  description = "AWS region (SQS/KEDA + ECR must live here)"
  type        = string
  default     = "ap-south-1"
}

variable "project" {
  description = "Resource name prefix"
  type        = string
  default     = "lingofy"
}

variable "retention_days" {
  description = "CloudWatch/default log retention, days"
  type        = number
  default     = 14
}

# ---- Container registry (3 images: gateway/full, assembler, slim-gw future) ----
resource "aws_ecr_repository" "full" {
  name                 = "${var.project}-full"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
  tags = { Project = var.project }
}

resource "aws_ecr_repository" "assembler" {
  name                 = "${var.project}-assembler"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
  tags = { Project = var.project }
}

resource "aws_ecr_repository" "gateway" {
  name                 = "${var.project}-gateway"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
  tags = { Project = var.project }
}

# ---- Object store (phase 2 data-plane; k3s phase 1 uses EBS PVCs) ----
resource "aws_s3_bucket" "audio" {
  bucket = "${var.project}-audio-${data.aws_caller_identity.me.account_id}"
  tags   = { Project = var.project }
}

resource "aws_s3_bucket" "output" {
  bucket = "${var.project}-output-${data.aws_caller_identity.me.account_id}"
  tags   = { Project = var.project }
}

resource "aws_s3_bucket_lifecycle_configuration" "output" {
  bucket = aws_s3_bucket.output.id
  rule {
    id     = "expire-sessions"
    status = "Enabled"
    expiration { days = 30 }
  }
}

data "aws_caller_identity" "me" {}

# ---- Queues: 1 work queue + DLQ with redrive (plan: visibility 60s, maxReceive 3) ----
resource "aws_sqs_queue" "dlq" {
  name                      = "${var.project}-dlq"
  message_retention_seconds = 1209600 # 14d
  tags                      = { Project = var.project }
}

resource "aws_sqs_queue" "dub_tasks" {
  name                       = "${var.project}-dub-tasks"
  visibility_timeout_seconds = 60
  message_retention_seconds  = 345600 # 4d
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 3
  })
  tags = { Project = var.project }
}

# ---- IAM: node role for k3s workers (ECR pull + SQS/S3 when phase 2 lands) ----
resource "aws_iam_role" "node" {
  name = "${var.project}-node"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = { Project = var.project }
}

resource "aws_iam_role_policy" "node_inline" {
  name = "${var.project}-node-inline"
  role = aws_iam_role.node.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"]
        Resource = [aws_sqs_queue.dub_tasks.arn, aws_sqs_queue.dlq.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.audio.arn, "${aws_s3_bucket.audio.arn}/*", aws_s3_bucket.output.arn, "${aws_s3_bucket.output.arn}/*"]
      }
    ]
  })
}

resource "aws_iam_instance_profile" "node" {
  name = "${var.project}-node"
  role = aws_iam_role.node.name
}
