# Bulk Certificate Generator

## Project Purpose
Bulk Certificate Generator is a backend service designed to handle large-scale, automated certificate issuance for events, courses, and hackathons. Instead of manually rendering individual certificates, clients submit a batch of participant data to generate personalized vector PDF certificates, track job execution progress, and retrieve output files reliably.

---

## Current Implementation Status
* **Current Stage**: Stage 5 — Automatic Certificate Generation & End-to-End Processing
* **Implemented Components**:
  * Clean, modular project structure with separated concern boundaries.
  * Application configuration using Pydantic Settings with environment variable support (`storage_dir`, `database_url`).
  * FastAPI application entrypoint with interactive API documentation at `/docs`.
  * Operational health check endpoint at `GET /health`.
  * SQLAlchemy 2.0 database engine, declarative Base, and session factories with SQLite WAL mode, foreign-key enforcement, automatic schema backward compatibility for SQLite, and `UTCDateTime` normalization.
  * Database domain models: `GenerationJob` (with counter check constraints, issue date persistence) and `RecipientCertificate`.
  * Standalone PDF Generation Engine (`CertificateGeneratorService` in `app/services/certificate_generator.py`):
    * ReportLab pure-Python vector PDF rendering on landscape Letter layout.
    * Single predefined, professional certificate template with double borders, decorative accents, and signature lines.
    * Dynamic text fitting that scales down font sizes for long recipient or event names without overflowing.
    * Server-side unique certificate code generation (`CERT-XXXXXX-XXXXXX`).
    * UUID-based safe filenames and path-traversal prevention.
    * Injected storage directory support for test isolation.
    * Atomic file creation via temporary files (`.pdf.tmp`) with automatic error cleanup.
  * Reliable Job Processing Service (`JobProcessorService` in `app/services/job_processor.py`):
    * Atomic job claiming using conditional database updates to prevent concurrent duplicate processing.
    * Recipient batch validation (strict 500-recipient limit and count consistency).
    * Independent recipient processing with isolated error handling (single failures do not abort remaining recipients).
    * Counter reconciliation and strict invariant validation before terminal status finalization.
    * Safe orphan-file cleanup if database commits fail.
  * REST API Layer (`app/api/jobs.py`):
    * `POST /jobs`: Validates input, atomically persists job and recipient records, and immediately schedules in-process background processing.
    * `GET /jobs/{job_id}`: Tracks live progress, counter metrics, completion percentage, and recipient summaries (`recipient_id`, `name`, `email`, `status`).
    * `GET /jobs/{job_id}/recipients/{recipient_id}/certificate`: Securely streams generated certificate PDFs with strict storage containment checks and sanitized public error messages.
  * Automatic In-Process Background Processing via FastAPI `BackgroundTasks`.
  * Comprehensive automated test suite (75 passing tests) verifying PDF validity, metadata extraction, uniqueness, security boundaries, database invariants, reliable processing, error sanitization, and API workflows.

---

## PDF Generation Engine Architecture

The certificate generator is completely decoupled from HTTP routing and database transactions. It can be invoked directly as a pure domain service.

### 1. Template Specifications
* **Page Dimensions**: Standard landscape Letter (792 x 612 pt).
* **Color Palette**: Deep Navy (`#1A365D`), Warm Gold (`#C5A059`), Charcoal (`#2D3748`), Slate Grey (`#718096`).
* **Visual Elements**:
  * Outer primary border (4pt) and inner gold border (1.5pt) with corner geometric accents.
  * Header: "CERTIFICATE OF ACHIEVEMENT" with decorative separator line.
  * Recipient Name: Scaled dynamically to fit within a 620pt boundary; underlined with a gold accent.
  * Body: "has successfully participated in and completed" followed by the event name.
  * Footer: Left issuance date with signature line; right unique certificate identifier with verification line.

