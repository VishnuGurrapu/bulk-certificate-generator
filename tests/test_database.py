import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import _get_connect_args, init_db
from app.models.job import GenerationJob, JobStatus
from app.models.recipient import RecipientCertificate, RecipientStatus


@pytest.fixture
def test_engine(tmp_path: Path):
    """Create an isolated SQLite database file for testing."""
    test_db_path = tmp_path / "test_bulk_certificates.db"
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
def db_session(session_factory):
    """Provide a transactional database session for an individual test."""
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def test_init_db_creates_tables(test_engine) -> None:
    """Verify that init_db creates all expected tables."""
    inspector = inspect(test_engine)
    table_names = inspector.get_table_names()
    assert "generation_jobs" in table_names
    assert "recipient_certificates" in table_names


def test_create_and_persist_job(db_session: Session) -> None:
    """Verify creating a job with UUID primary key and default attributes."""
    job = GenerationJob(
        event_name="National Robotics Championship 2026",
        total_count=100,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    assert isinstance(job.id, uuid.UUID)
    assert job.event_name == "National Robotics Championship 2026"
    assert job.status == JobStatus.PENDING
    assert job.total_count == 100
    assert job.processed_count == 0
    assert job.successful_count == 0
    assert job.failed_count == 0
    assert isinstance(job.created_at, datetime)
    assert job.started_at is None
    assert job.completed_at is None


def test_create_recipients_for_job(db_session: Session) -> None:
    """Verify creating multiple recipients associated with a parent job."""
    job = GenerationJob(
        event_name="AI Python Bootcamp",
        total_count=2,
    )
    db_session.add(job)
    db_session.commit()

    recipient_1 = RecipientCertificate(
        job_id=job.id,
        recipient_name="Alice Smith",
        recipient_email="alice@example.com",
    )
    recipient_2 = RecipientCertificate(
        job_id=job.id,
        recipient_name="Bob Jones",
        recipient_email="bob@example.com",
    )
    db_session.add_all([recipient_1, recipient_2])
    db_session.commit()

    # Query through the relationship
    db_session.refresh(job)
    assert len(job.recipients) == 2
    recipient_emails = {r.recipient_email for r in job.recipients}
    assert recipient_emails == {"alice@example.com", "bob@example.com"}

    # Verify recipient defaults
    for r in job.recipients:
        assert isinstance(r.id, uuid.UUID)
        assert r.status == RecipientStatus.PENDING
        assert r.certificate_code is None
        assert r.pdf_path is None
        assert r.error_message is None
        assert r.job.id == job.id


def test_foreign_key_enforcement(db_session: Session) -> None:
    """Verify SQLite foreign key enforcement rejects orphan recipients."""
    orphan_recipient = RecipientCertificate(
        job_id=uuid.uuid4(),  # Non-existent job ID
        recipient_name="Ghost User",
        recipient_email="ghost@example.com",
    )
    db_session.add(orphan_recipient)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_unique_certificate_code_constraint(db_session: Session) -> None:
    """Verify that duplicate certificate codes violate unique constraint."""
    job = GenerationJob(event_name="Web Dev Summit")
    db_session.add(job)
    db_session.commit()

    cert1 = RecipientCertificate(
        job_id=job.id,
        recipient_name="User One",
        recipient_email="user1@example.com",
        certificate_code="CERT-2026-0001",
    )
    cert2 = RecipientCertificate(
        job_id=job.id,
        recipient_name="User Two",
        recipient_email="user2@example.com",
        certificate_code="CERT-2026-0001",  # Duplicate code
    )
    db_session.add(cert1)
    db_session.commit()

    db_session.add(cert2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_multiple_recipients_can_have_none_certificate_code(db_session: Session) -> None:
    """Verify multiple recipients can have None as certificate_code without uniqueness collision."""
    job = GenerationJob(event_name="Design Workshop", total_count=3)
    db_session.add(job)
    db_session.commit()

    recipients = [
        RecipientCertificate(
            job_id=job.id,
            recipient_name=f"Participant {i}",
            recipient_email=f"participant{i}@example.com",
            certificate_code=None,
        )
        for i in range(3)
    ]
    db_session.add_all(recipients)
    db_session.commit()

    db_session.refresh(job)
    assert len(job.recipients) == 3
    for r in job.recipients:
        assert r.certificate_code is None


def test_cascade_deletion(db_session: Session) -> None:
    """Verify that deleting a job cascades deletion to all its recipients."""
    job = GenerationJob(event_name="Hackathon 2026", total_count=1)
    db_session.add(job)
    db_session.commit()

    recipient = RecipientCertificate(
        job_id=job.id,
        recipient_name="Charlie Brown",
        recipient_email="charlie@example.com",
    )
    db_session.add(recipient)
    db_session.commit()
    recipient_id = recipient.id

    # Delete the parent job
    db_session.delete(job)
    db_session.commit()

    # Verify recipient is removed
    deleted_recipient = db_session.get(RecipientCertificate, recipient_id)
    assert deleted_recipient is None


def test_update_and_read_across_sessions(session_factory) -> None:
    """Verify state transitions and count updates persist across distinct sessions."""
    # Session 1: Create initial job
    with session_factory() as session1:
        job = GenerationJob(event_name="Cloud Summit", total_count=1)
        session1.add(job)
        session1.commit()
        job_id = job.id

    # Session 2: Worker picks up job and updates state
    with session_factory() as session2:
        retrieved_job = session2.get(GenerationJob, job_id)
        assert retrieved_job is not None
        retrieved_job.status = JobStatus.PROCESSING
        retrieved_job.started_at = datetime.now(timezone.utc)
        retrieved_job.processed_count = 1
        retrieved_job.successful_count = 1
        retrieved_job.status = JobStatus.COMPLETED
        retrieved_job.completed_at = datetime.now(timezone.utc)
        session2.commit()

    # Session 3: Read back updated record
    with session_factory() as session3:
        final_job = session3.get(GenerationJob, job_id)
        assert final_job is not None
        assert final_job.status == JobStatus.COMPLETED
        assert final_job.processed_count == 1
        assert final_job.successful_count == 1
        assert final_job.failed_count == 0
        assert final_job.started_at is not None
        assert final_job.completed_at is not None


def test_utc_timestamps_remain_timezone_aware(session_factory) -> None:
    """Regression test: verify timestamps retain tzinfo=timezone.utc when reloaded across sessions."""
    now_utc = datetime.now(timezone.utc)
    job_id = uuid.uuid4()
    recipient_id = uuid.uuid4()

    with session_factory() as session1:
        job = GenerationJob(
            id=job_id,
            event_name="Timezone Verification Event",
            created_at=now_utc,
            started_at=now_utc,
        )
        session1.add(job)
        session1.commit()

        recipient = RecipientCertificate(
            id=recipient_id,
            job_id=job.id,
            recipient_name="Timezone User",
            recipient_email="tz@example.com",
            created_at=now_utc,
            processed_at=now_utc,
        )
        session1.add(recipient)
        session1.commit()

    with session_factory() as session2:
        reloaded_job = session2.get(GenerationJob, job_id)
        reloaded_recipient = session2.get(RecipientCertificate, recipient_id)

        assert reloaded_job is not None
        assert reloaded_job.created_at.tzinfo is not None
        assert reloaded_job.created_at.tzinfo == timezone.utc
        assert reloaded_job.started_at is not None
        assert reloaded_job.started_at.tzinfo == timezone.utc

        assert reloaded_recipient is not None
        assert reloaded_recipient.created_at.tzinfo is not None
        assert reloaded_recipient.created_at.tzinfo == timezone.utc
        assert reloaded_recipient.processed_at is not None
        assert reloaded_recipient.processed_at.tzinfo == timezone.utc


@pytest.mark.parametrize(
    "invalid_counts",
    [
        # Negative total_count
        {"total_count": -1, "processed_count": 0, "successful_count": 0, "failed_count": 0},
        # Negative processed_count
        {"total_count": 10, "processed_count": -1, "successful_count": 0, "failed_count": 0},
        # Negative successful_count
        {"total_count": 10, "processed_count": 0, "successful_count": -1, "failed_count": 0},
        # Negative failed_count
        {"total_count": 10, "processed_count": 0, "successful_count": 0, "failed_count": -1},
        # processed_count != successful_count + failed_count
        {"total_count": 10, "processed_count": 5, "successful_count": 3, "failed_count": 1},
        # processed_count > total_count
        {"total_count": 5, "processed_count": 6, "successful_count": 6, "failed_count": 0},
    ],
)
def test_job_counter_invariants_rejected(db_session: Session, invalid_counts: dict) -> None:
    """Verify that CheckConstraints reject invalid counter combinations with IntegrityError."""
    job = GenerationJob(
        event_name="Invalid Counter Job",
        **invalid_counts,
    )
    db_session.add(job)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_required_database_fields_reject_null(db_session: Session) -> None:
    """Verify that nullable=False constraints reject missing required values."""
    # 1. GenerationJob missing event_name
    job_without_event = GenerationJob(event_name=None)  # type: ignore[arg-type]
    db_session.add(job_without_event)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Create a valid job for recipient tests
    valid_job = GenerationJob(event_name="Valid Event")
    db_session.add(valid_job)
    db_session.commit()

    # 2. RecipientCertificate missing recipient_name
    recipient_no_name = RecipientCertificate(
        job_id=valid_job.id,
        recipient_name=None,  # type: ignore[arg-type]
        recipient_email="test@example.com",
    )
    db_session.add(recipient_no_name)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # 3. RecipientCertificate missing recipient_email
    recipient_no_email = RecipientCertificate(
        job_id=valid_job.id,
        recipient_name="Test Name",
        recipient_email=None,  # type: ignore[arg-type]
    )
    db_session.add(recipient_no_email)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
