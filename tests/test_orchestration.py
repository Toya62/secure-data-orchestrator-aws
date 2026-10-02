import sys
import unittest
from unittest.mock import MagicMock

# Add src modules to sys.path
sys.path.insert(0, "src/lambda")
sys.path.insert(0, "src/processor")

import index as lambda_handler_mod
import processor as processor_mod


class TestSecureDataOrchestrator(unittest.TestCase):
    def setUp(self):
        self.mock_s3 = MagicMock()
        self.mock_ddb = MagicMock()
        self.mock_ecs = MagicMock()
        self.mock_table = MagicMock()
        self.mock_ddb.Table.return_value = self.mock_table

    def test_lambda_success_with_valid_organization_tag(self):
        """Verify that a valid S3 record with organization-id tag triggers ECS and logs states."""
        record = {
            "s3": {
                "bucket": {"name": "test-ingress-bucket"},
                "object": {"key": "payloads/test_archive.zip"},
            }
        }

        # Mock S3 tagging
        self.mock_s3.get_object_tagging.return_value = {
            "TagSet": [{"Key": "organization-id", "Value": "org-alpha-99"}]
        }

        # Mock ECS response
        self.mock_ecs.run_task.return_value = {
            "tasks": [{"taskArn": "arn:aws:ecs:us-east-1:123456789012:task/cluster/test-task-123"}]
        }

        result = lambda_handler_mod.process_s3_record(
            record,
            s3_client=self.mock_s3,
            dynamodb_resource=self.mock_ddb,
            ecs_client=self.mock_ecs,
        )

        self.assertEqual(result["status"], "TRIGGERED")
        self.assertIn("test-task-123", result["task_arn"])

        # Check DynamoDB audit calls: Upload and Trigger
        self.assertEqual(self.mock_table.put_item.call_count, 2)
        statuses_logged = [call[1]["Item"]["status"] for call in self.mock_table.put_item.call_args_list]
        self.assertIn("Upload", statuses_logged)
        self.assertIn("Trigger", statuses_logged)

        # Check ECS RunTask parameters
        self.mock_ecs.run_task.assert_called_once()
        call_kwargs = self.mock_ecs.run_task.call_args[1]
        env_vars = call_kwargs["overrides"]["containerOverrides"][0]["environment"]
        env_map = {item["name"]: item["value"] for item in env_vars}
        self.assertEqual(env_map["ORG_ID"], "org-alpha-99")
        self.assertEqual(env_map["S3_KEY"], "payloads/test_archive.zip")

    def test_lambda_validation_failure_without_organization_tag(self):
        """Verify that S3 records missing organization-id tag fail validation and do NOT invoke ECS."""
        record = {
            "s3": {
                "bucket": {"name": "test-ingress-bucket"},
                "object": {"key": "unauthorized.zip"},
            }
        }

        # Empty TagSet
        self.mock_s3.get_object_tagging.return_value = {"TagSet": []}

        result = lambda_handler_mod.process_s3_record(
            record,
            s3_client=self.mock_s3,
            dynamodb_resource=self.mock_ddb,
            ecs_client=self.mock_ecs,
        )

        self.assertEqual(result["status"], "FAILED_VALIDATION")
        self.mock_ecs.run_task.assert_not_called()

        # Check DynamoDB audit calls: Upload and Failed Validation
        self.assertEqual(self.mock_table.put_item.call_count, 2)
        statuses_logged = [call[1]["Item"]["status"] for call in self.mock_table.put_item.call_args_list]
        self.assertIn("Upload", statuses_logged)
        self.assertIn("Failed Validation (No org-id)", statuses_logged)

    def test_processor_success(self):
        """Verify processor logs Processing Start and Completion after inspecting S3 object."""
        self.mock_s3.head_object.return_value = {
            "ContentLength": 1048576,
            "ServerSideEncryption": "AES256",
        }

        success = processor_mod.process_file(
            bucket="test-bucket",
            key="data.zip",
            job_id="job-uuid-1234",
            table_name="AuditLog",
            org_id="org-acme",
            s3_client=self.mock_s3,
            dynamodb_resource=self.mock_ddb,
        )

        self.assertTrue(success)
        self.assertEqual(self.mock_table.put_item.call_count, 2)
        statuses_logged = [call[1]["Item"]["status"] for call in self.mock_table.put_item.call_args_list]
        self.assertEqual(statuses_logged, ["Processing Start", "Completion"])

    def test_processor_failure_handling(self):
        """Verify processor handles S3 error gracefully and records Processing Failed in DynamoDB."""
        self.mock_s3.head_object.side_effect = RuntimeError("S3 Access Denied")

        success = processor_mod.process_file(
            bucket="test-bucket",
            key="missing.zip",
            job_id="job-uuid-1234",
            table_name="AuditLog",
            org_id="org-acme",
            s3_client=self.mock_s3,
            dynamodb_resource=self.mock_ddb,
        )

        self.assertFalse(success)
        statuses_logged = [call[1]["Item"]["status"] for call in self.mock_table.put_item.call_args_list]
        self.assertIn("Processing Failed", statuses_logged)


if __name__ == "__main__":
    unittest.main()
