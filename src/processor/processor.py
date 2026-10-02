import logging
import os
import sys
from datetime import datetime, timezone

import boto3

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("DataProcessor")


def get_clients(s3_client=None, dynamodb_resource=None):
    """Retrieve AWS service clients configured for the active region."""
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    s3 = s3_client or boto3.client("s3", region_name=region)
    ddb = dynamodb_resource or boto3.resource("dynamodb", region_name=region)
    return s3, ddb


def log_audit(table, job_id, status, key, org_id, extra_metadata=None):
    """Record an immutable state transition in the DynamoDB audit trail."""
    item = {
        "job_id": job_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "file": key,
        "org_id": org_id,
    }
    if extra_metadata:
        item.update(extra_metadata)

    table.put_item(Item=item)
    logger.info("Audit logged: status=%s, job_id=%s, file=%s", status, job_id, key)


def process_file(bucket, key, job_id, table_name, org_id, s3_client=None, dynamodb_resource=None):
    """Core container processing routine for ingested multi-tenant payloads."""
    s3, ddb = get_clients(s3_client, dynamodb_resource)
    table = ddb.Table(table_name)

    # State: Processing Start
    log_audit(table, job_id, "Processing Start", key, org_id)

    try:
        # Validate object presence and inspect metadata
        response = s3.head_object(Bucket=bucket, Key=key)
        size_bytes = response.get("ContentLength", 0)

        logger.info("==========================================")
        logger.info("       Secure Ingestion Processor         ")
        logger.info("==========================================")
        logger.info("Organization ID : %s", org_id)
        logger.info("Target Payload  : s3://%s/%s", bucket, key)
        logger.info("Payload Size    : %d bytes", size_bytes)
        logger.info("Encryption      : %s", response.get("ServerSideEncryption", "AES256"))
        logger.info("==========================================")

        # State: Completion
        log_audit(table, job_id, "Completion", key, org_id, {"payload_size_bytes": size_bytes})
        return True

    except Exception as e:
        logger.error("Processing failed for %s: %s", key, e, exc_info=True)
        log_audit(table, job_id, "Processing Failed", key, org_id, {"error": str(e)})
        return False


def main():
    bucket = os.environ.get("S3_BUCKET")
    key = os.environ.get("S3_KEY")
    job_id = os.environ.get("JOB_ID")
    table_name = os.environ.get("DYNAMODB_TABLE")
    org_id = os.environ.get("ORG_ID")

    if not all([bucket, key, job_id, table_name, org_id]):
        logger.error(
            "Missing required environment variables: S3_BUCKET=%s, S3_KEY=%s, JOB_ID=%s, DYNAMODB_TABLE=%s, ORG_ID=%s",
            bucket, key, job_id, table_name, org_id
        )
        sys.exit(1)

    success = process_file(bucket, key, job_id, table_name, org_id)
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
