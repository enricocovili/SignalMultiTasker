from unittest.mock import MagicMock

import requests

from bridge import config, llm


def configured(monkeypatch):
    monkeypatch.setattr(config, "LLM_URL", "http://llm/v1")
    monkeypatch.setattr(config, "LLM_MODEL", "m")


def response(content, finish="stop"):
    r = MagicMock()
    r.json.return_value = {
        "choices": [{"message": {"content": content}, "finish_reason": finish}]
    }
    r.elapsed.total_seconds.return_value = 0.1
    return r


def test_unconfigured_degrades_silently(monkeypatch):
    monkeypatch.setattr(config, "LLM_URL", "")
    assert llm.summarize("{body}", "x") == (None, None)


def test_summary_and_unknown_placeholder(monkeypatch):
    configured(monkeypatch)
    post = MagicMock(return_value=response(" hi "))
    monkeypatch.setattr(llm.requests, "post", post)
    assert llm.summarize("{body} {nope}", "text") == ("hi", None)
    assert post.call_args.kwargs["json"]["messages"][0]["content"] == "text "


def test_timeout_reports_error_code(monkeypatch):
    configured(monkeypatch)
    monkeypatch.setattr(
        llm.requests, "post", MagicMock(side_effect=requests.exceptions.Timeout)
    )
    assert llm.summarize("{body}", "x") == (None, "timeout")


def test_length_finish_with_empty_content_is_not_an_error(monkeypatch):
    configured(monkeypatch)
    monkeypatch.setattr(
        llm.requests, "post", MagicMock(return_value=response("", "length"))
    )
    assert llm.summarize("{body}", "x") == (None, None)
