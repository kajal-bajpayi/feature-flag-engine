import boto3
import json
import uuid
from datetime import datetime, timezone

dynamodb  = boto3.resource('dynamodb', region_name='ap-south-1')
flags_tbl = dynamodb.Table('flags')
audit_tbl = dynamodb.Table('audit')
events    = boto3.client('events', region_name='ap-south-1')

def send_invalidation(flag_id):
    """Tell all evaluator Lambdas to clear their cache for this flag."""
    events.put_events(Entries=[{
        'Source':       'feature-flag-engine',
        'DetailType':   'FlagUpdated',
        'Detail':       json.dumps({'flag_id': flag_id}),
        'EventBusName': 'default'
    }])

def write_audit(action, flag_id, details):
    audit_tbl.put_item(Item={
        'audit_id':  str(uuid.uuid4()),
        'flag_id':   flag_id,
        'action':    action,
        'details':   json.dumps(details),
        'timestamp': datetime.now(timezone.utc).isoformat()
    })

def respond(status, body):
    return {
        'statusCode': status,
        'headers': {
            'Content-Type':                'application/json',
            'Access-Control-Allow-Origin':  '*'
        },
        'body': json.dumps(body)
    }

def lambda_handler(event, context):
    method = event.get('httpMethod', '')
    path   = event.get('path', '')
    body   = json.loads(event.get('body') or '{}')

    # GET /flags — list all flags
    if method == 'GET' and path == '/flags':
        result = flags_tbl.scan()
        return respond(200, result['Items'])

    # POST /flags — create a new flag
    if method == 'POST' and path == '/flags':
        flag_id = body.get('flag_id', '').strip()
        if not flag_id:
            return respond(400, {'error': 'flag_id is required'})

        item = {
            'flag_id':            flag_id,
            'enabled':            str(body.get('enabled', False)),
            'percentage':         str(body.get('percentage', 0)),
            'target_users':       body.get('target_users', []),
            'target_regions':     body.get('target_regions', []),
            'rollback_threshold': body.get('rollback_threshold', {
                'error_rate':        '5',
                'latency_multiplier': '2'
            }),
            'created_at': datetime.now(timezone.utc).isoformat(),
            'updated_at': datetime.now(timezone.utc).isoformat()
        }
        flags_tbl.put_item(Item=item)
        write_audit('CREATE', flag_id, item)
        send_invalidation(flag_id)
        return respond(201, item)

    # PUT /flags/{flag_id} — update or toggle a flag
    if method == 'PUT' and path.startswith('/flags/'):
        flag_id  = path.split('/')[-1]
        existing = flags_tbl.get_item(Key={'flag_id': flag_id}).get('Item')
        if not existing:
            return respond(404, {'error': 'Flag not found'})

        existing.update({
            'enabled':        str(body.get('enabled', existing['enabled'])),
            'percentage':     str(body.get('percentage', existing['percentage'])),
            'target_users':   body.get('target_users',   existing['target_users']),
            'target_regions': body.get('target_regions', existing['target_regions']),
            'updated_at':     datetime.now(timezone.utc).isoformat()
        })
        flags_tbl.put_item(Item=existing)
        write_audit('UPDATE', flag_id, body)
        send_invalidation(flag_id)
        return respond(200, existing)

    # DELETE /flags/{flag_id} — delete a flag
    if method == 'DELETE' and path.startswith('/flags/'):
        flag_id = path.split('/')[-1]
        flags_tbl.delete_item(Key={'flag_id': flag_id})
        write_audit('DELETE', flag_id, {})
        send_invalidation(flag_id)
        return respond(200, {'message': f'Flag {flag_id} deleted'})

    return respond(404, {'error': 'Route not found'})
