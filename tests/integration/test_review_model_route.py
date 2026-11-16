import json
import time

import httpx
import pytest

from backend.jobs import JobService
from backend.models.openai_compat import OpenAICompatClient
from backend.settings import SettingsService
from backend.store import Repository

from .conftest import fact, payload


def model_transport(monkeypatch, responses, *, on_first_request=None):
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append((str(request.url), body["model"], request.headers["Authorization"]))
        assert any(item["type"] == "image_url" for item in body["messages"][-1]["content"])
        if len(requests) == 1 and on_first_request:
            on_first_request()
        response = next(responses)
        if body.get("stream"):
            event = {"choices": [{"index": 0, "delta": {"content": json.dumps(response)}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 50, "completion_tokens": 10}}
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(response)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10},
        })

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(SettingsService, "_make_client", lambda self, slot, secret: OpenAICompatClient(
        provider=slot["provider"], base_url=slot["base_url"], api_key=secret,
        transport=transport, trust_env=False))
    return requests


def precheck(client, version_id):
    result = client.post("/api/settings/precheck", json={"disclosure_version_id": version_id})
    assert result.status_code == 200 and result.json()["ok"]
    return result.json()


def start(client, version_id, disclosure):
    return client.post(f"/api/versions/{version_id}/tasks", json={"disclosure_id": disclosure})


def call_for(slot, key):
    return (slot["base_url"].rstrip("/") + "/chat/completions", slot["model"], "Bearer " + key)


