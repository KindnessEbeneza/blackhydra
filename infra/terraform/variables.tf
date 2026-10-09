variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "domain_name" {
  description = "Your domain name for HTTPS (e.g. greenhouse.example.com). Leave empty to use ALB DNS directly."
  type        = string
  default     = ""
}

variable "alert_email" {
  description = "Email address for CloudWatch alarm notifications"
  type        = string
  default     = ""
}

variable "ec2_key_name" {
  description = "Name of the EC2 key pair for SSH access"
  type        = string
  default     = ""
}

variable "ecr_image_uri" {
  description = "Full ECR image URI e.g. 123456789.dkr.ecr.us-east-1.amazonaws.com/greenhouse-guardian:latest"
  type        = string
  default     = "greenhouse-guardian:latest"
}

variable "environment" {
  description = "Deployment environment tag"
  type        = string
  default     = "production"
}
