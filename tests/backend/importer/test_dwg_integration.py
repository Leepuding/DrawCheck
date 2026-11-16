"""Importer-to-DC07 contract: usable CAD results and explicit persisted failures."""
import json

from backend.importer import ImportService
from backend.parsers.base import ParsedText, ParsedUnit, ParseResult
from backend.revisions import RevisionService
from backend.store import BBox, Repository, Source


class CadParser:
    def __init__(self):
        self.requests = []

    def parse(self, request):
        self.requests.append(request)
        assert request.format == "dwg"
        assert not request.readonly_path.stat().st_mode & 0o222
        request.cache_dir.mkdir(parents=True, exist_ok=True)
        (request.cache_dir / "cad.png").write_bytes(b"rendered-cad")
        return ParseResult([ParsedUnit("dwg_frame", 1, "CAD图框", "T0102", [ParsedText("尺寸200", Source.CAD_TEXT, BBox(.1, .2, .3, .4))], "cad.png")], [])


def test_dwg_import_runs_parser_preserves_text_and_manifest(tmp_path):
    source = tmp_path / "图纸.dwg"
    source.write_bytes(b"AC1027cad-fixture")
    missing = tmp_path / "不支持.txt"
    missing.write_text("unsupported")
    root, parser = tmp_path / "data", CadParser()
    with ImportService(root, dwg_parser=parser) as importer:
        job = importer.wait(importer.submit([source, missing]), 10)
        assert job["status"] == "completed"
        result = job["result"]
        assert result["file_count"] == result["unit_count"] == 1
        assert result["files"][0]["status"] == "imported"
        assert len(parser.requests) == 1
        # Duplicate append must not erase earlier unreadable diagnostics.
        importer.wait(importer.submit([source], version_id=result["version_id"]), 10)
    with Repository(root) as store:
        facts = store.list_records("facts", version_id=result["version_id"])
        assert len(facts) == 1 and facts[0]["source"] == "CAD文字"
        assert facts[0]["x0"] == .1 and facts[0]["text"] == "尺寸200"
        opened = RevisionService(store).open_project(result["project_id"])
        assert opened["unreadable"][0]["name"] == "不支持.txt"
        assert opened["drawings"][0]["drawing_number"] == "T0102"


def test_draft_import_can_fail_without_freezing_base(tmp_path):
    root = tmp_path / "data"
    with Repository(root) as store:
        project = store.create_project("工程")
        old = store.create_version(project["id"], "A")
        draft = RevisionService(store).create_revision(old["id"])
    unreadable = tmp_path / "坏文件.pdf"
    unreadable.write_bytes(b"%PDF-1.7bad")
    with ImportService(root) as importer:
        result = importer.wait(importer.submit([unreadable], version_id=draft["version_id"]), 10)
    assert result["status"] == "completed" and result["result"]["unreadable"]
    with Repository(root) as store:
        assert store.create_task(old["id"])["version_id"] == old["id"]
        manifest = root / f"projects/{project['id']}/cache/import-{draft['version_id']}.json"
        assert json.loads(manifest.read_text())["unreadable"]


def test_successful_retry_replaces_stale_file_diagnostics(tmp_path):
    class RetryParser(CadParser):
        failed = True
        def parse(self, request):
            if self.failed:
                return ParseResult([], ["原文件读不了"])
            return super().parse(request)
    source = tmp_path / "重试.dwg"
    source.write_bytes(b"AC1027bad")
    root, parser = tmp_path / "data", RetryParser()
    with ImportService(root, dwg_parser=parser) as importer:
        first = importer.wait(importer.submit([source]), 10)["result"]
        assert first["unreadable"]
        parser.failed = False
        source.write_bytes(b"AC1027fixed")
        second = importer.wait(importer.submit([source], version_id=first["version_id"]), 10)["result"]
        assert second["unreadable"] == []
    with Repository(root) as store:
        assert RevisionService(store).open_project(first["project_id"])["unreadable"] == []
