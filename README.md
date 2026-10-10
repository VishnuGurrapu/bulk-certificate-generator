# Bulk Certificate Generator

## Project Purpose
Bulk Certificate Generator is a backend service designed to handle large-scale, automated certificate issuance for events, courses, and hackathons. Instead of manually rendering individual certificates, clients submit a batch of participant data to generate personalized vector PDF certificates, track job execution progress, and retrieve output files reliably.

---

## Current Implementation Status
* **Current Stage**: Stage 1 — Project Foundation
* **Implemented Components**:
  * Clean, modular project structure with separated concern boundaries.
  * Application configuration using Pydantic Settings.
  * FastAPI application entrypoint with interactive API documentation at `/docs`.
  * Operational health check endpoint at `GET /health`.
  * Automated testing configuration and health endpoint test via Pytest.
* **Not Yet Implemented**:
  * Database models and SQLite persistence.
  * ReportLab certificate template and PDF generation.
  * Background processing worker.
  * Bulk job submission, status tracking, and download APIs.

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

### 2. Create an Isolated Project Virtual Environment
Create a dedicated project virtual environment named `.venv`:
```powershell
python -m venv .venv
```

### 3. Activate the Virtual Environment
```powershell
.\.venv\Scripts\Activate.ps1
```
*(If script execution is restricted on your system, enable it for the current process: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`)*

### 4. Install Dependencies
Install the required Stage 1 dependencies inside the virtual environment:
```powershell
pip install -r requirements.txt
```

### 5. Configure Environment Variables (Optional)
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
Alternatively, using curl:
```bash
curl http://127.0.0.1:8000/health
```

### Access Interactive Documentation (Swagger UI)
Open your web browser and navigate to:
```
http://127.0.0.1:8000/docs
```
You can also view the ReDoc interface at:
```
http://127.0.0.1:8000/redoc
```

---

## Running the Automated Tests

Run the test suite using `pytest`:
```powershell
pytest -v
```

To run with short tracebacks:
```powershell
pytest
```
