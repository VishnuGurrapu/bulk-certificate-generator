# Bulk Certificate Generator

## Project Purpose
Bulk Certificate Generator is a backend service designed to handle large-scale, automated certificate issuance for events, courses, and hackathons. Instead of manually rendering individual certificates, clients submit a batch of participant data to generate personalized vector PDF certificates, track job execution progress, and retrieve output files reliably.

---

## Current Implementation Status
* **Current Stage**: Stage 2 — Database Foundation
* **Implemented Components**:
  * Clean, modular project structure with separated concern boundaries.
  * Application configuration using Pydantic Settings with environment variable support.
  * FastAPI application entrypoint with interactive API documentation at `/docs`.
  * Operational health check endpoint at `GET /health`.
  * SQLAlchemy 2.0 database engine, declarative Base, and session factories with SQLite WAL mode and foreign-key enforcement (`PRAGMA foreign_keys=ON;`).
  * Database domain models:
    * `GenerationJob`: Job tracking with explicit lifecycle states (`PENDING`, `PROCESSING`, `COMPLETED`, `PARTIALLY_COMPLETED`, `FAILED`) and progress counters (`total_count`, `processed_count`, `successful_count`, `failed_count`).
    * `RecipientCertificate`: Recipient status (`PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`), unique certificate code, nullable PDF storage path, and error message tracking.
  * Database initialization function (`init_db`) with metadata registration.
  * Comprehensive automated test suite (Pytest + HTTPX) covering database persistence, constraints, foreign-key enforcement, cascading deletions, cross-session updates, and API health.
* **Not Yet Implemented**:
  * ReportLab certificate template and PDF generation.
  * Background processing worker.
  * Bulk job submission, status tracking, and download APIs.

---

## Database Architecture & Models

### 1. `generation_jobs` Table
Stores bulk certificate generation requests and high-level progress telemetry.

| Column | Type | Description |
| :--- | :--- | :--- |
| `id` | `UUID` (PK) | Globally unique identifier for the bulk job. |
| `event_name` | `VARCHAR(255)` | Name of the event or course for which certificates are issued. |
| `status` | `ENUM` | Current lifecycle state: `PENDING`, `PROCESSING`, `COMPLETED`, `PARTIALLY_COMPLETED`, `FAILED`. |
| `total_count` | `INTEGER` | Total number of recipient certificates requested in the batch. |
| `processed_count` | `INTEGER` | Total recipients processed so far (`successful_count + failed_count`). |
| `successful_count` | `INTEGER` | Number of certificates rendered and saved successfully. |
| `failed_count` | `INTEGER` | Number of certificates that encountered errors during rendering. |
| `created_at` | `DATETIME (UTC)` | Timestamp when the job was accepted and persisted. |
| `started_at` | `DATETIME (UTC)` | Nullable timestamp when the background worker began processing. |
| `completed_at` | `DATETIME (UTC)` | Nullable timestamp when the batch finished processing. |

#### Lifecycle State Semantics
* `PENDING`: Job is persisted and enqueued; worker has not yet picked it up.
* `PROCESSING`: Worker is actively processing and rendering recipient certificates.
* `COMPLETED`: All recipients succeeded (`successful_count == total_count`).
* `PARTIALLY_COMPLETED`: Processing finished, but at least one recipient failed and at least one succeeded (`successful_count > 0` and `failed_count > 0`).
* `FAILED`: Every recipient failed (`failed_count == total_count`) or an unhandled job-level exception aborted processing.

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

### 3. Relationships & Constraints
* **One-to-Many**: One `GenerationJob` has many `RecipientCertificate` records (`job.recipients`).
* **Cascade Delete**: Deleting a job automatically cascades to remove all associated recipient certificates.
* **Foreign Key Constraints**: Enforced at the SQLite connection level via `PRAGMA foreign_keys=ON;`.
* **Counter Invariants (CheckConstraints)**: Enforced directly on `generation_jobs`: non-negative counters, `processed_count = successful_count + failed_count`, and `processed_count <= total_count`.
* **Timezone Safety**: Custom `UTCDateTime` TypeDecorator ensures all timestamps retain `tzinfo=timezone.utc` when stored and reloaded from SQLite.
* **Concurrency & Safety**: Database connections are configured with busy timeouts (default 10s) and SQLite Write-Ahead Logging (`PRAGMA journal_mode=WAL;`). Transactions are strictly separated from CPU-bound PDF rendering work.

---

## Prerequisites
* **Operating System**: Windows (or compatible Unix/macOS environment)
* **Python**: Python 3.12+ (tested with Python 3.12.3)
* **Shell**: PowerShell (Windows)

---

## Setup Instructions (Windows PowerShell)

### 1. Clone or Open the Repository
Ensure your terminal is in the repository root directory:
```powershell
cd c:\vishnuprojects\aereoAssignment
```

### 2. Activate the Virtual Environment
```powershell
.\.venv\Scripts\Activate.ps1
```
*(If script execution is restricted on your system, enable it for the current process: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`)*

### 3. Install Dependencies
Install or update dependencies inside the virtual environment:
```powershell
pip install -r requirements.txt
```

### 4. Configure Environment Variables (Optional)
Copy the safe default environment configuration:
```powershell
Copy-Item .env.example .env
```

---

## Running the Application

Start the local development server using Uvicorn:
```powershell
uvicorn app.main:app --reload
```

The application will start at `http://127.0.0.1:8000`.

---

## Verifying the Application

### Test the Health Endpoint
In PowerShell, execute:
```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/health
```
Expected output:
```json
{"status": "ok"}
```

### Access Interactive Documentation (Swagger UI)
Open your web browser and navigate to:
```
http://127.0.0.1:8000/docs
```

---

## Running the Automated Tests

Run the full test suite (database persistence + API health check) using `pytest`:
```powershell
pytest -v
```

To run only the database tests:
```powershell
pytest tests/test_database.py -v
```
