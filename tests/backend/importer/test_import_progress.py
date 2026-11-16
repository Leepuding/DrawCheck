import threading
import zipfile

import pytest

from backend.importer.service import ImportService
from backend.parsers.base import ParseResult, ParsedUnit


class Clock:
    now = 0.0
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += seconds


@pytest.mark.parametrize("fail", [False, True])
def test_expanded_count_observed_eta_and_terminal_freeze(tmp_path, fail):
    archive = tmp_path / "set.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for n in range(3):
            output.writestr(f"nested/sheet-{n}.pdf", f"%PDF-1.4\n{n}")
    clock = Clock()
    entered, release = threading.Event(), threading.Event()
    class Parser:
        calls = 0
        def parse(self, request):
            self.calls += 1
            if self.calls == 3:
                entered.set()
                assert release.wait(5)
                if fail:
                    raise RuntimeError("private path or provider error must not be exposed")
            clock.advance(2)
            return ParseResult([ParsedUnit("pdf_page", 1, "页", "S-001", [])], [])
    with ImportService(tmp_path / "data", pdf_parser=Parser(), clock=clock) as service:
        job_id = service.submit([archive])
        try:
            assert entered.wait(5)
            running = service.get_job(job_id)
            assert running["phase_key"] == "importing"
            assert running["processed_count"] == 2 and running["total_count"] == 3
            assert running["elapsed_seconds"] == 4 and running["eta_seconds"] == 2
            assert running["eta_scope"] == "phase"
            assert running["current_file"] == "sheet-2.pdf"
            assert "/" not in running["current_file"]
            assert "实际平均耗时" in running["estimate_basis"]
        finally:
            release.set()
        finished = service.wait(job_id, 5)
        clock.advance(100)
        later = service.get_job(job_id)
        assert finished["status"] == ("failed" if fail else "completed")
        assert later["elapsed_seconds"] == finished["elapsed_seconds"] == (4 if fail else 6)
        assert later["eta_seconds"] is None
        assert (later["percent"] < 100) if fail else later["percent"] == 100


def test_queued_time_is_excluded_from_import_elapsed(tmp_path):
    clock = Clock()
    entered, release = threading.Event(), threading.Event()
    class Parser:
        calls = 0
        def parse(self, request):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                assert release.wait(5)
            clock.advance(2)
            return ParseResult([], [])
    first, second = tmp_path / "first.pdf", tmp_path / "second.pdf"
    first.write_bytes(b"%PDF-1.4 first")
    second.write_bytes(b"%PDF-1.4 second")
    with ImportService(tmp_path / "data", pdf_parser=Parser(), clock=clock) as service:
        a = service.submit([first])
        assert entered.wait(5)
        b = service.submit([second])
        try:
            clock.advance(50)
            waiting = service.get_job(b)
            assert waiting["status"] == "queued" and waiting["elapsed_seconds"] == 0
            assert waiting["eta_seconds"] is None and waiting["percent"] == 0
        finally:
            release.set()
        service.wait(a, 5)
        assert service.wait(b, 5)["elapsed_seconds"] == 2
