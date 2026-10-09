output "ec2_public_ip" {
  description = "Public IP of the EC2 instance"
  value       = aws_instance.app.public_ip
}

output "alb_dns_name" {
  description = "ALB DNS name — use this if no custom domain is set"
  value       = "http://${aws_lb.main.dns_name}"
}

output "cloudfront_url" {
  description = "CloudFront distribution URL for the frontend"
  value       = "https://${aws_cloudfront_distribution.frontend.domain_name}"
}

output "ecr_repository_url" {
  description = "ECR repository URL — push your Docker image here"
  value       = aws_ecr_repository.app.repository_url
}

output "s3_bucket_name" {
  description = "S3 bucket name for frontend static files"
  value       = aws_s3_bucket.frontend.id
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution ID — needed for cache invalidation"
  value       = aws_cloudfront_distribution.frontend.id
}

output "backend_ws_url" {
  description = "WebSocket URL for the backend (use in production wokwi.toml)"
  value = var.domain_name != "" ? "wss://${var.domain_name}/ws" : "ws://${aws_lb.main.dns_name}/ws"
}

output "wokwi_serial_url" {
  description = "Wokwi serial bridge URL — paste into arduino/wokwi/wokwi.toml"
  value = var.domain_name != "" ? "wss://${var.domain_name}/wokwi-serial" : "ws://${aws_lb.main.dns_name}/wokwi-serial"
}
