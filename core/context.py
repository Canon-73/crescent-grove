# core/context.py
"""
コンテキスト構築モジュール

役割:
    記憶ファイルと会話履歴からLLMに送るコンテキスト（messagesリスト）を構築する。
    トークン上限を監視し、必要に応じて会話履歴を圧縮する。
    エージェントの「ワーキングメモリ」に相当する。

システムプロンプトの配置:
    LLMは入力の最初と最後に注意を強く払う傾向がある（Primacy-Recency効果）。
    この特性を利用し、重要な指示を最上部と最下部に配置する。

    [system] system_prompts（最上部 - 人格定義、ツール説明、記憶保護ルール、リマインダー等）
    [system] 起動時記憶ファイル群（IDENTITY.md, SOUL.md, USER.md, MEMORY.md, compressed.md, letter_for_me.md 等）
    [system] 会話要約（layer2: 圧縮要約 → layer1: 詳細要約、の順で配置）
    [user/assistant/tool] 会話履歴（直近のやりとり）
    [system] post_prompts（真・最下部 - BOTTOM_PROMPT.md 等。post_prompts.enabled時のみ）
    [assistant] DeepSeek prefill（該当する場合のみ）

    ※ post_prompts は会話履歴の後（真・最下部）に置くエキスパート向け機能。
      末尾systemメッセージに非対応のプロバイダー（Anthropic等）ではAPIエラーになる点に注意。
      またこの領域はプロンプトキャッシュ対象外のため、長文は避けること。

コンテキスト圧縮（二段階要約構造）:
    トークン数が上限の一定割合（compression_threshold）を超えたら、
    古い会話履歴をLLMにターン単位で要約させ、layer1に格納する。
    直近の数往復（keep_recent_exchanges）は圧縮せず残す。
    layer1がさらに上限を超えたら、古い部分をlayer2に再圧縮する。
    圧縮の実行はagent.pyが担当し、本モジュールはデータ構造の管理を担う。

ユーザーメッセージの構造:
    各ユーザーメッセージには以下の情報が自動注入される:
    - [SYSTEM] 現在時刻（祝日含む）/ 天気 / コンテキスト使用率
    - <user_message> / <moonbeat_instruction> / <system_notice>（メッセージ種別に応じたタグ）
    - <self_memo>（workspace/memory/user_memo.md の先頭N文字、設定で有効化）
    - <assistant_inner>（MoodPhase・Desire等から生成された内面状態プロンプト）
"""

from core.time_utils import tlog
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from memory.manager import MemoryManager
from core.tokens import count_messages_tokens, count_message_tokens, count_text_tokens
from core.tools import get_tool_definitions
from core.i18n import t
import json
from core.weather import get_weather_string


# 日本標準時（時刻表示・ログ記録に使用）
JST = timezone(timedelta(hours=9))


def _load_prompt_files(config_section: dict) -> str:
    """
    config.yaml のプロンプトセクションで指定されたファイル群を読み込んで結合する。

    system_prompts（最上部プロンプト）と post_prompts（最下部プロンプト）の
    両方で使われる共通関数。

    Args:
        config_section: {"directory": "ディレクトリパス", "files": ["file1.md", "file2.md", ...]}

    Returns:
        全ファイルの内容を改行2つで結合した文字列。設定がない場合は空文字。
    """
    if not config_section:
        return ""

    # ディレクトリの基準点を CWD から data_root に変更（配布対応）。
    # 設定値が絶対パスならそのまま使い、相対パスなら data_root 基準で解決する。
    # 引数なし dev では data_root() == agent ディレクトリ == 通常の起動 CWD のため従来と同一。
    dir_value = config_section.get("directory", "")
    dir_path = Path(dir_value)
    if dir_path.is_absolute():
        directory = dir_path
    else:
        from core.paths import data_root
        directory = data_root() / dir_value
    files = config_section.get("files", [])

    if not directory.exists():
        print(f"警告: プロンプトディレクトリが見つかりません: {directory}")
        return ""

    parts = []
    for filename in files:
        filepath = directory / filename
        if filepath.exists():
            parts.append(filepath.read_text(encoding="utf-8"))
        else:
            print(f"警告: プロンプトファイルが見つかりません: {filepath}")

    return "\n\n".join(parts)


