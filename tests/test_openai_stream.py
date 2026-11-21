import json

import pytest

from fukamiru.local_llm.openai_stream import CompletionStream, StreamProtocolError


def event(stream, obj):
    stream.feed_line("data: " + json.dumps(obj, ensure_ascii=False))
    stream.feed_line("")


def test_reasoning_content_finish_and_separate_usage_are_retained():
    stream = CompletionStream()
    event(stream, {"choices": [{"delta": {"reasoning_content": "検討"}}]})
    event(stream, {"choices": [{"delta": {"content": '{"ok":true}'}}]})
    event(stream, {"choices": [{"finish_reason": "stop", "delta": {}}]})
    event(stream, {"choices": [], "usage": {"completion_tokens": 12}})
    stream.feed_line("data: [DONE]")
    stream.feed_line("")
    result = stream.result()
    assert result["choices"][0]["message"] == {
        "role": "assistant",
        "content": '{"ok":true}',
        "reasoning_content": "検討",
    }
    assert result["usage"]["completion_tokens"] == 12
    assert result["stream_statistics"]["saw_done"]


@pytest.mark.parametrize("kind", ["error_event", "error_object"])
def test_midstream_errors_are_not_silently_treated_as_an_empty_chunk(kind):
    stream = CompletionStream()
    event(stream, {"choices": [{"delta": {"reasoning_content": "途中"}}]})
    if kind == "error_event":
        stream.feed_line("event: error")
    with pytest.raises(StreamProtocolError, match="backend_error"):
        event(stream, {"error": {"message": "native decoder failed"}})


def test_terminal_finish_is_sufficient_without_waiting_forever_for_DONE():
    stream = CompletionStream()
    event(stream, {"choices": [{"delta": {"content": "本文"}, "finish_reason": "stop"}]})
    assert stream.finished
    assert stream.result()["choices"][0]["message"]["content"] == "本文"
    assert not stream.result()["stream_statistics"]["saw_done"]


def test_abrupt_EOF_and_DONE_without_finish_cannot_become_success():
    stream = CompletionStream()
    event(stream, {"choices": [{"delta": {"content": '{"incomplete":'}}]})
    with pytest.raises(StreamProtocolError, match="EOF_without_finish"):
        stream.result()
    stream.feed_line("data: [DONE]")
    with pytest.raises(StreamProtocolError, match="DONE_without_finish"):
        stream.feed_line("")


def test_comment_heartbeat_does_not_pretend_that_text_is_being_generated():
    stream = CompletionStream()
    stream.feed_line(": keep-alive")
    stream.feed_line("")
    assert stream.events == stream.reasoning_characters == stream.final_characters == 0
