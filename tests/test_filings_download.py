"""F-29: `_download_pdf` must clean up its `.part` file on every abort
path, including the mid-stream oversize cap. Phase 4a's contract is
"never keeps a partial" — the earlier revision skipped cleanup on
oversize aborts and left disk litter behind."""

from __future__ import annotations

from pathlib import Path

import httpx


def _iter_bytes_stream(chunks):
    """Wrap raw chunks in an iterator suitable for httpx.stream's
    `iter_bytes` interface via a MockTransport."""
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"".join(chunks))
    return httpx.MockTransport(_handler)


def test_oversize_stream_cleans_up_part_file(monkeypatch, tmp_path):
    """Reproduces the aborted-oversize path: stream more bytes than the
    per-PDF cap, then assert the `.part` file is gone. Without the
    cleanup, a re-run leaves a growing pile of half-downloaded PDFs
    under `data/filings/`."""
    monkeypatch.setenv("EXTRACT_MAX_PDF_MB", "1")  # 1 MB cap
    monkeypatch.setenv("DB_PATH", str(tmp_path / "brvm.sqlite"))
    from brvm.config import reset_settings_cache
    reset_settings_cache()
    from brvm.services import filings as svc

    dest = tmp_path / "SNTS" / "big.pdf"
    # 1.5 MB payload split into 128-KB chunks so the size check trips
    # partway through the stream, not on the very first chunk.
    payload = [b"x" * (128 * 1024)] * 12
    transport = _iter_bytes_stream(payload)
    with httpx.Client(transport=transport) as client:
        result = svc._download_pdf(client, "https://example/big.pdf", dest)
    assert result is None
    assert not dest.exists()
    part = dest.with_suffix(dest.suffix + ".part")
    assert not part.exists(), f"stale .part left behind: {part}"


def test_http_error_cleans_up_part_file(monkeypatch, tmp_path):
    """The HTTPError path already cleaned up. Pin the behaviour so a
    future refactor doesn't regress the existing case while fixing
    the oversize one."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "brvm.sqlite"))
    from brvm.config import reset_settings_cache
    reset_settings_cache()
    from brvm.services import filings as svc

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)
    transport = httpx.MockTransport(_handler)
    dest = tmp_path / "SNTS" / "broken.pdf"
    with httpx.Client(transport=transport) as client:
        result = svc._download_pdf(client, "https://example/broken.pdf", dest)
    assert result is None
    part = dest.with_suffix(dest.suffix + ".part")
    assert not part.exists()


def test_happy_path_writes_final_destination(monkeypatch, tmp_path):
    """Sanity check that the cleanup logic doesn't wipe successful
    downloads by mistake."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "brvm.sqlite"))
    from brvm.config import reset_settings_cache
    reset_settings_cache()
    from brvm.services import filings as svc

    payload = b"%PDF-1.7\n" + b"x" * 100
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)
    transport = httpx.MockTransport(_handler)
    dest = tmp_path / "SNTS" / "ok.pdf"
    with httpx.Client(transport=transport) as client:
        result = svc._download_pdf(client, "https://example/ok.pdf", dest)
    assert result is not None
    size, _sha = result
    assert size == len(payload)
    assert dest.exists()
    assert not Path(str(dest) + ".part").exists()
