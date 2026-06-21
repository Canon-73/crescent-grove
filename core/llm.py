# core/llm.py
"""
LLM API 抽象化レイヤー

役割:
    LLMプロバイダー（Deepseek, Claude, ローカル等）の違いを吸収し、
    統一的なインターフェースでLLMとの通信を提供する。

設計:
    - 全プロバイダーは LLMProvider 基底クラスを継承する
    - config.yaml の llm.provider でプロバイダーを切り替える
    - OpenAI互換APIを持つプロバイダーは OpenAICompatibleProvider で対応
      （Deepseek, ローカルLLM等はこれで動く）

拡張方法:
    新しいプロバイダーを追加する場合:
    1. LLMProvider を継承したクラスを作る
    2. create_provider() に分岐を追加する
"""

from core.time_utils import tlog
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
import json
import yaml
from openai import AsyncOpenAI

# --- 簡体字検出（中国語混入防止） ---
_SIMPLIFIED_CHARS = set(
    "这个们说为时对过还么发经从动两长"
    "样现将进实种声话儿业间问电门东开头无"
    "让请认议论讲许诉该谁调谢谈识记设试读课"
    "钱错银钟铁针锁链镇钢"
    "红纪约纯纸线练组细织终绝绿继续编"
    "饭馆饮"
    "转较输轻辆软"
    "关闪闭闻阅闲"
    "妈骂驾骗验"
    "观规视觉览"
    "财货贵费贸购赛质贫败"
    "顺须预领题额颜"
    "鸡鹤"
    "吗呢哪啊"
)

def _count_simplified(text: str) -> int:
    """テキスト中の簡体字の数を返す"""
    if not text:
        return 0
    return sum(1 for c in text if c in _SIMPLIFIED_CHARS)

# --- データ構造 ---

@dataclass
class ToolCall:
    """LLMが要求したツール呼び出し"""
    id: str           # ツール呼び出しのID（LLMが生成）
    name: str         # ツール名（例: "read_file"）
    arguments: dict   # 引数（例: {"path": "SOUL.md"}）


@dataclass 
class LLMResponse:
    """LLMからの応答"""
    content: Optional[str]           # テキスト応答（ツール呼び出し時はNone）
    tool_calls: list[ToolCall]       # ツール呼び出しリスト（テキスト応答時は空）
    raw: dict                        # 生のAPI応答（デバッグ用）
    reasoning_content: Optional[str] = None  # 追加


# --- 基底クラス ---

class LLMProvider(ABC):
    """全LLMプロバイダーが実装するインターフェース"""

    @abstractmethod
    async def chat(self, messages: list[dict], tools: Optional[list[dict]] = None) -> LLMResponse:
        """メッセージを送信し、応答を得る（非ストリーミング）"""
        pass

    async def chat_streamed(
        self, messages: list[dict], tools: Optional[list[dict]] = None,
        on_delta: Optional[Callable[[str], Awaitable[None]]] = None,
        on_stream_reset: Optional[Callable[[], Awaitable[None]]] = None,
        **kwargs,
    ) -> LLMResponse:
        """
        メッセージを送信し、テキスト断片を on_delta に流しながら完全な応答を返す。

        デフォルト実装はストリーミング非対応プロバイダ向けのフォールバック:
        chat() で完全な応答を得てから、完成テキストを1回だけ on_delta に渡す。
        戻り値の LLMResponse は chat() と完全に同形（呼び出し側は区別不要）。

        on_stream_reset は再生成（簡体字リトライ等）の開始時に呼ばれ、
        UI側が「ここまで流したテキストを破棄してやり直す」ために使う。
        """
        resp = await self.chat(messages, tools=tools, **kwargs)
        if on_delta and resp.content:
            await on_delta(resp.content)
        return resp


# --- OpenAI互換プロバイダー ---

