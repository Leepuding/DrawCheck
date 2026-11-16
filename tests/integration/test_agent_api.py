import asyncio
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.agent_api import AgentResults, create_http_app
from backend.settings import SettingsService
from backend.settings.credentials import CredentialService
from backend.store import BBox, Evidence, Repository, Source, StoreError

from .conftest import ScriptedModel, fact, finish_task, make_issue, payload
from .test_review_model_route import precheck, start, wait_for_task


TOKEN = "test-agent-token-that-is-not-a-secret"


@pytest.fixture
def agent_http(repo, drawing):
    app = create_http_app(AgentResults(repo.root, drawing["vid"]), TOKEN)
    with TestClient(app, base_url="http://127.0.0.1:8765") as http:
        yield http


def _url(version_id, resource):
    return f"/agent/v1/versions/{version_id}/{resource}"


def _headers():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_http_permissions_limit_access_to_scoped_read_only_version(agent_http, drawing):
    summary = _url(drawing["vid"], "summary")
    assert agent_http.get(summary).status_code == 401
    assert agent_http.get(summary, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert agent_http.get(summary, headers={"Cookie": f"drawcheck_token={TOKEN}"}).status_code == 401
    assert agent_http.get(summary, headers={**_headers(), "Host": "example.invalid"}).status_code == 400
    assert agent_http.get(summary, headers={**_headers(), "Origin": "https://example.invalid"}).status_code == 403
    response = agent_http.get(summary, headers=_headers())
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert response.json()["outcome"] == "not_started" and response.json()["issue_count"] is None
    assert agent_http.get(_url("0" * 32, "summary"), headers=_headers()).status_code == 404
    assert agent_http.post(summary, headers=_headers()).status_code == 405
    for path in ("/api/settings", "/api/import", "/openapi.json", "/api/history"):
        assert agent_http.get(path, headers=_headers()).status_code == 404
    issues = _url(drawing["vid"], "issues")
    assert agent_http.get(issues, headers=_headers()).json()["error"]["code"] == "RESULTS_NOT_READY"
    assert agent_http.get(issues, params={"limit": 101}, headers=_headers()).status_code == 422


def test_read_only_store_does_not_create_migrate_or_modify_database(repo, tmp_path):
    missing = tmp_path / "no-data"
    with pytest.raises(StoreError):
        with Repository(missing, read_only=True):
            pytest.fail("Missing database unexpectedly opened")
    assert not missing.exists()
    old = tmp_path / "old-data"
    old.mkdir()
    old_db = old / "drawcheck.sqlite3"
    with closing(sqlite3.connect(old_db)) as connection:
        connection.execute("PRAGMA user_version=1")
    previous = old_db.read_bytes()
    with pytest.raises(StoreError):
        with Repository(old, read_only=True):
            pytest.fail("Database requiring a migration unexpectedly opened")
    assert old_db.read_bytes() == previous
    database = repo.root / "drawcheck.sqlite3"
    before = database.read_bytes()
    with Repository(repo.root, read_only=True) as readonly:
        with pytest.raises(StoreError):
            readonly.create_project("不应写入")
    assert database.read_bytes() == before


def test_http_reads_results_from_desktop_review_without_raw_content(client, agent_http, drawing, configure, monkeypatch, repo):
    configure()
    model = ScriptedModel([payload(fact("2400mm")), payload(fact("2500mm"), number="S-002")])
    monkeypatch.setattr(SettingsService, "_make_client", lambda *args: model)
    version_id = drawing["vid"]
    submitted = start(client, version_id, precheck(client, version_id)["disclosure_id"])
    progress = wait_for_task(client, submitted.json()["task_id"])
    assert progress["status"] == "已完成" and len(model.requests) == 2
    desktop = client.get("/api/issues", params={"version_id": version_id}).json()
    assert desktop["issue_count"] == 1

    def deny_credential_store(*_args):
        pytest.fail("Agent results must not initialize the credential store")

    monkeypatch.setattr(CredentialService, "_detect_backend", deny_credential_store)
    database = repo.root / "drawcheck.sqlite3"
    before = database.read_bytes()
    summary = agent_http.get(_url(version_id, "summary"), headers=_headers()).json()
    paged = agent_http.get(_url(version_id, "issues"), params={"limit": 1}, headers=_headers()).json()
    assert summary["results_available"] and summary["has_effective_comparison"]
    for key in ("issue_count", "pending_count", "incomplete_count", "unreadable_count", "out_of_scope_count"):
        assert summary[key] == desktop[key]
    assert summary["comparison_summary"] == desktop["comparison_summary"]
    assert paged["items"] == [{"id": desktop["items"][0]["id"], "category": "尺寸", "review_status": "疑似",
                               "comparison_tag": None, "left_unit_id": desktop["items"][0]["finding"]["left"]["unit_id"],
                               "right_unit_id": desktop["items"][0]["finding"]["right"]["unit_id"]}]
    assert paged["total"] == 1 and paged["next_offset"] is None
    serial = json.dumps([summary, paged], ensure_ascii=False)
    for private in ("2400mm", "2500mm", "基础图", "集成验收", str(repo.root), "test-integration-secret"):
        assert private not in serial
    assert database.read_bytes() == before and len(model.requests) == 2


def test_unverified_results_stay_distinct_from_unfinished_and_issue_rows(agent_http, repo, drawing):
    version_id = drawing["vid"]
    first, second = drawing["units"]
    repo.create_issue(version_id, "private text", Evidence(first["id"], Source.PDF_TEXT, "private left", BBox(.1, .1, .1, .1)),
                      Evidence(second["id"], Source.PDF_TEXT, "private right", BBox(.1, .1, .3, .3)),
                      rule_result={"category": "尺寸"})
    task = finish_task(repo, drawing, incomplete=[{"unit_id": first["id"], "category": "incomplete", "reason": "private reason"}])
    summary = agent_http.get(_url(version_id, "summary"), headers=_headers()).json()
    assert summary["results_available"] and summary["outcome"] == "partial"
    assert not summary["has_effective_comparison"] and summary["issue_count"] == 0 and summary["incomplete_count"] == 1
    assert {"category": "尺寸", "verified": False, "difference": 0, "unverified": 1} in summary["comparison_summary"]
    assert agent_http.get(_url(version_id, "issues"), headers=_headers()).json()["items"] == []
    assert "private" not in json.dumps(summary)
    repo.update_task(task["id"], status="失败", checkpoints=task["checkpoints"], incomplete=task["incomplete"],
                     estimated_cost_micros=0)
    summary = agent_http.get(_url(version_id, "summary"), headers=_headers()).json()
    assert summary["outcome"] == "failed" and summary["issue_count"] is None
    assert agent_http.get(_url(version_id, "issues"), headers=_headers()).status_code == 409


def test_mcp_stdio_negotiates_and_matches_http(agent_http, repo, drawing):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    active = make_issue(repo, drawing)
    excluded = make_issue(repo, drawing, status="排除")
    finish_task(repo, drawing)
    version_id = drawing["vid"]
    http_summary = agent_http.get(_url(version_id, "summary"), headers=_headers()).json()
    http_issues = agent_http.get(_url(version_id, "issues"), headers=_headers()).json()

    async def exercise():
        config = StdioServerParameters(command=sys.executable,
            args=["-m", "backend.agent_api", "stdio", "--version-id", version_id, "--data-dir", str(repo.root)],
            cwd=str(Path(__file__).resolve().parents[2]), env={"PYTHONPATH": os.environ.get("PYTHONPATH", "")})
        async with stdio_client(config) as (reader, writer), ClientSession(reader, writer) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            assert {tool.name for tool in tools} == {"get_review_summary", "list_review_issues"}
            assert all(tool.annotations.readOnlyHint and not tool.annotations.destructiveHint for tool in tools)
            summary = await session.call_tool("get_review_summary")
            issues = await session.call_tool("list_review_issues")
            assert not summary.isError and summary.structuredContent == http_summary
            assert not issues.isError and issues.structuredContent == http_issues
            first = await session.call_tool("list_review_issues", {"limit": 1})
            second = await session.call_tool("list_review_issues", {"limit": 1, "offset": 1})
            assert first.structuredContent["items"][0]["id"] == active["id"]
            assert first.structuredContent["next_offset"] == 1 and first.structuredContent["total"] == 2
            assert second.structuredContent["items"][0]["id"] == excluded["id"]
            assert second.structuredContent["next_offset"] is None
            invalid = await session.call_tool("list_review_issues", {"limit": 101})
            assert invalid.isError and "INVALID_PAGE" in invalid.content[0].text

    asyncio.run(asyncio.wait_for(exercise(), timeout=15))
