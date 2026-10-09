# 🌿 Greenhouse Guardian

> Smart irrigation monitoring for a single tomato greenhouse — detects water problems before the crop does, tells the farmer what to do in plain words, and compares against a dumb timer system.

**Live demo:** `https://<your-cloudfront-url>` · **Auto demo:** `POST /demo`

---

## What it does

| Feature | Detail |
|---|---|
| 4 drip zones × 6 beds | Zone-level moisture monitoring and smart watering |
| 24-hour sim with speed control | 1× real time up to 720× (1 day in ~2 min) |
| 6 fault types detected | Dry zone, forecast, leak, blocked line, empty tank, faulty sensor |
| Tomato advice rules | Heat, chill, humidity/fungal risk, VPD, tank level |
| Scout robot | Patrols zones, verifies sensors, spot-waters blocked zones |
| Smart vs timer scoreboard | Water saved, stress hours, waste litres |
| Wokwi integration | Arduino/ESP32 circuit drives real sensor values via WebSocket bridge |
| AWS deployment | EC2 + ALB + S3 + CloudFront + CloudWatch — ~$34/mo |

---

## Quick start (local)

### Option A — Python directly

```bash
# 1. Clone and install
git clone https://github.com/your-org/greenhouse-guardian.git
cd greenhouse-guardian
pip install -r requirements.txt

# 2. Run
python -m uvicorn backend.server:app --host 0.0.0.0 --port 8000

# 3. Open browser
open http://localhost:8000
```

### Option B — Docker Compose

```bash
docker compose up --build
open http://localhost:8000
```

---

## 3-minute demo script

Either click the **▶ Auto Demo** button on the dashboard, or trigger it via:

```bash
curl -X POST http://localhost:8000/demo
```

The sequence runs automatically:

| Real time | Sim event | What to watch |
|---|---|---|
| 0–30 s | Normal operation | Smart system waters just in time; timer twin overwaters |
| 30 s | 🌡️ Heatwave injected | Forecast times shift, heat + VPD alerts appear |
| 60 s | 🟠 Blocked line Zone 3 | Robot routes to Zone 3, spot-waters; alert fires within seconds |
| 90 s | 🔴 Leak Zone 4 | Critical alert shows litres/hour lost |
| 120 s | ✅ Faults cleared | Scoreboard shows water savings vs timer twin |

### Manual fault injection

Use the buttons on the dashboard or:

```bash
curl -X POST http://localhost:8000/fault/leak
curl -X POST http://localhost:8000/fault/blocked_line
curl -X POST http://localhost:8000/fault/empty_tank
curl -X POST http://localhost:8000/fault/heatwave
curl -X POST http://localhost:8000/fault/broken_sensor
curl -X POST http://localhost:8000/fault/clear
```

### Speed control

```bash
curl -X POST http://localhost:8000/speed/360    # 360× (default — 1 day in 4 min)
curl -X POST http://localhost:8000/speed/720    # 720× (1 day in 2 min)
curl -X POST http://localhost:8000/speed/1      # real time
```

---

## Wokwi integration

1. Open the Wokwi project: [arduino/wokwi/](arduino/wokwi/)
2. For **local dev** — the `wokwi.toml` already points to `ws://localhost:8000/wokwi-serial`
3. For **production** — update `wokwi.toml` with the URL from Terraform output:
   ```bash
   terraform -chdir=infra/terraform output wokwi_serial_url
   # → wss://your-domain.com/wokwi-serial
   ```
4. Run the Wokwi simulation — the dashboard's **Wokwi: 🟢 live** badge lights up
5. Toggle fault pins (buttons in the circuit) to inject faults from the hardware side

> **Local tunnel:** If the backend is running locally and Wokwi needs to reach it from the internet, use ngrok:
> ```bash
> ngrok http 8000
> # Then update wokwi.toml serial url to the ngrok wss:// URL
> ```

---

## Project structure

```
BlackHydra/
├── arduino/
│   ├── greenhouse_sensors.ino   # ESP32 sketch — 1Hz JSON serial output
│   └── wokwi/
│       ├── diagram.json         # Wokwi circuit (ESP32 + sensors + buttons)
│       └── wokwi.toml           # Serial WebSocket bridge target
│
├── backend/
│   ├── simulator.py             # Physics engine + twin greenhouse
│   ├── detection.py             # Fault detection + forecasting
│   ├── advisor.py               # Tomato agronomic advice rules
│   ├── robot.py                 # Scout robot agent
│   ├── server.py                # FastAPI + WebSocket server
│   └── tests/
│       └── test_simulator.py    # pytest test suite
│
├── frontend/
│   └── index.html               # Single-file dashboard (vanilla JS)
│
├── infra/
│   ├── terraform/
│   │   ├── main.tf              # VPC, EC2, ALB, S3, CloudFront, CloudWatch
│   │   ├── variables.tf
│   │   └── outputs.tf
│   └── scripts/
│       ├── userdata.sh          # EC2 bootstrap — installs Docker, starts service
│       └── deploy.sh            # Manual deploy (build → ECR → EC2 → S3 → CF)
│
├── .github/workflows/
│   └── deploy.yml               # CI/CD: test → build → push ECR → deploy EC2 → S3
│
├── Dockerfile                   # 2-stage python:3.11-slim build
├── docker-compose.yml           # Local dev
└── requirements.txt
```

