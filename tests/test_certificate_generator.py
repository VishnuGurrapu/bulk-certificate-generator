from datetime import date
from pathlib import Path
from unittest.mock import patch

import pypdf
import pytest

from app.services.certificate_generator import (
    CertificateGenerationError,
    CertificateGenerationResult,
    CertificateGeneratorService,
)


@pytest.fixture
def temp_storage(tmp_path: Path) -> Path:
    """Provide an isolated temporary storage directory for tests."""
    storage = tmp_path / "test_certificates"
    storage.mkdir(parents=True, exist_ok=True)
    return storage


@pytest.fixture
def generator(temp_storage: Path) -> CertificateGeneratorService:
    """Provide a CertificateGeneratorService instance bound to temp storage."""
    return CertificateGeneratorService(storage_dir=temp_storage)


def test_generate_certificate_creates_valid_pdf(
    generator: CertificateGeneratorService, temp_storage: Path
) -> None:
    """Verify that generate_certificate produces a valid, readable PDF file."""
    result = generator.generate_certificate(
        recipient_name="Jane Doe",
        event_name="Cloud Engineering Summit 2026",
        issue_date="October 11, 2026",
    )

    assert isinstance(result, CertificateGenerationResult)
    assert result.file_path.exists()
    assert result.file_path.is_file()
    assert result.file_path.parent == temp_storage

    # Verify PDF magic bytes
    with open(result.file_path, "rb") as f:
        header = f.read(5)
    assert header == b"%PDF-"

    # Verify readable with pypdf
    reader = pypdf.PdfReader(str(result.file_path))
    assert len(reader.pages) == 1


def test_pdf_contains_expected_metadata_and_text(
    generator: CertificateGeneratorService,
) -> None:
    """Verify that extracted text contains recipient name, event name, date, and code."""
    recipient = "Dr. Marcus Vance"
    event = "Deep Learning Systems Symposium"
    issue_date = "November 15, 2026"

    result = generator.generate_certificate(
        recipient_name=recipient,
        event_name=event,
        issue_date=issue_date,
    )

    reader = pypdf.PdfReader(str(result.file_path))
    extracted_text = reader.pages[0].extract_text()

    assert recipient in extracted_text
    assert event in extracted_text
    assert issue_date in extracted_text
    assert result.certificate_code in extracted_text
    assert "CERTIFICATE OF ACHIEVEMENT" in extracted_text


def test_unique_codes_and_filenames_per_recipient(
    generator: CertificateGeneratorService,
) -> None:
    """Verify different recipients receive distinct certificate codes and output filenames."""
    result_1 = generator.generate_certificate(
        recipient_name="Alice Smith",
        event_name="Web Dev Workshop",
    )
    result_2 = generator.generate_certificate(
        recipient_name="Bob Jones",
        event_name="Web Dev Workshop",
    )

    assert result_1.certificate_code != result_2.certificate_code
    assert result_1.filename != result_2.filename
    assert result_1.file_path != result_2.file_path
    assert result_1.file_path.exists()
    assert result_2.file_path.exists()


def test_long_recipient_and_event_names_handled_gracefully(
    generator: CertificateGeneratorService,
) -> None:
    """Verify that extremely long recipient and event names scale without error."""
    very_long_name = (
        "Professor Maximillian Alexander Montgomery-Fitzgerald III of Greater Upper Bavaria"
    )
    very_long_event = (
        "International Advanced Collaborative Research Symposium on Distributed Autonomous Agent Systems and Scalable Architectures 2026"
    )

    result = generator.generate_certificate(
        recipient_name=very_long_name,
        event_name=very_long_event,
    )

    assert result.file_path.exists()
    reader = pypdf.PdfReader(str(result.file_path))
    extracted_text = reader.pages[0].extract_text()
    assert "Maximillian" in extracted_text
    assert "Symposium" in extracted_text


def test_files_created_strictly_in_injected_directory(
    temp_storage: Path,
) -> None:
    """Verify files are generated strictly inside the provided storage directory."""
    service = CertificateGeneratorService(storage_dir=temp_storage)
    result = service.generate_certificate(
        recipient_name="John Sample",
        event_name="Test Event",
    )

    assert result.file_path.parent.resolve() == temp_storage.resolve()


@pytest.mark.parametrize(
    "unsafe_filename",
    [
        "../escaped.pdf",
        "..\\escaped.pdf",
        "sub/../../escaped.pdf",
        "/etc/passwd.pdf",
        "nested/path.pdf",
    ],
)
def test_path_traversal_filenames_rejected(
    generator: CertificateGeneratorService, unsafe_filename: str
) -> None:
    """Verify that path traversal attempts raise ValueError and do not create files."""
    with pytest.raises(ValueError):
        generator.generate_certificate(
            recipient_name="Hacker",
            event_name="Penetration Testing Event",
            output_filename=unsafe_filename,
        )


def test_empty_or_whitespace_arguments_raise_value_error(
    generator: CertificateGeneratorService,
) -> None:
    """Verify that blank recipient or event names raise ValueError."""
    with pytest.raises(ValueError, match="Recipient name cannot be empty"):
        generator.generate_certificate(recipient_name="", event_name="Event")

    with pytest.raises(ValueError, match="Recipient name cannot be empty"):
        generator.generate_certificate(recipient_name="   ", event_name="Event")

    with pytest.raises(ValueError, match="Event name cannot be empty"):
        generator.generate_certificate(recipient_name="Valid Name", event_name="")

    with pytest.raises(ValueError, match="Event name cannot be empty"):
        generator.generate_certificate(recipient_name="Valid Name", event_name="   ")


def test_failed_generation_cleans_up_and_raises_exception(
    generator: CertificateGeneratorService, temp_storage: Path
) -> None:
    """Verify that rendering errors raise CertificateGenerationError and leave no partial files."""
    with patch.object(
        generator, "_render_pdf", side_effect=RuntimeError("Simulated canvas crash")
    ):
        with pytest.raises(CertificateGenerationError, match="Simulated canvas crash"):
            generator.generate_certificate(
                recipient_name="Test User",
                event_name="Test Event",
            )

    # Ensure no lingering .pdf or .tmp files exist
    lingering_files = list(temp_storage.glob("*"))
    assert lingering_files == []


def test_custom_code_and_date_types_supported(
    generator: CertificateGeneratorService,
) -> None:
    """Verify that pre-allocated custom code and date object types are properly formatted."""
    custom_code = "CERT-2026-SPECIAL-001"
    issue_date = date(2026, 12, 25)

    result = generator.generate_certificate(
        recipient_name="Eve Holiday",
        event_name="Winter Tech Fest",
        issue_date=issue_date,
        custom_code=custom_code,
    )

    assert result.certificate_code == custom_code
    reader = pypdf.PdfReader(str(result.file_path))
    extracted = reader.pages[0].extract_text()
    assert custom_code in extracted
    assert "December 25, 2026" in extracted
