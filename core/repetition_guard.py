"""
反復表現検知システム（Repetition Guard）
直近のassistant応答から繰り返しフレーズを検出し、注意喚起メッセージを生成する。
会話履歴・記憶ファイルには一切触れない。読み取りのみ。
"""

from difflib import SequenceMatcher
from core.i18n import t

try:
    import fugashi
    _tagger = fugashi.Tagger()
    _HAS_FUGASHI = True
except Exception:
    _tagger = None
    _HAS_FUGASHI = False


# 反復検知から除外するフレーズ（外部ファイルから読み込み）
def _load_exclude_words() -> set:
    """data/repetition_exclude.txt から除外ワードを読み込む（data_root 基準）

    各行は {{agent_name}} / {{user_honorific}} プレースホルダを実際の値に置換する。
    （呼称はエージェントが毎ターン口にするため、反復検知から除外したいケースが多い）
    """
    exclude = set()
    # プレースホルダ置換用の値を config から取得（初回ロード時に一度だけ）
    try:
        from core.config_loader import load_config, apply_prompt_placeholders
        _cfg = load_config()
        _agent_name = _cfg.get("profile", {}).get("agent", {}).get("name", "Assistant")
        _honorific = _cfg.get("profile", {}).get("user", {}).get("honorific", "ユーザー")
    except Exception:
        from core.config_loader import apply_prompt_placeholders
        _agent_name, _honorific = "Assistant", "ユーザー"
    try:
        from core.paths import config_file
        with open(str(config_file("repetition_exclude.txt")), "r", encoding="utf-8") as f:
            for line in f:
                word = apply_prompt_placeholders(line.strip(), _agent_name, _honorific)
                if word and not word.startswith("#"):
                    exclude.add(word)
    except FileNotFoundError:
        pass
    return exclude


# 除外ワードは「遅延ロード＋キャッシュ」する。
# モジュール import 時に読み込むと set_data_root() より前に CWD/install_root 基準で
# 確定する罠になるため、初回使用時に data_root から解決する。
# dev（data_root == install_root, CWD==agent）では従来と同一ファイル。
_EXCLUDE_WORDS_CACHE = None


def _get_exclude_words() -> set:
    global _EXCLUDE_WORDS_CACHE
    if _EXCLUDE_WORDS_CACHE is None:
        _EXCLUDE_WORDS_CACHE = _load_exclude_words()
    return _EXCLUDE_WORDS_CACHE

# 除外する品詞（助詞、助動詞、記号、接続詞等）
EXCLUDE_POS = {"助詞", "助動詞", "記号", "補助記号", "空白", "接続詞", "感動詞"}


def _extract_content_words(text: str) -> list[str]:
    """テキストから内容語（名詞・動詞・形容詞等）を抽出する"""
    if not _HAS_FUGASHI or not text:
        return []
    words = []
    for word in _tagger(text):
        pos = word.feature[0] if word.feature else ""
        if pos in EXCLUDE_POS:
            continue
        surface = word.surface.strip()
        if surface in _get_exclude_words():
            continue
        words.append(surface)
    return words


def _get_ngrams(words: list[str], n: int) -> list[tuple]:
    """単語リストからn-gramを生成する"""
    return [tuple(words[i:i+n]) for i in range(len(words) - n + 1)]


def detect_repetition(conversation_history: list[dict], lookback: int = 5, ngram_min: int = 3, ngram_max: int = 6, min_occurrences: int = 3) -> list[str]:
    """
    直近のassistantターン最終応答から反復フレーズを検出する。
    ツールループ中の中間応答は除外し、各ターンの最後のassistant発言のみを対象とする。

    Args:
        conversation_history: 会話履歴（読み取りのみ）
        lookback: 検査対象のターン最終応答数
        ngram_min: n-gramの最小長
        ngram_max: n-gramの最大長
        min_occurrences: 反復と判定する最小出現回数

    Returns:
        検出された反復フレーズのリスト
    """
    if not _HAS_FUGASHI:
        return []

    # ターンの最終assistant応答を抽出
    # 「直後がuser/systemであるassistantメッセージ」が最終応答
    final_texts = []
    for i in range(len(conversation_history) - 1):
        msg = conversation_history[i]
        next_msg = conversation_history[i + 1]
        if msg.get("role") == "assistant" and next_msg.get("role") in ("user", "system"):
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
            if content:
                final_texts.append(content)

    # 直近lookback件のみ
    final_texts = final_texts[-lookback:]

    if len(final_texts) < 2:
        return []

    # 各応答から内容語を抽出
    all_word_lists = [_extract_content_words(text) for text in final_texts]

    # n-gramカウント（どの応答に出現したか）
    ngram_response_count = {}
    for resp_idx, words in enumerate(all_word_lists):
        seen_in_this_response = set()
        for n in range(ngram_min, ngram_max + 1):
            for ngram in _get_ngrams(words, n):
                if ngram not in seen_in_this_response:
                    seen_in_this_response.add(ngram)
                    ngram_response_count[ngram] = ngram_response_count.get(ngram, 0) + 1

    # min_occurrences以上の応答に出現するn-gramを抽出
    repeated = []
    for ngram, count in ngram_response_count.items():
        if count >= min_occurrences:
            phrase = " ".join(ngram)
            repeated.append((phrase, count, len(ngram)))

    # 長いn-gramを優先し、短いn-gramが長いn-gramに含まれる場合は除外
    repeated.sort(key=lambda x: (-x[2], -x[1]))
    final_phrases = []
    for phrase, count, length in repeated:
        if not any(phrase in existing for existing in final_phrases):
            final_phrases.append(phrase)

    # 上限5フレーズ
    return final_phrases[:5]


def build_repetition_warning(conversation_history: list[dict]) -> str:
    """
    反復検知結果から注意喚起メッセージを生成する。
    反復がなければ空文字を返す。

    Args:
        conversation_history: 会話履歴（読み取りのみ）

    Returns:
        注入用メッセージ文字列（反復なしなら空文字）
    """
    phrases = detect_repetition(conversation_history)
    if not phrases:
        return ""

    print(f"[RepetitionGuard] 反復フレーズ検出（{len(phrases)}件）:")
    for phrase in phrases:
        print(f"  ・「{phrase}」")

    lines = [t("rep_header"),
             t("rep_line1"),
             t("rep_line2"),
             t("rep_line3")]
    for phrase in phrases:
        lines.append(t("rep_phrase_item", phrase=phrase))
    lines.append(t("rep_focus_line"))

    return "\n".join(lines)