### 2. Security & Storage Guarantees
* **Server-Generated Codes**: Certificate identifiers are generated using cryptographically strong tokens (`secrets.token_hex`).
* **Path Traversal Protection**: Filenames are strictly validated against directory separators (`/`, `\`, `..`). All target paths must resolve strictly within the configured storage directory (`is_relative_to(storage_dir)`).
* **Atomic File Writing**: PDFs are initially rendered to a `.pdf.tmp` file and atomically renamed upon successful completion. Any rendering or disk failure immediately deletes partial files and raises `CertificateGenerationError`.

---

## Database Architecture & Models

### 1. `generation_jobs` Table
Stores bulk certificate generation requests and progress telemetry.

| Column | Type | Description |
| :--- | :--- | :--- |
| `id` | `UUID` (PK) | Globally unique identifier for the bulk job. |
| `event_name` | `VARCHAR(255)` | Name of the event or course for which certificates are issued. |
| `issue_date` | `DATE` | Nullable requested certificate issuance date printed on all certificates. |
| `status` | `ENUM` | Current lifecycle state: `PENDING`, `PROCESSING`, `COMPLETED`, `PARTIALLY_COMPLETED`, `FAILED`. |
| `total_count` | `INTEGER` | Total number of recipient certificates requested in the batch. |
| `processed_count` | `INTEGER` | Total recipients processed so far (`successful_count + failed_count`). |
| `successful_count` | `INTEGER` | Number of certificates rendered and saved successfully. |
| `failed_count` | `INTEGER` | Number of certificates that encountered errors during rendering. |
| `created_at` | `DATETIME (UTC)` | Timestamp when the job was accepted and persisted. |
| `started_at` | `DATETIME (UTC)` | Nullable timestamp when the background worker began processing. |
| `completed_at` | `DATETIME (UTC)` | Nullable timestamp when the batch finished processing. |

### 2. `recipient_certificates` Table
Stores individual participant records and their certificate generation outcomes.

| Column | Type | Description |
| :--- | :--- | :--- |
| `id` | `UUID` (PK) | Unique identifier for the recipient record. |
| `job_id` | `UUID` (FK) | References `generation_jobs.id` (`ON DELETE CASCADE`). |
| `recipient_name` | `VARCHAR(255)` | Full name printed on the certificate. |
| `recipient_email` | `VARCHAR(255)` | Email address of the participant (indexed). |
| `status` | `ENUM` | Individual state: `PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`. |
| `certificate_code` | `VARCHAR(100)` | Unique identifier string for verification (indexed, unique, nullable). |
| `pdf_path` | `VARCHAR(500)` | Nullable relative file path to the generated PDF in local storage. |
| `error_message` | `VARCHAR(1000)`| Nullable error explanation if generation failed for this recipient. |
| `created_at` | `DATETIME (UTC)` | Timestamp when the record was created. |
| `processed_at` | `DATETIME (UTC)` | Nullable timestamp when processing completed or failed. |

---

## Background Processing Architecture & Operational Constraints

When a client calls `POST /jobs`, the application creates the job, persists its recipients, commits the database transaction, and automatically schedules the job processor in the background.

### 1. In-Process Execution via FastAPI `BackgroundTasks`
* **Triggering**: Automatic processing is scheduled using FastAPI's standard in-process `BackgroundTasks` after the creation transaction commits.
* **Non-Blocking**: The HTTP request returns immediately with `201 Created` and initial `PENDING` status, keeping the API responsive for clients.
* **Concurrency Protection**: The background processor reuses the service's atomic claim mechanism (`UPDATE generation_jobs SET status = 'PROCESSING' WHERE id = :id AND status = 'PENDING'`). Duplicate triggers or concurrent workers cannot claim or process the same job twice.
* **Fault Isolation**: Each recipient in the batch is processed independently. A rendering or data failure for one recipient does not stop processing for subsequent recipients.

### 2. Operational Limitations & Non-Durable Queue Notice
> [!WARNING]
> **In-Process Background Task Lifecycle & Volatility**:
> Automatic processing executes in-process within the running application process. If the application server process crashes, terminates, or restarts while background tasks are queued or running, in-progress jobs may be interrupted or lost.
>
> This architecture does **not** use a durable distributed queue (such as Celery, Redis Streams, or RabbitMQ). For production workloads requiring high availability and persistent crash recovery across restarts, an external durable message broker and dedicated worker fleet should be deployed.

---

## Prerequisites
* **Operating System**: Windows (or compatible Unix/macOS environment)
* **Python**: Python 3.12+ (tested with Python 3.12.3)
* **Shell**: PowerShell (Windows)

---

## Setup Instructions (Windows PowerShell)

### 1. Clone or Open the Repository
```powershell
cd c:\vishnuprojects\aereoAssignment
```

### 2. Activate the Virtual Environment
```powershell
.\.venv\Scripts\Activate.ps1
```

### 3. Install Dependencies
```powershell
pip install -r requirements.txt
```

### 4. Configure Environment Variables (Optional)
```powershell
Copy-Item .env.example .env
```

---

## Running the Application

Start the development server using Uvicorn:
```powershell
uvicorn app.main:app --reload
```

---

## Verifying the Application

### Test the Health Endpoint
```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/health
```

### API Usage Examples (PowerShell)

#### 1. Create a Generation Job (`POST /jobs`)
Submit a certificate generation batch. The job is created and automatically begins processing in the background:

```powershell
$body = @{
    event_name = "Annual Tech Summit 2026"
    issue_date = "2026-10-15"
    recipients = @(
        @{ recipient_name = "Alice Smith"; recipient_email = "alice@example.com" },
        @{ recipient_name = "Bob Jones"; recipient_email = "bob@example.com" }
    )
} | ConvertTo-Json -Depth 5

$job = Invoke-RestMethod -Uri "http://127.0.0.1:8000/jobs" -Method Post -ContentType "application/json" -Body $body
$job
```

Example response (`201 Created`):
```json
{
  "id": "c0bc522d-3cb8-48b4-b258-29ba0e52702b",
  "event_name": "Annual Tech Summit 2026",
  "issue_date": "2026-10-15",
  "status": "PENDING",
  "total_count": 2,
  "processed_count": 0,
  "successful_count": 0,
  "failed_count": 0,
  "created_at": "2026-10-11T06:05:27.123456Z",
  "started_at": null,
  "completed_at": null
}
```

#### 2. Check Job Processing Status (`GET /jobs/{job_id}`)
Check batch execution status, progress percentage, counters, and per-recipient outcomes:

```powershell
$status = Invoke-RestMethod -Uri "http://127.0.0.1:8000/jobs/$($job.id)" -Method Get
$status
$status.recipients
```

Example response (`200 OK`):
```json
{
  "id": "c0bc522d-3cb8-48b4-b258-29ba0e52702b",
  "event_name": "Annual Tech Summit 2026",
  "issue_date": "2026-10-15",
  "status": "COMPLETED",
  "total_count": 2,
  "processed_count": 2,
  "successful_count": 2,
  "failed_count": 0,
  "created_at": "2026-10-11T06:05:27.123456Z",
  "started_at": "2026-10-11T06:05:27.200000Z",
  "completed_at": "2026-10-11T06:05:27.500000Z",
  "progress_percentage": 100.0,
  "recipients": [
    {
      "recipient_id": "4a737f55-1f9e-4eb7-91a5-29ce4fe0a56e",
      "name": "Alice Smith",
      "email": "alice@example.com",
      "status": "COMPLETED"
    },
    {
      "recipient_id": "8c21a412-2d1b-4fa8-bb32-11c5f89104b2",
      "name": "Bob Jones",
      "email": "bob@example.com",
      "status": "COMPLETED"
    }
  ]
}
```

#### 3. Download a Generated Certificate (`GET /jobs/{job_id}/recipients/{recipient_id}/certificate`)
Extract the recipient ID from the job-status response and download the rendered vector PDF file for that recipient:

```powershell
# Extract recipient ID from the job status response
$recipientId = $status.recipients[0].recipient_id

# Download and save the certificate PDF
Invoke-WebRequest -Uri "http://127.0.0.1:8000/jobs/$($job.id)/recipients/$recipientId/certificate" -OutFile "alice_certificate.pdf"
```

### Access Interactive Documentation (Swagger UI)
Navigate to:
```
http://127.0.0.1:8000/docs
```

---

## Running the Automated Tests

Run the full automated test suite:
```powershell
pytest -v
```

Run only the certificate generator engine tests:
```powershell
pytest tests/test_certificate_generator.py -v
```

Run only the database persistence tests:
```powershell
pytest tests/test_database.py -v
```
