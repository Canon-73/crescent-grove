"""
OpenAICompatibleProvider.chat_streamed テスト
モックのストリームチャンク列を流し、LLMResponse が非ストリーミング版と
同等に組み立てられること（content / tool_calls / usage / reasoning）を検証する。
（pytest不要・python tests/test_llm_chat_streamed.py で直接実行）
"""
import asyncio
import os
import sys
from types import SimpleNamespace

# tests/ から見て1段上がリポジトリルート
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.llm import OpenAICompatibleProvider

passed = 0
failed = 0


def run_test(name, fn):
    global passed, failed
    try:
        asyncio.run(fn())
        print(f"[OK] {name}")
        passed += 1
    except AssertionError as e:
        print(f"[NG] {name}: {e}")
        failed += 1
    except Exception as e:
        print(f"[NG] {name}: 予期しない例外 {type(e).__name__}: {e}")
        failed += 1


# --- OpenAI SDK のストリームチャンクを模倣するヘルパー ---

def text_chunk(text, reasoning=None):
    delta = SimpleNamespace(content=text, tool_calls=None, reasoning_content=reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)], usage=None)


def tool_chunk(index, id=None, name=None, arguments=None):
    fn = SimpleNamespace(name=name, arguments=arguments)
    tc = SimpleNamespace(index=index, id=id, function=fn)
    delta = SimpleNamespace(content=None, tool_calls=[tc], reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)], usage=None)


class FakeUsage:
    prompt_tokens = 100
    completion_tokens = 20
    total_tokens = 120
    prompt_tokens_details = None

    def model_dump(self):
        return {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}


def usage_chunk():
    # usage は choices が空の最終チャンクに載ってくる
    return SimpleNamespace(choices=[], usage=FakeUsage())


class FakeStream:
    """async for で回せて close() できる疑似ストリーム"""
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration

    async def close(self):
        self.closed = True


def make_provider(streams):
    """create() が呼ばれるたびに streams から1つ返すプロバイダを作る"""
    provider = OpenAICompatibleProvider(api_key="dummy", base_url="http://localhost:1/v1",
                                        model="test-model", provider="deepseek")
    queue = list(streams)
    calls = []

    async def fake_create(**kwargs):
        calls.append(kwargs)
        return queue.pop(0)

    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    provider._calls = calls
    return provider


async def test_text_streaming():
    """テキストが delta 単位で on_delta に届き、完全な content が返る"""
    stream = FakeStream([text_chunk("こん"), text_chunk("にちは"), text_chunk("！"), usage_chunk()])
    provider = make_provider([stream])
    deltas = []

    async def on_delta(t):
        deltas.append(t)

    resp = await provider.chat_streamed([{"role": "user", "content": "hi"}], on_delta=on_delta)
    assert deltas == ["こん", "にちは", "！"], f"deltas={deltas!r}"
    assert resp.content == "こんにちは！", f"content={resp.content!r}"
    assert resp.tool_calls == [], f"tool_calls={resp.tool_calls!r}"
    assert resp.raw["usage"]["total_tokens"] == 120, f"usage={resp.raw['usage']!r}"
    assert stream.closed, "ストリームが close されていない"
    assert provider._calls[0]["stream"] is True
    assert provider._calls[0]["stream_options"] == {"include_usage": True}


async def test_tool_call_accumulation():
    """index毎の部分JSON連結で tool_calls が正しく組み立てられる"""
    stream = FakeStream([
        text_chunk("確認しますね。"),
        tool_chunk(0, id="call_1", name="read_file", arguments=""),
        tool_chunk(0, arguments='{"pa'),
        tool_chunk(0, arguments='th": "SOUL'),
        tool_chunk(0, arguments='.md"}'),
        tool_chunk(1, id="call_2", name="list_files", arguments='{"directory": "/"}'),
        usage_chunk(),
    ])
    provider = make_provider([stream])
    resp = await provider.chat_streamed([{"role": "user", "content": "x"}])
    assert resp.content == "確認しますね。", f"content={resp.content!r}"
    assert len(resp.tool_calls) == 2, f"tool_calls={resp.tool_calls!r}"
    assert resp.tool_calls[0].id == "call_1"
    assert resp.tool_calls[0].name == "read_file"
    assert resp.tool_calls[0].arguments == {"path": "SOUL.md"}, resp.tool_calls[0].arguments
    assert resp.tool_calls[1].arguments == {"directory": "/"}, resp.tool_calls[1].arguments


