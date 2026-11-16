import json
from types import SimpleNamespace

import pytest
from PIL import Image

from backend.review import ReviewService
from backend.review.service import DISAGREEMENT
from backend.store import BBox, Evidence, Source


class Client:
    def __init__(self, verdict="agree", error=False):
        self.verdict, self.error, self.calls = verdict, error, []

    def chat_completions(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise TimeoutError("timeout")
        return SimpleNamespace(content=json.dumps({"verdict": self.verdict, "explanation": "证据复核说明"}), input_tokens=100, output_tokens=20)


@pytest.fixture
def drawing(repo, tmp_path):
    p = repo.create_project("复核")
    v = repo.create_version(p["id"], "v1")
    path = tmp_path / "r.pdf"
    path.write_bytes(b"%PDF-test")
    o = repo.import_original(v["id"], path)["record"]
    image = repo.root / "cache" / "r.png"
    image.parent.mkdir(exist_ok=True)
    Image.new("RGB", (800, 600), "white").save(image)
    u = repo.create_unit(v["id"], o["id"], kind="pdf_page", ordinal=1, name="图", metadata={"review_relative_path": "cache/r.png"})
    issue = repo.create_issue(v["id"], "尺寸不一致", Evidence(u["id"], Source.VISION, "100mm", BBox(.1,.2,.2,.3)), Evidence(u["id"], Source.VISION, "200mm", BBox(.3,.2,.4,.3)), rule_result={"category":"尺寸", "left_value":"100mm", "right_value":"200mm", "basis":"本地比较超过容差"})
    task = repo.create_task(v["id"])
    return v, u, issue, task


def svc(repo, drawing, client, **kwargs):
    return ReviewService(repo, task_id=drawing[3]["id"], primary_model={"provider":"custom", "model":"model-one"}, review_model={"provider":"custom", "model":"model-two"}, client=client, **kwargs)


@pytest.mark.parametrize("verdict", ["agree", "disagree", "uncertain"])
def test_verdict_and_images_never_confirm_human(repo, drawing, verdict):
    client = Client(verdict)
    result = svc(repo, drawing, client).run(drawing[0]["id"])
    issue = repo.get("issues", drawing[2]["id"])
    assert issue["review_status"] == "疑似"
    assert issue["review_opinion"]["verdict"] == verdict
    assert bool(result.incomplete) == (verdict == "uncertain")
    assert (DISAGREEMENT in issue["review_opinion"]["explanation"]) == (verdict == "disagree")
    parts = client.calls[0]["messages"][1]["content"]
    assert len([p for p in parts if p["type"] == "image_url"]) == 2
    assert "禁止计算" in client.calls[0]["messages"][0]["content"]
    assert "本地比较超过容差" in parts[0]["text"]
    assert len(repo.list_records("model_calls", version_id=drawing[0]["id"])) == 1


def test_timeout_single_retry_and_cache(repo, drawing):
    client = Client(error=True)
    service = svc(repo, drawing, client)
    result = service.run(drawing[0]["id"])
    assert result.incomplete and result.model_calls == 1
    assert repo.get("issues", drawing[2]["id"])["review_opinion"]["status"] == "incomplete"
    client.error = False
    assert service.retry_issue(drawing[2]["id"]).reviewed == 1
    assert len(client.calls) == 2
    assert service.run(drawing[0]["id"]).cache_hits == 1
    assert len(client.calls) == 2
    # Changing the configured second model invalidates cached review.
    service.review["model"] = "model-three"
    assert service.run(drawing[0]["id"]).model_calls == 1


def test_same_model_rejected_no_calls(repo, drawing):
    client = Client()
    service = svc(repo, drawing, client)
    service.review = service.primary.copy()
    with pytest.raises(ValueError, match="同模"):
        service.run(drawing[0]["id"])
    assert client.calls == []


def test_single_model_and_cost_pause_zero_calls(repo, drawing):
    client = Client()
    service = svc(repo, drawing, client)
    service.review = {}
    assert service.run(drawing[0]["id"]).model_calls == 0
    assert svc(repo, drawing, client, allow_model_call=lambda: False).run(drawing[0]["id"]).cost_paused
    assert client.calls == []


def test_invalid_bbox_incomplete_without_call(repo, drawing):
    repo._update("issues", drawing[2]["id"], {"left_x1": .1})
    client = Client()
    result = svc(repo, drawing, client).run(drawing[0]["id"])
    assert result.incomplete and result.model_calls == 0
    assert client.calls == []


def test_disagreed_numeric_reading_creates_pending_without_model_arithmetic(repo, drawing):
    class ReadingClient(Client):
        def chat_completions(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(content=json.dumps({"verdict":"disagree", "explanation":"左侧读数不同", "left_text":"110mm", "right_text":"200mm"}), input_tokens=10, output_tokens=5)
    result = svc(repo, drawing, ReadingClient()).run(drawing[0]["id"])
    pending = repo.list_records("pending_confirmations", version_id=drawing[0]["id"])
    assert len(pending) == 1
    assert pending[0]["text"] == "100mm" and "两模型读数不一致" in pending[0]["reason"]
    assert repo.get("issues", drawing[2]["id"])["review_status"] == "疑似"