class OpenAICompatibleProvider(LLMProvider):
    """
    OpenAI互換APIを持つプロバイダー用の実装。
    Deepseek, ローカルLLM (Ollama/vLLM), OpenAI本体などに対応。
    base_url を変えるだけで切り替え可能。
    """
    
    def __init__(self, api_key: str, base_url: str, model: str,
                 temperature: float = 1.0, max_tokens: int = 4096,
                 provider: str = "", thinking: str = "auto"):
        self.model = model
        self.provider = provider           # deepseek/openai/gemini/grok/local 等。thinking 適用の分岐に使う
        self.thinking = thinking or "auto" # auto/off/low/medium/high（thinking/reasoning モード）
        self.temperature = temperature
        self.max_tokens = max_tokens
        # AsyncOpenAI クライアントを初期化
        # base_url でAPI先を切り替える:
        #   Deepseek: https://api.deepseek.com
        #   Ollama:   http://localhost:11434/v1
        #   OpenAI:   https://api.openai.com/v1
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    
    def _build_chat_kwargs(self, messages: list[dict], tools: Optional[list[dict]],
                           frequency_penalty_override: Optional[float]) -> dict:
        """chat / chat_streamed 共通の API リクエスト引数を構築する"""
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if not self.model.startswith("gemini"):
            kwargs["frequency_penalty"] = frequency_penalty_override if frequency_penalty_override is not None else 0.5
        # thinking（reasoning）モードを設定値 self.thinking から provider 別に適用する。
        # 以前はモデル名（flash/pro）から推測していたが、同一モデルで thinking を切り替えられず、
        # サリアと本体で別々に設定もできなかったため、明示設定（auto/off/low/medium/high）に変更した。
        # auto は何も足さない（モデル既定に任せる）。
        apply_thinking(kwargs, self.provider, self.model, self.thinking)

        # ツール定義がある場合のみ渡す
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"  # LLMが自分で判断
        return kwargs

    def _log_reasoning(self, rc: str) -> None:
        """reasoning_content（thinking時の中間思考）をログファイルに書き出す"""
        try:
            from datetime import datetime
            with open("logs/reasoning.log", "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\t{rc}\n")
        except Exception:
            pass

    def _log_usage(self, u) -> None:
        """トークン使用量をコンソールとログファイルに記録する"""
        cached = getattr(u, 'prompt_tokens_details', None)
        cached_count = getattr(cached, 'cached_tokens', 0) if cached else 0
        tlog(f"[LLM] tokens: in={u.prompt_tokens} (cached={cached_count}) out={u.completion_tokens} total={u.total_tokens}")
        try:
            from datetime import datetime
            with open("logs/llm_usage.log", "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\tin={u.prompt_tokens}\tcached={cached_count}\tout={u.completion_tokens}\ttotal={u.total_tokens}\n")
        except Exception:
            pass

    async def chat(self, messages: list[dict], tools: Optional[list[dict]] = None, frequency_penalty_override: Optional[float] = None) -> LLMResponse:
        """
        LLMにメッセージを送信し、完全な応答を得る。
        ツール呼び出しが含まれる場合は tool_calls に格納される。
        """
        kwargs = self._build_chat_kwargs(messages, tools, frequency_penalty_override)

        max_chinese_retry = 5
        for attempt in range(max_chinese_retry + 1):
            response = await self.client.chat.completions.create(**kwargs)
            if response is None:
                continue
            # reasoning_contentをログに書き出す
            rc = getattr(response.choices[0].message, 'reasoning_content', None)
            if rc:
                self._log_reasoning(rc)
            if hasattr(response, 'usage') and response.usage:
                self._log_usage(response.usage)
            choice = response.choices[0].message

            # 簡体字チェック（テキスト応答 + ツール引数の両方）
            check_text = choice.content or ""
            if choice.tool_calls:
                for tc in choice.tool_calls:
                    try:
                        check_text += " " + tc.function.arguments
                    except Exception:
                        pass
            sc_count = _count_simplified(check_text)
            if sc_count >= 5 and attempt < max_chinese_retry:
                tlog(f"[LLM] 簡体字検出 ({sc_count}文字, 試行{attempt+1}/{max_chinese_retry}) → 再生成")
                # 再生成時にシステムメッセージを追加
                if not any(m.get("content", "").startswith("[SYSTEM] 日本語で") for m in messages):
                    messages.append({
                        "role": "user",
                        "content": "[SYSTEM] 日本語で回答してください。中国語（簡体字）は使用禁止です。"
                    })
                continue
            if sc_count >= 5:
                tlog(f"[LLM] 簡体字検出 ({sc_count}文字) → {max_chinese_retry}回再生成しても解消せず")
            break

        # ツール呼び出しの解析
        tool_calls = []
        if choice.tool_calls:
            for tc in choice.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError as e:
                    args = {"__parse_error__": str(e), "raw_arguments": tc.function.arguments}
                    print(f"Tool Argument JSON Parse Error: {e}")

                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                ))

        return LLMResponse(
            content=choice.content,
            tool_calls=tool_calls,
            raw=response.model_dump(),
            reasoning_content=getattr(choice, 'reasoning_content', None),  # 追加
        )
        
    async def chat_streamed(
        self, messages: list[dict], tools: Optional[list[dict]] = None,
        on_delta: Optional[Callable[[str], Awaitable[None]]] = None,
        on_stream_reset: Optional[Callable[[], Awaitable[None]]] = None,
        frequency_penalty_override: Optional[float] = None,
    ) -> LLMResponse:
        """
        LLMにメッセージを送信し、テキスト断片を on_delta に流しながら完全な応答を返す。
        戻り値は chat() と完全に同形の LLMResponse（ツール呼び出し・usage含む）。
        ストリーム開始に失敗した場合は chat() に自動フォールバックする。
        """
        kwargs = self._build_chat_kwargs(messages, tools, frequency_penalty_override)
        kwargs["stream"] = True
        # usage はストリーム最終チャンクで受け取る（スタミナ計算・トークンバーに必要）
        kwargs["stream_options"] = {"include_usage": True}

        max_chinese_retry = 5
        for attempt in range(max_chinese_retry + 1):
            try:
                stream = await self.client.chat.completions.create(**kwargs)
            except Exception as e:
                if "stream_options" in kwargs:
                    # gemini互換等、stream_options 非対応エンドポイントの可能性 → 外して1回だけ再試行
                    tlog(f"[LLM] stream_options を外して再試行: {e}")
                    kwargs.pop("stream_options", None)
                    try:
                        stream = await self.client.chat.completions.create(**kwargs)
                    except Exception as e2:
                        tlog(f"[LLM] ストリーム開始失敗、非ストリーミングへフォールバック: {e2}")
                        return await self.chat(messages, tools=tools, frequency_penalty_override=frequency_penalty_override)
                else:
                    tlog(f"[LLM] ストリーム開始失敗、非ストリーミングへフォールバック: {e}")
                    return await self.chat(messages, tools=tools, frequency_penalty_override=frequency_penalty_override)

            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_acc: dict[int, dict] = {}  # index → {"id","name","args"}（部分JSONを連結して組み立てる）
            usage = None
            try:
                async for chunk in stream:
                    # usage は choices が空の最終チャンクに載ってくる
                    if getattr(chunk, "usage", None):
                        usage = chunk.usage
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta is None:
                        continue
                    rc = getattr(delta, "reasoning_content", None)
                    if rc:
                        reasoning_parts.append(rc)
                    if delta.content:
                        content_parts.append(delta.content)
                        if on_delta:
                            await on_delta(delta.content)
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            acc = tool_acc.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                            if tc.id:
                                acc["id"] = tc.id
                            if tc.function:
                                if tc.function.name:
                                    acc["name"] += tc.function.name
                                if tc.function.arguments:
                                    acc["args"] += tc.function.arguments
            finally:
                # 中断（CancelledError）や途中エラーでもHTTP接続を確実に閉じる
                try:
                    await stream.close()
                except Exception:
                    pass

            content = "".join(content_parts)
            reasoning = "".join(reasoning_parts) or None
            if reasoning:
                self._log_reasoning(reasoning)
            if usage:
                self._log_usage(usage)

            # 簡体字チェック（chat() と同じ基準: テキスト応答 + ツール引数）
            check_text = content
            for acc in tool_acc.values():
                check_text += " " + acc["args"]
            sc_count = _count_simplified(check_text)
            if sc_count >= 5 and attempt < max_chinese_retry:
                tlog(f"[LLM] 簡体字検出 ({sc_count}文字, 試行{attempt+1}/{max_chinese_retry}) → 再生成")
                if not any(m.get("content", "").startswith("[SYSTEM] 日本語で") for m in messages):
                    messages.append({
                        "role": "user",
                        "content": "[SYSTEM] 日本語で回答してください。中国語（簡体字）は使用禁止です。"
                    })
                # UI側に「ここまでのストリームを破棄してやり直す」ことを通知
                if on_stream_reset:
                    await on_stream_reset()
                continue
            if sc_count >= 5:
                tlog(f"[LLM] 簡体字検出 ({sc_count}文字) → {max_chinese_retry}回再生成しても解消せず")
            break

        # ツール呼び出しの解析（chat() と同じパースエラー処理）
        tool_calls = []
        for idx in sorted(tool_acc):
            acc = tool_acc[idx]
            try:
                args = json.loads(acc["args"]) if acc["args"] else {}
            except json.JSONDecodeError as e:
                args = {"__parse_error__": str(e), "raw_arguments": acc["args"]}
                print(f"Tool Argument JSON Parse Error: {e}")
            tool_calls.append(ToolCall(id=acc["id"], name=acc["name"], arguments=args))

        # raw は agent 側が usage しか読まないため、ストリームでは最小限の dict を組み立てる
        raw = {
            "usage": usage.model_dump() if usage else {},
            "streamed": True,
            "model": self.model,
        }
        return LLMResponse(
            content=content or None,
            tool_calls=tool_calls,
            raw=raw,
            reasoning_content=reasoning,
        )


