#!/bin/bash
# EC2 bootstrap script (Amazon Linux 2023)
# Installs Docker, pulls the container image from ECR, and starts it as a systemd service.

set -euo pipefail
export AWS_REGION="${aws_region}"
export ECR_IMAGE_URI="${ecr_image_uri}"

# ── Logging ───────────────────────────────────────────────────────────────────
exec > >(tee /var/log/userdata.log | logger -t userdata) 2>&1
echo "=== Greenhouse Guardian EC2 bootstrap starting ==="

# ── System update ─────────────────────────────────────────────────────────────
dnf update -y
dnf install -y docker aws-cli

# ── Docker ────────────────────────────────────────────────────────────────────
systemctl enable --now docker
usermod -aG docker ec2-user

# ── ECR login ─────────────────────────────────────────────────────────────────
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REGISTRY="$${ACCOUNT_ID}.dkr.ecr.$${AWS_REGION}.amazonaws.com"
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"

# ── Pull image ────────────────────────────────────────────────────────────────
docker pull "$ECR_IMAGE_URI"

# ── Systemd service ───────────────────────────────────────────────────────────
cat > /etc/systemd/system/greenhouse-guardian.service << EOF
[Unit]
Description=Greenhouse Guardian FastAPI backend
After=docker.service network-online.target
Requires=docker.service

[Service]
User=root
Restart=always
RestartSec=5
ExecStartPre=-/usr/bin/docker stop greenhouse-guardian
ExecStartPre=-/usr/bin/docker rm greenhouse-guardian
ExecStart=/usr/bin/docker run --name greenhouse-guardian \
  --rm \
  -p 8000:8000 \
  -e PORT=8000 \
  --log-driver=awslogs \
  --log-opt awslogs-region=$${AWS_REGION} \
  --log-opt awslogs-group=/greenhouse-guardian/app \
  --log-opt awslogs-create-group=true \
  $${ECR_IMAGE_URI}
ExecStop=/usr/bin/docker stop greenhouse-guardian

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now greenhouse-guardian

echo "=== Bootstrap complete. Service status: ==="
systemctl status greenhouse-guardian --no-pager || true
