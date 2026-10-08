import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from filings.document_selection import (  # noqa: E402
    FilingIndexParseError,
    _detail_filename_from_href,
)

EXPECTED_DIRECTORY = "/Archives/edgar/data/320193/000032019324000081/"
DOCUMENT_PATH = f"{EXPECTED_DIRECTORY}aapl-20240629.htm"
FILENAME = "aapl-20240629.htm"


@pytest.mark.parametrize(
    "href",
    [
        FILENAME,
        DOCUMENT_PATH,
        f"https://www.sec.gov{DOCUMENT_PATH}",
        f"/ix?doc={DOCUMENT_PATH}",
        "/ix?doc=%2FArchives%2Fedgar%2Fdata%2F320193%2F000032019324000081%2Faapl-20240629.htm",
        f"/ixviewer/doc/action?doc={DOCUMENT_PATH}",
        f"https://www.sec.gov/ix?doc={DOCUMENT_PATH}",
        f"//www.sec.gov/ix?doc={DOCUMENT_PATH}",
        f"https://data.sec.gov/ix?doc={DOCUMENT_PATH}",
        f"https://www.sec.gov:443/ix?doc={DOCUMENT_PATH}",
    ],
)
def test_accepts_direct_and_known_sec_viewer_links(href):
    assert _detail_filename_from_href(href, EXPECTED_DIRECTORY) == FILENAME


@pytest.mark.parametrize(
    "href",
    [
        "",
        f"/ix?doc={DOCUMENT_PATH}&doc={DOCUMENT_PATH}",
        f"/ix?doc={DOCUMENT_PATH}&download=1",
        "/ix?download=1",
        f"/ix?doc={DOCUMENT_PATH}#section",
        f"/ixviewer/other?doc={DOCUMENT_PATH}",
        f"/Archives/edgar/data/320193/000032019324000081/{FILENAME}?doc=x",
        f"https://example.com/ix?doc={DOCUMENT_PATH}",
        f"http://www.sec.gov/ix?doc={DOCUMENT_PATH}",
        f"https://user:secret@www.sec.gov/ix?doc={DOCUMENT_PATH}",
        f"https://www.sec.gov:444/ix?doc={DOCUMENT_PATH}",
        "/ix?doc=https%3A%2F%2Fevil.example%2Ffile.htm",
        "/ix?doc=/Archives/edgar/data/320194/000032019324000081/aapl-20240629.htm",
        "/ix?doc=/Archives/edgar/data/320193/000032019324000082/aapl-20240629.htm",
        "/ix?doc=/Archives/edgar/data/320193/000032019324000081/../aapl-20240629.htm",
        "/ix?doc=%252FArchives%252Fedgar%252Fdata%252F320193%252F000032019324000081%252Faapl-20240629.htm",
        "/ix?doc=/Archives/edgar/data/320193/000032019324000081/%252e%252e/aapl-20240629.htm",
    ],
)
def test_rejects_unsafe_or_noncanonical_viewer_links(href):
    with pytest.raises(FilingIndexParseError):
        _detail_filename_from_href(href, EXPECTED_DIRECTORY)