# --- Claudeプロバイダー（将来用のスケルトン） ---

class ClaudeProvider(LLMProvider):
    """
    Anthropic Claude API用のプロバイダー。
    Claude APIはOpenAI互換ではないため、個別実装が必要。
    
    TODO: 必要になった時点で実装する
    """
    
    def __init__(self, api_key: str, model: str = "claude-opus-4-6", **kwargs):
        self.api_key = api_key
        self.model = model
        # anthropic パッケージが必要
        # pip install anthropic
    
    async def chat(self, messages, tools=None, **kwargs) -> LLMResponse:
        raise NotImplementedError("Claude provider is not yet implemented")


# --- 未設定プロバイダ（配布版の初期状態用） ---

class UnconfiguredProvider(LLMProvider):
    """LLM プロバイダが未選択のとき使うダミープロバイダ。

    配布版は config.yaml の provider を空にして出荷し、ユーザーが初回に
    「設定 → LLM設定」で好きなプロバイダ（deepseek / claude / openai / local 等）を
    選ぶ方式。プロバイダ未選択のまま会話されたら、deepseek 等を勝手に仮定せず、
    設定方法を案内するメッセージを返す（実APIは叩かない）。
    dev は config.yaml に provider があるためここは使われない。
    """

    SETUP_MESSAGE = (
        "まだ使う AI（LLMプロバイダ）が選ばれていません。\n\n"
        "画面上部の「設定」→「LLM設定」を開いて、使いたいプロバイダ"
        "（deepseek / claude / openai / ローカル 等）とモデルを選んでください。\n"
        "次に「APIキー管理」で、そのプロバイダの API キーを登録します。\n"
        "最後に画面右上の「再起動」ボタンを押すと反映されます。\n\n"
        "設定が終わったら、もう一度話しかけてくださいね。"
    )

    def __init__(self):
        # update_llm_config が稼働中プロバイダに temperature/max_tokens を代入するため属性を持たせる
        self.temperature = 1.0
        self.max_tokens = 4096

    async def chat(self, messages: list[dict], tools: Optional[list[dict]] = None, **kwargs) -> LLMResponse:
        return LLMResponse(content=self.SETUP_MESSAGE, tool_calls=[], raw={"unconfigured": True})