def wait_for_task(client, task_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        progress = client.get(f"/api/tasks/{task_id}").json()
        if progress["status"] in {"已完成", "失败", "暂停"}:
            return progress
        time.sleep(.02)
    pytest.fail("Review worker did not reach a terminal state")


def replace_review(settings, old_slot):
    return settings.save_settings({
        "review_model": {**old_slot, "provider": "custom", "model": "review-b",
                         "base_url": "https://review-b.example.invalid/v1"},
    }, review_api_key="test-route-review-b")["review_model"]


def test_settings_change_after_disclosure_rejects_stale_confirmation(client, repo, drawing, configure):
    saved = configure(dual=True)
    vid = drawing["vid"]
    shown = precheck(client, vid)
    assert shown["providers"] == ["硅基流动", "通义千问"]
    replace_review(SettingsService(repo), saved["review_model"])
    response = start(client, vid, shown["disclosure_id"])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REVIEW_DISCLOSURE_REQUIRED"
    assert repo.list_records("review_tasks", version_id=vid) == []
    assert repo.list_records("model_calls", version_id=vid) == []
    new = precheck(client, vid)
    assert new["providers"] == ["硅基流动", "自定义"]
    assert new["disclosure_id"] != shown["disclosure_id"]


def test_worker_uses_confirmed_route_after_save_during_primary_call(client, repo, drawing, configure, monkeypatch):
    saved = configure(dual=True)
    primary, review_a = saved["primary_model"], saved["review_model"]
    vid = drawing["vid"]

    def save_from_another_connection():
        with Repository() as other:
            replace_review(SettingsService(other), review_a)

    responses = iter([payload(fact("2400mm")), payload(fact("2500mm"), number="S-002"),
                      {"verdict": "agree", "explanation": "复核 A"},
                      {"verdict": "agree", "explanation": "复核 B"}])
    requests = model_transport(monkeypatch, responses, on_first_request=save_from_another_connection)
    response = start(client, vid, precheck(client, vid)["disclosure_id"])
    assert response.status_code == 200
    task_id = response.json()["task_id"]
    assert wait_for_task(client, task_id)["status"] == "已完成"
    primary_call = call_for(primary, "test-integration-secret-primary")
    review_a_call = call_for(review_a, "test-integration-secret-review")
    assert requests == [primary_call, primary_call, review_a_call]
    first = repo.get("review_tasks", task_id)
    route = first["checkpoints"]["_meta"]["model_route"]
    assert route == {"primary_model": primary, "review_model": review_a}
    assert "test-integration-secret" not in json.dumps(first)
    rows = repo.list_records("model_calls", version_id=vid)
    assert [(row["role"], row["provider"], row["model"]) for row in rows if row["task_id"] == task_id] == [
        ("primary", primary["provider"], primary["model"]),
        ("primary", primary["provider"], primary["model"]),
        ("review", review_a["provider"], review_a["model"]),
    ]
    review_b = repo.get_settings()["review_model"]
    assert review_b["credential_ref"] != review_a["credential_ref"]
    second = JobService(repo).start_review(vid)
    assert second["status"] == "已完成"
    assert requests == [primary_call, primary_call, review_a_call, call_for(review_b, "test-route-review-b")]
    repo.save_settings({"review_model": review_a})
    third = JobService(repo).start_review(vid)
    assert third["status"] == "已完成"
    assert len(requests) == 4
    assert repo.get("review_tasks", third["task_id"])["checkpoints"]["dual_review"]["detail"]["cache_hits"] == 1
    issue, = repo.list_records("issues", version_id=vid)
    assert issue["review_opinion"]["model"] == review_a["model"]
    assert issue["review_opinion"]["explanation"] == "复核 A"


def test_queued_review_with_missing_old_reference_never_switches_to_new_key(repo, drawing, configure, monkeypatch):
    saved = configure(dual=True)
    review_a = saved["review_model"]
    vid = drawing["vid"]
    settings = SettingsService(repo)
    jobs = JobService(repo, settings_service=settings)
    accepted = jobs.start_review(vid, run=False, disclosure_id=jobs.prepare_disclosure(vid)["disclosure_id"])
    replace_review(settings, review_a)
    settings.credentials.delete(review_a["credential_ref"])
    requests = model_transport(monkeypatch, iter([payload(fact("2400mm")), payload(fact("2500mm"), number="S-002")]))
    jobs.run_task(accepted["task_id"])
    result = jobs.get_progress(accepted["task_id"])
    assert result["status"] == "失败"
    task = repo.get("review_tasks", accepted["task_id"])
    assert any("本次确认的复核模型凭据不可用" in row["reason"] for row in task["incomplete"])
    assert [row["role"] for row in repo.list_records("model_calls", version_id=vid)] == ["primary", "primary"]
    assert requests == [call_for(saved["primary_model"], "test-integration-secret-primary")] * 2
    assert jobs.prepare_disclosure(vid)["providers"] == ["硅基流动", "通义千问"]


def test_legacy_review_only_slot_is_disclosed_and_executes_as_primary(client, repo, drawing, configure, monkeypatch):
    review = configure(dual=True)["review_model"]
    repo.save_settings({"primary_model": None})
    vid = drawing["vid"]
    shown = precheck(client, vid)
    assert shown["model_status"] == "single"
    assert shown["providers"] == ["通义千问"]
    requests = model_transport(monkeypatch, iter([payload(), payload(number="S-002")]))
    response = start(client, vid, shown["disclosure_id"])
    assert response.status_code == 200
    task_id = response.json()["task_id"]
    assert wait_for_task(client, task_id)["status"] == "已完成"
    assert requests == [call_for(review, "test-integration-secret-review")] * 2
    task = repo.get("review_tasks", task_id)
    assert task["checkpoints"]["dual_review"]["reason"] == "single_model"
    assert {row["role"] for row in repo.list_records("model_calls", version_id=vid)} == {"primary"}


def test_replacing_a_key_isolates_extraction_cache_even_for_same_endpoint(repo, drawing, configure, monkeypatch):
    slot = configure()["primary_model"]
    vid = drawing["vid"]
    requests = model_transport(monkeypatch, iter([payload(), payload(number="S-002")] * 2))
    first = JobService(repo).start_review(vid)
    before = [unit["metadata"]["extract_cache_identity"] for unit in
              repo.list_records("drawing_units", version_id=vid)]
    new_slot = SettingsService(repo).save_settings({"primary_model": slot},
        primary_api_key="test-replacement-key")["primary_model"]
    second = JobService(repo).start_review(vid)
    assert first["status"] == second["status"] == "已完成"
    assert new_slot["credential_ref"] != slot["credential_ref"]
    assert requests == [call_for(slot, "test-integration-secret-primary")] * 2 + [
        call_for(new_slot, "test-replacement-key")] * 2
    after = [unit["metadata"]["extract_cache_identity"] for unit in
             repo.list_records("drawing_units", version_id=vid)]
    assert set(before).isdisjoint(after)
