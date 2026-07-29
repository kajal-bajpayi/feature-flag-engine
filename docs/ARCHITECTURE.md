# Architecture

## System Overview

The Feature Flag Engine is a self-built progressive rollout system — no LaunchDarkly, no third-party tools. Built entirely on AWS serverless services.

## Three Core Components

### 1. Control Plane
Where operators create, configure, and toggle flags.

```
React Dashboard (S3 + CloudFront)
        │
        │ HTTP
        ▼
API Gateway (REST)
        │
        │ Lambda Proxy
        ▼
flag-api Lambda
        │
        ├── DynamoDB (flags table)    — stores flag config
        ├── DynamoDB (audit table)    — immutable change log
        └── EventBridge               — cache invalidation signal
```

### 2. Evaluation Engine
The hot path — called on every feature flag check.

```
Any App / Python SDK
        │
        │ POST /evaluate
        ▼
API Gateway
        │
        ▼
flag-evaluator Lambda
        │
        ├── Memory cache (30s TTL)    — sub-ms response on cache hit
        ├── DynamoDB (flags table)    — loaded on cache miss
        ├── Consistent hashing        — stable user → bucket mapping
        └── SQS (async)              — logs evaluation without slowing response
```

### 3. Analytics + Auto-Rollback
Watches what happens after a flag is enabled.

```
SQS (flag-evaluations-queue)
        │
        │ Event Source Mapping
        ▼
flag-rollback-monitor Lambda
        │
        ├── CloudWatch custom metrics  — EvaluationCount, ErrorRate, AvgLatencyMs
        └── Auto-rollback logic
                │
                ├── If error_rate > threshold → disable flag + SNS alert
                └── If avg_latency > threshold → disable flag + SNS alert
```

## Key Design Decisions

### Consistent Hashing
```python
def consistent_hash(flag_id, user_id):
    key   = f"{flag_id}:{user_id}"
    value = int(hashlib.md5(key.encode()).hexdigest(), 16)
    return value % 100
```
Same user always gets the same bucket (0-99). No flag flicker — a user sees the same variant on every page load.

### Lambda Memory Cache
Flag config is loaded from DynamoDB once and cached in Lambda memory. Subsequent evaluations on the same warm container are sub-millisecond with zero DynamoDB cost. Cache is invalidated via EventBridge when a flag is updated.

### Async Evaluation Logging
Evaluations are logged to SQS asynchronously — the response is returned to the caller before the log write happens. This means a slow SQS call never adds latency to the critical evaluation path.

### Evaluation Priority Order
1. Flag disabled → always `false`
2. User in `target_users` list → always `true`
3. User region not in `target_regions` → `false`
4. `hash(flag_id + user_id) % 100 < percentage` → `true`/`false`

## AWS Services

| Service | Purpose | Free Tier |
|---|---|---|
| Lambda ×3 | flag-api, flag-evaluator, flag-rollback-monitor | 1M req/mo |
| DynamoDB ×3 | flags, evaluations, audit tables | 25GB forever |
| API Gateway | REST endpoints | 1M calls/mo |
| EventBridge | Cache invalidation on flag changes | 14M events/mo |
| SQS | Async evaluation logging buffer | 1M req/mo |
| CloudWatch | Custom metrics + alarms | 10 metrics/mo |
| SNS + SES | Rollback alerts + notifications | Free tier |
| S3 + CloudFront | React dashboard hosting | 5GB + 1TB |

## DynamoDB Schema

### flags table
| Field | Type | Description |
|---|---|---|
| flag_id (PK) | String | e.g. `new-checkout-ui` |
| enabled | String | `'True'` or `'False'` |
| percentage | String | `'0'`–`'100'` |
| target_users | List | Specific user IDs |
| target_regions | List | e.g. `['ap-south-1']` |
| rollback_threshold | Map | `{error_rate, latency_multiplier}` |
| created_at | String | ISO timestamp |
| updated_at | String | ISO timestamp |

### evaluations table
| Field | Type | Description |
|---|---|---|
| eval_id (PK) | String | UUID |
| flag_id | String | Which flag was evaluated |
| user_id | String | Who was evaluated |
| result | String | `'True'` or `'False'` |
| region | String | User's region |
| latency_ms | Number | Caller-reported latency |
| error | String | `'true'` or `'false'` |
| timestamp | String | ISO timestamp |

### audit table
| Field | Type | Description |
|---|---|---|
| audit_id (PK) | String | UUID |
| flag_id | String | Which flag changed |
| action | String | CREATE / UPDATE / DELETE |
| details | String | JSON of what changed |
| timestamp | String | ISO timestamp |