class ContextBuilder:
    """
    LLMに送るコンテキスト（メッセージリスト）を構築・管理するクラス。

    システムプロンプト、起動時記憶、二段階要約、会話履歴を統合し、
    build_messages()でLLM API呼び出し用のメッセージリストを生成する。
    トークン数の監視、圧縮の判定、状態の永続化も担う。
    """

    def __init__(self, memory_manager: MemoryManager, config: dict):
        """
        コンテキストビルダーを初期化する。

        Args:
            memory_manager: 記憶管理インスタンス（ワークスペース内ファイルの読み書き）
            config: config.yaml + settings.json のマージ済み設定辞書
        """
        self.memory = memory_manager
        self.config = config

        # --- コンテキスト管理の設定値 ---
        ctx_config = config.get("context", {})
        self.max_tokens = ctx_config.get("max_tokens", 65536)                  # コンテキストウィンドウの上限トークン数
        self.compression_threshold = ctx_config.get("compression_threshold", 0.70)  # 圧縮を開始する使用率（定期・force_compress用）
        self.emergency_compression_threshold = ctx_config.get("emergency_compression_threshold", 0.90)  # 緊急圧縮の使用率（_compress_if_needed用）
        self.keep_recent_exchanges = ctx_config.get("keep_recent_exchanges", 4)     # 圧縮時に残す直近の往復数
        self.keep_recent_images = ctx_config.get("keep_recent_images", 4)           # 履歴に保持する直近の画像枚数（保存時・送信時の両方に適用）

        # システムプロンプト・起動時記憶を構築
        self._rebuild_all()

        # 会話履歴（user/assistant/toolメッセージのリスト）
        self.conversation_history: list[dict] = []

        # --- 二段階要約構造（v1。summary_v2 稼働中は読み書きしないがロールバック用に保持） ---
        self.summary_layer1: str = ""   # 詳細要約（ターン単位1行要約の蓄積、新しい）
        self.summary_layer2: str = ""   # 圧縮要約（layer1の再要約、古い）
        # summary_v2 へ切り替えたときに旧要約を移す退避欄。コンテキストには出さない。
        # ロールバック（v2 を止める）で元の欄へ戻す。データは消さない
        self.summary_layer1_archived: str = ""
        self.summary_layer2_archived: str = ""

        # --- summary_v2 のビュー（agent.py が set_summary_view() で差し込む） ---
        # 描画済みテキストのみを保持する。DB本体は core/summary_v2.SummaryStore が持つ。
        self._summary_view: str = ""

        # コンテキスト状態の永続化ファイルパス（set_state_path()で設定）
        self._state_path = None

        # agent.pyから注入される次ターン用のバイタルプロンプト
        self._pending_vital_prompt = ""

        # DeepSeek prefill用バッファ（/beta API使用時にassistantメッセージの先頭に挿入）
        self._pending_prefill = ""

        # ツール定義のトークン数を事前計算（LLMに毎回送信される隠れコスト）
        # 生成ツール（manifest 昇格分）も含めるため get_tool_definitions() を通す
        tools_json = json.dumps(get_tool_definitions(), ensure_ascii=False)
        self.tools_tokens = count_text_tokens(tools_json)

    def _rebuild_all(self):
        """システムプロンプトと起動時記憶を（再）構築する。reload_memories()から呼ばれる。"""
        # 最上部プロンプト: TOP_PROMPT.md, TOOL_INSTRUCTIONS.md, SAFETY_PROMPT.md 等
        top_prompts = _load_prompt_files(self.config.get("system_prompts"))

        self.system_top = top_prompts

        # 最下部プロンプト（BOTTOM_PROMPT.md 等）: post_prompts.enabled が True のときだけ、
        # 会話履歴の後（真・最下部）に配置する。エキスパート向け機能。
        # 注意:
        #   - 末尾systemメッセージに非対応のプロバイダー（Anthropic等）ではAPIエラーになる。
        #   - この領域はプロンプトキャッシュ対象外のため、長文を入れると毎ターン課金・遅延が増える。
        # 実際の挿入は build_messages() で履歴の後・prefillの前に毎ターン行う（履歴には保存しない）。
        post_config = self.config.get("post_prompts") or {}
        if post_config.get("enabled", False):
            self.system_bottom = _load_prompt_files(post_config)
        else:
            self.system_bottom = ""

        # 起動時記憶: IDENTITY.md, SOUL.md, USER.md, MEMORY.md, compressed.md, letter_for_me.md 等
        boot_files = self.config.get("boot_memories", [])
        self.system_memories = self.memory.load_boot_memories(boot_files) if boot_files else ""

        # 全プロンプト共通のプレースホルダ（{{agent_name}} / {{user_honorific}}）を実際の値に置換する。
        # 値は settings.json優先・config.yamlフォールバック（load_config()でマージ済み）。
        from core.config_loader import apply_prompt_placeholders
        agent_name = self.config.get("profile", {}).get("agent", {}).get("name", "Assistant")
        honorific = self.config.get("profile", {}).get("user", {}).get("honorific", "ユーザー")
        self.system_top = apply_prompt_placeholders(self.system_top, agent_name, honorific)
        self.system_memories = apply_prompt_placeholders(self.system_memories, agent_name, honorific)

    def _get_context_tz(self):
        """コンテキストに注入する時刻表示用の (tzinfo, ラベル) を設定から返す。

        一般設定の time.tz_offset（UTCからの時差・時間単位の小数可）と
        time.tz_label（"JST" 等の表示札）に従う。未設定・不正値は JST(+9) に丸める。
        オフセットは常識的な範囲（UTC-12〜+14）にクランプする。

        ※ ここで切り替わるのは「コンテキストに入る時刻表示」だけ。
          ログ記録や論理日付（午前3時境界）は従来どおり time_utils.JST のまま。
        毎ターン self.config から読むため、設定保存後は再起動なしで反映される。
        """
        time_cfg = self.config.get("time", {}) or {}
        try:
            offset = float(time_cfg.get("tz_offset", 9))
        except (TypeError, ValueError):
            offset = 9.0
        offset = max(-12.0, min(14.0, offset))
        label = str(time_cfg.get("tz_label", "JST")).strip() or "JST"
        return timezone(timedelta(hours=offset)), label

    def _get_current_time_str(self):
        """現在時刻を日本語フォーマットの文字列で返す（祝日情報付き）。

        タイムゾーンは一般設定（time.tz_offset / time.tz_label）に従う。既定は JST(+9)。
        """
        tz, label = self._get_context_tz()
        now = datetime.now(tz)
        # 曜日名は言語別（ja_short: 月..日 / en_short: Mon..Sun）。i18n キーで引く。
        weekday_keys = ["ctx_wd_mon", "ctx_wd_tue", "ctx_wd_wed", "ctx_wd_thu",
                        "ctx_wd_fri", "ctx_wd_sat", "ctx_wd_sun"]
        weekday = t(weekday_keys[now.weekday()])
        # 日付フォーマットも言語別（ja: %Y年%m月%d日（曜）… / en: %Y-%m-%d (曜)…）。
        fmt = t("ctx_datetime_format").replace("{weekday}", weekday).replace("{label}", label)
        date_str = now.strftime(fmt)

        # 祝日チェック（日本の祝日。該当すれば末尾に追記）。_check_holiday は i18n キーを返す。
        holiday_key = self._check_holiday(now)
        if holiday_key:
            date_str += t("ctx_holiday_prefix", holiday=t(holiday_key))

        return date_str
    
    def _check_holiday(self, dt):
        """指定日時が日本の祝日であれば i18n キーを返す。該当しなければNone。
        （表示名は呼び出し側で t() により言語別に解決する）"""
        m, d = dt.month, dt.day

        # 固定日の祝日
        fixed = {
            (1,1): "ctx_hol_new_year", (2,11): "ctx_hol_foundation", (2,23): "ctx_hol_emperor_bday",
            (4,29): "ctx_hol_showa", (5,3): "ctx_hol_constitution", (5,4): "ctx_hol_greenery",
            (5,5): "ctx_hol_childrens", (8,11): "ctx_hol_mountain", (11,3): "ctx_hol_culture",
            (11,23): "ctx_hol_labor_thanks",
        }
        if (m,d) in fixed:
            return fixed[(m,d)]

        # ハッピーマンデー制度による月曜固定の祝日
        w = dt.weekday()
        week_num = (d - 1) // 7 + 1
        if w == 0:  # 月曜日
            if m == 1 and week_num == 2: return "ctx_hol_coming_of_age"
            if m == 7 and week_num == 3: return "ctx_hol_marine"
            if m == 9 and week_num == 3: return "ctx_hol_respect_aged"
            if m == 10 and week_num == 2: return "ctx_hol_sports"

        # 春分・秋分（年によって変動するが、概ねこの日付範囲）
        if m == 3 and d in (20, 21): return "ctx_hol_vernal_equinox"
        if m == 9 and d in (22, 23): return "ctx_hol_autumnal_equinox"

        return None

    @staticmethod
    def _get_text_from_content(content) -> str:
        """メッセージのcontent（文字列またはリスト形式）からテキスト部分を抽出して返す。"""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # マルチモーダルメッセージ（画像+テキスト）からtextパートのみ抽出
            return "\n".join(
                part.get("text", "") for part in content if part.get("type") == "text"
            )
        return str(content)

    # 本文末尾に付加される独立ブロック系タグ（flashback/tips/note_fragment等）。
    # これらは本文タグ（<user_message>/<moonbeat_instruction>）の外へ出して入れ子を防ぐ。
    _APPENDIX_TAGS = ("flashback", "tips", "note_fragment")

    def _extract_appendix_blocks(self, content: str):
        """
        本文から付加ブロック系タグを抽出し、(本文, [付加ブロック...]) を返す。

        付加ブロックを本文タグの内側に残すと <moonbeat_instruction> 内に <tips> が
        入れ子になってしまうため、抽出してシブリング（兄弟要素）として並べる。

        Args:
            content: 元のメッセージ本文（末尾に付加ブロックが連結されている場合がある）

        Returns:
            (clean_content, appendices): 付加ブロックを除いた本文と、抽出した各ブロックのリスト
        """
        import re
        pattern = re.compile(
            r'<(' + '|'.join(self._APPENDIX_TAGS) + r')>.*?</\1>',
            re.DOTALL,
        )
        appendices = [m.group(0) for m in pattern.finditer(content)]
        clean_content = pattern.sub('', content).strip()
        return clean_content, appendices

    def add_user_message(self, content: str, image: str = None, msg_type: str = "user",
                         images: list = None, files: list = None):
        """
        ユーザーメッセージを構造化して会話履歴に追加する。

        時刻・天気・コンテキスト使用率・self_memo・バイタルプロンプトを
        自動注入し、メッセージ種別に応じたXMLタグで囲む。

        Args:
            content: ユーザーのテキスト
            image: 画像データURL（data:image/...;base64,...）またはNone（後方互換の単一画像）
            msg_type: "user" = ユーザー発言, "system" = システム通知, "moonbeat" = Moonbeat自発思考
            images: 画像データURLのリスト（複数画像添付。image と併用された場合は両方積む）
            files: 添付テキストファイルのリスト。各要素は {"name": ファイル名, "content": 中身} の dict。
                   中身は <attached_file> タグで囲んで本文に注入する（モダリティではなく文字として渡す）
        """
        # メッセージの各パーツを組み立てる
        parts = [self._build_system_header()]
        parts.extend(self._build_typed_body(content, msg_type))
        parts.extend(self._build_attached_file_parts(files))

        # 添付画像を正規化してまとめる
        image_entries = self._collect_image_entries(image, images)

        # 画像認識対応フラグ（設定→LLM）。未設定なら対応扱い＝従来挙動。
        # 非対応モデルに画像データ（image_url）を送るとエラーになるため、
        # falseのときは画像をモダリティとして送らず、ファイル名のみ本文で伝える。
        supports_images = self.config.get("llm", {}).get("supports_images", True)
        parts.extend(self._build_attached_image_parts(image_entries, supports_images))
        parts.extend(self._build_vital_prompt_parts())

        full_text = "\n\n".join(parts)
        msg_content = self._build_message_content(full_text, image_entries, supports_images)

        self.conversation_history.append({
            "role": "user",
            "content": msg_content,
        })

    def _build_system_header(self) -> str:
        """[SYSTEM]ヘッダー（時刻・天気・コンテキスト使用率）を組み立てる。"""
        from core.weather import get_weather_string
        time_str = self._get_current_time_str()
        weather = get_weather_string()

        # コンテキスト使用率の計算（エージェントが残量を意識できるよう注入する）
        current_tokens = self.get_token_count()
        max_tokens = self.max_tokens
        usage_pct = round(current_tokens / max_tokens * 100) if max_tokens > 0 else 0
        return f"[SYSTEM]\n{time_str}\n{weather}\n" + t("ctx_usage", cur=f"{current_tokens:,}", max=f"{max_tokens:,}", pct=usage_pct)

    # メッセージ種別 → 本文を囲むXMLタグ名。ここに無い種別は system_notice 扱い。
    _MSG_TYPE_TAGS = {
        "user": "user_message",
        "moonbeat": "moonbeat_instruction",
        "task": "task_notice",
        "city_event": "city_event_notice",
        # 外部サービスから柚月宛てに届いたもの（X の返信・将来の Discord / DM 等）。
        # **発信元はタグではなく本文の先頭で名乗る**（"[X] @someone から返信" の形）。
        # サービスが増えるたびにタグを足すと core を触り続けることになるため、
        # ここは1つに保つこと。city_event は先行事例だが、柚月の履歴に既に
        # "city_event:" として大量に残っているので移行しない（過去と食い違う）。
        "external": "external_notice",
    }

    def _build_typed_body(self, content: str, msg_type: str) -> list:
        """メッセージ種別に応じたタグで本文を囲み、パーツのリストを返す。

        _MSG_TYPE_TAGS にある種別は、末尾付加ブロック（flashback/tips/note_fragment等）を
        抽出して本文タグの外へ出す（入れ子防止）。user/moonbeat には self_memo
        （エージェントの自由メモ）も毎ターン注入する。
        """
        tag = self._MSG_TYPE_TAGS.get(msg_type)
        if tag is None:
            # その他のシステム通知
            return [f"<system_notice>\n{content}\n</system_notice>"]

        clean_content, appendices = self._extract_appendix_blocks(content)
        parts = [f"<{tag}>\n{clean_content}\n</{tag}>"] + appendices
        if msg_type in ("user", "moonbeat"):
            memo = self._load_self_memo()
            if memo:
                parts.append(memo)
        return parts

    def _build_attached_file_parts(self, files: list) -> list:
        """添付テキストファイルを <attached_file> タグのパーツ群に変換する。

        テキストは画像のようなモダリティではなく「ただの文字」なので、本文に
        タグで囲んで積む（どのLLMでも読めるようにするため）。
        """
        parts = []
        if files:
            for f in files:
                fname = str(f.get("name", "file"))
                fbody = f.get("content", "")
                parts.append(f'<attached_file name="{fname}">\n{fbody}\n</attached_file>')
        return parts

    def _collect_image_entries(self, image, images) -> list:
        """添付画像を {"name", "url"} の dict リストに正規化する。

        後方互換で3形態を受ける:
          - 単一 image（文字列URL）
          - images の各要素が文字列URL
          - images の各要素が {"name", "url"} の dict（ファイル名つき）
        """
        raw_entries = []
        if image:
            raw_entries.append({"name": None, "url": image})
        if images:
            for it in images:
                if isinstance(it, dict):
                    raw_entries.append({"name": it.get("name"), "url": it.get("url"),
                                        "source": it.get("source"),
                                        "context": it.get("context"),
                                        "from_flashback": it.get("from_flashback")})
                else:
                    raw_entries.append({"name": None, "url": it})

        # 1. 原本を workspace/received/ に保存して「出所」を与える。
        #    これが無いと、履歴から画像が落ちた後に見直すことも、region で拡大することも
        #    できない（see_image に渡せるものが何も残らない）。原本を保存するのは、
        #    縮小後を保存すると拡大しても細部が出てこないため。
        # 2. 送る側は縮小・JPEG 化する。スマホ写真は 1 枚数 MB あり、
        #    履歴に残る限り毎回送り直されるため。
        from core.image_norm import normalize_data_url, save_received_image, remember_source
        from core import picture_memory
        workspace = str(self.memory.workspace)
        image_entries = []
        # このターンで見た絵の出所を agent が拾えるよう置いておく
        # （ターン終了時に「そのとき思ったこと」を結びつけるため）
        self._last_saved_image_sources = []
        for i, ent in enumerate(raw_entries, 1):
            # 既に出所を持つ絵（フラッシュバックで浮かんだ絵など）は保存し直さない
            saved = ent.get("source") or save_received_image(ent["url"], workspace, i)
            if saved:
                remember_source(saved)
                self._last_saved_image_sources.append(saved)
                picture_memory.record_view(
                    workspace, saved,
                    # カノンが見せた絵は柚月が選んで見たものではないが、届いた時点で
                    # 「見た」ことに変わりはないので数える。フラッシュバック（こちらから
                    # 見せ返す絵）だけを deliberate=False にして重みから外す。
                    deliberate=not ent.get("from_flashback"),
                    context=ent.get("context") or "chat")
            image_entries.append({
                # 名前は <attached_images> に出る。保存できたら相対パスにして、
                # 柚月が see_image でそのまま開き直せるようにする。
                "name": saved or ent.get("name"),
                "url": normalize_data_url(ent["url"]),
            })
        return image_entries

    def _build_attached_image_parts(self, image_entries: list, supports_images: bool) -> list:
        """<attached_images> パーツを組み立てる。

        画像の image_url にはファイル名が乗らないため、本文側に名前を明記して伝える
        （何枚目がどのファイルか AI が対応づけられるよう、画像の並び順と揃える）。
        supports_images が false の場合は画像を送らないので、見られないことを明示して
        AIが正直に答えられるようにする。
        """
        if not image_entries:
            return []
        name_lines = []
        for i, ent in enumerate(image_entries, 1):
            nm = ent.get("name") or t("ctx_image_default", i=i)
            name_lines.append(f"{i}. {nm}")
        if supports_images:
            return ["<attached_images>\n" + "\n".join(name_lines) + "\n</attached_images>"]
        return ['<attached_images note="' + t("ctx_image_unsupported_note") + '">\n'
                + "\n".join(name_lines) + "\n</attached_images>"]

    def _build_vital_prompt_parts(self) -> list:
        """バイタルプロンプト（MoodPhase・Desire等の内面状態）のパーツを組み立てる。

        DeepSeek /beta API の場合は prefill として assistant メッセージの先頭に挿入するため、
        本文パーツには積まず _pending_prefill に退避する。いずれの場合も
        _pending_vital_prompt はここで消費（クリア）される。
        """
        if not self._pending_vital_prompt:
            return []
        parts = []
        base_url = self.config.get("llm", {}).get("base_url", "")
        if "/beta" in base_url:
            # DeepSeek /beta API: prefillとしてassistantメッセージの先頭に挿入
            self._pending_prefill = f"<assistant_inner>\n{self._pending_vital_prompt}\n</assistant_inner>"
        else:
            # 通常: userメッセージの末尾に追加
            parts.append(f"<assistant_inner>\n{self._pending_vital_prompt}\n</assistant_inner>")
        self._pending_vital_prompt = ""
        return parts

    def _build_message_content(self, full_text: str, image_entries: list, supports_images: bool):
        """本文テキストと画像から content を構築する。

        画像付きの場合はOpenAI互換のマルチモーダル形式（リスト形式content）にし、
        画像が複数あれば image_url を並べて積む（<attached_images> の並び順と一致させる）。
        supports_images が false のモデルには image_url を載せない（エラー回避）。
        """
        if image_entries and supports_images:
            msg_content = [{"type": "text", "text": full_text}]
            for ent in image_entries:
                if ent.get("url"):
                    msg_content.append({"type": "image_url", "image_url": {"url": ent["url"]}})
            return msg_content
        return full_text

    def _load_self_memo(self) -> str:
        """エージェントの自由メモ（user_memo.md）を読み込み、<self_memo>タグで囲んで返す。無効・未設定時は空文字。"""
        try:
            import json as _json
            import os as _os

            # 設定ファイルからself_memoの有効化状態・ファイルパス・文字数上限を読み込む（config/ 基準）
            from core.paths import config_file
            memo_config_path = str(config_file("user_memo_config.json"))
            with open(memo_config_path, 'r', encoding='utf-8') as f:
                memo_config = _json.load(f)
            if not memo_config.get('enabled', False):
                return ""
            memo_file = memo_config.get('file', 'memory/user_memo.md')
            max_chars = memo_config.get('max_chars', 200)

            # ワークスペース内のメモファイルを読み込む
            memo_path = _os.path.join(str(self.memory.workspace), memo_file)
            if _os.path.exists(memo_path):
                with open(memo_path, 'r', encoding='utf-8') as f:
                    memo_text = f.read().strip()
                if memo_text:
                    return f"<self_memo>\n{memo_text[:max_chars]}\n</self_memo>"
        except Exception:
            pass
        return ""
      
    def add_system_notice(self, content: str):
        """システム内部通知を会話履歴に追加する。時刻・天気ヘッダーなしの軽量な通知用。"""
        self.conversation_history.append({
            "role": "user",
            "content": f"<system_notice>\n{content}\n</system_notice>",
        })

    def add_assistant_message(self, content: str):
        """エージェントのテキスト応答を会話履歴に追加する。"""
        self.conversation_history.append({
            "role": "assistant",
            "content": content,
        })

    def add_tool_call(self, assistant_message: dict):
        """ツール呼び出しを含むアシスタントメッセージを会話履歴に追加する。"""
        self.conversation_history.append(assistant_message)

    def add_tool_result(self, tool_call_id: str, result: str, system_notice: str = ""):
        """ツール実行結果をtoolロールのメッセージとして会話履歴に追加する。
        
        system_noticeが指定された場合、ツール結果の末尾に<system_notice>タグで追記する。
        userロールを挟まずにシステム通知を注入できるため、ログが綺麗に保たれる。
        """
        if system_notice:
            content = f"{result}\n\n<system_notice>\n{system_notice}\n</system_notice>"
        else:
            content = result
        self.conversation_history.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        })
        
    def add_tool_result_with_image(self, tool_call_id: str, content: list):
        """画像データを含むツール実行結果を会話履歴に追加する。"""
        self.conversation_history.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        })

    def trim_large_tool_results(self, max_chars: int = 1500) -> list[dict]:
        """
        会話履歴内の大きなツール結果をプレビュー版に置換し、元の全文をRAG退避用データとして返す。

        max_charsを超えるツール結果を検出し、以下を行う:
        1. 元の全文とメタデータをRAG格納用辞書として収集
        2. 会話履歴内の該当メッセージをプレビュー（先頭部分+「RAGに保存済み」表示）に置換
        
        ファイル操作系ツール（read_file等）とcitronエディタはトリミング対象外。

        Returns:
            RAGに格納すべきデータのリスト: [{"document": str, "metadata": dict}, ...]
        """
        trimmed = []
        
        for msg in self.conversation_history:
            if msg.get("role") != "tool":
                continue
            content = msg.get("content", "")
            
            # リスト形式content（画像付き）はスキップ
            if not isinstance(content, str):
                continue
            
            # max_chars以下はトリミング不要
            if len(content) <= max_chars:
                continue
            
            # ツール名と引数を特定するため、対応するassistantメッセージのtool_callsを検索
            tool_name = "unknown"
            tool_args = {}
            tool_call_id = msg.get("tool_call_id", "")
            for prev in self.conversation_history:
                if prev.get("role") != "assistant":
                    continue
                for tc in prev.get("tool_calls", []):
                    if tc.get("id") == tool_call_id:
                        tool_name = tc.get("function", {}).get("name", "unknown")
                        try:
                            import json
                            tool_args = json.loads(tc.get("function", {}).get("arguments", "{}"))
                        except (json.JSONDecodeError, TypeError):
                            tool_args = {}
                        break
                      
            # ファイル操作系ツールはトリミング対象外（編集中のファイル内容を失わないため）
            SKIP_TOOLS = {"read_file", "edit_file", "write_file", "replace_file", "read_secret", "edit_secret", "write_secret"}
            if tool_name in SKIP_TOOLS:
                continue
            # run_programのcitronエディタもスキップ（ファイル編集ツールのため）
            if tool_name == "run_program" and tool_args.get("app_name") == "citron_ai_text_editor":
                continue
          
            # RAG格納用のデータを作成
            from datetime import datetime
            trimmed.append({
                "document": content,
                "metadata": {
                    "type": "tool_result",
                    "tool_name": tool_name,
                    "args": str(tool_args)[:200],
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "original_length": len(content),
                }
            })
            
            # --- プレビュー生成: 会話履歴内のメッセージを短縮版に置換 ---
            import re as _re
            preview = ""
            # JSON内のtitleフィールドがあればリスト表示（検索結果等）
            titles = _re.findall(r'"title"\s*:\s*"([^"]+)"', content)
            if titles:
                preview = "\n".join(f"・{t}" for t in titles[:10])
            else:
                # それ以外は先頭500文字を句点で区切って表示
                cleaned = _re.sub(r'---\s*以下は外部コンテンツ.*?---', '', content, flags=_re.DOTALL).strip()
                preview = cleaned[:500].rsplit("。", 1)[0] + "。" if "。" in cleaned[:500] else cleaned[:500]
            
            # ラベル生成（ツール名と主要な引数を表示）
            arg_hint = tool_args.get("query") or tool_args.get("path") or tool_args.get("source") or tool_args.get("url") or tool_args.get("app_name") or ""
            label = f"[{tool_name}" + (f': "{arg_hint}"' if arg_hint else "") + "]"
            msg["content"] = f"{label}\n{preview}\n（…全文はRAGに保存済み）"            
            tlog(f"[ToolTrim] {tool_name}: {len(content)}文字 → {len(msg['content'])}文字 (RAG退避)")
        
        return trimmed
    
    def inject_system_internal(self, vital_prompt: str = ""):
        """
        動的情報（時刻・天気・バイタル・反復表現警告）をsystemメッセージとして会話履歴に追加する。

        各ターンの最初に1回だけ呼ぶこと。
        履歴に保存することでプロンプトキャッシュ（プレフィックスキャッシュ）が維持される。
        """
        time_str = self._get_current_time_str()
        parts = []
        parts.append(
            f"Current time: {time_str}\n{get_weather_string()}"
        )
        if vital_prompt:
            parts.append(vital_prompt)

        # 反復表現検知: 直近の応答で同じフレーズが繰り返されていたら警告を注入
        from core.repetition_guard import build_repetition_warning
        rep_warning = build_repetition_warning(self.conversation_history)
        if rep_warning:
            parts.append(rep_warning)            
        self.conversation_history.append({
            "role": "system",
            "content": "\n\n".join(parts),
        })

    def build_messages(self) -> list[dict]:
        """
        LLMに送信するメッセージリストを構築して返す。

        構成順序:
        1. system_top（システムプロンプト）
        2. system_memories（起動時記憶ファイル群）
        3. 会話要約（layer2 → layer1 の順。古い順に配置）
        4. conversation_history（直近の会話履歴）
        5. system_bottom（ポストプロンプト。post_prompts.enabled時のみ・真・最下部）
        6. DeepSeek prefill（/beta API使用時のみ）
        """
        messages = []

        # 最上部: システムプロンプト（人格定義・ツール説明・安全制約・リマインダー等）
        messages.append({"role": "system", "content": self.system_top})

        # 起動時記憶（IDENTITY.md, SOUL.md, USER.md 等。ツール実行後にreload_memories()で最新化される）
        if self.system_memories:
            messages.append({"role": "system", "content": self.system_memories})

        # 会話要約を挿入
        # summary_v2 のビュー（日付見出し付き・会話履歴より古い日だけ）が古い側、
        # v1 の layer2/layer1 が新しい側。両方あるときは必ず両方載せる。
        #
        # v2 稼働中でも v1 の layer1 が空とは限らない。日単位で圧縮できない状況では
        # 緊急圧縮が v1 経路へ退避し、直近ターンの要約を layer1 に書く。
        # ここでビューを優先して layer1 を捨てると、その要約が柚月に永久に届かず、
        # 履歴からは消えているので実質的な記憶の欠落になる。
        summary_content = self._build_summary_content()
        if summary_content:
            messages.append({"role": "system", "content": summary_content})

        # 会話履歴（user/assistant/toolメッセージ）
        # messages.extend(self.conversation_history)
        history = self._filter_conversation_history()
        messages.extend(history)

        # ポストプロンプト（BOTTOM_PROMPT.md 等）を真・最下部に配置する。
        # post_prompts.enabled が True のときのみ system_bottom に値が入る（_rebuild_all参照）。
        # 履歴には保存せず毎ターンここで付加するため、常に生成直前に1個だけ存在する。
        if self.system_bottom:
            messages.append({"role": "system", "content": self.system_bottom})

        # DeepSeek /beta API用: assistantメッセージのprefillを末尾に追加
        if self._pending_prefill:
            messages.append({"role": "assistant", "content": self._pending_prefill, "prefix": True})
        return messages
    
    def _build_summary_content(self) -> str:
        """会話要約 system メッセージの本文を組み立てて返す（要約が無ければ空文字）。

        build_messages（送信）と get_token_breakdown（計測）の両方がここを通ることで、
        送った内容と数えた内容が必ず一致する。
        """
        # summary_v2 のビューはここには入れない（2026-09-07）。ビューは workspace/memory/layer1.md に
        # 書き出され、柚月に読ませるかは config.yaml の boot_memories が決める（LETHE の compressed.md と
        # 同じ流儀）。ここに残るのは旧方式の要約と、v2 が失敗したときの退避先（v1 の緊急圧縮）だけ。
        v1_summary = ""
        if self.summary_layer2:
            v1_summary += self.summary_layer2
        if self.summary_layer1:
            if v1_summary:
                v1_summary += "\n\n"
            v1_summary += self.summary_layer1
        if v1_summary:
            return t("ctx_summary_heading") + "\n" + v1_summary
        return ""

    def get_token_count(self) -> int:
        """現在のコンテキスト全体のトークン数を返す（メッセージ + ツール定義分）。"""
        msg_tokens = count_messages_tokens(self.build_messages())
        return msg_tokens + self.tools_tokens

    def get_token_breakdown(self) -> dict:
        """
        LLM へ送るコンテキストを構成要素ごとに実測して返す（UI の体調タブ用）。

        どの項目も逆算しない。build_messages と同じ材料を count_message_tokens で数えるので、
        各項目の和 total は get_token_count() と一致する。

        - system:       TOP プロンプト＋起動時記憶＋BOTTOM プロンプト。**会話要約は含まない**
                        （以前は要約 system メッセージまで System に足していたため、Layer1/2 が
                        二重に数えられ、逆算していた Raw がその分だけ小さく出ていた）
        - tools:        ツール定義
        - raw:          会話履歴のうち Layer0 未圧縮のメッセージ（tool 呼び出し・結果も含む）
        - layer0:       Layer0 圧縮済みペア（<!-- layer0 --> 付き user と、それに続くメッセージ）
        - layer1/2:     v1 要約の本文
        - summary_view: summary_v2 のビュー本文
        - other:        要約メッセージの見出し・区切り・メッセージ overhead と DeepSeek prefill
                        （数トークン〜十数トークン）
        """
        system = count_message_tokens({"role": "system", "content": self.system_top})
        if self.system_memories:
            system += count_message_tokens({"role": "system", "content": self.system_memories})
        if self.system_bottom:
            system += count_message_tokens({"role": "system", "content": self.system_bottom})

        layer1 = self.get_layer1_token_count()
        layer2 = self.get_layer2_token_count()
        summary_view = self.get_summary_view_token_count()
        other = 0
        summary_content = self._build_summary_content()
        if summary_content:
            other += count_message_tokens({"role": "system", "content": summary_content}) \
                     - layer1 - layer2 - summary_view
        if self._pending_prefill:
            other += count_message_tokens(
                {"role": "assistant", "content": self._pending_prefill, "prefix": True})

        # 会話履歴: user メッセージのマーカーで「その user から次の user の手前まで」の帰属を決める。
        # Layer0 済みターンは user + assistant の2件に凝縮されているので、
        # マーカー付き user に続く assistant は Layer0 側、それ以外は全て Raw 側。
        raw = 0
        layer0 = 0
        layer0_turns = 0
        in_layer0 = False
        for m in self._filter_conversation_history():
            if m.get("role") == "user":
                in_layer0 = "<!-- layer0 -->" in self._get_text_from_content(m.get("content", ""))
                if in_layer0:
                    layer0_turns += 1
            n = count_message_tokens(m)
            if in_layer0:
                layer0 += n
            else:
                raw += n

        tools = self.tools_tokens
        return {
            "system": system,
            "tools": tools,
            "raw": raw,
            "raw_turns": self.count_uncompressed_turns(),
            "layer0": layer0,
            "layer0_turns": layer0_turns,
            "layer1": layer1,
            "layer2": layer2,
            "summary_view": summary_view,
            "other": other,
            "total": system + tools + raw + layer0 + layer1 + layer2 + summary_view + other,
        }

    def get_token_usage(self) -> dict:
        """
        トークン使用状況を辞書で返す（UI表示・判定用）。

        used は get_token_breakdown() の合計（= get_token_count() と同値）。
        内訳（system / tools / raw / layer0 / layer1 / layer2 / summary_view / other）も
        そのまま同梱し、UI 側で引き算をさせない。

        Returns:
            {"used": 現在のトークン数, "max": 上限トークン数, "ratio": 使用率（0.0〜1.0）, ...内訳}
        """
        b = self.get_token_breakdown()
        used = b["total"]
        return {
            "used": used,
            "max": self.max_tokens,
            "ratio": used / self.max_tokens if self.max_tokens > 0 else 0,
            "system": b["system"],
            "tools": b["tools"],
            "raw": b["raw"],
            "raw_turns": b["raw_turns"],
            "layer0": b["layer0"],
            "layer0_turns": b["layer0_turns"],
            "layer1": b["layer1"],
            "layer2": b["layer2"],
            "summary_view": b["summary_view"],
            "other": b["other"],
        }

    def needs_compression(self) -> bool:
        """コンテキストのトークン使用率が compression_threshold を超えているか返す（定期圧縮・force_compress用）。"""
        usage = self.get_token_usage()
        return usage["ratio"] >= self.compression_threshold

    def needs_emergency_compression(self) -> bool:
        """コンテキストのトークン使用率が emergency_compression_threshold を超えているか返す（緊急圧縮用）。"""
        usage = self.get_token_usage()
        return usage["ratio"] >= self.emergency_compression_threshold

    def build_compression_messages(self, keep_exchanges: Optional[int] = None,
                                    count: Optional[int] = None) -> tuple[list[dict], list[dict]]:
        """
        会話履歴を「圧縮対象」と「残す部分」に分離する。

        2つのモードがある:

        [countモード] count が指定された場合:
            Layer0済み（<!-- layer0 --> マーカー付き）のターンのみを対象とし、
            古い方から count 件分を圧縮対象として返す。
            残りの Layer0済みターン + 生ログはすべて「残す部分」に含まれる。
            定期圧縮・緊急圧縮（_compress_if_needed / compress_layer1_scheduled）で使用。

        [従来モード] count が None の場合:
            全履歴を対象に後ろから keep_recent_exchanges 往復分を残す。
            force_compress（手動圧縮）で使用。Layer0済みかどうかを問わない。

        Args:
            keep_exchanges: 直近何往復を残すか（Noneの場合はconfigのkeep_recent_exchangesを使用）
            count:          Layer0済みターンを古い方から何件圧縮するか（Noneで従来モード）

        Returns:
            (圧縮対象のメッセージリスト, 残すメッセージリスト)
        """
        # --- countモード: Layer0済みターンのみを古い方からcount件切り出す ---
        if count is not None:
            # Layer0済みターンの (start_idx, end_idx) を収集
            layer0_turns: list[tuple[int, int]] = []
            i = 0
            while i < len(self.conversation_history):
                msg = self.conversation_history[i]
                if msg.get("role") == "user":
                    text = self._get_text_from_content(msg.get("content", ""))
                    if "<!-- layer0 -->" in text:
                        # このuserから次のuserの手前まで（またはリスト末尾まで）が1ターン
                        end = i + 1
                        while end < len(self.conversation_history):
                            if self.conversation_history[end].get("role") == "user":
                                break
                            end += 1
                        layer0_turns.append((i, end))
                        i = end
                        continue
                i += 1

            # 先頭からcount件分のLayer0ターンを圧縮対象にする
            target_turns = layer0_turns[:count]
            if not target_turns:
                return [], list(self.conversation_history)

            # 分割点 = 最後の対象ターンの終了位置
            split_point = target_turns[-1][1]
            old_messages    = self.conversation_history[:split_point]
            recent_messages = self.conversation_history[split_point:]
            return old_messages, recent_messages

        # --- 従来モード: 全履歴対象・keep_exchangesベース（force_compress用） ---
        target_keep = keep_exchanges if keep_exchanges is not None else self.keep_recent_exchanges
        keep_count = 0
        exchanges_found = 0

        # 後ろから走査して、残すべきメッセージ数を決定
        for i in range(len(self.conversation_history) - 1, -1, -1):
            msg = self.conversation_history[i]
            keep_count += 1
            # userメッセージを見つけたら1往復とカウント
            if msg["role"] == "user":
                exchanges_found += 1
                if exchanges_found >= target_keep:
                    break

        # 分離点を決定し、古い部分と残す部分に分割
        split_point = len(self.conversation_history) - keep_count
        if split_point <= 0:
            return [], self.conversation_history

        old_messages = self.conversation_history[:split_point]
        recent_messages = self.conversation_history[split_point:]

        return old_messages, recent_messages

    def apply_compression(self, summary_lines: list[str], recent_messages: list[dict]):
        """
        ターン単位要約の結果をlayer1に追加し、会話履歴を直近分のみに置き換える。

        Args:
            summary_lines: agent.pyの_summarize_turns()が生成した各ターンの1行要約リスト
            recent_messages: 残す直近の会話履歴
        """
        new_text = "\n".join(summary_lines)
        if self.summary_layer1:
            self.summary_layer1 = f"{self.summary_layer1}\n{new_text}"
        else:
            self.summary_layer1 = new_text

        # 会話履歴を直近分のみに置き換え（古い部分は要約に吸収済み）
        self.conversation_history = recent_messages

    # ================================================================
    # summary_v2: ビューと日単位の履歴操作
    # ================================================================

    def archive_v1_summaries(self) -> bool:
        """旧方式の要約文（summary_layer1/2）を退避欄へ移し、コンテキストから外す。

        summary_v2 へ切り替えたときに使う。同じ日々が新方式のビューと旧要約の両方に載る
        二重掲載を防ぐ。データは消さず、restore_v1_summaries() で戻せる。移したら True。
        v2 稼働中に v1 の緊急圧縮が新しく書いた要約も、次の起動でここへ移る（追記）。
        """
        moved = False
        if self.summary_layer1:
            self.summary_layer1_archived = (
                f"{self.summary_layer1_archived}\n{self.summary_layer1}" if self.summary_layer1_archived
                else self.summary_layer1)
            self.summary_layer1 = ""
            moved = True
        if self.summary_layer2:
            self.summary_layer2_archived = (
                f"{self.summary_layer2_archived}\n{self.summary_layer2}" if self.summary_layer2_archived
                else self.summary_layer2)
            self.summary_layer2 = ""
            moved = True
        return moved

    def restore_v1_summaries(self) -> bool:
        """退避していた旧方式の要約文を元の欄へ戻す（summary_v2 を止めてロールバックするとき）。
        元の欄に何か入っていれば上書きしない。戻したら True。"""
        moved = False
        if self.summary_layer1_archived and not self.summary_layer1:
            self.summary_layer1, self.summary_layer1_archived = self.summary_layer1_archived, ""
            moved = True
        if self.summary_layer2_archived and not self.summary_layer2:
            self.summary_layer2, self.summary_layer2_archived = self.summary_layer2_archived, ""
            moved = True
        return moved

    def set_summary_view(self, view: str):
        """summary_v2 が描画したビューを差し込む。空文字なら v1 の layer1/2 が使われる。"""
        self._summary_view = view or ""

    def get_summary_view_token_count(self) -> int:
        return count_text_tokens(self._summary_view) if self._summary_view else 0

    def get_history_days(self) -> list[dict]:
        """会話履歴を論理日（午前3時境界）ごとに区切って返す。

        summary_v2 の日単位圧縮で使う。戻り値の要素は
        {'date','start','end','turns','times'}（core/summary_v2.slice_history_by_day 参照）。
        """
        from core.summary_v2 import slice_history_by_day
        return slice_history_by_day(self.conversation_history, self._get_text_from_content_msg)

    def _get_text_from_content_msg(self, msg: dict) -> str:
        """メッセージ dict からテキストを取り出す（summary_v2 へ渡すアダプタ）。"""
        return self._get_text_from_content(msg.get("content", ""))

    def drop_history_before(self, index: int):
        """会話履歴の先頭から index 件を削除する（日単位圧縮の確定処理）。

        summary_db への保存・ログ・RAG・LETHE がすべて成功した後にのみ呼ぶこと。
        """
        if index > 0:
            self.conversation_history = self.conversation_history[index:]

    def get_layer1_token_count(self) -> int:
        """layer1（詳細要約）のトークン数を返す。"""
        return count_text_tokens(self.summary_layer1) if self.summary_layer1 else 0

    def get_layer2_token_count(self) -> int:
        """layer2（圧縮要約）のトークン数を返す。"""
        return count_text_tokens(self.summary_layer2) if self.summary_layer2 else 0

    def get_layer0_token_count(self) -> int:
        """Layer0済みターンのトークン数を返す（get_token_breakdown と同じ数え方）。

        以前は本文だけを count_text_tokens で数えていたため、Total 側（count_message_tokens）
        が持つメッセージあたりの overhead 分が漏れていた。
        """
        return self.get_token_breakdown()["layer0"]
  
    def needs_layer2_compression(self, config: dict) -> tuple[bool, list[str]]:
        """
        layer1がトークン上限を超えているか確認し、超えていれば古い行を分離して返す。

        layer1のうち前半（compress_ratio分）を圧縮対象として切り出し、
        残りをlayer1に残す。切り出された行はagent.pyでLLMに再要約される。

        Returns:
            (圧縮が必要か, 圧縮対象の行リスト)
        """
        max_tokens = config.get("layer1_max_tokens", 10000)
        if self.get_layer1_token_count() <= max_tokens:
            return False, []

        lines = self.summary_layer1.split("\n")
        compress_ratio = config.get("layer1_compress_ratio", 0.5)
        split_idx = int(len(lines) * compress_ratio)

        # 前半を圧縮対象として分離し、後半をlayer1に残す
        old_lines = lines[:split_idx]
        self.summary_layer1 = "\n".join(lines[split_idx:])

        return True, old_lines

    def apply_layer2_compression(self, compressed_text: str, config: dict):
        """
        LLMで再圧縮されたテキストをlayer2に追加する。上限を超えたら古い行から削除する。

        Args:
            compressed_text: agent.pyの_compress_layer1_if_needed()が生成した圧縮テキスト
            config: compression_config.json の内容（layer2_max_tokens等）
        """
        if self.summary_layer2:
            self.summary_layer2 = f"{self.summary_layer2}\n{compressed_text}"
        else:
            self.summary_layer2 = compressed_text

        # layer2のトークン上限チェック: 超過分は古い行から順に削除（FIFO）
        max_tokens = config.get("layer2_max_tokens", 5000)
        while self.get_layer2_token_count() > max_tokens:
            lines = self.summary_layer2.split("\n")
            if len(lines) <= 1:
                break
            lines.pop(0)
            self.summary_layer2 = "\n".join(lines)

    def reload_memories(self):
        """
        記憶ファイルとシステムプロンプトを再読み込みする。

        エージェントがSOUL.md・MEMORY.md等を更新した後や、
        VitalManagerの日次リセット時に呼ばれ、コンテキストを最新の状態に反映する。
        """
        self._rebuild_all()

    def remove_last_exchange(self):
        """
        直近のやり取り（最後のuserメッセージ以降の全メッセージ）を会話履歴から削除する。

        ユーザーによる応答中断時のロールバックに使用。
        中断されたやり取りが履歴に残らないようにする。
        """
        # 会話履歴を後ろから探し、最後のuserメッセージのインデックスを見つける
        last_user_idx = -1
        for i in range(len(self.conversation_history) - 1, -1, -1):
            if self.conversation_history[i]["role"] == "user":
                last_user_idx = i
                break
        
        if last_user_idx != -1:
            # 最後のuserメッセージを含めてそれ以降を全て削除
            self.conversation_history = self.conversation_history[:last_user_idx]
            
    def set_state_path(self, path: str):
        """コンテキスト状態の永続化先ファイルパスを設定する。"""
        self._state_path = path

    # 古い画像を置き換えるプレースホルダ。
    # 画像の出所（ファイル名・URL）は同じメッセージの text パートに書かれているので、
    # もう一度見たいときは柚月が see_image で取り直せる。
    _IMAGE_PLACEHOLDER = "[画像はここでは省略されています（出所は本文の記載を参照。もう一度見るなら see_image で開き直してください）]"

    def _limit_recent_images(self, messages: list, keep: int) -> list:
        """直近 keep 枚より古い画像パートをプレースホルダに置き換えたコピーを返す。

        画像は1枚のbase64が1〜2MBになる。トークン換算では安い（DeepSeekで384）が、
        リクエスト本文（48MiB上限）とstateファイルには毎回そのサイズがかかる。
        そこで枚数で頭を押さえる。

        守っている不変条件:
          - 元のリスト（＝会話履歴の実体）は決して書き換えない。必ずコピーを返す。
            履歴から画像が永久に消えるのを避けるため（core/llm.py の _strip_images と同じ方針）。
          - メッセージ件数を変えない。DEBUG画面の history_idx 対応付け
            （core/agent.py:_update_debug_context）が「_filter_conversation_history は件数不変」
            を前提にしているため。
        """
        if keep is None or keep < 0:
            return list(messages)

        # 画像パートの位置を出現順に集める
        positions = []
        for mi, msg in enumerate(messages):
            content = msg.get("content")
            if isinstance(content, list):
                for pi, part in enumerate(content):
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        positions.append((mi, pi))

        if len(positions) <= keep:
            return list(messages)

        # 後ろから keep 枚を残し、それより前を落とす
        drop = set(positions[:len(positions) - keep])

        result = []
        for mi, msg in enumerate(messages):
            content = msg.get("content")
            if not isinstance(content, list):
                result.append(msg)
                continue
            if not any((mi, pi) in drop for pi in range(len(content))):
                result.append(msg)
                continue
            new_content = []
            for pi, part in enumerate(content):
                if (mi, pi) in drop:
                    new_content.append({"type": "text", "text": self._IMAGE_PLACEHOLDER})
                else:
                    new_content.append(part)
            new_msg = dict(msg)
            new_msg["content"] = new_content
            result.append(new_msg)
        return result

    def _sanitize_for_save(self, messages: list) -> list:
        """保存用にメッセージリストを整える。

        2026-08-22 変更: 直近 keep_recent_images 枚の画像は base64 のまま保存する。
        以前は全画像をプレースホルダに落としていたため、**再起動のたびに過去の画像が
        失われていた**（メモリ上の履歴には画像があるのに、stateファイルには無いので
        復元しても戻らない）。柚月が画像を見られるようになった以上これは記憶の欠落なので、
        直近分は残す。それより古い画像は落としてファイルサイズを有界に保つ。
        """
        limited = self._limit_recent_images(messages, self.keep_recent_images)
        sanitized = []
        for msg in limited:
            content = msg.get("content")
            if isinstance(content, list) and not any(
                isinstance(p, dict) and p.get("type") == "image_url" for p in content
            ):
                # 画像が残っていないリストは文字列に畳む（従来の保存形式を維持）
                msg_copy = dict(msg)
                msg_copy["content"] = "\n".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
                sanitized.append(msg_copy)
            else:
                sanitized.append(msg)
        return sanitized

    # =========================================================
    # Layer0 圧縮サポートメソッド
    # =========================================================

    def count_uncompressed_turns(self) -> int:
        """Layer0 未圧縮のターン数。

        「ターンの先頭」の判定は extract_oldest_uncompressed_turn と同じ _is_turn_boundary に
        揃える（以前は別のリストで数えていて、<external_notice> を含む件などで
        抽出側と件数がずれた）。system_notice だけの途中メッセージは数えない。
        """
        count = 0
        for msg in self.conversation_history:
            if msg.get("role") == "user":
                text = self._get_text_from_content(msg.get("content", ""))
                if "<!-- layer0 -->" not in text and self._is_turn_boundary(text):
                    count += 1
        return count

    def extract_oldest_uncompressed_turn(self):
        """
        最古の未圧縮ターンを抽出する。

        <!-- layer0 --> マーカーが付いていない最初の user メッセージを起点として、
        次の user メッセージの手前までの全メッセージ（user + assistant + tool）を収集する。

        Returns:
            (start_idx: int, turn_msgs: list[dict]) のタプル。
            未圧縮ターンがなければ None を返す。
        """
        # 起点は「未圧縮かつターンの先頭（境界マーカーを持つ）」の user。
        # count_uncompressed_turns と同じ判定にし、件数と抽出結果がずれないようにする
        # （system_notice だけの user が先頭に迷い込んでいても、それを起点にしない）
        start_idx = None
        for i, msg in enumerate(self.conversation_history):
            if msg.get("role") == "user":
                text = self._get_text_from_content(msg.get("content", ""))
                if "<!-- layer0 -->" not in text and self._is_turn_boundary(text):
                    start_idx = i
                    break

        if start_idx is None:
            return None

        # start_idx から始まり、次の「本物の」user ロールの直前まで収集。
        # 境界の判定は _is_turn_boundary（_TURN_BOUNDARY_MARKERS）に一元化する。
        # 以前はここに <external_notice> が無く、X の返信などが前のターンに飲み込まれて
        # Layer0 で消えていた（7月以降 4 件中 0 件残存・2026-09-07 修正）。
        turn_msgs = [self.conversation_history[start_idx]]
        for j in range(start_idx + 1, len(self.conversation_history)):
            msg = self.conversation_history[j]
            if msg.get("role") == "user":
                text = self._get_text_from_content(msg.get("content", ""))
                # system_noticeのみのターンは区切りとして扱わず同じターンに含める
                if self._is_turn_boundary(text):
                    break
            turn_msgs.append(msg)

        return start_idx, turn_msgs

    def replace_turn_with_layer0(self, start_idx: int, turn_length: int,
                                  compressed_user: str, compressed_assistant: Optional[str]):
        """
        指定インデックスのターン（元のメッセージ群）を Layer0 圧縮済みペアに置換する。

        元の tool_calls / tool ロールは消え、user + assistant の2メッセージに凝縮される。
        user content の末尾には <!-- layer0 --> マーカーが付与される。

        compressed_assistant が None のときは user メッセージだけに置換する。柚月が何も
        言わなかったターン（通知が届いたまま応答が無かった等）で、無い発言を LLM に
        作らせないため。元のターンも user だけなので、履歴の形は変わらない。

        Args:
            start_idx:            置換対象ターンの開始インデックス
            turn_length:          置換対象ターンのメッセージ数（= len(turn_msgs)）
            compressed_user:      ルールベースで整形済みのユーザーテキスト
            compressed_assistant: 圧縮済みassistantテキスト（None なら assistant を置かない）
        """
        new_msgs = [{"role": "user", "content": compressed_user}]
        if compressed_assistant is not None:
            new_msgs.append({"role": "assistant", "content": compressed_assistant})
        self.conversation_history[start_idx:start_idx + turn_length] = new_msgs

    # ターン開始を示すマーカー集合（extract_oldest_uncompressed_turn の境界判定と同一）
    _TURN_BOUNDARY_MARKERS = (
        "<task_notice>", "<city_event_notice>", "<user_message>",
        "<moonbeat_instruction>", "<external_notice>", "<!-- layer0 -->",
    )

    def _is_turn_boundary(self, text: str) -> bool:
        """このuserメッセージ本文が「新しいターンの開始」かを判定する。
        extract_oldest_uncompressed_turn と同じマーカー集合を使い、判定を一元化する。"""
        return any(marker in text for marker in self._TURN_BOUNDARY_MARKERS)

    def find_turn_at(self, history_idx: int):
        """
        指定した conversation_history インデックスを含む「1ターン」の範囲を返す。

        部分Layer0圧縮（手動）で、DEBUG画面が示すメッセージ位置から
        そのメッセージが属するターン全体（user + assistant + tool の連なり）を特定する。
        境界判定は extract_oldest_uncompressed_turn と完全に同一。

        Args:
            history_idx: conversation_history 内のインデックス

        Returns:
            (start_idx, turn_msgs) のタプル。
            無効なインデックス、またはそのターンが既に Layer0 済みの場合は None。
        """
        h = self.conversation_history
        if history_idx < 0 or history_idx >= len(h):
            return None

        # --- 開始位置: history_idx から後方へ、最も近い「ターン開始のuser」を探す ---
        start_idx = None
        for i in range(history_idx, -1, -1):
            msg = h[i]
            if msg.get("role") == "user":
                text = self._get_text_from_content(msg.get("content", ""))
                if self._is_turn_boundary(text):
                    start_idx = i
                    break
        if start_idx is None:
            return None

        # 既に Layer0 済みのターンは圧縮対象外
        start_text = self._get_text_from_content(h[start_idx].get("content", ""))
        if "<!-- layer0 -->" in start_text:
            return None

        # --- 終了位置: start_idx の次から、次の「ターン開始のuser」の手前まで ---
        turn_msgs = [h[start_idx]]
        for j in range(start_idx + 1, len(h)):
            msg = h[j]
            if msg.get("role") == "user":
                text = self._get_text_from_content(msg.get("content", ""))
                if self._is_turn_boundary(text):
                    break
            turn_msgs.append(msg)

        return start_idx, turn_msgs

    def _toolcall_app(self, tc: dict) -> str:
        """tool_call から「ツール名」を取り出す。run_program は app_name まで展開する。"""
        import json as _json
        fn = tc.get("function", {}) or {}
        name = fn.get("name", "") or "?"
        if name != "run_program":
            return name
        try:
            args = _json.loads(fn.get("arguments", "{}") or "{}")
            return str(args.get("app_name") or "run_program")
        except Exception:
            return "run_program"

    def _turn_time_str(self, text: str) -> str:
        """ターン先頭userの本文から HH:MM を取り出す（[SYSTEM]和暦 or Layer0ダッシュ形式）。"""
        import re
        m = (re.search(r'(\d{4})年(\d{2})月(\d{2})日.*?(\d{2}:\d{2})', text)
             or re.search(r'(\d{4})-(\d{2})-(\d{2})\s+(\d{2}:\d{2})', text))
        return m.group(4) if m else "?"

    def detect_loop_turns(self, same_tool_threshold: int = 3, life_note_threshold: int = 4) -> list[dict]:
        """
        強化ループの疑いがあるターンを検出して返す（Layer0圧縮の候補リスト）。

        判定（RAW＝未Layer0ターンのみ対象）:
          - 1ターン内で同一ツールが same_tool_threshold 回以上、または
          - life_action + note_quill の合計が life_note_threshold 以上
        ニュースレビュー等の正常な多ツールターン（life_action/note_quill が少ない）は
        後者の条件に引っかからないので自然に除外される。

        Returns:
            [{start_idx, time, life_action, note_quill, total_tools, severity}] のリスト
            （severity = life_action+note_quill。大きいほど重症。降順ソート済み）
        """
        h = self.conversation_history
        N = len(h)
        results: list[dict] = []
        i = 0
        while i < N:
            msg = h[i]
            if msg.get("role") == "user" and self._is_turn_boundary(self._get_text_from_content(msg.get("content", ""))):
                # ターン範囲 [i, j)
                j = i + 1
                while j < N:
                    nxt = h[j]
                    if nxt.get("role") == "user" and self._is_turn_boundary(self._get_text_from_content(nxt.get("content", ""))):
                        break
                    j += 1
                uc = self._get_text_from_content(msg.get("content", ""))
                # 既にLayer0済みのターンは対象外
                if "<!-- layer0 -->" not in uc:
                    counts: dict[str, int] = {}
                    for k in range(i, j):
                        for tc in (h[k].get("tool_calls") or []):
                            app = self._toolcall_app(tc)
                            counts[app] = counts.get(app, 0) + 1
                    la = counts.get("life_action", 0)
                    nq = counts.get("note_quill", 0)
                    max_same = max(counts.values()) if counts else 0
                    if max_same >= same_tool_threshold or (la + nq) >= life_note_threshold:
                        results.append({
                            "start_idx": i,
                            "time": self._turn_time_str(uc),
                            "life_action": la,
                            "note_quill": nq,
                            "total_tools": sum(counts.values()),
                            "severity": la + nq,
                        })
                i = j
            else:
                i += 1

        results.sort(key=lambda r: r["severity"], reverse=True)
        return results

    # =========================================================
    # DEBUG画面からの会話履歴編集・置換（手動・本文テキストのみ）
    # =========================================================

    def edit_message(self, history_idx, new_content: str, expected_content: str = None) -> dict:
        """
        指定した conversation_history インデックスのメッセージ本文を書き換える。

        DEBUG画面のインライン編集から呼ばれる。role / tool_calls / tool_call_id は
        変更しないため、ツール連結（assistant.tool_calls ↔ tool）は保全される。

        Args:
            history_idx:      conversation_history 内のインデックス
            new_content:      新しい本文テキスト
            expected_content: 楽観ロック用。編集前に画面が表示していた本文テキスト。
                              指定時、現在の本文テキストと一致しなければ拒否する（誤爆防止）。

        Returns:
            {"success": bool, "message": str, "stale"?: bool}
        """
        h = self.conversation_history
        try:
            idx = int(history_idx)
        except (TypeError, ValueError):
            return {"success": False, "message": "インデックスが不正です"}
        if idx < 0 or idx >= len(h):
            return {"success": False, "stale": True,
                    "message": "対象メッセージが存在しません（画面が古い可能性があります）。更新してください"}

        msg = h[idx]
        content = msg.get("content", "")
        current_text = self._get_text_from_content(content)
        if expected_content is not None and expected_content != current_text:
            return {"success": False, "stale": True,
                    "message": "メッセージが変化しています（画面が古い可能性があります）。更新してください"}

        if isinstance(content, list):
            # マルチモーダル: textパートを new_content に集約し、画像等の非textパートは保持
            non_text = [p for p in content
                        if not (isinstance(p, dict) and p.get("type") == "text")]
            if new_content:
                msg["content"] = [{"type": "text", "text": new_content}] + non_text
            else:
                msg["content"] = non_text
        else:
            msg["content"] = new_content
        return {"success": True, "message": "編集を保存しました"}

    def delete_turn_at(self, history_idx, expected_count=None) -> dict:
        """
        指定した conversation_history インデックスを含む「1ターン」を履歴から丸ごと削除する。

        DEBUG画面のターン削除ボタンから呼ばれる。境界判定は find_turn_at と同一だが、
        こちらは Layer0 圧縮済みターンも削除対象にできる。ターン単位で消すため、
        assistant.tool_calls ↔ tool の連結は壊れない。
        記憶ファイル（ノート・日記・要約）には一切触れない。会話コンテキストのみ。

        Args:
            history_idx:    削除したいターン内のメッセージのインデックス（通常はターン開始位置）
            expected_count: 楽観ロック用。画面が表示していたターン内メッセージ数。
                            指定時、現在のターン長と一致しなければ拒否する（誤爆防止）。

        Returns:
            {"success": bool, "message": str, "deleted": int, "stale"?: bool}
        """
        h = self.conversation_history
        try:
            idx = int(history_idx)
        except (TypeError, ValueError):
            return {"success": False, "message": "インデックスが不正です", "deleted": 0}
        if idx < 0 or idx >= len(h):
            return {"success": False, "stale": True, "deleted": 0,
                    "message": "対象メッセージが存在しません（画面が古い可能性があります）。更新してください"}

        # --- 開始位置: idx から後方へ、最も近い「ターン開始のuser」を探す ---
        start_idx = None
        for i in range(idx, -1, -1):
            msg = h[i]
            if msg.get("role") == "user" and self._is_turn_boundary(self._get_text_from_content(msg.get("content", ""))):
                start_idx = i
                break
        if start_idx is None:
            return {"success": False, "deleted": 0,
                    "message": "ターン開始が見つかりません（ターンに属さないメッセージは削除できません）"}

        # --- 終了位置: start_idx の次から、次の「ターン開始のuser」の手前まで ---
        end = start_idx + 1
        while end < len(h):
            msg = h[end]
            if msg.get("role") == "user" and self._is_turn_boundary(self._get_text_from_content(msg.get("content", ""))):
                break
            end += 1

        length = end - start_idx
        if expected_count is not None:
            try:
                if int(expected_count) != length:
                    return {"success": False, "stale": True, "deleted": 0,
                            "message": "ターン構成が変化しています（画面が古い可能性があります）。更新してください"}
            except (TypeError, ValueError):
                pass  # 不正な expected_count は照合なし扱い

        del h[start_idx:end]
        return {"success": True, "deleted": length,
                "message": f"ターンを削除しました（{length} メッセージ）"}

    @staticmethod
    def _sub_count(pattern, replacement: str, text: str, only_nth: int = None):
        """
        pattern にマッチした箇所を replacement（リテラル文字列）で置換する。

        only_nth が None なら全マッチを置換、整数なら 0 始まりで n 番目のマッチのみ置換。
        replacement はリテラル扱い（正規表現の後方参照 \\1 等は展開しない）。

        Returns:
            (new_text, マッチ総数, only_nth が存在したか)
        """
        state = {"i": -1, "hit": False}

        def repl(m):
            state["i"] += 1
            if only_nth is None or state["i"] == only_nth:
                state["hit"] = True
                return replacement
            return m.group(0)

        new_text = pattern.sub(repl, text)
        return new_text, state["i"] + 1, state["hit"]

    def replace_text_in_history(self, find: str, replacement: str, *,
                                start_idx: int = None, end_idx: int = None,
                                occurrence: int = None,
                                use_regex: bool = False, case_sensitive: bool = True,
                                dry_run: bool = False) -> dict:
        """
        会話履歴の本文テキスト（content が文字列のメッセージ）に対し検索置換を行う。

        対象範囲は [start_idx, end_idx]（inclusive。省略時は全履歴）。content がリスト
        （マルチモーダル）のメッセージはスキップする。tool_calls 等には触れない。

        Args:
            find:           検索文字列（use_regex=True なら正規表現パターン）
            replacement:    置換文字列（リテラル。後方参照は展開しない）
            start_idx/end_idx: 対象とする conversation_history の区間（inclusive）
            occurrence:     1件モード。指定時、start_idx のメッセージの occurrence 番目
                            （0始まり）のマッチのみを置換する（範囲一括ではない）。
            use_regex:      find を正規表現として扱うか
            case_sensitive: 大文字小文字を区別するか
            dry_run:        True なら実体を変更せずマッチ件数のみ返す（プレビュー用）

        Returns:
            {"success": bool, "message": str, "count": int, "affected": int, "stale"?: bool}
        """
        import re
        if not find:
            return {"success": False, "message": "検索文字列が空です", "count": 0, "affected": 0}

        h = self.conversation_history
        n = len(h)
        if n == 0:
            return {"success": False, "message": "会話履歴が空です", "count": 0, "affected": 0}

        lo = 0 if start_idx is None else int(start_idx)
        hi = (n - 1) if end_idx is None else int(end_idx)
        lo = max(0, min(lo, n - 1))
        hi = max(0, min(hi, n - 1))
        if lo > hi:
            lo, hi = hi, lo

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(find if use_regex else re.escape(find), flags)
        except re.error as e:
            return {"success": False, "message": f"正規表現エラー: {e}", "count": 0, "affected": 0}

        replacement = replacement or ""

        # --- 1件モード（occurrence指定）: 単一メッセージの n 番目のマッチだけ置換 ---
        if occurrence is not None:
            idx = lo
            msg = h[idx]
            content = msg.get("content", "")
            if not isinstance(content, str):
                return {"success": False,
                        "message": "このメッセージは本文置換に対応していません", "count": 0, "affected": 0}
            new_text, _, hit = self._sub_count(pattern, replacement, content, only_nth=int(occurrence))
            if not hit:
                return {"success": False, "stale": True,
                        "message": "対象の一致が見つかりません（画面が古い可能性があります）。更新してください",
                        "count": 0, "affected": 0}
            if not dry_run:
                msg["content"] = new_text
            return {"success": True, "message": "1件置換しました", "count": 1, "affected": 1}

        # --- 範囲一括（プレビュー / 実置換）---
        total = 0
        affected = 0
        for i in range(lo, hi + 1):
            msg = h[i]
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            new_text, n_matches, _ = self._sub_count(pattern, replacement, content)
            if n_matches > 0:
                total += n_matches
                affected += 1
                if not dry_run:
                    msg["content"] = new_text

        if dry_run:
            message = f"範囲内に {total} 件（{affected} メッセージ）"
        else:
            message = f"{total} 件を置換しました（{affected} メッセージ）"
        return {"success": True, "message": message, "count": total, "affected": affected}

    def save_state(self):
        """コンテキスト状態（会話履歴・二段階要約）をJSONファイルに保存する。アトミック書き込み（tmp→rename）を使用。"""
        if not self._state_path:
            return
        import json as _json
        import os

        # 保存前に画像データを除去（base64は巨大になるため）
        sanitized_history = self._sanitize_for_save(self.conversation_history)
        current_count = len(sanitized_history)

        # --- ターン数監視: 保存前に既存ファイルのターン数と比較 ---
        monitor_path = str(self._state_path).replace("context_state.json", "state_monitor.json")
        try:
            if os.path.exists(monitor_path):
                with open(monitor_path, 'r', encoding='utf-8') as f:
                    monitor = _json.load(f)
                last_count = monitor.get("last_count", 0)
                if current_count < last_count:
                    tlog(f"[CRITICAL] context_state.jsonのターン数が減少しました: {last_count} → {current_count}")
        except Exception as e:
            tlog(f"[Monitor] 監視ファイル読み込みエラー: {e}")

        state = {
            "conversation_history": sanitized_history,
            "summary_layer1": self.summary_layer1,
            "summary_layer2": self.summary_layer2,
            "summary_layer1_archived": self.summary_layer1_archived,
            "summary_layer2_archived": self.summary_layer2_archived,
        }

        # アトミック書き込み: tmpファイルに書いてからrename（書き込み途中のクラッシュ対策）
        tmp_path = self._state_path + ".tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            _json.dump(state, f, ensure_ascii=False)
        os.replace(tmp_path, self._state_path)

        # --- 監視ファイル更新 ---
        try:
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S")
            with open(monitor_path, 'w', encoding='utf-8') as f:
                _json.dump({"last_count": current_count, "last_saved": now}, f, ensure_ascii=False)
        except Exception as e:
            tlog(f"[Monitor] 監視ファイル更新エラー: {e}")

    def load_state(self):
        """コンテキスト状態をJSONファイルから復元する。前回セッションの会話履歴と要約を引き継ぐ。"""
        if not self._state_path:
            return False
        from pathlib import Path
        if not Path(self._state_path).exists():
            return False
        try:
            import json as _json
            with open(self._state_path, 'r', encoding='utf-8') as f:
                state = _json.load(f)
            self.conversation_history = state.get("conversation_history", [])
            self.summary_layer1 = state.get("summary_layer1", "")
            self.summary_layer2 = state.get("summary_layer2", "")
            self.summary_layer1_archived = state.get("summary_layer1_archived", "")
            self.summary_layer2_archived = state.get("summary_layer2_archived", "")

            # 旧形式（単一要約）からの移行: conversation_summaryをlayer1に取り込む
            if not self.summary_layer1 and state.get("conversation_summary"):
                self.summary_layer1 = state["conversation_summary"]
            tlog(f"[Context] 前回のコンテキストを復元しました（履歴{len(self.conversation_history)}件）")
            return True
        except Exception as e:
            tlog(f"[Context] コンテキスト復元に失敗: {e}")
            return False



    def _filter_conversation_history(self) -> list[dict]:
        """会話履歴をLLM投入用に返す（浅いコピー）。

        ※ 旧「Watsonランダム間引き」フィルタは廃止した。過去のassistantメッセージを
        毎リクエスト書き換えると、その地点以降のプロンプトキャッシュ（プレフィックス
        キャッシュ）が全てミスになるため。テキストはそのまま素通しする。

        画像だけは例外で、直近 keep_recent_images 枚より古いものをプレースホルダに置き換える。
        画像1枚のbase64は1〜2MBあり、放っておくと過去に見た画像が毎リクエスト全部乗って
        DeepSeekの本文上限48MiB（JPEGで約190枚）に達し、以降のリクエストが全て400で落ちる。
        キャッシュミスは「画像が枠から外れた瞬間」の1回だけで、画像は履歴の末尾寄りにあるため
        無効化される範囲も短い。
        置き換えはコピー上で行い、conversation_history の実体には画像を残す。
        """
        return self._limit_recent_images(self.conversation_history, self.keep_recent_images)