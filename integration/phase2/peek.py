"""Read-only look at the local Phase 2 emulators (Moto S3 on 19500, ElasticMQ on 19324). Local dummy credentials."""
import boto3

KW = dict(region_name="us-east-1", aws_access_key_id="local-test", aws_secret_access_key="local-test")
s3 = boto3.client("s3", endpoint_url="http://127.0.0.1:19500", **KW)
sqs = boto3.client("sqs", endpoint_url="http://127.0.0.1:19324", **KW)
for o in s3.list_objects_v2(Bucket="pg-phase2").get("Contents", []):
    print("S3", o["Key"], o["Size"])
for q in ("pg-judgement.fifo", "pg-judgement-dlq.fifo"):
    url = sqs.get_queue_url(QueueName=q)["QueueUrl"]
    a = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
    print("SQS", q, {k: v for k, v in a.items() if k.startswith("ApproximateNumber")})
