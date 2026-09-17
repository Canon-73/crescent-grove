# core/tokens.py
"""
トークン数計測ユーティリティ

役割:
    メッセージリストのトークン数を見積もる。
    コンテキスト圧縮のタイミング判定とUI表示に使う。

トークナイザー設定:
    config.yaml の tokenizer セクションで切り替え可能。
    tokenizer:
      type: "deepseek"          # "deepseek", "tiktoken", "qwen"
      path: "data/tokenizer.json"  # type が deepseek/qwen の場合
      encoding: "cl100k_base"      # type が tiktoken の場合
"""

import os

_encoder = None
_tokens_per_image = None


def _get_tokens_per_image() -> int:
    """画像1枚あたりのトークン数見積もりを config から読む（初回のみ読んでキャッシュ）。

    config.yaml の llm.vision.tokens_per_image。未設定なら384。
    384 は DeepSeek Vision の実測上限（2048pxで348、256pxで116）。
    以前はここに 1100 が直接書かれていたが、これは Gemini の media_resolution 由来の
    残骸で、DeepSeek では3倍近く過大に見積もっていた（コンテキスト使用率の表示が
    実際より悲観的に出る）。モデル差し替えで壊れないよう設定に出してある。
    """
    global _tokens_per_image
    if _tokens_per_image is not None:
        return _tokens_per_image
    value = 384
    try:
        from core.config_loader import load_config
        config = load_config()
        value = int(config.get("llm", {}).get("vision", {}).get("tokens_per_image", 384))
    except Exception as e:
        print(f"[Tokenizer] vision設定の読み込み失敗、既定値{value}を使用: {e}")
    _tokens_per_image = value
    return _tokens_per_image


def _init_encoder():
    global _encoder
    if _encoder is not None:
        return

    # config.yaml から設定を読み込み
    tokenizer_type = "tiktoken"
    tokenizer_path = None
    tokenizer_encoding = "cl100k_base"

    try:
        # config.yaml + settings.json をマージした設定を使う（WebUIのトークナイザー設定が
        # settings.json に保存されるため、config.yaml 直読みでは反映されない）。
        from core.config_loader import load_config
        config = load_config()
        tok_config = config.get("tokenizer", {})
        tokenizer_type = tok_config.get("type", "tiktoken")
        tokenizer_path = tok_config.get("path", None)
        tokenizer_encoding = tok_config.get("encoding", "cl100k_base")
    except Exception as e:
        print(f"[Tokenizer] config読み込み失敗、デフォルト使用: {e}")

    if tokenizer_type in ("deepseek", "qwen", "gemini") and tokenizer_path:
        try:
            from transformers import PreTrainedTokenizerFast
            # tokenizer_path（例 "data/tokenizer_deepseek.json"）を data_root 基準で解決する。
            from core.paths import resolve_path
            abs_path = str(resolve_path(tokenizer_path))
            tokenizer = PreTrainedTokenizerFast(tokenizer_file=abs_path)
            _encoder = tokenizer
            print(f"[Tokenizer] {tokenizer_type} トークナイザーを使用 ({tokenizer_path})")
            return
        except Exception as e:
            print(f"[Tokenizer] {tokenizer_type} 初期化失敗、tiktokenにフォールバック: {e}")

    # フォールバック: tiktoken
    import tiktoken
    _encoder = tiktoken.get_encoding(tokenizer_encoding)
    print(f"[Tokenizer] tiktoken ({tokenizer_encoding}) を使用")


def count_text_tokens(text: str) -> int:
    """テキストのトークン数を返す"""
    if not text:
        return 0
    _init_encoder()
    if hasattr(_encoder, 'encode_ordinary'):
        # tiktoken
        return len(_encoder.encode(text))
    else:
        # transformers
        return len(_encoder.encode(text, add_special_tokens=False))


def count_message_tokens(message: dict) -> int:
    """
    1つのメッセージのトークン数を見積もる。

    OpenAI形式のメッセージ dict を受け取り、
    role + content + tool_calls のトークン数を合算する。
    メッセージのオーバーヘッド（role名等）として+4トークンを加算。
    """
    tokens = 4  # メッセージのオーバーヘッド

    content = message.get("content")
    if content:
        if isinstance(content, str):
            tokens += count_text_tokens(content)
        elif isinstance(content, list):
            # リスト形式（画像付きメッセージ）の場合
            for part in content:
                if part.get("type") == "text":
                    tokens += count_text_tokens(part.get("text", ""))
                elif part.get("type") == "image_url":
                    # 画像トークン概算（モデル依存。config の llm.vision.tokens_per_image）
                    tokens += _get_tokens_per_image()

    # ツール呼び出しが含まれる場合
    if message.get("tool_calls"):
        for tc in message["tool_calls"]:
            func = tc.get("function", {})
            tokens += count_text_tokens(func.get("name", ""))
            tokens += count_text_tokens(func.get("arguments", ""))

    return tokens


def count_messages_tokens(messages: list[dict]) -> int:
    """メッセージリスト全体のトークン数を見積もる"""
    total = 0
    for msg in messages:
        total += count_message_tokens(msg)
    return total
