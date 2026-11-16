"""全部使用临时自制字节，不读取或外发客户样图。"""
from pathlib import Path

import pytest

from backend.store import BBox, Evidence, Repository, Source


@pytest.fixture
def repo(tmp_path):
    with Repository(tmp_path / "数据 空间（本地）") as store:
        yield store


@pytest.fixture
def drawing(repo, tmp_path):
    source = tmp_path / "T0105(二） 图纸.pdf"
    source.write_bytes(b"%PDF-TEST\n\xe4\xb8\xad\xe6\x96\x87\n")
    project = repo.create_project("中文 项目（测试）")
    version = repo.create_version(project["id"], "第一版 (二）")
    original = repo.import_original(version["id"], source)["record"]
    unit = repo.create_unit(version["id"], original["id"], kind="pdf_page", ordinal=1,
                            name="T0105(二） 图纸", drawing_number="T0105")
    evidence = Evidence(unit["id"], Source.PDF_TEXT, "尺寸 2400", BBox(0.1, 0.2, 0.3, 0.4))
    return {"source": source, "project": project, "version": version,
            "original": original, "unit": unit, "evidence": evidence}


def issue(repo, drawing):
    return repo.create_issue(drawing["version"]["id"], "尺寸不一致", drawing["evidence"], drawing["evidence"],
                             rule_result={"left": "2400", "right": "2410", "matched": False},
                             primary_opinion={"model": "模拟主模型", "text": "不一致"},
                             review_opinion={"model": "模拟复核模型", "text": "需核对"})
