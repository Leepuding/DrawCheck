import pytest

from backend.jobs import JobError, JobService
from backend.settings.service import SettingsService
from backend.settings.credentials import CredentialService, MemoryCredentialBackend
from tests.backend.jobs.conftest import configure_dual_model
from tests.backend.review.test_review import Client, drawing


def test_single_issue_retry_clears_task_incomplete_without_replaying_other_steps(repo, drawing):
    settings = SettingsService(repo, credentials=CredentialService(MemoryCredentialBackend()))
    configure_dual_model(settings)
    version, unit, issue, task = drawing
    checkpoints = {step: {"state":"done"} for step in ("extract", "link", "rules_a", "rules_b")}
    checkpoints.update(dual_review={"state":"failed"}, finalize={"state":"pending"})
    saved = repo.get_settings()
    checkpoints["_meta"] = {"model_route": {key: saved[key] for key in ("primary_model", "review_model")}}
    repo.update_task(task["id"], status="失败", checkpoints=checkpoints,
        incomplete=[{"unit_id":unit["id"], "issue_id":issue["id"], "step":"dual_review", "reason":"超时", "category":"incomplete"}], estimated_cost_micros=0)
    client = Client()
    jobs = JobService(repo, settings_service=settings)
    jobs.extras["review_client"] = client
    repo.save_settings({"review_model": None})
    result = jobs.retry_issue_review(issue["id"])
    assert result["model_calls"] == 1 and len(client.calls) == 1
    task = repo.get("review_tasks", task["id"])
    assert task["status"] == "已完成" and task["incomplete"] == []
    assert repo.get("issues", issue["id"])["review_status"] == "疑似"


def test_issue_retry_requires_new_disclosure_for_legacy_task(repo, drawing):
    _, _, issue, _ = drawing
    with pytest.raises(JobError) as error:
        JobService(repo).retry_issue_review(issue["id"])
    assert error.value.code == "REVIEW_DISCLOSURE_REQUIRED"