# --- thinking（reasoning）モードの provider 別適用 ---

def apply_thinking(kwargs: dict, provider: str, model: str, level: str) -> None:
    """統一 thinking 設定（auto/off/low/medium/high）を、OpenAI互換プロバイダの
    API パラメータに変換して kwargs に適用する（in-place）。

    各社仕様（2026・docs/LLM_THINKING_DESIGN.md 参照）:
      - DeepSeek V4 : extra_body={"thinking":{"type":"enabled"/"disabled"}} ＋ reasoning_effort(high/max)
                      ※low/medium は high に丸まる。off は thinking disabled（reasoning_effort 不可）
      - OpenAI/Gemini/Grok : reasoning_effort = none/low/medium/high
      - local/lmstudio/ollama/custom : 能力不明のため auto 以外は送らない（誤パラメータでの error 回避）
    auto は何も足さない（モデル既定）。
    """
    lvl = (level or "auto").lower()
    if lvl == "auto":
        return
    p = (provider or "").lower()
    m = (model or "").lower()

    is_deepseek = p == "deepseek" or "deepseek" in m
    is_effort = p in ("openai", "gemini", "grok") or m.startswith(("gpt", "o1", "o3", "o4", "gemini", "grok"))

    if is_deepseek:
        eb = dict(kwargs.get("extra_body") or {})
        if lvl == "off":
            eb["thinking"] = {"type": "disabled"}
            kwargs.pop("reasoning_effort", None)  # disabled時は reasoning_effort 不可
        else:
            eb["thinking"] = {"type": "enabled"}
            # DeepSeek は high/max のみ。low/medium/high → high、（必要なら最大は max）
            kwargs["reasoning_effort"] = "high"
        kwargs["extra_body"] = eb
    elif is_effort:
        # OpenAI/Gemini/Grok は reasoning_effort をそのまま。off は none。
        kwargs["reasoning_effort"] = "none" if lvl == "off" else lvl
    else:
        # local/lmstudio/ollama/custom 等は対応が読めないので何も送らない（auto と同等）。
        return


