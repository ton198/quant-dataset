"""Selected-document fetch orchestration without archive persistence."""

from __future__ import annotations

from .document_selection import DocumentCandidate
from .sec_client import FetchResponse, SecClient, validate_sec_url


class UnselectedDocumentError(ValueError):
    """A caller attempted to fetch an attachment not selected by its plan."""


def fetch_document(candidate: DocumentCandidate, client: SecClient) -> FetchResponse:
    """Fetch one selected SEC document and return its bounded response in memory.

    This helper never creates directories or writes bytes. The archive writer is a
    separate layer and must decide how fetched response metadata becomes committed.
    """
    if not isinstance(candidate, DocumentCandidate):
        raise TypeError("candidate must be a DocumentCandidate from document_selection")
    if not candidate.selected or candidate.selection_status != "required":
        raise UnselectedDocumentError("document is not selected for acquisition")
    url = validate_sec_url(candidate.url)
    return client.get(url)
