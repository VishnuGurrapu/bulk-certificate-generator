import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import _get_connect_args, get_db, init_db
from app.main import app
from app.models.job import GenerationJob, JobStatus
from app.models.recipient import RecipientCertificate, RecipientStatus
from app.services.job_processor import MAX_RECIPIENTS_PER_JOB


@pytest.fixture
def test_engine(tmp_path: Path):
    """Create an isolated SQLite database file for testing."""
    test_db_path = tmp_path / "test_api_jobs.db"
    db_url = f"sqlite:///{test_db_path.as_posix()}"
    engine = create_engine(
        db_url,
        connect_args=_get_connect_args(db_url),
    )
    init_db(target_engine=engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(test_engine):
    """Return a sessionmaker bound to the test database engine."""
    return sessionmaker(autocommit=False, autoflush=False, bind=test_engine, class_=Session)


@pytest.fixture
def client(session_factory) -> TestClient:
    """Provide a FastAPI TestClient with overridden get_db dependency."""

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()


def test_create_job_success(client: TestClient, session_factory) -> None:
    """Verify successful job creation returns 201 Created and persists records."""
    payload = {
        "event_name": "Cloud Native Architecture 2026",
        "recipients": [
            {"name": "Alice Johnson", "email": "alice@example.com"},
            {"name": "Bob Smith", "email": "bob@example.com"},
        ],
    }

    response = client.post("/jobs", json=payload)
    assert response.status_code == 201
    data = response.json()

    assert "id" in data
    job_id = uuid.UUID(data["id"])
    assert data["event_name"] == "Cloud Native Architecture 2026"
    assert data["status"] == "PENDING"
    assert data["total_count"] == 2
    assert "created_at" in data

    # Verify directly in the database
    with session_factory() as session:
        job = session.get(GenerationJob, job_id)
        assert job is not None
        assert job.status == JobStatus.PENDING
        assert job.total_count == 2
        assert len(job.recipients) == 2
        emails = {r.recipient_email for r in job.recipients}
        assert emails == {"alice@example.com", "bob@example.com"}


def test_create_job_empty_recipients_allowed(client: TestClient, session_factory) -> None:
    """Verify job creation with zero recipients is accepted and creates total_count=0."""
    payload = {
        "event_name": "Zero Recipient Event",
        "recipients": [],
    }

    response = client.post("/jobs", json=payload)
    assert response.status_code == 201
    data = response.json()

    assert data["total_count"] == 0
    assert data["status"] == "PENDING"

    job_id = uuid.UUID(data["id"])
    with session_factory() as session:
        job = session.get(GenerationJob, job_id)
        assert job is not None
        assert job.total_count == 0
        assert len(job.recipients) == 0


def test_create_job_validation_missing_event_name(client: TestClient) -> None:
    """Verify validation error when event_name is missing or empty."""
    response = client.post("/jobs", json={"event_name": "", "recipients": []})
    assert response.status_code == 422


def test_create_job_validation_malformed_recipient(client: TestClient) -> None:
    """Verify validation error when recipient has invalid email or empty name."""
    # Invalid email
    response_bad_email = client.post(
        "/jobs",
        json={
            "event_name": "Event",
            "recipients": [{"name": "User", "email": "not-an-email"}],
        },
    )
    assert response_bad_email.status_code == 422

    # Empty name
    response_bad_name = client.post(
        "/jobs",
        json={
            "event_name": "Event",
            "recipients": [{"name": "   ", "email": "valid@example.com"}],
        },
    )
    assert response_bad_name.status_code == 422


def test_create_job_exceeds_max_recipients(client: TestClient) -> None:
    """Verify that batches exceeding MAX_RECIPIENTS_PER_JOB (500) are rejected with 422."""
    too_many_recipients = [
        {"name": f"Recipient {i}", "email": f"user{i}@example.com"}
        for i in range(MAX_RECIPIENTS_PER_JOB + 1)
    ]
    response = client.post(
        "/jobs",
        json={"event_name": "Large Event", "recipients": too_many_recipients},
    )
    assert response.status_code == 422


def test_create_job_database_rollback_on_failure(client: TestClient, session_factory) -> None:
    """Verify that if persisting recipients fails, entire transaction rolls back."""
    # Force failure by inserting a duplicate recipient where certificate_code or DB constraint fails
    # Alternatively, simulate a flush error by overriding Session.add_all
    with session_factory() as session:
        initial_job_count = session.query(GenerationJob).count()

    # Pass an invalid payload at DB level by mocking or patching
    class ErrorSession(Session):
        def add_all(self, instances):
            raise RuntimeError("Database disk failure during recipient insertion")

    failing_factory = sessionmaker(
        autocommit=False, autoflush=False, bind=session_factory.kw["bind"], class_=ErrorSession
    )

    def override_failing_db():
        db = failing_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_failing_db
    try:
        with pytest.raises(RuntimeError):
            client.post(
                "/jobs",
                json={
                    "event_name": "Rollback Event",
                    "recipients": [{"name": "User", "email": "user@test.com"}],
                },
            )
    finally:
        app.dependency_overrides.clear()

    # Verify no partial job was created
    with session_factory() as session:
        current_job_count = session.query(GenerationJob).count()
        assert current_job_count == initial_job_count


def test_get_job_status_success(client: TestClient, session_factory) -> None:
    """Verify GET /jobs/{job_id} returns accurate counter and progress metrics."""
    with session_factory() as session:
        job = GenerationJob(
            event_name="Progress Tracking Event",
            total_count=4,
            processed_count=2,
            successful_count=2,
            failed_count=0,
            status=JobStatus.PROCESSING,
            started_at=datetime.now(timezone.utc),
        )
        session.add(job)
        session.commit()
        job_id = job.id

    response = client.get(f"/jobs/{job_id}")
    assert response.status_code == 200
    data = response.json()

    assert data["id"] == str(job_id)
    assert data["event_name"] == "Progress Tracking Event"
    assert data["status"] == "PROCESSING"
    assert data["total_count"] == 4
    assert data["processed_count"] == 2
    assert data["successful_count"] == 2
    assert data["failed_count"] == 0
    assert data["progress_percentage"] == 50.0
    assert data["started_at"] is not None
    assert data["completed_at"] is None


def test_get_job_status_zero_recipients_safe(client: TestClient, session_factory) -> None:
    """Verify progress calculation does not raise ZeroDivisionError for total_count=0."""
    with session_factory() as session:
        job = GenerationJob(
            event_name="Zero Recipient Completed Event",
            total_count=0,
            processed_count=0,
            successful_count=0,
            failed_count=0,
            status=JobStatus.COMPLETED,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )
        session.add(job)
        session.commit()
        job_id = job.id

    response = client.get(f"/jobs/{job_id}")
    assert response.status_code == 200
    data = response.json()

    assert data["total_count"] == 0
    assert data["progress_percentage"] == 100.0


def test_get_job_status_not_found(client: TestClient) -> None:
    """Verify GET /jobs/{job_id} returns 404 for non-existent job UUID."""
    random_id = uuid.uuid4()
    response = client.get(f"/jobs/{random_id}")
    assert response.status_code == 404
    assert f"Job with ID '{random_id}' not found." in response.json()["detail"]


def test_get_job_status_invalid_uuid(client: TestClient) -> None:
    """Verify GET /jobs/{job_id} returns 422 for malformed UUID in path."""
    response = client.get("/jobs/not-a-valid-uuid")
    assert response.status_code == 422
