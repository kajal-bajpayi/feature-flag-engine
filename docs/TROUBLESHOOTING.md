# Troubleshooting Log

Real errors encountered during the build and exactly how they were resolved.
This is a record of the actual debugging process — not a sanitised tutorial.

---

## Error 1 — `cannot import name 'decimal' from 'decimal'`

**Where:** `flag-api` Lambda — first deployment
**Symptom:** `Runtime.ImportModuleError` in CloudWatch logs
**Root cause:** Python naming conflict with the built-in `decimal` module when trying to use `Decimal` types for DynamoDB numeric fields.

**Fix:** Removed the `from decimal import Decimal` import entirely. Stored all numeric values as strings in DynamoDB instead:
```python
'percentage': str(body.get('percentage', 0))
```
DynamoDB handles string numerics cleanly and avoids the Decimal type requirement altogether.

**Lesson:** DynamoDB's Python SDK rejects float types and requires Decimal for numbers — but the simplest solution is to store numeric values as strings and convert on read. Avoids the Decimal complexity entirely.

---

## Error 2 — `Handler 'lambda_handler' missing on module 'lambda_function'`

**Where:** Lambda invocation after first API Gateway trigger
**Symptom:** `Runtime.HandlerNotFound` in CloudWatch logs
**Root cause:** Lambda's default handler setting is `lambda_function.lambda_handler` but the function was defined as `def handler(event, context)` — missing the `lambda_` prefix.

**Fix:** Renamed the function in code from `handler` to `lambda_handler` across all Lambda functions for consistency.

**Lesson:** Lambda resolves the entry point using the format `filename.functionname`. The default is `lambda_function.lambda_handler` — always match this unless you explicitly change the handler setting in Lambda configuration.

---

## Error 3 — `Missing Authentication Token` from API Gateway

**Where:** Browser and curl hitting API Gateway URL
**Symptom:** `{"message": "Missing Authentication Token"}` on every request
**Root cause:** Lambda proxy integration was not enabled on the API Gateway methods. Without proxy integration, API Gateway doesn't pass the full HTTP request — path, method, and body — to Lambda. The Lambda receives a partial event with no routing information.

**Fix:** Enabled Lambda Proxy Integration on each method:
API Gateway → method → Integration Request → Edit → check **Lambda Proxy Integration** → redeploy to `prod` stage.

**Lesson:** Always enable Lambda proxy integration when using a single Lambda to handle multiple routes. Without it, `event['httpMethod']` and `event['path']` are empty and every route check in the Lambda fails.

---

## Error 4 — `Route not found` returned from Lambda

**Where:** POST to `/flags` via curl after enabling proxy integration
**Symptom:** `{"error": "Route not found"}` despite the route existing in code
**Root cause:** Same root cause as Error 3 — proxy integration was enabled on some methods but not all. The methods where it wasn't enabled still passed empty path and method fields.

**Fix:** Went through every method (GET, POST, PUT, DELETE) and confirmed proxy integration was checked on each one individually. Redeployed.

**Lesson:** In the AWS console, proxy integration must be enabled per method — enabling it on one method does not apply to others on the same resource.

---

## Error 5 — `ValidationException: Missing the key flag-id in the item`

**Where:** DynamoDB `put_item` call in `flag-api`
**Symptom:** DynamoDB rejecting every write with a ValidationException
**Root cause:** The DynamoDB table was created with partition key `flag-id` (hyphen) but the Lambda code used `flag_id` (underscore). DynamoDB requires the partition key field to be present in every write — a mismatched name means it's always missing.

**Fix:** Deleted and recreated the DynamoDB table with partition key `flag_id` (underscore) to match the Lambda code exactly.

**Lesson:** Always use underscores in DynamoDB key names. Hyphens are valid in DynamoDB but cause constant confusion with Python variable names. Catch this at table creation — changing a partition key requires deleting and recreating the table.

---

## Error 6 — `AccessDeniedException: not authorized to perform events:PutEvents`

**Where:** `flag-api` Lambda calling EventBridge after a successful DynamoDB write
**Symptom:** Lambda crashes on the `send_invalidation()` call despite DynamoDB write succeeding
**Root cause:** IAM role `feature-flag-engine-role` was missing EventBridge permissions. The role had DynamoDB and SQS access but `AmazonEventBridgeFullAccess` was never attached.

**Fix:** IAM → Roles → `feature-flag-engine-role` → Add permissions → Attach `AmazonEventBridgeFullAccess`.

**Lesson:** IAM permissions are additive — every new AWS service you call from Lambda needs an explicit permission. The pattern for debugging is: if the Lambda works until a specific line then crashes with `AccessDeniedException`, that line is calling a service missing from the IAM role. Check the role first before looking at the code.

---

## Error 7 — Flag evaluates as `false` even when enabled

**Where:** `flag-evaluator` Lambda
**Symptom:** All users returning `false` despite the flag showing as enabled in DynamoDB
**Root cause:** Two separate issues compounding each other:

1. **Type inconsistency** — the flag was created via POST which stored `enabled` as the string `'True'`. When updated via PUT, it was stored as the JSON boolean `true`. The evaluation check `flag.get('enabled') != 'True'` only handled the string format, so boolean `true` failed the check and always returned `false`.

2. **Stale memory cache** — Lambda memory cache held the old disabled state from before the flag was enabled. The cache doesn't clear until the Lambda container restarts.

**Fix for type inconsistency:**
```python
enabled = flag.get('enabled')
if enabled != 'True' and enabled != True and str(enabled).lower() != 'true':
    return False
```

**Fix for stale cache:** Forced a Lambda cold start by changing memory from 256MB → 257MB → Save → 256MB → Save. This restarts the container and clears the in-memory cache.

**Lesson:** When storing boolean values in DynamoDB via API, be explicit about types. Pick one format — string `'True'`/`'False'` or boolean — and handle it consistently across all write paths. Mixed types across create and update operations cause silent evaluation bugs that are hard to trace.

---

## Error 8 — Silent correctness bug: `if` block missing `return False`

**Where:** `evaluate_flag` function in `flag-evaluator`
**Symptom:** No error in CloudWatch — flag just always returned `false` with no exception
**Root cause:** The disabled-flag check had no body:
```python
# BROKEN — if block has no body
if enabled != 'True' and enabled != True and str(enabled).lower() != 'true':

# next line was mistakenly treated as the if body due to indentation
target_users = flag.get('target_users', [])
```
Python in some editors made the indentation ambiguous. The condition evaluated but did nothing — execution fell through to the hashing logic which returned `false` for users outside the percentage bucket.

**Fix:** Added the missing `return False`:
```python
if enabled != 'True' and enabled != True and str(enabled).lower() != 'true':
    return False  # ← this line was missing
```

**Lesson:** A missing `return` in evaluation logic is a silent correctness bug, not a runtime error — CloudWatch shows no error because the function completes successfully, just with the wrong answer. When flag evaluation returns unexpected results with no errors in logs, check every conditional branch has an explicit return value.

---

