"""DC-05 自制夹具验收测试（非金样本）。"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

import pytest
from pypdf import PdfReader

from backend.importer import ImportService
from backend.store.models import BBox
from tools.fixtures.generate import compare_dirs, generate, sha256_file
from tools.fixtures.schema import CATEGORIES, validate_answers

ROOT = Path(__file__).resolve().parents[2]
SYN = ROOT / "fixtures" / "synthetic"
SAMPLES = Path(os.environ.get("DRAWCHECK_SAMPLES", "/workspace/projects/REQ-DRAWCHECK/samples/extracted"))


@pytest.fixture(scope="module")
def fixtures_ready(tmp_path_factory):
    """生成到临时目录，避免改写仓库内已跟踪的 MANIFEST/answers 等。"""
    root = tmp_path_factory.mktemp("dc05-synthetic")
    generate(root)
    return root


def _load_answers(root: Path) -> dict:
    return json.loads((root / "answers.json").read_text(encoding="utf-8"))


def test_not_golden_disclaimer(fixtures_ready):
    answers = _load_answers(fixtures_ready)
    assert answers["is_golden_sample"] is False
    assert "金样本" in answers["disclaimer"] or "不是金样本" in answers["disclaimer"]
    manifest = json.loads((fixtures_ready / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["is_golden_sample"] is False
    assert "generated_at_local" not in manifest
    opt = manifest.get("optional_dwg") or {}
    assert "sha256" not in opt


def test_nine_categories_report_and_ok(fixtures_ready):
    answers = _load_answers(fixtures_ready)
    needed = {
        "目录张数", "图签编号前缀", "尺寸", "标高", "编号写法",
        "材料规格", "数量与材料表", "详图索引断链", "同名不同对象",
    }
    by_cat: dict[str, set[str]] = {c: set() for c in needed}
    for issue in answers["issues"]:
        cat = issue["category"]
        if cat == "目录与图签不符":
            # 裁定归类；计入目录张数覆盖
            by_cat["目录张数"].add(issue["expectation"])
        if cat in by_cat:
            by_cat[cat].add(issue["expectation"])
    for cat, exps in by_cat.items():
        assert "不应报" in exps, f"{cat} 缺少不应报"
        if cat == "同名不同对象":
            # 该类正向期望为「不下结论」，不要求「应报」
            assert "不下结论" in exps, f"{cat} 缺少不下结论"
        else:
            assert "应报" in exps, f"{cat} 缺少应报"


def test_rule_a_extras(fixtures_ready):
    answers = _load_answers(fixtures_ready)
    issues = {i["id"]: i for i in answers["issues"]}
    conflict = issues["ISSUE-DIM-CONFLICT"]
    assert "100×100×100" in conflict["side_a"]["text"]
    assert "100×200×100" in conflict["side_b"]["text"]
    BBox(**conflict["side_a"]["bbox"])
    BBox(**conflict["side_b"]["bbox"])
    assert issues["ISSUE-DIM-TOL-EQ"]["expectation"] == "不应报"
    assert issues["ISSUE-DIM-TOL-OVER"]["expectation"] == "应报"
    assert issues["ISSUE-ELV-TOL-EQ"]["expectation"] == "不应报"
    assert issues["ISSUE-ELV-TOL-OVER"]["expectation"] == "应报"
    assert issues["ISSUE-DIM-NO-UNIT"]["expectation"] == "未核验"
    for key in ("ISSUE-DIM-TOL-EQ", "ISSUE-DIM-TOL-OVER", "ISSUE-ELV-TOL-EQ", "ISSUE-ELV-TOL-OVER"):
        assert "config_key" in issues[key]["tolerance"]


def test_blur_pending(fixtures_ready):
    answers = _load_answers(fixtures_ready)
    blur = next(i for i in answers["issues"] if i["id"] == "ISSUE-BLUR")
    assert blur["expectation"] == "待确认"
    assert blur["side_a"]["unclear"] is True
    replay = json.loads((fixtures_ready / "replay/A/index.json").read_text(encoding="utf-8"))
    entry = next(e for e in replay["entries"] if e["file"].endswith("S-009-blur.pdf"))
    assert any(f.get("unclear") for f in entry["facts"])


def test_revision_b_suite(fixtures_ready):
    answers = _load_answers(fixtures_ready)
    summary = answers["revision_summary"]
    assert summary["仍存在"] >= 1 and summary["已解决"] >= 1 and summary["新出现"] >= 1
    unchanged = answers["unchanged_drawings"]
    assert unchanged
    a = fixtures_ready / "build" / unchanged[0]["file_a"]
    b = fixtures_ready / "build" / unchanged[0]["file_b"]
    assert sha256_file(a) == sha256_file(b)


def test_anomaly_count_and_codes(fixtures_ready):
    manifest = json.loads((fixtures_ready / "MANIFEST.json").read_text(encoding="utf-8"))
    anoms = manifest["anomalies"]
    assert len(anoms) >= 8
    codes = {a["expect_code"] for a in anoms if "expect_code" in a}
    for required in ("PDF_CORRUPT", "PDF_ENCRYPTED", "EMPTY_FILE", "UNSUPPORTED_FORMAT",
                     "UNSAFE_ARCHIVE_PATH", "NESTED_ARCHIVE", "ARCHIVE_ENCRYPTED"):
        assert required in codes, required
    fake = next(a for a in anoms if a["relative_path"].endswith("fake.dwg"))
    assert fake.get("expect_import_status") == "unreadable"
    assert fake.get("expect_dc07") == "UNREADABLE"
    assert fake.get("expect_code") == "DWG_INCOMPLETE"


def test_deterministic_twice(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    generate(a)
    generate(b)
    diffs = compare_dirs(a, b)
    assert diffs == [], diffs


def test_no_text_layer(fixtures_ready):
    for pdf in sorted((fixtures_ready / "build").rglob("*.pdf")):
        reader = PdfReader(str(pdf))
        chars = 0
        for page in reader.pages:
            chars += len((page.extract_text() or "").strip())
        assert chars == 0, f"{pdf} 提取到 {chars} 字符"


def test_answers_structure(fixtures_ready):
    answers = _load_answers(fixtures_ready)
    validate_answers(answers)
    for issue in answers["issues"]:
        for key in ("side_a", "side_b"):
            side = issue.get(key)
            if side and side.get("bbox"):
                BBox(**side["bbox"])


def test_dc06_import_suites_and_anomalies(fixtures_ready, tmp_path):
    a_files = sorted((fixtures_ready / "build" / "A").glob("*.pdf"))
    b_files = sorted((fixtures_ready / "build" / "B").glob("*.pdf"))
    assert a_files and b_files
    with ImportService(tmp_path / "data-a") as svc:
        job = svc.wait(svc.submit(a_files, project_name="DC05-A"), 180)
    assert job["status"] == "completed", job
    assert job["result"]["unit_count"] == len(a_files)
    assert job["result"]["unit_count"] == len(job["result"]["drawings"])
    with ImportService(tmp_path / "data-b") as svc:
        job_b = svc.wait(svc.submit(b_files, project_name="DC05-B"), 180)
    assert job_b["status"] == "completed"
    assert job_b["result"]["unit_count"] == len(b_files)

    manifest = json.loads((fixtures_ready / "MANIFEST.json").read_text(encoding="utf-8"))
    for entry in manifest["anomalies"]:
        path = fixtures_ready / entry["relative_path"]
        assert path.exists(), path
        with ImportService(tmp_path / f"anom-{path.name}") as svc:
            job = svc.wait(svc.submit([path], project_name=path.name), 60)
        if expected_status := entry.get("expect_import_status"):
            assert job["result"]["files"], job
            assert job["result"]["files"][0]["status"] == expected_status
            assert job["result"]["files"][0]["format"] == "dwg"
        expect = entry["expect_code"]
        unreadable = job["result"]["unreadable"]
        assert unreadable, f"{path.name} 期望 unreadable {expect}: {job}"
        codes = {u["code"] for u in unreadable}
        assert expect in codes, f"{path.name}: 期望 {expect} 实际 {codes}"


def test_dc07_fake_and_optional_dwg(fixtures_ready, tmp_path):
    from backend.parsers.base import ParseInput
    from backend.parsers.dwg import DwgFileStatus, discover_tools, parse_dwg_file

    tools = discover_tools()
    if tools is None:
        pytest.skip("LibreDWG 不可用（DRAWCHECK_LIBREDWG_BIN）")

    def parse_one(source: Path):
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        cache = tmp_path / f"cache-{source.stem}"
        originals = tmp_path / f"orig-{source.stem}"
        originals.mkdir(parents=True, exist_ok=True)
        cache.mkdir(parents=True, exist_ok=True)
        local = originals / source.name
        local.write_bytes(source.read_bytes())
        return parse_dwg_file(ParseInput(source.stem, local, digest, cache, "dwg"))

    fake = fixtures_ready / "anomalies" / "fake.dwg"
    result = parse_one(fake)
    assert result.status == DwgFileStatus.UNREADABLE
    assert result.reason

    optional = fixtures_ready / "build" / "optional" / "tiny.dwg"
    if not optional.is_file():
        pytest.skip("未生成自制 tiny.dwg（dxf2dwg 跳过）")
    tiny = parse_one(optional)
    assert tiny.status in (DwgFileStatus.SUCCESS, DwgFileStatus.PARTIAL, DwgFileStatus.UNREADABLE)
    # 记录到证据由调用方写日志；此处要求有明确状态
    assert tiny.reason
    manifest = json.loads((fixtures_ready / "MANIFEST.json").read_text(encoding="utf-8"))
    opt = manifest.get("optional_dwg") or {}
    assert "sha256" not in opt, "optional DWG 的 SHA 不得写入 MANIFEST"
    if opt.get("status") not in (None, "skipped"):
        assert opt.get("status") == "partial"
        assert opt.get("parse_status") == "PARTIAL"


def test_replay_matches_answers_and_review_hash(fixtures_ready, tmp_path):
    answers = _load_answers(fixtures_ready)
    # 建立 answers 中带 bbox 的 (drawing, text) → bbox
    expected = []
    for issue in answers["issues"]:
        for key in ("side_a", "side_b"):
            side = issue.get(key) or {}
            if side.get("bbox") and side.get("text"):
                expected.append(side)

    # 汇总 A+B 回放事实，answers 每条带 bbox 的侧须在某一套回放中命中
    all_facts: dict[str, list] = {}
    for suite in ("A", "B"):
        index = json.loads((fixtures_ready / "replay" / suite / "index.json").read_text(encoding="utf-8"))
        assert index.get("poppler_version")
        files = {Path(e["file"]).name for e in index["entries"]}
        build_files = {p.name for p in (fixtures_ready / "build" / suite).glob("*.pdf")}
        assert files == build_files
        for entry in index["entries"]:
            for fact in entry["facts"]:
                if fact.get("bbox"):
                    all_facts.setdefault(fact["text"], []).append(fact["bbox"])

        from tools.fixtures.generate import render_review_png
        for entry in index["entries"]:
            pdf = fixtures_ready / entry["file"]
            dest = tmp_path / f"{suite}-{pdf.stem}.png"
            try:
                review_sha = render_review_png(pdf, dest)
            except Exception as exc:
                pytest.skip(f"pdftoppm 不可用: {exc}")
            if entry["review_png_sha256"] != review_sha:
                pdf_sha = sha256_file(pdf)
                assert entry["fallback_key"] == f"{pdf_sha}:1"
                pytest.fail(
                    f"审查图 SHA 与回放不一致（Poppler 可能漂移）: {entry['file']} "
                    f"index={entry['review_png_sha256']} local={review_sha} "
                    f"poppler={index.get('poppler_version')}"
                )

    for side in expected:
        text = side["text"]
        assert text in all_facts, f"回放缺少 text={text}"
        assert side["bbox"] in all_facts[text], f"回放缺少与 answers 一致的 bbox: {text}"


def test_repo_size_and_no_sample_overlap(fixtures_ready):
    """提交进仓文件体积与客户样本 SHA 不重合。"""
    # 仅统计应提交路径（不含 build/）
    tracked = []
    for rel in ("answers.json", "MANIFEST.json", "README.md"):
        p = fixtures_ready / rel
        if p.is_file():
            tracked.append(p)
    for p in (fixtures_ready / "anomalies").glob("*"):
        if p.is_file():
            tracked.append(p)
    for p in (fixtures_ready / "replay").rglob("*.json"):
        tracked.append(p)
    # 工具与测试也算本单新增，但此处只卡 fixtures/synthetic 提交物
    total = sum(p.stat().st_size for p in tracked)
    assert total <= 1_000_000, f"synthetic 提交物合计 {total} bytes > 1MB"
    for p in tracked:
        assert p.stat().st_size <= 200_000, f"{p} 过大"

    if not SAMPLES.is_dir():
        pytest.skip("DRAWCHECK_SAMPLES 不存在，跳过样本 SHA 交叉检查")
    sample_hashes = set()
    for path in SAMPLES.rglob("*"):
        if path.is_file() and path.stat().st_size <= 50_000_000:
            try:
                sample_hashes.add(sha256_file(path))
            except OSError:
                continue
    for p in tracked:
        digest = sha256_file(p)
        assert digest not in sample_hashes, f"{p} 与客户样本 SHA 重合"


def test_mixed_page_sizes_and_boundaries(fixtures_ready):
    """幅面含 A3/A2；答案含全半角图名、多图框、无图签标记。"""
    answers = _load_answers(fixtures_ready)
    catalog = answers["catalog"]["A"]
    assert any(r.get("page_size") == "A2_LANDSCAPE" for r in catalog)
    assert any(r.get("page_size") == "A3_LANDSCAPE" for r in catalog)
    assert any("（一）" in r["name"] for r in catalog)
    assert any("(二）" in r["name"] for r in catalog)
    assert any(r.get("multi_frame") for r in catalog)
    assert any(r.get("no_title_block") for r in catalog)
    assert any(r.get("out_of_scope") for r in catalog)
