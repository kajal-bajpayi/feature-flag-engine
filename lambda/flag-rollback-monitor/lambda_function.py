import boto3
import json
from datetime import datetime, timezone

dynamodb   = boto3.resource('dynamodb', region_name='ap-south-1')
flags_tbl  = dynamodb.Table('flags')
cloudwatch = boto3.client('cloudwatch', region_name='ap-south-1')
sns        = boto3.client('sns', region_name='ap-south-1')
events     = boto3.client('events', region_name='ap-south-1')

# Replace with your SNS topic ARN
SNS_TOPIC_ARN = 'arn:aws:sns:ap-south-1:YOUR_ACCOUNT_ID:flag-rollback-alerts'

def publish_metric(flag_id, metric_name, value):
    cloudwatch.put_metric_data(
        Namespace='FeatureFlagEngine',
        MetricData=[{
            'MetricName': metric_name,
            'Dimensions': [{'Name': 'FlagId', 'Value': flag_id}],
            'Value':      value,
            'Unit':       'Count',
            'Timestamp':  datetime.now(timezone.utc)
        }]
    )

def rollback_flag(flag_id, reason):
    """Disable flag instantly and notify via SNS."""
    flag = flags_tbl.get_item(Key={'flag_id': flag_id}).get('Item')
    if not flag:
        return

    flag['enabled']   = 'False'
    flag['percentage'] = '0'
    flag['updated_at'] = datetime.now(timezone.utc).isoformat()
    flags_tbl.put_item(Item=flag)

    # Invalidate all evaluator caches immediately
    events.put_events(Entries=[{
        'Source':       'feature-flag-engine',
        'DetailType':   'FlagUpdated',
        'Detail':       json.dumps({'flag_id': flag_id}),
        'EventBusName': 'default'
    }])

    # Alert via SNS
    sns.publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject=f'Flag Auto-Rolled Back: {flag_id}',
        Message=f"""
Flag: {flag_id}
Reason: {reason}
Action: Disabled and set to 0% rollout
Time: {datetime.now(timezone.utc).isoformat()}

Log into the Feature Flag dashboard to investigate.
        """
    )
    print(f"ROLLBACK executed for {flag_id}: {reason}")

def lambda_handler(event, context):
    """
    Triggered by SQS — processes batches of evaluation events.
    Publishes metrics to CloudWatch and checks rollback thresholds.
    """
    flag_stats = {}

    for record in event['Records']:
        body    = json.loads(record['body'])
        flag_id = body.get('flag_id')
        error   = body.get('error', 'false').lower() == 'true'
        latency = float(body.get('latency_ms', 0))

        if flag_id not in flag_stats:
            flag_stats[flag_id] = {
                'total': 0, 'errors': 0, 'total_latency': 0
            }

        flag_stats[flag_id]['total']         += 1
        flag_stats[flag_id]['total_latency'] += latency
        if error:
            flag_stats[flag_id]['errors'] += 1

    for flag_id, stats in flag_stats.items():
        total   = stats['total']
        errors  = stats['errors']
        avg_lat = stats['total_latency'] / total if total > 0 else 0

        error_rate = (errors / total * 100) if total > 0 else 0

        # Publish to CloudWatch
        publish_metric(flag_id, 'EvaluationCount', total)
        publish_metric(flag_id, 'ErrorCount',      errors)
        publish_metric(flag_id, 'ErrorRate',       error_rate)
        publish_metric(flag_id, 'AvgLatencyMs',    avg_lat)

        print(f"{flag_id}: {total} evals, {error_rate:.1f}% errors, {avg_lat:.0f}ms avg latency")

        # Get rollback thresholds from flag config
        flag         = flags_tbl.get_item(Key={'flag_id': flag_id}).get('Item', {})
        threshold    = flag.get('rollback_threshold', {})
        max_err_rate = float(threshold.get('error_rate', 5))
        max_latency  = float(threshold.get('latency_multiplier', 2)) * 200

        # Auto-rollback if thresholds breached (minimum 10 evaluations)
        if error_rate > max_err_rate and total >= 10:
            rollback_flag(flag_id, f"Error rate {error_rate:.1f}% exceeded threshold {max_err_rate}%")
        elif avg_lat > max_latency and total >= 10:
            rollback_flag(flag_id, f"Avg latency {avg_lat:.0f}ms exceeded threshold {max_latency:.0f}ms")

    return {'statusCode': 200, 'processed': len(event['Records'])}