---

## AWS deployment

### Prerequisites

- AWS CLI configured (`aws configure`)
- Terraform ≥ 1.6 installed
- Docker installed
- A domain name (optional — ALB DNS works without one)

### Step 1 — Bootstrap Terraform

```bash
cd infra/terraform

# Review and set variables
cp terraform.tfvars.example terraform.tfvars   # create this file (see below)

terraform init
terraform plan
terraform apply
```

Example `terraform.tfvars`:
```hcl
aws_region   = "us-east-1"
domain_name  = ""                # leave empty to use ALB DNS directly
alert_email  = "you@example.com"
ec2_key_name = "my-key-pair"     # your existing EC2 key pair name
```

### Step 2 — Push the first Docker image

```bash
# Get ECR URL from Terraform output
ECR_URI=$(terraform -chdir=infra/terraform output -raw ecr_repository_url)

# Build and push
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin "$ECR_URI"

docker build -t greenhouse-guardian:latest .
docker tag greenhouse-guardian:latest "$ECR_URI:latest"
docker push "$ECR_URI:latest"
```

### Step 3 — Sync frontend to S3

```bash
S3=$(terraform -chdir=infra/terraform output -raw s3_bucket_name)
aws s3 sync frontend/ "s3://$S3/" --delete
```

### Step 4 — Get your live URLs

```bash
terraform -chdir=infra/terraform output
# alb_dns_name       = "http://gg-alb-xxxx.us-east-1.elb.amazonaws.com"
# cloudfront_url     = "https://xxxx.cloudfront.net"
# wokwi_serial_url   = "ws://gg-alb-xxxx.../wokwi-serial"
```

Open the CloudFront URL — dashboard is live.

---

## CI/CD setup (GitHub Actions)

Add these secrets to your GitHub repository (**Settings → Secrets → Actions**):

| Secret | Where to find it |
|---|---|
| `AWS_ROLE_ARN` | ARN of the OIDC role — see note below |
| `EC2_HOST` | `terraform output ec2_public_ip` |
| `EC2_SSH_KEY` | Contents of your `.pem` private key file |
| `S3_BUCKET_NAME` | `terraform output s3_bucket_name` |
| `CLOUDFRONT_DIST_ID` | `terraform output cloudfront_distribution_id` |

**OIDC role setup** (one-time, no static AWS keys in GitHub):
```bash
# In AWS Console → IAM → Identity Providers → Add provider
# Provider type: OpenID Connect
# Provider URL: https://token.actions.githubusercontent.com
# Audience: sts.amazonaws.com

# Then create an IAM role with:
#   - Trust: token.actions.githubusercontent.com
#   - Condition: repo:your-org/your-repo:ref:refs/heads/main
#   - Permissions: ECR push, EC2 describe, CloudFront invalidate, S3 sync
```

Once secrets are set, every push to `main` automatically:
1. Runs the full pytest test suite
2. Builds and pushes the Docker image to ECR
3. SSH-deploys to EC2 and restarts the service
4. Syncs frontend to S3 + invalidates CloudFront cache
5. Posts a deployment summary to the GitHub Actions job

---

## Running tests

```bash
# With pytest installed
pytest backend/tests/ -v

# Without installing (stdlib only)
python3 -m pytest backend/tests/ -v
```

---

## Detection thresholds (tomato-tuned)

| Condition | Threshold | Action |
|---|---|---|
| Soil dry — warning | < 25 % | Water now |
| Soil dry — critical / stress | < 20 % | Immediate irrigation |
| Stress forecast | time-to-20% < 4 h | Advance watering schedule |
| Leak | flow > 1.5× expected, soil not rising | Check drip joints |
| Blocked line | flow < 0.3× expected, moisture not rising | Robot spot-waters |
| Empty tank — warning | < 20 % | ~6 h of water left |
| Faulty sensor | Robot probe vs fixed > 8 points | Inspect sensor |
| Frozen sensor | No change for 30 sim-minutes | Check wiring |

---

## Tomato advice rules

| Condition | Advice |
|---|---|
| Temp > 32 °C | Open vents, roll down shade net, water early morning |
| Temp < 13 °C | Close vents before sunset — chilling damage risk |
| Humidity > 85 % | Fungal risk (botrytis, blight) — ventilate, avoid evening watering |
| VPD > 1.5 kPa | Plants losing water fast — mist or shade at midday |
| Tank 10–20 % | Plan a refill — about 1 day of water left |

---

## Cost estimate (AWS)

| Resource | ~Monthly |
|---|---|
| EC2 t3.small | $15 |
| Application Load Balancer | $18 |
| S3 + CloudFront | $0.12 |
| CloudWatch logs + alarms | $1 |
| **Total** | **~$34** |

> **Budget option:** Replace ALB with Caddy (reverse proxy on the EC2 instance) and use t3.micro → **~$10/mo total**.

---

## Roadmap (out of scope for MVP)

- Camera-based disease detection
- Real hardware (ESP32 + capacitive moisture sensors)
- Multiple crops / greenhouses
- SMS / WhatsApp alerts
- Machine learning yield prediction
- Historical moisture charts (time series DB)

---

## License

MIT
