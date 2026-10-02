# Hybrid Strategy: Decoupling Storage and Compute

**Objective:** Abstract the Backbone architecture so that Member Organizations can optionally use their own on-premises S3-compatible storage and local servers without rewriting the core orchestration logic.

## 1. Abstraction via Interfaces (The "Adapter Pattern")
Currently, the Backbone uses direct AWS SDKs (`boto3`) to interface with Amazon S3 and ECS Fargate. To decouple this, the system must interact with **Storage** and **Compute** interfaces rather than specific cloud services.

* **Storage Interface:** Defines methods like `upload_file()`, `get_metadata()`, and `download_file()`.
* **Compute Interface:** Defines methods like `trigger_job(container_image, env_vars)`.

## 2. Decoupling Storage
For organizations requiring local data residency, the Storage layer can easily be swapped.
* **AWS Implementation:** Uses `boto3` pointing to Amazon S3 endpoints.
* **On-Premises Implementation:** Organizations deploy an S3-compatible object store (e.g., MinIO or Ceph). The backbone's Storage Adapter is simply reconfigured to point to the local MinIO endpoint URL using standard S3 API calls. Because MinIO matches the AWS S3 API, the underlying code logic remains completely unchanged. 

## 3. Decoupling Compute
Running processing workloads locally requires abstracting away ECS Fargate.
* **Cloud Implementation:** The orchestrator invokes `ecs:RunTask`.
* **On-Premises Implementation:** Organizations run a local container orchestration tool (like Docker Swarm, Kubernetes, or HashiCorp Nomad). 
* **The Bridge:** Instead of the central Backbone pushing an ECS trigger, we invert the flow using an **Agent-based Event system** or a secure Webhook relay.
  * The cloud orchestrator logs the "Trigger" audit state and publishes a message to an event bus (e.g., Amazon EventBridge or RabbitMQ).
  * A lightweight, secure agent running on the organization's local server subscribes to this event bus.
  * Upon receiving the event, the local agent pulls the required Docker image and runs the processing container against their local MinIO storage.

## Summary
By enforcing **Storage compatibility (MinIO/S3 APIs)** and shifting to an **Event-driven Compute Abstraction** (using local agents listening to a central event bus), the orchestration layer remains firmly in the cloud. It manages the audit trail and validation rules while agnostic execution environments handle the sensitive data safely on-premises.