async def test_broken_tool_json():
    """壊れたツール引数JSONは chat() と同じく __parse_error__ に落ちる"""
    stream = FakeStream([tool_chunk(0, id="c", name="f", arguments='{"a": '), usage_chunk()])
    provider = make_provider([stream])
    resp = await provider.chat_streamed([{"role": "user", "content": "x"}])
    assert "__parse_error__" in resp.tool_calls[0].arguments, resp.tool_calls[0].arguments
    assert resp.tool_calls[0].arguments["raw_arguments"] == '{"a": '


async def test_reasoning_content():
    """reasoning_content（thinking）が蓄積されて返る"""
    stream = FakeStream([
        text_chunk(None, reasoning="まず考"),
        text_chunk(None, reasoning="える"),
        text_chunk("答えです"),
        usage_chunk(),
    ])
    provider = make_provider([stream])
    deltas = []

    async def on_delta(t):
        deltas.append(t)

    resp = await provider.chat_streamed([{"role": "user", "content": "x"}], on_delta=on_delta)
    assert resp.reasoning_content == "まず考える", resp.reasoning_content
    assert resp.content == "答えです"
    assert deltas == ["答えです"], f"reasoningがdeltaに混ざった: {deltas!r}"


async def test_chinese_retry_resets_stream():
    """簡体字応答は on_stream_reset を呼んで再生成される"""
    bad = FakeStream([text_chunk("这个时间对说为还经从动两长"), usage_chunk()])
    good = FakeStream([text_chunk("日本語の応答です"), usage_chunk()])
    provider = make_provider([bad, good])
    resets = []

    async def on_reset():
        resets.append(True)

    messages = [{"role": "user", "content": "x"}]
    resp = await provider.chat_streamed(messages, on_stream_reset=on_reset)
    assert resp.content == "日本語の応答です", resp.content
    assert len(resets) == 1, f"resets={resets!r}"
    assert any("[SYSTEM] 日本語で" in (m.get("content") or "") for m in messages), \
        "再生成用システムメッセージが追加されていない"


async def test_fallback_to_chat_on_stream_failure():
    """ストリーム開始に失敗したら chat() にフォールバックする"""
    provider = make_provider([])

    async def failing_create(**kwargs):
        raise RuntimeError("stream not supported")

    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=failing_create)))

    from core.llm import LLMResponse

    async def fake_chat(messages, tools=None, frequency_penalty_override=None):
        return LLMResponse(content="フォールバック応答", tool_calls=[], raw={"usage": {}})

    provider.chat = fake_chat
    resp = await provider.chat_streamed([{"role": "user", "content": "x"}])
    assert resp.content == "フォールバック応答", resp.content


async def test_base_class_fallback():
    """基底クラスのデフォルト実装: chat()の全文が1回だけon_deltaに渡る"""
    from core.llm import UnconfiguredProvider
    provider = UnconfiguredProvider()
    deltas = []

    async def on_delta(t):
        deltas.append(t)

    resp = await provider.chat_streamed([{"role": "user", "content": "x"}], on_delta=on_delta)
    assert len(deltas) == 1 and deltas[0] == resp.content, f"deltas={deltas!r}"


if __name__ == "__main__":
    run_test("テキストストリーミング", test_text_streaming)
    run_test("ツール呼び出しの組み立て", test_tool_call_accumulation)
    run_test("壊れたツールJSON", test_broken_tool_json)
    run_test("reasoning_content蓄積", test_reasoning_content)
    run_test("簡体字リトライ+reset", test_chinese_retry_resets_stream)
    run_test("chat()フォールバック", test_fallback_to_chat_on_stream_failure)
    run_test("基底クラスのフォールバック", test_base_class_fallback)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
