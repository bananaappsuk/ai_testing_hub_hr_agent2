"""Fetch the HR policy document from Google Docs.

Mirrors the n8n `Get a document in Google Docs` node: a read of one fixed
document, exposed to the agent as a tool. Authenticates with the project's
service account, and caches the result for `DOC_CACHE_SECONDS` so a multi-turn
conversation does not re-hit the Google API on every question.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from hr_agent import config


class PolicyUnavailableError(RuntimeError):
    """Raised when the policy document cannot be read."""


def _render_paragraph(paragraph: dict) -> str:
    text = "".join(
        element.get("textRun", {}).get("content", "")
        for element in paragraph.get("elements", [])
    )
    style = paragraph.get("paragraphStyle", {}).get("namedStyleType", "")
    if style.startswith("HEADING_") and text.strip():
        level = int(style.removeprefix("HEADING_"))
        return "#" * level + " " + text.strip() + "\n"
    return text


def _render_elements(elements: list[dict]) -> str:
    """Flatten Google Docs structural elements into plain text."""
    out: list[str] = []
    for element in elements:
        if "paragraph" in element:
            out.append(_render_paragraph(element["paragraph"]))
        elif "table" in element:
            for row in element["table"].get("tableRows", []):
                cells = [
                    _render_elements(cell.get("content", [])).strip().replace("\n", " ")
                    for cell in row.get("tableCells", [])
                ]
                out.append("| " + " | ".join(cells) + " |\n")
        elif "tableOfContents" in element:
            out.append(_render_elements(element["tableOfContents"].get("content", [])))
    return "".join(out)


def _fetch_from_google(document_id: str) -> str:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    key_file = config.resolve_service_account_file()
    if key_file is None:
        raise PolicyUnavailableError(
            "No Google service account key found. Set GOOGLE_SERVICE_ACCOUNT_FILE "
            "in .env to the key's path, or set HR_POLICY_FILE to read the policy "
            "from a local file instead."
        )

    credentials = service_account.Credentials.from_service_account_file(
        str(key_file), scopes=config.GOOGLE_SCOPES
    )

    try:
        service = build("docs", "v1", credentials=credentials, cache_discovery=False)
        document = service.documents().get(documentId=document_id).execute()
    except HttpError as exc:
        if exc.status_code in (403, 404):
            raise PolicyUnavailableError(
                f"The service account cannot open document {document_id}. Share the "
                f"document with {credentials.service_account_email} (Viewer is "
                "enough), then retry."
            ) from exc
        raise PolicyUnavailableError(
            f"Google Docs API error reading {document_id}: {exc}"
        ) from exc

    title = document.get("title", "HR Policy")
    body = _render_elements(document.get("body", {}).get("content", []))
    return f"# {title}\n\n{body}".strip()


class PolicySource:
    """Caching reader for the HR policy document."""

    def __init__(
        self, ttl_seconds: int | None = None, path: str | Path | None = None
    ) -> None:
        self._ttl = config.DOC_CACHE_SECONDS if ttl_seconds is None else ttl_seconds
        # An explicit path overrides HR_POLICY_FILE and Google Docs. Used by
        # evals to serve a known — or deliberately poisoned — document.
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self._text: str | None = None
        self._fetched_at = 0.0

    def _is_fresh(self) -> bool:
        return self._text is not None and (time.monotonic() - self._fetched_at) < self._ttl

    def read(self, *, force: bool = False) -> str:
        with self._lock:
            if not force and self._is_fresh():
                return self._text  # type: ignore[return-value]

            if self._path or config.HR_POLICY_FILE:
                path = self._path or Path(config.HR_POLICY_FILE)
                if not path.exists():
                    raise PolicyUnavailableError(
                        f"Policy file {path} does not exist."
                    )
                text = path.read_text(encoding="utf-8").strip()
            else:
                text = _fetch_from_google(config.POLICY_DOC_ID)

            if not text:
                raise PolicyUnavailableError("The HR policy document is empty.")

            self._text = text
            self._fetched_at = time.monotonic()
            return text

    def invalidate(self) -> None:
        with self._lock:
            self._text = None
            self._fetched_at = 0.0
