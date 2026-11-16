"""Synthetic provider response shapes and production client policy; no network or real credentials."""
import json

import httpx
import pytest

from backend.models.errors import ModelError, CODE_TIMEOUT
from backend.models.openai_compat import OpenAICompatClient
from backend.jobs import JobService
from backend.settings import SettingsService
from backend.settings.credentials import CredentialService, MemoryCredentialBackend
from backend.store import Repository


def client(**kwargs):
    return OpenAICompatClient(provider="siliconflow", base_url="https://api.example.invalid/v1", api_key="fake-test-only", **kwargs)


def parse(message, finish="stop"):
    return client()._parse_success({"choices":[{"message":message, "finish_reason":finish}],
        "usage":{"prompt_tokens":10,"completion_tokens":20}}, model="zai-org/GLM-4.5V", status=200, latency_ms=1)


def test_text_parts_join_only_text_and_output_text():
    answer = parse({"content":[{"type":"text","text":"{\"facts\":"},
        {"type":"reasoning","text":"synthetic reasoning must not become facts"},
        {"type":"refusal","text":"synthetic refusal"},
        {"type":"output_text","text":"[]}"}, {"type":"image_url","image_url":{"url":"ignored"}},
        {"type":"text","text":{"value":"unsupported nested form"}}, "ignored raw string"]})
    assert answer.content == '{"facts":[]}'
    assert json.loads(answer.content) == {"facts":[]}
    assert answer.extras["content_length"] == len(answer.content)
    assert answer.extras["content_type"] == "parts"
    assert not answer.extras["empty_final"]


@pytest.mark.parametrize("content", [None, "", "   "])
def test_empty_final_has_safe_reasoning_metadata(content):
    private_reasoning = "synthetic-private-reasoning-do-not-copy"
    result = parse({"content":content,"reasoning_content":private_reasoning}, "length")
    assert result.content == (content or "")
    assert result.extras["reasoning_length"] == len(private_reasoning)
    assert result.extras["has_reasoning"]
    assert result.extras["empty_final"] and result.extras["truncated"]
    assert result.finish_reason == result.extras["finish_reason"] == "length"
    assert private_reasoning not in json.dumps(result.extras)
    assert result.input_tokens == 10 and result.output_tokens == 20


def test_truncated_valid_json_remains_explicitly_identifiable_by_upstream():
    result = parse({"content":'{"facts":[]}'}, "length")
    assert json.loads(result.content) == {"facts":[]}
    assert result.extras["truncated"] and not result.extras["empty_final"]


def test_unsupported_content_is_not_mistaken_for_authoritative_json():
    result = parse({"content":{"facts":[]}, "reasoning_content":"synthetic"})
    assert result.content == "" and result.extras["content_type"] == "unsupported"
    assert result.extras["empty_final"]


@pytest.fixture
def settings(tmp_path):
    with Repository(tmp_path/"data") as repo:
        service = SettingsService(repo, credentials=CredentialService(MemoryCredentialBackend(),allow_env=False))
        service.save_settings({"monthly_limit_yuan":1,"primary_model":{"provider":"siliconflow",
            "model":"zai-org/GLM-4.5V","base_url":"https://api.siliconflow.cn/v1"}}, primary_api_key="mock-only-test-key")
        yield repo, service


def test_job_policy_separates_read_timeout_and_does_not_change_connection_test(settings):
    repo, service = settings
    work = JobService(repo,settings_service=service).model_client("primary")
    with work._client() as http:
        assert http.timeout.read == 300
        assert http.timeout.connect == 15
        assert http.timeout.write == 60
        assert http.timeout.pool == 15
    assert work.max_retries == 0
    slot = repo.get_settings()["primary_model"]
    probe = service._make_client(slot, service.credentials.resolve(slot["credential_ref"]))
    repo.save_settings({"review_model": {**slot, "model": "Qwen/Qwen3-VL-32B-Instruct"}})
    review = JobService(repo, settings_service=service).model_client("review")
    assert review.timeout_sec == 180
    assert probe.timeout_sec == 60 and probe.max_retries == 2
    with probe._client() as http:
        assert http.timeout.read == http.timeout.connect == 60


