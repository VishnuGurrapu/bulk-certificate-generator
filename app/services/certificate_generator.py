import secrets
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Union

from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from app.core.config import get_settings


class CertificateGenerationError(Exception):
    """Raised when certificate PDF generation fails."""

    pass


class CertificateFileExistsError(CertificateGenerationError, FileExistsError):
    """Raised when the target certificate output file already exists."""

    pass


@dataclass(frozen=True)
class CertificateGenerationResult:
    """Result data structure returned after successfully rendering a certificate PDF."""

    certificate_code: str
    file_path: Path
    filename: str


class CertificateGeneratorService:
    """Service for rendering personalized vector certificate PDFs using ReportLab."""

    PAGE_WIDTH, PAGE_HEIGHT = landscape(letter)  # 792 x 612 pt (11 x 8.5 in)

    # Curated color scheme
    PRIMARY_COLOR = colors.HexColor("#1A365D")  # Deep Navy
    SECONDARY_COLOR = colors.HexColor("#C5A059")  # Warm Gold
    TEXT_DARK = colors.HexColor("#2D3748")  # Charcoal
    TEXT_MUTED = colors.HexColor("#718096")  # Slate Grey

    def __init__(self, storage_dir: Optional[Union[str, Path]] = None) -> None:
        """Initialize certificate generator service with an injected or default storage directory."""
        if storage_dir is not None:
            self.storage_dir = Path(storage_dir).resolve()
        else:
            settings = get_settings()
            self.storage_dir = Path(settings.storage_dir).resolve()

    def generate_certificate_code(self) -> str:
        """Generate a cryptographically random, unique server-side certificate code."""
        part1 = secrets.token_hex(3).upper()
        part2 = secrets.token_hex(3).upper()
        return f"CERT-{part1}-{part2}"

    def _resolve_safe_path(self, filename: str) -> Path:
        """Resolve target file path and guarantee it does not escape the configured storage directory."""
        if not filename or "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"Unsafe filename detected: {filename}")

        target_path = (self.storage_dir / filename).resolve()
        try:
            # Python 3.9+ is_relative_to guarantees target is strictly within storage_dir
            if not target_path.is_relative_to(self.storage_dir):
                raise ValueError(f"Path traversal detected: {filename}")
        except ValueError:
            raise ValueError(f"Path traversal detected: {filename}")

        return target_path

    def generate_certificate(
        self,
        recipient_name: str,
        event_name: str,
        issue_date: Optional[Union[date, datetime, str]] = None,
        custom_code: Optional[str] = None,
        output_filename: Optional[str] = None,
    ) -> CertificateGenerationResult:
        """Generate a personalized certificate PDF and save it safely to disk.

        Args:
            recipient_name: Full name of the recipient.
            event_name: Title of the event or course.
            issue_date: Date to display on the certificate (defaults to today).
            custom_code: Optional pre-allocated server certificate code (e.g. from DB).
            output_filename: Optional safe filename (defaults to UUID-based filename).

        Returns:
            CertificateGenerationResult containing the certificate code, resolved file path, and filename.

        Raises:
            ValueError: If input arguments are invalid or path traversal is attempted.
            CertificateGenerationError: If rendering or disk writing fails.
        """
        if not recipient_name or not recipient_name.strip():
            raise ValueError("Recipient name cannot be empty")
        if not event_name or not event_name.strip():
            raise ValueError("Event name cannot be empty")

        clean_recipient_name = recipient_name.strip()
        clean_event_name = event_name.strip()

        # Format issue date
        if issue_date is None:
            formatted_date = date.today().strftime("%B %d, %Y")
        elif isinstance(issue_date, (date, datetime)):
            formatted_date = issue_date.strftime("%B %d, %Y")
        else:
            formatted_date = str(issue_date).strip()

        certificate_code = (custom_code or self.generate_certificate_code()).strip()
        filename = output_filename or f"{uuid.uuid4()}.pdf"

        # Resolve output path securely
        target_path = self._resolve_safe_path(filename)

        # Refuse to overwrite an existing certificate file to preserve data integrity
        if target_path.exists():
            raise CertificateFileExistsError(
                f"Target certificate file already exists: {target_path.name}"
            )

        # Ensure target directory exists
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise CertificateGenerationError(
                f"Failed to create storage directory '{target_path.parent}': {e}"
            ) from e

        # Write to temporary file first for atomic creation
        temp_path = target_path.with_suffix(".pdf.tmp")
        target_created = False

        try:
            self._render_pdf(
                output_path=temp_path,
                recipient_name=clean_recipient_name,
                event_name=clean_event_name,
                issue_date_str=formatted_date,
                certificate_code=certificate_code,
            )

            # Atomic rename to final destination
            temp_path.replace(target_path)
            target_created = True

            return CertificateGenerationResult(
                certificate_code=certificate_code,
                file_path=target_path,
                filename=target_path.name,
            )
        except Exception as e:
            # Clean up temporary file created by this operation
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            # Only clean up target_path if it was actually created during this operation
            if target_created and target_path.exists():
                try:
                    target_path.unlink()
                except OSError:
                    pass

            if isinstance(e, (ValueError, TypeError, CertificateGenerationError)):
                raise
            raise CertificateGenerationError(
                f"Failed to generate certificate PDF: {e}"
            ) from e

    def _render_pdf(
        self,
        output_path: Path,
        recipient_name: str,
        event_name: str,
        issue_date_str: str,
        certificate_code: str,
    ) -> None:
        """Draw the certificate layout onto the canvas."""
        c = canvas.Canvas(str(output_path), pagesize=(self.PAGE_WIDTH, self.PAGE_HEIGHT))
        c.setTitle(f"Certificate of Achievement - {recipient_name}")
        c.setAuthor("Bulk Certificate Generator")

        center_x = self.PAGE_WIDTH / 2.0

        # 1. Background & Borders
        # Outer thick border
        c.setStrokeColor(self.PRIMARY_COLOR)
        c.setLineWidth(4)
        c.rect(30, 30, self.PAGE_WIDTH - 60, self.PAGE_HEIGHT - 60)

        # Inner thin border
        c.setStrokeColor(self.SECONDARY_COLOR)
        c.setLineWidth(1.5)
        c.rect(38, 38, self.PAGE_WIDTH - 76, self.PAGE_HEIGHT - 76)

        # Corner corner accents
        c.setFillColor(self.SECONDARY_COLOR)
        corner_size = 8
        c.rect(42, 42, corner_size, corner_size, fill=1, stroke=0)
        c.rect(self.PAGE_WIDTH - 42 - corner_size, 42, corner_size, corner_size, fill=1, stroke=0)
        c.rect(42, self.PAGE_HEIGHT - 42 - corner_size, corner_size, corner_size, fill=1, stroke=0)
        c.rect(
            self.PAGE_WIDTH - 42 - corner_size,
            self.PAGE_HEIGHT - 42 - corner_size,
            corner_size,
            corner_size,
            fill=1,
            stroke=0,
        )

        # 2. Header
        c.setFillColor(self.PRIMARY_COLOR)
        c.setFont("Helvetica-Bold", 26)
        c.drawCentredString(center_x, 495, "CERTIFICATE OF ACHIEVEMENT")

        # Decorative separator bar
        c.setStrokeColor(self.SECONDARY_COLOR)
        c.setLineWidth(2)
        c.line(center_x - 180, 475, center_x + 180, 475)

        # Subtitle
        c.setFillColor(self.TEXT_MUTED)
        c.setFont("Helvetica", 11)
        c.drawCentredString(center_x, 440, "THIS PROUDLY CERTIFIES THAT")

        # 3. Recipient Name (with dynamic font scaling and safety truncation)
        name_font_size, display_name = self._fit_text_to_width(
            text=recipient_name,
            font_name="Helvetica-Bold",
            initial_size=32,
            min_size=16,
            max_width=620,
        )
        c.setFillColor(self.PRIMARY_COLOR)
        c.setFont("Helvetica-Bold", name_font_size)
        c.drawCentredString(center_x, 380, display_name)

        # Underline accent for recipient name
        name_width = min(stringWidth(display_name, "Helvetica-Bold", name_font_size) + 40, 640)
        c.setStrokeColor(self.SECONDARY_COLOR)
        c.setLineWidth(1)
        c.line(center_x - (name_width / 2.0), 365, center_x + (name_width / 2.0), 365)

        # 4. Body Description
        c.setFillColor(self.TEXT_DARK)
        c.setFont("Helvetica", 12)
        c.drawCentredString(center_x, 325, "has successfully participated in and completed")

        # 5. Event Name (with dynamic scaling if long)
        event_font_size, display_event = self._fit_text_to_width(
            text=event_name,
            font_name="Helvetica-Bold",
            initial_size=20,
            min_size=13,
            max_width=620,
        )
        c.setFillColor(self.PRIMARY_COLOR)
        c.setFont("Helvetica-Bold", event_font_size)
        c.drawCentredString(center_x, 285, display_event)

        # 6. Bottom Signatures & Metadata
        # Left: Issue Date
        c.setStrokeColor(self.TEXT_MUTED)
        c.setLineWidth(0.8)
        c.line(90, 160, 270, 160)
        c.setFillColor(self.TEXT_DARK)
        c.setFont("Helvetica", 10)
        c.drawCentredString(180, 142, f"Date: {issue_date_str}")
        c.setFillColor(self.TEXT_MUTED)
        c.setFont("Helvetica-Oblique", 8)
        c.drawCentredString(180, 128, "Date of Issuance")

        # Right: Certificate Code
        c.setStrokeColor(self.TEXT_MUTED)
        c.setLineWidth(0.8)
        c.line(522, 160, 702, 160)
        c.setFillColor(self.TEXT_DARK)
        c.setFont("Helvetica-Bold", 10)
        c.drawCentredString(612, 142, f"ID: {certificate_code}")
        c.setFillColor(self.TEXT_MUTED)
        c.setFont("Helvetica-Oblique", 8)
        c.drawCentredString(612, 128, "Certificate Identifier")

        # Bottom subtle authenticity notice
        c.setFillColor(self.TEXT_MUTED)
        c.setFont("Helvetica", 8)
        c.drawCentredString(
            center_x, 60, "Verify this certificate's authenticity using the unique certificate ID"
        )

        c.showPage()
        c.save()

    @staticmethod
    def _fit_text_to_width(
        text: str,
        font_name: str,
        initial_size: int,
        min_size: int,
        max_width: float,
    ) -> tuple[int, str]:
        """Scale down font size or truncate text if necessary to fit within max_width points."""
        # First attempt: scale font size down
        for size in range(initial_size, min_size - 1, -1):
            if stringWidth(text, font_name, size) <= max_width:
                return size, text

        # If text still exceeds max_width at min_size, safely truncate with ellipsis
        truncated = text
        while len(truncated) > 4 and stringWidth(truncated + "...", font_name, min_size) > max_width:
            truncated = truncated[:-1]

        return min_size, truncated + "..."
