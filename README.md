# Bulk Certificate Generator

## Project Purpose
Bulk Certificate Generator is a backend service designed to handle large-scale, automated certificate issuance for events, courses, and hackathons. Instead of manually rendering individual certificates, clients submit a batch of participant data to generate personalized vector PDF certificates, track job execution progress, and retrieve output files reliably.

---

## Current Implementation Status
* **Current Stage**: Stage 3 — Standalone Certificate PDF Generation Engine
* **Implemented Components**:
  * Clean, modular project structure with separated concern boundaries.
  * Application configuration using Pydantic Settings with environment variable support (`storage_dir`, `database_url`).
  * FastAPI application entrypoint with interactive API documentation at `/docs`.
  * Operational health check endpoint at `GET /health`.
  * SQLAlchemy 2.0 database engine, declarative Base, and session factories with SQLite WAL mode, foreign-key enforcement, and `UTCDateTime` normalization.
  * Database domain models: `GenerationJob` (with counter check constraints) and `RecipientCertificate`.
  * Standalone PDF Generation Engine (`CertificateGeneratorService` in `app/services/certificate_generator.py`):
    * ReportLab pure-Python vector PDF rendering on landscape Letter layout.
    * Single predefined, professional certificate template with double borders, decorative accents, and signature lines.
    * Dynamic text fitting that scales down font sizes for long recipient or event names without overflowing.
    * Server-side unique certificate code generation (`CERT-XXXXXX-XXXXXX`).
    * UUID-based safe filenames and path-traversal prevention.
    * Injected storage directory support for test isolation.
    * Atomic file creation via temporary files (`.pdf.tmp`) with automatic error cleanup.
  * Comprehensive automated test suite (30 passing tests) verifying PDF validity, metadata extraction via `pypdf`, uniqueness, security boundaries, database invariants, and API health.
* **Not Yet Implemented**:
  * Background processing worker and queue orchestration.
  * Bulk job submission and status inspection endpoints.
  * Certificate download endpoints.

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