@pytest.mark.parametrize("timeout_type, expected", [
    (httpx.ReadTimeout, "等待模型响应超时"),
    (httpx.WriteTimeout, "发送模型请求超时"),
    (httpx.PoolTimeout, "等待模型连接池超时"),
    (httpx.ConnectTimeout, "连接模型服务超时"),
])
def test_job_timeout_makes_one_physical_request(settings, timeout_type, expected):
    repo, service = settings
    work = JobService(repo,settings_service=service).model_client("primary")
    calls = []
    def timeout(request):
        calls.append(request)
        raise timeout_type("synthetic", request=request)
    work._transport = httpx.MockTransport(timeout)
    with pytest.raises(ModelError) as error:
        work.chat_completions(model="zai-org/GLM-4.5V",messages=[])
    assert error.value.code == CODE_TIMEOUT and len(calls) == 1
    assert expected in str(error.value)


def test_original_float_timeout_constructor_remains_compatible():
    with client(timeout_sec=2.5)._client() as http:
        assert http.timeout.read == http.timeout.connect == http.timeout.write == http.timeout.pool == 2.5


def test_non_object_message_is_an_explicit_model_format_error():
    from backend.models.errors import CODE_BAD_RESPONSE
    with pytest.raises(ModelError) as error:
        parse([])
    assert error.value.code == CODE_BAD_RESPONSE


def test_stream_uses_terminal_event_and_preserves_usage_and_safe_timings():
    def stream(request):
        assert json.loads(request.content)["stream"] is True
        events = [
            {"choices": [{"index": 0, "delta": {"reasoning_content": "private reasoning"}}]},
            {"choices": [{"index": 0, "delta": {"content": '{"facts":'}}]},
            {"choices": [{"index": 0, "delta": {"content": "[]}"}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 10, "completion_tokens": 20}},
        ]
        body = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"x-siliconcloud-trace-id": "ti_example",
                                                      "x-private-header": "private header"})
    result = client(transport=httpx.MockTransport(stream)).chat_completions(model="m", messages=[], stream=True)
    assert result.content == '{"facts":[]}' and result.input_tokens == 10 and result.output_tokens == 20
    log = result.extras["request"]
    assert log["headers_ms"] <= log["first_event_ms"] <= log["first_content_ms"] <= log["elapsed_ms"]
    assert log["provider_trace_id"] == "ti_example" and log["finish_reason"] == "stop"
    assert "private" not in json.dumps(result.extras)


@pytest.mark.parametrize("terminal", ["missing_done", "read_timeout", "http_504", "auth"])
def test_stream_errors_preserve_phase_and_never_retry(terminal):
    calls = []

    class PartialBody(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"{}"},"finish_reason":"stop"}]}\n\n'
            if terminal == "read_timeout":
                raise httpx.ReadTimeout("synthetic private transport message")

    def handler(request):
        calls.append(request)
        if terminal in ("http_504", "auth"):
            return httpx.Response(504 if terminal == "http_504" else 401, text="private provider error",
                                  headers={"x-siliconcloud-trace-id": "ti_example"})
        return httpx.Response(200, stream=PartialBody())

    with pytest.raises(ModelError) as caught:
        client(transport=httpx.MockTransport(handler)).chat_completions(model="m", messages=[], stream=True)
    error = caught.value
    assert len(calls) == 1 and error.retryable is (terminal != "auth")
    assert error.diagnostics["http_status"] in {200, 401, 504}
    assert error.diagnostics["phase"] == "response_body"
    assert ("first_content_ms" in error.diagnostics) is (terminal in ("read_timeout", "missing_done"))
    assert "private" not in json.dumps(error.diagnostics)
