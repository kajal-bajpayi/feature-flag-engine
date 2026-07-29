import boto3
import json
import uuid
import hashlib
from datetime import datetime, timezone

dynamodb   = boto3.resource('dynamodb', region_name='ap-south-1')
flags_tbl  = dynamodb.Table('flags')
sqs        = boto3.client('sqs', region_name='ap-south-1')

# SQS queue URL — replace with your queue URL
SQS_URL = 'https://sqs.ap-south-1.amazonaws.com/YOUR_ACCOUNT_ID/flag-evaluations-queue'

# In-memory cache — lives for the lifetime of this Lambda container
# Invalidated via EventBridge when a flag is updated
_cache = {}

def get_flag(flag_id):
    """Return flag from memory cache if available, else fetch from DynamoDB."""
    if flag_id in _cache:
        print(f"Cache hit for {flag_id}")
        return _cache[flag_id]

    result = flags_tbl.get_item(Key={'flag_id': flag_id})
    flag   = result.get('Item')
    if flag:
        _cache[flag_id] = flag
        print(f"Cache miss — loaded {flag_id} from DynamoDB")
    return flag

def consistent_hash(flag_id, user_id):
    """
    Produces a stable number 0-99 for any flag+user combination.
    Same inputs always produce the same output — no flag flicker.
    """
    key   = f"{flag_id}:{user_id}"
    value = int(hashlib.md5(key.encode()).hexdigest(), 16)
    return value % 100

def evaluate_flag(flag, user_id, user_region):
    """
    Evaluation priority:
    1. Flag disabled → always False
    2. User in target_users list → always True
    3. User region not in target_regions → False
    4. Consistent hash bucket < percentage → True
    """
    enabled = flag.get('enabled')
    if enabled != 'True' and enabled != True and str(enabled).lower() != 'true':
        return False

    # Specific user targeting — always wins
    target_users = flag.get('target_users', [])
    if user_id in target_users:
        return True

    # Region targeting — must match if regions are set
    target_regions = flag.get('target_regions', [])
    if target_regions and user_region not in target_regions:
        return False

    # Percentage rollout via consistent hashing
    percentage = int(flag.get('percentage', '0'))
    bucket     = consistent_hash(flag['flag_id'], user_id)
    return bucket < percentage

def log_evaluation(flag_id, user_id, result, region):
    """Log evaluation async to SQS — never slows the response."""
    try:
        sqs.send_message(
            QueueUrl=SQS_URL,
            MessageBody=json.dumps({
                'eval_id':   str(uuid.uuid4()),
                'flag_id':   flag_id,
                'user_id':   user_id,
                'result':    str(result),
                'region':    region,
                'timestamp': datetime.now(timezone.utc).isoformat()
            })
        )
    except Exception as e:
        # Never fail the evaluation because logging failed
        print(f"Log error (non-fatal): {e}")

def respond(status, body):
    return {
        'statusCode': status,
        'headers': {
            'Content-Type':               'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(body)
    }

def lambda_handler(event, context):
    body        = json.loads(event.get('body') or '{}')
    flag_id     = body.get('flag_id', '')
    user_id     = body.get('user_id', '')
    user_region = body.get('region', 'ap-south-1')

    if not flag_id or not user_id:
        return respond(400, {'error': 'flag_id and user_id are required'})

    flag = get_flag(flag_id)
    if not flag:
        return respond(404, {'error': f'Flag {flag_id} not found'})

    result = evaluate_flag(flag, user_id, user_region)
    log_evaluation(flag_id, user_id, result, user_region)

    print(f"Evaluated {flag_id} for {user_id} → {result}")
    return respond(200, {
        'flag_id': flag_id,
        'user_id': user_id,
        'result':  result,
        'region':  user_region
    })
