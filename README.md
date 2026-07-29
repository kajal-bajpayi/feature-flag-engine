# Feature Flag Engine — AWS

A production-grade feature flag and progressive rollout system built from scratch on AWS — no LaunchDarkly, no third-party tools.

![AWS](https://img.shields.io/badge/AWS-Serverless-orange?logo=amazon-aws)
![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python)
![Status](https://img.shields.io/badge/Status-Live-green)
![Region](https://img.shields.io/badge/Region-ap--south--1-yellow)

---

## What it does

Ships features to a controlled percentage of users — 1% first, watch the metrics, roll to 100% when confident. If error rate or latency degrades, the system rolls back automatically — no 3am incident, no hotfix.

```
Operator creates flag at 10% rollout
        │
        ▼
Users start hitting the feature
        │
        ├── 10% of users → new experience
        └── 90% of users → old experience
                │
                ▼
        Error rate spikes to 8% (threshold: 5%)
                │
                ▼
        Auto-rollback fires → flag disabled in seconds
        SNS alert sent → operator notified
```

---

## Live Demo

- **Control Plane Dashboard:** `http://feature-flag-dashboard.s3-website.ap-south-1.amazonaws.com`
- **Flags API:** `https://uiqrb6enf0.execute-api.ap-south-1.amazonaws.com/prod/flags`
- **Evaluate API:** `https://uiqrb6enf0.execute-api.ap-south-1.amazonaws.com/prod/evaluate`

### Try it

```bash
# Create a flag
curl -X POST https://uiqrb6enf0.execute-api.ap-south-1.amazonaws.com/prod/flags \
  -H "Content-Type: application/json" \
  -d '{"flag_id": "my-feature", "enabled": true, "percentage": 50}'

# Evaluate for a user
curl -X POST https://uiqrb6enf0.execute-api.ap-south-1.amazonaws.com/prod/evaluate \
  -H "Content-Type: application/json" \
  -d '{"flag_id": "my-feature", "user_id": "user_123", "region": "ap-south-1"}'

# Same user always gets the same result — consistent hashing
curl -X POST https://uiqrb6enf0.execute-api.ap-south-1.amazonaws.com/prod/evaluate \
  -H "Content-Type: application/json" \
  -d '{"flag_id": "my-feature", "user_id": "user_123", "region": "ap-south-1"}'
```

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   CONTROL PLANE                      │
│  React Dashboard → API Gateway → flag-api Lambda     │
│                        │                             │
│              DynamoDB (flags + audit)                │
│                        │                             │
│              EventBridge (cache invalidation)        │
└─────────────────────────────────────────────────────┘
                         │
┌─────────────────────────────────────────────────────┐
│                 EVALUATION ENGINE                    │
│  App → POST /evaluate → flag-evaluator Lambda        │
│              │                                       │
│    ┌─────────┴──────────┐                           │
│    │  Memory cache hit? │                           │
│    └──Yes──────────No───┘                           │
│       │              │                              │
│    Return         DynamoDB                          │
│    instantly      fetch + cache                     │
│              │                                       │
│    Consistent hash → true/false                     │
│              │                                       │
│    SQS (async evaluation log)                       │
└─────────────────────────────────────────────────────┘
                         │
┌─────────────────────────────────────────────────────┐
│              ANALYTICS + AUTO-ROLLBACK               │
│  SQS → flag-rollback-monitor Lambda                  │
│              │                                       │
│    CloudWatch custom metrics per flag               │
│    (EvaluationCount, ErrorRate, AvgLatencyMs)       │
│              │                                       │
│    Threshold breach? → Rollback + SNS alert         │
└─────────────────────────────────────────────────────┘
```

---

## Key Technical Decisions

### Consistent Hashing
```python
def consistent_hash(flag_id, user_id):
    key   = f"{flag_id}:{user_id}"
    value = int(hashlib.md5(key.encode()).hexdigest(), 16)
    return value % 100
```
Same user always gets the same bucket. No flag flicker — a user on 50% rollout sees the same variant on every request, every day.

### Lambda Memory Cache
Flag config loads from DynamoDB once per Lambda container lifetime. Warm evaluations are sub-millisecond with zero DynamoDB cost. Cache invalidates via EventBridge the moment a flag is updated.

### Async Evaluation Logging
Evaluations log to SQS asynchronously — response returned before the log write. A slow SQS call never adds latency to the evaluation path. Try/except around the log call means a logging failure never kills a flag evaluation.

### Evaluation Priority
1. Flag disabled → always `false`
2. User in `target_users` → always `true` (override)
3. User region not in `target_regions` → `false`
4. `hash(flag + user) % 100 < percentage` → `true/false`

---

## AWS Services

| Service | Purpose | Free Tier |
|---|---|---|
| Lambda ×3 | flag-api, flag-evaluator, flag-rollback-monitor | 1M req/mo |
| DynamoDB ×3 | flags, evaluations, audit | 25GB forever |
| API Gateway | REST endpoints for flags + evaluate | 1M calls/mo |
| EventBridge | Cache invalidation on every flag update | 14M events/mo |
| SQS | Async evaluation logging buffer | 1M req/mo |
| CloudWatch | Custom metrics: EvaluationCount, ErrorRate, AvgLatencyMs | 10 metrics/mo |
| SNS | Rollback alerts | Free tier |
| SES | Email notifications | 3k emails/mo |
| S3 + CloudFront | React control plane dashboard | 5GB + 1TB |

**Total monthly cost: $0 (within AWS free tier)**

---

## API Reference

### Flags API

```
GET    /flags              — List all flags
POST   /flags              — Create a flag
PUT    /flags/{flag_id}    — Update / toggle a flag
DELETE /flags/{flag_id}    — Delete a flag
```

**Create flag request body:**
```json
{
  "flag_id": "new-checkout-ui",
  "enabled": true,
  "percentage": 50,
  "target_users": ["user_vip_1", "user_vip_2"],
  "target_regions": ["ap-south-1"],
  "rollback_threshold": {
    "error_rate": "5",
    "latency_multiplier": "2"
  }
}
```

### Evaluate API

```
POST /evaluate
```

```json
{
  "flag_id": "new-checkout-ui",
  "user_id": "user_kajal_42",
  "region": "ap-south-1"
}
```

**Response:**
```json
{
  "flag_id": "new-checkout-ui",
  "user_id": "user_kajal_42",
  "result": true,
  "region": "ap-south-1"
}
```

---

## Repository Structure

```
feature-flag-engine/
│
├── lambda/
│   ├── flag-api/
│   │   └── lambda_function.py       # CRUD API for flags + audit log
│   ├── flag-evaluator/
│   │   └── lambda_function.py       # Consistent hash evaluation engine
│   └── flag-rollback-monitor/
│       └── lambda_function.py       # SQS consumer + CloudWatch metrics + auto-rollback
│
├── dashboard/
│   └── index.html                   # React control plane (hosted on S3)
│
├── iam/
│   └── lambda-policy.json           # Least-privilege IAM policy
│
├── docs/
│   ├── ARCHITECTURE.md              # Deep-dive on design decisions
│   └── TROUBLESHOOTING.md          # Real errors encountered + fixes
│
└── README.md
```

---

## Setup Guide

### Prerequisites
- AWS account (free tier)
- AWS Console access as IAM user (not root)

### Step 1 — IAM Role
Create role `feature-flag-engine-role` with policies:
- `AmazonDynamoDBFullAccess`
- `AmazonSQSFullAccess`
- `AmazonSNSFullAccess`
- `AmazonSESFullAccess`
- `CloudWatchFullAccess`
- `AmazonEventBridgeFullAccess`
- `AWSLambdaBasicExecutionRole`

### Step 2 — DynamoDB Tables
Create three tables with **String** partition keys:
- `flags` → partition key: `flag_id`
- `evaluations` → partition key: `eval_id`, sort key: `flag_id`
- `audit` → partition key: `audit_id`

### Step 3 — SQS Queue
Create Standard queue: `flag-evaluations-queue`

### Step 4 — SNS Topic + SES
- Create SNS topic: `flag-rollback-alerts` → subscribe your email
- Verify your email in SES

### Step 5 — Lambda Functions
Deploy three Lambdas using code from `lambda/` directory:
- `flag-api` — 256MB, 15s timeout
- `flag-evaluator` — 256MB, 10s timeout
- `flag-rollback-monitor` — 256MB, 30s timeout, SQS trigger

### Step 6 — API Gateway
Create REST API with Lambda proxy integration:
- `GET /flags` → flag-api
- `POST /flags` → flag-api
- `PUT /flags/{flag_id}` → flag-api
- `DELETE /flags/{flag_id}` → flag-api
- `POST /evaluate` → flag-evaluator

Deploy to `prod` stage.

### Step 7 — Dashboard
Upload `dashboard/index.html` to an S3 bucket with static website hosting enabled.

---

## Troubleshooting

See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) for a full log of every error encountered during the build and exactly how each was resolved.

---


