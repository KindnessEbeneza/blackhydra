#!/bin/bash
# deploy.sh — Manual deploy script
# Builds the Docker image, pushes to ECR, restarts the EC2 service,
# and syncs the frontend to S3 with a CloudFront invalidation.
#
# Prerequisites:
#   - AWS CLI configured with sufficient permissions
#   - SSH key for EC2 accessible
#   - Terraform outputs available (run: terraform -chdir=infra/terraform output)
#
# Usage:
#   export EC2_HOST=<ec2-public-ip>
#   export EC2_KEY=~/.ssh/your-key.pem
#   bash infra/scripts/deploy.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ── Read Terraform outputs ─────────────────────────────────────────────────────
TF_DIR="$ROOT_DIR/infra/terraform"
echo "Reading Terraform outputs..."
ECR_URI=$(terraform -chdir="$TF_DIR" output -raw ecr_repository_url)
S3_BUCKET=$(terraform -chdir="$TF_DIR" output -raw s3_bucket_name)
CF_DIST_ID=$(terraform -chdir="$TF_DIR" output -raw cloudfront_distribution_id)
AWS_REGION=$(terraform -chdir="$TF_DIR" output -raw aws_region 2>/dev/null || echo "us-east-1")

IMAGE_TAG="${IMAGE_TAG:-latest}"
IMAGE_URI="$ECR_URI:$IMAGE_TAG"

echo "ECR: $IMAGE_URI"
echo "S3:  $S3_BUCKET"
echo "CF:  $CF_DIST_ID"

# ── Build & push Docker image ─────────────────────────────────────────────────
echo ""
echo "=== Building Docker image ==="
docker build -t greenhouse-guardian:$IMAGE_TAG "$ROOT_DIR"

echo "=== Pushing to ECR ==="
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REGISTRY="$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"

docker tag greenhouse-guardian:$IMAGE_TAG "$IMAGE_URI"
docker push "$IMAGE_URI"
echo "Image pushed: $IMAGE_URI"

# ── Restart service on EC2 ────────────────────────────────────────────────────
EC2_HOST="${EC2_HOST:-}"
EC2_KEY="${EC2_KEY:-~/.ssh/id_rsa}"

if [ -n "$EC2_HOST" ]; then
  echo ""
  echo "=== Restarting service on EC2 ($EC2_HOST) ==="
  ssh -i "$EC2_KEY" -o StrictHostKeyChecking=no ec2-user@"$EC2_HOST" \
    "aws ecr get-login-password --region $AWS_REGION \
       | docker login --username AWS --password-stdin $ECR_REGISTRY \
     && docker pull $IMAGE_URI \
     && sudo systemctl restart greenhouse-guardian"
  echo "Service restarted."
else
  echo "EC2_HOST not set — skipping EC2 restart. Set EC2_HOST=<ip> to deploy."
fi

# ── Sync frontend to S3 ───────────────────────────────────────────────────────
echo ""
echo "=== Syncing frontend to S3 ==="
aws s3 sync "$ROOT_DIR/frontend/" "s3://$S3_BUCKET/" \
  --delete \
  --cache-control "max-age=300" \
  --exclude "*.map"

# ── CloudFront invalidation ───────────────────────────────────────────────────
echo "=== Invalidating CloudFront cache ==="
INVALIDATION_ID=$(aws cloudfront create-invalidation \
  --distribution-id "$CF_DIST_ID" \
  --paths "/*" \
  --query 'Invalidation.Id' --output text)
echo "Invalidation created: $INVALIDATION_ID"

echo ""
echo "✅ Deploy complete."
echo "   CloudFront URL: $(terraform -chdir="$TF_DIR" output -raw cloudfront_url)"
