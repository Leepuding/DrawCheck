"""Independent integration fixtures: real SQLite/files, injected model boundary only.

No browser, shared data, system keychain, or provider network is used.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter

from backend.app import create_app
from backend.models import CallResult
from backend.settings.credentials import CredentialService, MemoryCredentialBackend
from backend.settings.service import SettingsService
from backend.store import BBox, Evidence, Repository, Source


@dataclass
class ScriptedModel:
    responses: list = field(default_factory=list)
    requests: list = field(default_factory=list)
    input_tokens: int = 100
    output_tokens: int = 30

    def chat_completions(self, **kwargs):
        self.requests.append(kwargs)
        if not self.responses:
            raise AssertionError("Unexpected model invocation: integration script exhausted")
        payload = self.responses.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return CallResult(content=payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
                          input_tokens=self.input_tokens, output_tokens=self.output_tokens,
                          raw_status=200, model=kwargs["model"], provider="integration", latency_ms=1)


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    import socket
    monkeypatch.setenv("DRAWCHECK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DRAWCHECK_EXPORT_DIR", str(tmp_path / "reports"))
    monkeypatch.delenv("DRAWCHECK_ENABLE_TEST_ROUTES", raising=False)
    monkeypatch.delenv("DRAWCHECK_LIVE_MODEL", raising=False)
    backend = MemoryCredentialBackend()
    monkeypatch.setattr(CredentialService, "_detect_backend", lambda self: backend)
    def deny(*args, **kwargs):
        raise AssertionError("Real provider/network access is forbidden in integration acceptance")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    # Tests that exercise a model explicitly replace this factory with ScriptedModel.
    monkeypatch.setattr(SettingsService, "_make_client", deny)


@pytest.fixture
def repo():
    with Repository() as value:
        yield value


@pytest.fixture
def client():
    with TestClient(create_app("integration-local-token")) as value:
        value.headers["Authorization"] = "Bearer integration-local-token"
        yield value


@pytest.fixture
def configure(repo):
    def save(dual=False, limit=100):
        return SettingsService(repo).save_settings({
            "monthly_limit_yuan": limit,
            "primary_model": {"provider": "siliconflow"},
            "review_model": {"provider": "qwen"} if dual else None,
        }, primary_api_key="test-integration-secret-primary",
           review_api_key="test-integration-secret-review" if dual else None)
    return save


@pytest.fixture
def drawing(repo, tmp_path):
    project = repo.create_project("集成验收")
    version = repo.create_version(project["id"], "A")
    source = tmp_path / "synthetic.pdf"
    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=400, height=300)
    writer.write(source)
    original = repo.import_original(version["id"], source)["record"]
    units = []
    for index in range(2):
        image = repo.root / f"page-{index}.png"
        Image.new("RGB", (400, 300), (255, 255, 255-index)).save(image)
        units.append(repo.create_unit(version["id"], original["id"], kind="pdf_page", ordinal=index+1,
                     name=f"基础图{index+1}", drawing_number=f"S-00{index+1}",
                     metadata={"review_relative_path": image.name}))
    return {"project": project, "version": version, "original": original,
            "units": units, "source": source, "vid": version["id"]}


def fact(text, *, x=.1, value=None, category="尺寸", object_id="XB-01", confidence=.99):
    return {"text": text, "numeric_value": value, "category": category, "unit": "mm",
            "bbox": {"x0": x, "y0": .2, "x1": x+.15, "y1": .3},
            "object_id": object_id, "identity_confirmed": True,
            "comparison_key": object_id+":基础长度", "confidence": confidence}


def payload(*facts, number="S-001", out_of_scope=False):
    return {"title_blocks": [{"drawing_number": number, "name": "模型识别图名", "specialty": "土建",
                              "bbox": {"x0": .7, "y0": .8, "x1": .95, "y1": .95},
                              "confidence": .99, "out_of_scope": out_of_scope}], "facts": list(facts)}


def make_issue(repo, drawing, *, status="疑似", title="基础尺寸不一致"):
    left, right = drawing["units"]
    issue = repo.create_issue(drawing["vid"], title,
        Evidence(left["id"], Source.VISION, "2400 mm", BBox(.1,.2,.25,.3)),
        Evidence(right["id"], Source.VISION, "2500 mm", BBox(.5,.2,.65,.3)),
        rule_result={"category": "尺寸", "summary": "基础长度 2400 与 2500 不一致"})
    if status != "疑似":
        issue = repo.update_issue(issue["id"], review_status=status, review_note="人工复核记录")
    return issue


def finish_task(repo, drawing, *, incomplete=None, status="已完成"):
    task = repo.create_task(drawing["vid"])
    checkpoints = {name: {"state": "done"} for name in ("extract", "link", "rules_a", "rules_b", "finalize")}
    checkpoints["dual_review"] = {"state": "skipped", "reason": "single_model"}
    return repo.update_task(task["id"], status=status, checkpoints=checkpoints,
                            incomplete=incomplete or [], estimated_cost_micros=0)