# --- ファクトリー関数 ---

def create_provider(config: dict) -> LLMProvider:
    """
    config.yaml の設定からLLMプロバイダーを生成する。
    
    使い方:
        from core.config_loader import load_config
        config = load_config()
        provider = create_provider(config["llm"])
    
    新しいプロバイダーを追加する場合:
        1. LLMProvider を継承したクラスを作る
        2. ここに elif 分岐を追加する
    """
    import os
    import re

    # provider 未設定（配布版の初期状態）。deepseek 等を勝手に仮定せず、ユーザーに
    # 「設定→LLM設定でプロバイダを選んでください」と案内する未設定プロバイダを返す。
    # dev は config.yaml に provider:"deepseek" があるためここには来ない（挙動不変）。
    provider_name = config.get("provider")
    if not provider_name:
        print("[LLM] provider 未設定。UnconfiguredProvider（設定案内）を使用します。")
        return UnconfiguredProvider()
    
    # APIキーの取得ロジック（環境変数の動的解決を含む）
    api_key_raw = config.get("api_key", "")
    api_key = ""
    
    if api_key_raw:
        # "${CG_LLM_XXX_API_KEY}" のような形式かチェック
        match = re.match(r'^\$\{([A-Za-z0-9_]+)\}$', str(api_key_raw).strip())
        if match:
            env_var_name = match.group(1)
            api_key = os.environ.get(env_var_name, "")
        else:
            api_key = api_key_raw
    
    if not api_key:
        # まず UI（APIキー管理）から登録される CG_LLM_XXX_API_KEY を探索
        api_key = os.environ.get(f"CG_LLM_{provider_name.upper()}_API_KEY", "")
        # なければ従来の XXX_API_KEY をフォールバックとして探索
        if not api_key:
            api_key = os.environ.get(f"{provider_name.upper()}_API_KEY", "")

    # 健全化: 過去バグで config.yaml が api_key: \"${VAR}\" に壊れていると、確定キーが
    # \"sk-...\" のように前後にダブルクォート/バックスラッシュを含むことがある。APIキーに
    # これらは含まれないため、前後の空白・"・\ を除去して過去破損データも救済する。
    # （"・\ を1つの文字集合で strip しないと \"key\" の先頭 " が残るため注意）
    if api_key:
        api_key = api_key.strip().strip('"\\')

    if provider_name in ("deepseek", "openai", "local", "lmstudio", "ollama", "custom", "gemini", "grok", "anthropic"):
        # Anthropic, Gemini等は暫定的にOpenAI互換を試みるか、独自のAPI呼び出しクラスを作る必要があります。
        # ここでは指示に従い、まず全ての設定をパース・ルーティングできるようにします。
        default_urls = {
            "deepseek": "https://api.deepseek.com",
            "openai": "https://api.openai.com/v1",
            "local": "http://localhost:11434/v1",
            "lmstudio": "http://127.0.0.1:1234/v1",
            "ollama": "http://127.0.0.1:11434/v1",
            "custom": "",
            "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "grok": "https://api.x.ai/v1"
        }
        
        if provider_name == "claude" or provider_name == "anthropic":
            return ClaudeProvider(
                api_key=api_key,
                model=config.get("model", "claude-opus-4-6"),
            )
            
        base_url = config.get("base_url", default_urls.get(provider_name, ""))
        
        # model 欠落時もクラッシュさせない（キーがある時は従来通り）。
        model_name = config.get("model")
        if not model_name:
            print("警告: llm 設定に 'model' がありません。'deepseek-chat' で続行します。")
            model_name = "deepseek-chat"

        return OpenAICompatibleProvider(
            api_key=api_key,
            base_url=base_url,
            model=model_name,
            temperature=config.get("temperature", 1.0),
            max_tokens=config.get("max_tokens", 4096),
            provider=provider_name,
            thinking=config.get("thinking", "auto"),
        )
    

    else:
        raise ValueError(f"Unknown LLM provider: {provider_name}")# core/__init__.py
# このディレクトリはエージェントの中核モジュールです