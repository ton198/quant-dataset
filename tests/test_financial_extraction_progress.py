from __future__ import annotations

import hashlib
import io
import re
import sys
import time
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from filings.extraction_progress import ExtractionProgressReporter
from financial_extraction.domain import ExtractionLimits, ExtractionTask, VerifiedDocument
from financial_extraction.runtime import ModelDescriptor, ModelResponse
from financial_extraction.workflow import run_extraction


class TTYStream(io.StringIO):
    def isatty(self):
        return True


def _document() -> VerifiedDocument:
    payload = b"<html><body><p>Revenue is 12.</p></body></html>"
    digest = hashlib.sha256(payload).hexdigest()
    return VerifiedDocument(
        "source",
        "doc",
        "synthetic_html",
        True,
        payload,
        "utf-8",
        digest,
        len(payload),
        digest,
        "progress-test-v1",
        limits={
            "maxpayloadbytes": 10_000,
            "maxnodes": 100,
            "maxcells": 100,
            "maxgridspan": 100,
        },
    )


def test_failed_window_is_counted_and_progress_stays_on_stderr(tmp_path: Path):
    class FailingClient:
        descriptor = ModelDescriptor("synthetic", "progress")

        def complete(self, request):
            raise RuntimeError("offline provider failure")

    stream = TTYStream()
    with ExtractionProgressReporter(stream=stream, interval=0.01) as reporter:
        result = run_extraction(
            [_document()],
            client=FailingClient(),
            task=ExtractionTask(()),
            limits=ExtractionLimits(),
            work_dir=tmp_path,
            workers=1,
            on_progress=reporter,
        )
        reporter.result_writing()
        reporter.finish(result.status)

    output = stream.getvalue()
    assert result.status == "partial_failure"
    assert "failed=1" in output
    assert "windows=1/1" in output
    assert "完成：partial_failure" in output
    assert "Revenue" not in output


def test_idle_heartbeat_reports_long_running_stage(tmp_path: Path):
    class SlowClient:
        descriptor = ModelDescriptor("synthetic", "progress-heartbeat")

        def complete(self, request):
            time.sleep(0.04)
            return ModelResponse('{"records": []}')

    stream = TTYStream()
    with ExtractionProgressReporter(stream=stream, interval=0.01) as reporter:
        result = run_extraction(
            [_document()],
            client=SlowClient(),
            task=ExtractionTask(()),
            limits=ExtractionLimits(),
            work_dir=tmp_path,
            workers=1,
            on_progress=reporter,
        )
        reporter.finish(result.status)

    lines = [line for line in re.split(r"[\r\n]", stream.getvalue()) if line]
    assert result.status == "complete"
    assert any("inflight=1" in line for line in lines)
    assert any("windows=1/1" in line for line in lines)
