# super-q AWS infrastructure.
#
# Minimal Terraform module that provisions what the AWS backend needs:
#   * A private S3 bucket for sandbox tarballs and built bitstreams
#   * An IAM instance profile for worker VMs
#   * A security group that permits egress only (no inbound SSH by default)
#   * A spot-optimized launch template referencing the super-q AMI
#
# You'll still need to build the super-q AMI once (see cloud/AMI.md).
#
# Usage:
#   cd cloud/terraform
#   terraform init
#   terraform apply -var="region=us-east-2" -var="bucket_name=my-superq-artifacts"

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "region" {
  description = "AWS region to deploy to."
  type        = string
  default     = "us-east-2"
}

variable "bucket_name" {
  description = "S3 bucket for artifacts. Must be globally unique."
  type        = string
}

variable "ami_id" {
  description = "Pre-baked super-q Quartus AMI id."
  type        = string
}

variable "subnet_id" {
  description = "Existing VPC subnet to place workers in."
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type for workers."
  type        = string
  default     = "c7i.4xlarge"
}

variable "max_spot_price" {
  description = "Maximum USD/hr per spot instance."
  type        = number
  default     = 0.40
}

provider "aws" {
  region = var.region
}

# ---- Artifacts bucket --------------------------------------------------

resource "aws_s3_bucket" "artifacts" {
  bucket        = var.bucket_name
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    id     = "expire-jobs"
    status = "Enabled"
    filter { prefix = "jobs/" }
    expiration { days = 30 }
  }
}

# ---- IAM ---------------------------------------------------------------

data "aws_iam_policy_document" "worker_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "worker" {
  name               = "superq-worker"
  assume_role_policy = data.aws_iam_policy_document.worker_assume.json
}

data "aws_iam_policy_document" "worker_policy" {
  statement {
    actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/*"]
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn]
  }
  statement {
    actions   = ["ec2:TerminateInstances"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/superq:job"
      values   = ["*"]
    }
  }
}

resource "aws_iam_role_policy" "worker" {
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker_policy.json
}

resource "aws_iam_instance_profile" "worker" {
  name = "superq-worker"
  role = aws_iam_role.worker.name
}

# ---- Security ----------------------------------------------------------

resource "aws_security_group" "worker" {
  name        = "superq-worker"
  description = "Egress-only SG for super-q Quartus workers."
  vpc_id      = data.aws_subnet.target.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

data "aws_subnet" "target" {
  id = var.subnet_id
}

# ---- Launch template ---------------------------------------------------

resource "aws_launch_template" "worker" {
  name_prefix   = "superq-worker-"
  image_id      = var.ami_id
  instance_type = var.instance_type

  iam_instance_profile {
    name = aws_iam_instance_profile.worker.name
  }

  vpc_security_group_ids = [aws_security_group.worker.id]

  instance_market_options {
    market_type = "spot"
    spot_options {
      max_price                      = tostring(var.max_spot_price)
      spot_instance_type             = "one-time"
      instance_interruption_behavior = "terminate"
    }
  }

  instance_initiated_shutdown_behavior = "terminate"

  tag_specifications {
    resource_type = "instance"
    tags = {
      Service = "super-q"
    }
  }

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }
}

# ---- Outputs -----------------------------------------------------------

output "bucket" { value = aws_s3_bucket.artifacts.bucket }
output "iam_profile" { value = aws_iam_instance_profile.worker.name }
output "security_group" { value = aws_security_group.worker.id }
output "launch_template" { value = aws_launch_template.worker.id }
output "env_exports" {
  description = "Copy these into your shell to point super-q's AWS backend at this infra."
  value = join("\n", [
    "export SUPERQ_AWS_REGION=${var.region}",
    "export SUPERQ_AWS_AMI=${var.ami_id}",
    "export SUPERQ_AWS_INSTANCE=${var.instance_type}",
    "export SUPERQ_AWS_SUBNET=${var.subnet_id}",
    "export SUPERQ_AWS_SG=${aws_security_group.worker.id}",
    "export SUPERQ_AWS_IAM=${aws_iam_instance_profile.worker.name}",
    "export SUPERQ_AWS_BUCKET=${aws_s3_bucket.artifacts.bucket}",
    "export SUPERQ_AWS_MAX_SPOT=${var.max_spot_price}",
  ])
}
