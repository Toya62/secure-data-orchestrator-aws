import json
import logging
import os
import urllib.parse
import uuid
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Configurable environment settings
TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "AuditLog")
CLUSTER = os.environ.get("ECS_CLUSTER", "data-processing-cluster")
TASK_DEF = os.environ.get("ECS_TASK_DEF", "data-processor-task")
SUBNETS = [s.strip() for s in os.environ.get("SUBNETS", "").split(",") if s.strip()]


def get_clients(s3_client=None, dynamodb_resource=None, ecs_client=None):
    """Retrieve or reuse AWS service clients."""
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    s3 = s3_client or boto3.client("s3", region_name=region)
    ddb = dynamodb_resource or boto3.resource("dynamodb", region_name=region)
    ecs = ecs_client or boto3.client("ecs", region_name=region)
    return s3, ddb, ecs


def process_s3_record(record, s3_client=None, dynamodb_resource=None, ecs_client=None):
    """Process a single S3 ObjectCreated event record."""
    s3, ddb, ecs = get_clients(s3_client, dynamodb_resource, ecs_client)
    table = ddb.Table(TABLE_NAME)

    bucket = record["s3"]["bucket"]["name"]
    key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])

    job_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()

    # Log Audit: Upload Ingress
    table.put_item(
        Item={
            "job_id": job_id,
            "timestamp": timestamp,
            "status": "Upload",
            "file": key,
            "bucket": bucket,
        }
    )
    logger.info("Audit logged: Upload for %s (job_id: %s)", key, job_id)

    # Validation: Mandatory Organization Tag
    try:
        tags = s3.get_object_tagging(Bucket=bucket, Key=key)
        tag_dict = {t["Key"]: t["Value"] for t in tags.get("TagSet", [])}

        if "organization-id" not in tag_dict:
            logger.warning("Validation failed: No organization-id tag found for %s", key)
            table.put_item(
                Item={
                    "job_id": job_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "status": "Failed Validation (No org-id)",
                    "file": key,
                }
            )
            return {"job_id": job_id, "status": "FAILED_VALIDATION"}

    except Exception as e:
        logger.error("Error reading tags for %s: %s", key, e)
        table.put_item(
            Item={
                "job_id": job_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "Failed Validation (Tag Error)",
                "error": str(e),
                "file": key,
            }
        )
        return {"job_id": job_id, "status": "ERROR_TAGS"}

    org_id = tag_dict["organization-id"]

    # Log Audit: Trigger Authorized
    table.put_item(
        Item={
            "job_id": job_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "Trigger",
            "file": key,
            "org_id": org_id,
        }
    )

    # Execution: Trigger ECS Fargate Task
    try:
        run_task_kwargs = {
            "cluster": CLUSTER,
            "launchType": "FARGATE",
            "taskDefinition": TASK_DEF,
            "overrides": {
                "containerOverrides": [
                    {
                        "name": "processor",
                        "environment": [
                            {"name": "S3_BUCKET", "value": bucket},
                            {"name": "S3_KEY", "value": key},
                            {"name": "JOB_ID", "value": job_id},
                            {"name": "DYNAMODB_TABLE", "value": TABLE_NAME},
                            {"name": "ORG_ID", "value": org_id},
                        ],
                    }
                ]
            },
        }

        if SUBNETS:
            run_task_kwargs["networkConfiguration"] = {
                "awsvpcConfiguration": {
                    "subnets": SUBNETS,
                    "assignPublicIp": "ENABLED",
                }
            }

        response = ecs.run_task(**run_task_kwargs)
        task_arn = response.get("tasks", [{}])[0].get("taskArn", "N/A")
        logger.info("Started ECS task for %s: %s", key, task_arn)
        return {"job_id": job_id, "status": "TRIGGERED", "task_arn": task_arn}

    except Exception as e:
        logger.error("Error starting ECS task: %s", e)
        table.put_item(
            Item={
                "job_id": job_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "Failed Trigger",
                "error": str(e),
            }
        )
        return {"job_id": job_id, "status": "FAILED_TRIGGER", "error": str(e)}


def lambda_handler(event, context=None):
    """AWS Lambda entry point."""
    results = []
    for record in event.get("Records", []):
        res = process_s3_record(record)
        results.append(res)
    return {"statusCode": 200, "results": results}
