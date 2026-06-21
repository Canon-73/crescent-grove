# core/agent.py
"""
エージェント メインループ

役割:
    エージェントの「脳」にあたる部分。ユーザーからのメッセージを受け取り、
    LLMで思考し、必要に応じてツールを実行し、応答を返す。

処理フロー:
    1. ユーザーメッセージを受け取る → バイタル更新 → 会話履歴に追加 → ログ記録
    2. コンテキスト（システムプロンプト + 記憶 + 会話履歴）を構築
    3. LLMに送信（on_stream_event があればトークン生成と同時にUIへストリーミング配信）
    4. LLMの応答を解析:
       - テキスト応答 → XMLタグ除去 → VITAL_REPORT/internalパース → ログ記録 → ユーザーに返す
       - ツール呼び出し → ログ記録 → 実行 → 結果をコンテキストに追加 → 記憶リロード → 3に戻る
    5. 最終応答をユーザーに返す
    6. トークン数チェック → 必要なら会話履歴を圧縮（layer1→layer2の二段階圧縮）
    7. 大きなツール結果をRAGに退避（ToolTrim）

ストリーミング表示:
    一部のLLM（Deepseek等）はテキスト内に <internal>（内面思考）や XMLツール呼び出し、
    [VITAL_REPORT:] を埋め込むため、生のdeltaをそのまま流すと隠しタグがリークする。
    対策として StreamSanitizer（core/stream_sanitizer.py）でストリーム中に除去し、
    さらに最終応答（完全クリーン済み）でUI側の吹き出しを置換する二段構えとする。
    会話履歴・ログへの保存は従来どおり「完成した全文」のみ。
    config.yaml の llm.streaming を false にすると従来の非ストリーミングに戻せる。

ログ:
    全てのやりとり（ツール呼び出し・結果含む）はfull logにJSONL形式で記録される。
    ユーザー↔エージェントの会話はchat logにもMarkdown形式で記録される。

コンテキスト圧縮:
    応答後にトークン数をチェックし、閾値を超えていたら
    古い会話履歴をLLMにターン単位で要約させて圧縮する（layer1）。
    layer1の要約がさらに上限を超えた場合、layer2に再圧縮する。
    要約はlogs/summary/YYYY-MM-DD.mdにも追記される。
"""

from core.time_utils import tlog
import asyncio
import json
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable
from pathlib import Path

from core.paths import data_file, resolve_path, config_file, system_prompt_dir
from core.config_loader import apply_prompt_placeholders
from core.llm import LLMProvider, LLMResponse
from core.stream_sanitizer import StreamSanitizer
from core.context import ContextBuilder
from core.tools import get_tool_definitions, execute_tool
from core.logger import ConversationLogger
from memory.manager import MemoryManager
from core.time_utils import get_logical_date
from core.wyrd_network import load_graph, process_fact_buffer
from core.salia import Salia
from core.i18n import t, get_language

class ProcessingLock:
    """
    会話とバックグラウンドタスクの排他制御・中断シグナルを管理するクラス。

    lock: asyncio.Lockによる排他ロック。ユーザー会話とスケジュールタスクの同時実行を防ぐ。
    interrupt_flag: Trueにすると実行中のツールループを中断させる。UIのStopボタンや
                    ユーザーメッセージ受信時にバックグラウンドタスクを中断するために使用。
                    中断を実行した側が消費（False に戻す）する。残したままにすると
                    次のターンやバックグラウンドタスクを誤って中断してしまう。
    chat_turn_active: ユーザー会話の処理中はTrue。新着ユーザーメッセージによる割込みを
                      バックグラウンドタスクに限定するための判定に使う。
    """
    def __init__(self):
        self.lock = asyncio.Lock()
        self.interrupt_flag = False
        self.chat_turn_active = False


# ツールループの最大回数（無限ループ防止用の安全弁）
MAX_TOOL_LOOPS = 40

# 日本標準時（タイムスタンプ生成・ログ記録に使用）
JST = timezone(timedelta(hours=9))


# 記録判定フックで注入する内省プロンプト（組み込み既定）。
# 以前はこの3点判定を TOP_PROMPT.md に常駐させていたが、コンテキストが肥大すると
# システムプロンプト先頭の指示が守られなくなるため、「ツールを使ったターンの最終ステップ後」に
# user ロールのシステム通知として毎回注入する方式へ移した（生成直前＝最も守られる位置）。
# この内省ターンは通常のターンの続きとして可視化される（発言もツールログ行も普通に表示される）。
# なお、本編のツールループ中にすでに記録サテライト（手紙/雑記帳/好悪）を使っていれば、
# 二重の問いかけを避けるため注入しない。
# 文言は system_prompt/RECORD_CHECK.md があればそちらが優先される（設定UIから編集可能）。
# このリテラルはファイルが無い/空のときのフォールバック。{{user_honorific}} は実行時に置換される。
# RECORD_CHECK_PROMPT_DEFAULT は廃止し、t("agent_record_check_fallback") に統合した。
# 文字列は lang/<lang>.json の "agent_record_check_fallback" にあり、{{agent_name}} /
# {{user_honorific}} の二重括弧プレースホルダはそのまま残し、_load_record_check_prompt
# が apply_prompt_placeholders で実行時置換する（外部 RECORD_CHECK.md と同じ流儀）。


# --- 「宣言だけしてツールを呼ばずに終わる」検知用パターン ---
# 「雑記帳に書いてみようと思います」のように、行動を示唆する語と
# 意志・予告系の文末表現が同居していたら「宣言したのに未実行」とみなす。
# 行動キーワードと文末表現の AND で判定する（誤検知抑制）。
# 文末表現は誤検知を避けるため「〜したいです（願望のみ）」「〜します（断定だが宣言性弱）」は含めない。
_ACTION_KEYWORDS_RE = re.compile(
    r'(?:'
    # ファイル操作
    r'雑記帳|日記|メモ|self_memo|ブログ|下書き|ノート'
    r'|書[きいこ]|記録|残[しそ]|保存|作成|追記|編集|更新|投稿|公開'
    # 読み取り
    r'|読[みも]|覗[きいく]|チェック|確認|目を通'
    # 場所移動・探索
    r'|入[っり]|行[っき]|探索|散歩'
    r'|Amphitheater|Music Studio|cafe|OpenBotCity'
    # ゲーム操作
    r'|Cosmic Harvest|収穫|植え|成長具合'
    # 情報収集
    r'|Moltbook|調べ|検索|探[しそ]'
    # ツール名直接
    r'|enter_building|known_buildings|perform|watch'
    r')'
)

_INTENT_ENDINGS_RE = re.compile(
    r'(?:'
    # ですます調・基本形
    r'てみようかな'
    r'|てみるのもいいかもしれません'
    r'|てみるのもいいかも'
    r'|てみようと思います'
    r'|てみたいと思います'
    r'|てみたいです'
    r'|てみます'
    r'|ておきたいです'
    r'|ておきます'
    r'|ておこうと思います'
    r'|ておこう'
    r'|ていきます'
    r'|てきます'
    r'|ようと思います'
    r'|ようかな'
    r'|つもりです'
    # だ・である調（独白モード時に出る）
    r'|てみるのもいいかもしれない'
    r'|てみようと思う'
    r'|てみたいと思う'
    r'|ておこうと思う'
    r'|ようと思う'
    r'|つもりだ'
    # 「ておくのもいいかも」系
    r'|ておくのもいいかもしれません'
    r'|ておくのもいいかもしれない'
    r'|ておくのもいいかも'
    # 意志形単独（「確認してみよう。」）
    r'|てみよう'
    # 願望「たい」複合形（「書き留めたい気持ち」「更新したいと思う」）
    r'|たい気持ち'
    r'|たいと思います'
    r'|たいと思う'
    r')。?'
)


def has_unfulfilled_declaration(text: str) -> bool:
    """発言テキストに「行動の宣言だけして未実行で終わる」兆候があるか判定する。

    行動を示唆するキーワード（_ACTION_KEYWORDS_RE）と意志・予告系の文末表現
    （_INTENT_ENDINGS_RE）が同じテキスト中に両方現れた場合に True を返す。
    呼び出し側はこの真偽だけ見て、ツールコールが伴っていなければ
    「ツールを使わずに終わろうとしている」と判断して再ターンを促す。

    なお、`_ACTION_KEYWORDS_RE` / `_INTENT_ENDINGS_RE` は日本語固有の語彙パターン
    （「てみようと思います」「ようかな」など）に基づくため、language=ja 以外の
    環境では誤検知になる。よって ja 以外では常に False を返して機能を無効化する。
    """
    if not text:
        return False
    if get_language() != "ja":
        return False
    return bool(_ACTION_KEYWORDS_RE.search(text) and _INTENT_ENDINGS_RE.search(text))


def strip_tool_xml(text: str) -> str:
    """
    LLMの応答テキストからツール呼び出しのXMLタグを除去する。

    一部のLLM（Deepseek等）はfunction callingではなく
    テキスト内にXMLでツール呼び出しを埋め込むことがある。
    それらをユーザーに見せないよう除去する。
    """
    # DSML形式のfunction_call XMLタグを除去
    text = re.sub(
        r'<\s*\|?\s*DSML\s*\|?\s*function_calls?\s*>.*?<\s*/\s*\|?\s*DSML\s*\|?\s*function_calls?\s*>',
        '', text, flags=re.DOTALL
    )
    # 標準的なfunction_call XMLタグを除去
    text = re.sub(r'<function_call>.*?</function_call>', '', text, flags=re.DOTALL)
    text = text.rstrip()
    return text


class Agent:
    """
    エージェントのメインクラス。

    LLMとの対話ループ、ツール実行、コンテキスト圧縮、ログ記録、
    バイタル管理、RAG連携などを統合的に管理する。
    server.pyからWebSocketセッションごとに1インスタンス生成されるほか、
    スケジュールタスク実行用のグローバルインスタンスも存在する。
    """

    # 応答テキストから特殊タグを除去するための正規表現（コンパイル済み）
    _VITAL_REPORT_RE = re.compile(r'\[VITAL_REPORT:.*?\]')     # バイタル自己申告タグ
    _INTERNAL_RE = re.compile(r'<internal>.*?</internal>', re.DOTALL)  # 内面思考タグ

    def __init__(self, llm: LLMProvider, context: ContextBuilder,
                 memory: MemoryManager, logger: Optional[ConversationLogger] = None,
                 scheduler=None, rag_db=None, processing_lock=None, agent_name: str = "Assistant",
                 on_context_update: Optional[Callable[[dict], None]] = None,
                 vital_manager=None, honorific: str = "ユーザー"):
        """
        エージェントを初期化する。

        Args:
            llm: LLMプロバイダー（API通信を担当）
            context: コンテキストビルダー（システムプロンプト・会話履歴の構築を担当）
            memory: 記憶管理（ワークスペース内ファイルの読み書きを担当）
            logger: 会話ログ記録（Noneの場合はログを記録しない）
            scheduler: タスクスケジューラ（Noneの場合はスケジュール機能無効）
            rag_db: RAGデータベース（Noneの場合はベクトル検索機能無効）
            processing_lock: タスク排他制御・中断シグナル用のロック
            agent_name: エージェント名（ログ・UI表示に使用）
            on_context_update: デバッグ用コンテキスト更新時のコールバック（server.pyのWebSocket配信に接続）
            vital_manager: バイタル/メンタル/欲求管理（Noneの場合はバイタル機能無効）
            honorific: エージェントがユーザーを呼ぶ呼称（config の profile.user.honorific。
                       未設定時は中立語 "ユーザー"。dev では "ご主人様"）
        """
        self.llm = llm
        self.context = context
        self.memory = memory
        self.logger = logger
        self.scheduler = scheduler
        self.rag_db = rag_db
        self.processing_lock = processing_lock
        self.agent_name = agent_name
        self.honorific = honorific
        self.on_context_update = on_context_update
        self.vital_manager = vital_manager
        # サリエンスネットワークシステム（サリア）
        self.salia: Optional[Salia] = None

        # VitalManagerの日次リセット時にコンテキストの記憶を再読み込みするコールバックを設定
        if self.vital_manager:
            self.vital_manager.on_day_reset = self.context.reload_memories

        # スケジューラにRAGデータベースの参照を渡す（フラッシュバック生成に使用）
        if self.scheduler:
            self.scheduler.rag_db = self.rag_db
      
        # デバッグ用：最後にLLMに送信したmessagesリストとトークン情報
        self.last_debug_context = None
        # デバッグ用：直前の発言を生んだ「実際にLLMへ送った入力」のスナップショット（読み取り専用ビュー用）
        # 最新ビュー（last_debug_context）が応答後に最新化されても、こちらは送信時点の入力を凍結保持する。
        self.last_input_context = None

        # 秘密ファイルシステム（AES-256-GCM暗号化）の初期化
        import os
        from core.secret import SecretManager
        from core.paths import data_root
        # 秘密鍵は data_root 基準で解決する（従来は install_root 直下だったが、配布版では
        # install_root が読み取り専用になり得るため）。dev（data_root == bundle_root）では
        # 従来と同一パス（agent/.secret_key）。
        key_path = str(data_root() / ".secret_key")
        self.secret_manager = SecretManager(
            workspace_path=str(self.memory.workspace),
            key_path=key_path
        )

    async def _update_debug_context(self, messages=None, is_input=False):
        """現在のコンテキスト状態をデバッグ用に計測・保存し、on_context_updateコールバック経由でUIに通知する。

        is_input=True のとき、このスナップショットを「直前の発言を生んだ入力」として
        last_input_context に凍結保存する（DEBUG画面の読み取り専用ビュー用）。
        通常の最新化呼び出し（is_input=False）では last_input_context は据え置き、
        配信payloadに input_context として相乗りさせる（最新ビューと入力ビューの両方を1配信で届ける）。
        """
        from core.tokens import count_message_tokens, count_text_tokens
        import json

        if messages is None:
            messages = self.context.build_messages()

        # --- DEBUG番号 → 会話履歴インデックス の対応付け ---
        # build_messages() の構成:
        #   [system_top, system_memories?, 要約?]  ← 先頭の system ブロック（prefix）
        #   + conversation_history（_filter_conversation_history で件数不変）
        #   + system_bottom?（ポストプロンプト） + prefill?（DeepSeek /beta） ← 末尾（trailing）
        #
        # 「履歴領域内の非systemメッセージを順番に数えて history_idx を振る」方式にする。
        #   - conversation_history には system ロールが存在しない（system_notice も role=user）ため、
        #     履歴領域内に現れる system はスキップ対象（通常は存在しない）。
        #   - prefix は先頭の連続する system ロールの個数（履歴の手前まで）として求める。
        hist = self.context.conversation_history
        hist_len = len(hist)
        has_bottom = bool(getattr(self.context, "system_bottom", ""))
        has_prefill = bool(getattr(self.context, "_pending_prefill", ""))
        trailing = (1 if has_bottom else 0) + (1 if has_prefill else 0)
        region_end = len(messages) - trailing

        # 履歴領域の開始位置 = 先頭の連続する system ロールの直後
        prefix = 0
        while prefix < len(messages) and messages[prefix].get("role") == "system":
            prefix += 1

        # --- 秘密日記の中身をデバッグ画面に漏らさないための伏せ字処理 ---
        # 秘密日記（read/write/edit_secret）はオーナーにも読めない暗号化領域。
        # デバッグ画面にツール結果（復号した本文）や書き込み引数（本文）が
        # 平文で出てしまうと設計が破れるため、ここでプレースホルダに伏せる。
        # JS側ではなくサーバ側で伏せることで、平文がデバッグWebSocketにすら載らない。
        _SECRET_TOOL_NAMES = {"read_secret", "write_secret", "edit_secret"}
        _SECRET_PLACEHOLDER = "🔒 秘密日記の内容はデバッグ画面では非表示です"

        # tool_call_id → そのツール名が秘密系か（toolロール結果を伏せるか判定するため）
        secret_tool_call_ids = set()
        for m in messages:
            if m.get("role") == "assistant":
                for tc in (m.get("tool_calls") or []):
                    fn = tc.get("function", {}) or {}
                    if fn.get("name") in _SECRET_TOOL_NAMES and tc.get("id"):
                        secret_tool_call_ids.add(tc.get("id"))

        def _redact_secret_tool_calls(tool_calls):
            """秘密系ツールの引数のうち本文（content / append_content）だけ伏せる。
            filename は残す。実体（会話履歴）は変えず、表示用にコピーして加工する。"""
            if not tool_calls:
                return tool_calls
            out = []
            for tc in tool_calls:
                fn = tc.get("function", {}) or {}
                if fn.get("name") not in _SECRET_TOOL_NAMES:
                    out.append(tc)
                    continue
                new_tc = dict(tc)
                new_fn = dict(fn)
                try:
                    args = json.loads(fn.get("arguments", "") or "{}")
                except Exception:
                    args = {}
                if isinstance(args, dict):
                    for key in ("content", "append_content"):
                        if key in args:
                            args[key] = _SECRET_PLACEHOLDER
                    new_fn["arguments"] = json.dumps(args, ensure_ascii=False)
                new_tc["function"] = new_fn
                out.append(new_tc)
            return out

        # 各メッセージのトークン数を個別に計測
        debug_messages = []
        msg_tokens_sum = 0
        hist_counter = 0  # 履歴領域内で見つけた非systemメッセージの通し番号（= conversation_history の index）
        for i, m in enumerate(messages):
            m_tokens = count_message_tokens(m)
            msg_tokens_sum += m_tokens

            # このメッセージが会話履歴由来なら history_idx を割り当てる
            history_idx = None
            is_turn_start = False
            is_layer0 = False
            # 履歴領域内かつ非systemなら履歴メッセージ
            if prefix <= i < region_end and m.get("role") != "system" and hist_counter < hist_len:
                history_idx = hist_counter
                hist_counter += 1
                if m.get("role") == "user":
                    # 境界/Layer0判定は生の会話履歴テキストで（分割後のcore本文ではなく実体で）
                    raw_text = self.context._get_text_from_content(hist[history_idx].get("content", ""))
                    is_layer0 = "<!-- layer0 -->" in raw_text
                    is_turn_start = self.context._is_turn_boundary(raw_text)

            # 秘密日記の伏せ字: read_secret の結果（toolロール本文）と
            # write/edit_secret の引数本文を隠す。
            # secret_redacted は「本文（content）を伏せた」場合のみ立て、JS側で
            # その要素を編集・置換不可にする（伏せ字を保存して実体を壊さないため）。
            # 引数だけ伏せた assistant メッセージの本文は実体の発言なので編集可のまま。
            content = m.get("content")
            tool_calls = _redact_secret_tool_calls(m.get("tool_calls"))
            secret_redacted = False
            if m.get("role") == "tool" and m.get("tool_call_id") in secret_tool_call_ids:
                content = _SECRET_PLACEHOLDER
                secret_redacted = True

            debug_messages.append({
                "role": m.get("role"),
                "content": content,
                "tokens": m_tokens,
                "tool_calls": tool_calls,
                "tool_call_id": m.get("tool_call_id"),
                "history_idx": history_idx,
                "is_turn_start": is_turn_start,
                "is_layer0": is_layer0,
                "secret_redacted": secret_redacted,
            })
        
        # ツール定義のトークン数を計測（LLMに送信される隠れコスト）
        # 配布版では git 系を除外した実際の提示ツールで計測・表示する。
        _tools_for_llm = get_tool_definitions()
        tools_json = json.dumps(_tools_for_llm, ensure_ascii=False)
        tools_tokens = count_text_tokens(tools_json)
        total_tokens = msg_tokens_sum + tools_tokens

        snapshot = {
            "messages": debug_messages,
            "tools": _tools_for_llm,
            "tools_tokens": tools_tokens,
            "total_tokens": total_tokens,
            "timestamp": datetime.now(JST).isoformat()
        }

        # 送信時の呼び出し（is_input=True）なら、これを「入力スナップショット」として凍結保存する。
        # snapshot 自体は input_context キーを持たないため、後段で相乗りさせても循環参照は起きない。
        if is_input:
            self.last_input_context = snapshot

        # 配信payload = 最新スナップショット ＋ 凍結入力スナップショット（input_context として相乗り）。
        # フロントは messages=最新（編集・圧縮対象）、input_context.messages=直前入力（読み取り専用）を出し分ける。
        payload = dict(snapshot)
        payload["input_context"] = self.last_input_context
        self.last_debug_context = payload

        # server.pyのregister_debug_contextを通じてデバッグWebSocketクライアントに配信
        if self.on_context_update:
            await self.on_context_update(payload)

    def _save_compression_to_memory(self, summary: str):
        """
        圧縮された会話要約をlogs/summary/YYYY-MM-DD.mdに追記する。

        ファイルが存在しなければヘッダー付きで新規作成、存在すれば末尾に追記する。
        RAGのdaily_memoriesコレクションにも自動登録する。
        """
        now_jst = datetime.now(JST)
        date_str = get_logical_date()
        time_str = now_jst.strftime("%H:%M JST")
        filename = f"logs/summary/{date_str}.md"

        content = f"\n\n## コンテキスト圧縮 ({time_str})\n\n{summary}\n"

        try:
            existing = self.memory.read_file(filename)
            if existing is not None:
                self.memory.edit_file(filename, content)
            else:
                header = f"# {date_str} の記憶\n"
                self.memory.write_file(filename, header + content)
        except Exception as e:
            print(f"警告: 圧縮記憶の保存に失敗しました: {e}")

        # RAGに自動登録（日次要約として検索可能にする）
        try:
            if self.rag_db:
                self.rag_db.add("daily_memories", content, {
                    "date": date_str,
                    "source": filename
                })
                tlog(f"[RAG] daily_memoriesに自動登録しました: {date_str}")
        except Exception as e:
            print(f"警告: RAGへの自動登録に失敗しました: {e}")
            
    # =========================================================
    # Layer0: 常時整理型圧縮（毎ターン・最古ターンを1件ずつ整形）
    # =========================================================

    def _format_user_for_layer0(self, raw_text: str) -> str:
        """
        userロールのrawテキストをLayer0保存用に整形する（ルールベース）。

        仕様書テーブルに従い、不要なシステム情報を削除・圧縮し、
        人間が読める最小限の形式に変換する。末尾に <!-- layer0 --> マーカーを付与。

        変換ルール:
          - [SYSTEM] ブロック: 時刻(YYYY-MM-DD HH:MM)と天気のみ残す（使用率・祝日は削除）
          - <user_message>...</user_message>: "user: 内容" として残す
          - <moonbeat_instruction>...</moonbeat_instruction>: "moonbeat" に置換
          - <system_notice>...</system_notice>: "notice: 内容" として残す
          - <self_memo>...</self_memo>: 削除
          - <assistant_inner>...</assistant_inner>: 削除
        """
        import re

        result_parts = []

        # --- [SYSTEM] ブロックの整形 ---
        # 時刻を YYYY-MM-DD HH:MM 形式で抽出。context.py の日付フォーマットは language で
        # 切り替わるため（ja: 「YYYY年MM月DD日（曜） …」/ en: 「YYYY-MM-DD (Wd) …」）、
        # ここでも language 別の正規表現で抽出する。
        if get_language() == "ja":
            time_match = re.search(r'(\d{4})年(\d{2})月(\d{2})日.*?(\d{2}:\d{2})', raw_text)
        else:
            time_match = re.search(r'(\d{4})-(\d{2})-(\d{2})\s+\([^)]*\)\s+(\d{2}:\d{2})', raw_text)
        if time_match:
            y, mo, d, hm = time_match.groups()
            result_parts.append(f"{y}-{mo}-{d} {hm}")

        # 現在の気温と天気のみ抽出。天気文字列は core/weather.py の言語別出力に合わせる。
        # ja: 「現在NN℃」「現在の空: 〜」 / en: 「Now NN°C」「Sky now: 〜」
        if get_language() == "ja":
            temp_match = re.search(r'現在(\d+\.?\d*)℃', raw_text)
            weather_match = re.search(r'現在の空:\s*(.+)', raw_text)
            temp_unit = "℃"
        else:
            temp_match = re.search(r'Now\s+(\d+\.?\d*)°C', raw_text)
            weather_match = re.search(r'Sky now:\s*(.+)', raw_text)
            temp_unit = "°C"
        temp_str = temp_match.group(1) + temp_unit if temp_match else ""
        weather_str = weather_match.group(1).strip() if weather_match else ""
        if temp_str or weather_str:
            result_parts.append(f"{temp_str} {weather_str}".strip())

        # --- 各XMLタグの変換 ---
        # <user_message> → "user: 内容"
        for m in re.finditer(r'<user_message>\s*(.*?)\s*</user_message>', raw_text, re.DOTALL):
            content = m.group(1).strip()
            if content:
                result_parts.append(f"user: {content}")

        # <moonbeat_instruction> → "moonbeat"
        if re.search(r'<moonbeat_instruction>', raw_text):
            result_parts.append("moonbeat")

        # <task_notice> → "task: 内容"
        for m in re.finditer(r'<task_notice>\s*(.*?)\s*</task_notice>', raw_text, re.DOTALL):
            content = m.group(1).strip()
            if content:
                result_parts.append(f"task: {content}")

        # <city_event_notice> → "city_event: 内容"
        for m in re.finditer(r'<city_event_notice>\s*(.*?)\s*</city_event_notice>', raw_text, re.DOTALL):
            content = m.group(1).strip()
            if content:
                result_parts.append(f"city_event: {content}")

        # <system_notice> は削除（内部通知なので不要）
      
        # <self_memo>, <assistant_inner> は削除（含めない）

        # --- マーカー付与 ---
        result_parts.append("<!-- layer0 -->")

        return "\n".join(result_parts)

    async def _layer0_compress_turn(self, start_idx: int, turn_msgs: list, layer0_prompt: str) -> bool:
        """
        1ターン分のメッセージ群（user + assistant + tool）をLayer0圧縮し、
        conversation_history を圧縮済みの user/assistant ペアに置換する。

        3am定期圧縮（scheduler._check_layer0_compression）と
        手動部分圧縮（compress_layer0_at）の共通処理。これにより両者のロジックを一元化する。

        - fact_buffer への追記まで行う。
        - wyrd への登録（process_fact_buffer）は呼び出し側でバッチ実行する（1ターンごとに呼ばない）。
        - conversation_history の置換は replace_turn_with_layer0 経由のみ（= <!-- layer0 --> マーカー付与）。
          これによりトークン表示（get_token_usage）が自動的に正しくなる。

        Args:
            start_idx:     置換対象ターンの開始インデックス
            turn_msgs:     そのターンのメッセージ群（context.find_turn_at / extract_oldest_uncompressed_turn の戻り値）
            layer0_prompt: Layer0圧縮用システムプロンプト

        Returns:
            成功時 True / LLM失敗・空応答・圧縮結果が空の場合 False
        """
        user_msg = turn_msgs[0]
        raw_user_text = self.context._get_text_from_content(user_msg.get("content", ""))
        formatted_user = self._format_user_for_layer0(raw_user_text)

        # --- ターン全体を人間可読テキストに変換 ---
        turn_text_parts = [f"[user]\n{raw_user_text}"]
        for msg in turn_msgs[1:]:
            role = msg.get("role", "")
            content = self.context._get_text_from_content(msg.get("content", ""))
            if role == "assistant":
                turn_text_parts.append(f"[assistant]\n{content}")
            elif role == "tool":
                import re as _re
                clean_content = _re.sub(r'\n*<system_notice>.*?</system_notice>', '', content, flags=_re.DOTALL).strip()
                turn_text_parts.append(f"[tool_result]\n{clean_content}")
        turn_text = "\n\n".join(turn_text_parts)

        # --- LLM呼び出し ---
        summary_messages = [
            {"role": "system", "content": layer0_prompt},
            {"role": "user", "content": turn_text},
        ]
        try:
            response = await self.llm.chat(summary_messages, tools=None)
            raw_response = (response.content or "").strip()
        except Exception as e:
            tlog(f"[Layer0] LLM呼び出し失敗、スキップします: {e}")
            return False

        if not raw_response:
            tlog("[Layer0] LLMの応答が空のため、スキップします")
            return False

        # --- レスポンスのパース ---
        compressed_assistant, facts = self._parse_layer0_response(raw_response)
        if not compressed_assistant:
            tlog("[Layer0] 圧縮結果が空のため、スキップします")
            return False

        # --- 事実をfact_bufferに追記（wyrd登録は呼び出し側でまとめて） ---
        if facts:
            self._append_to_fact_buffer(facts, user_msg)

        # --- conversation_historyの該当ターンを圧縮済みペアに置換 ---
        self.context.replace_turn_with_layer0(
            start_idx, len(turn_msgs), formatted_user, compressed_assistant,
        )

        # --- Layer0ログを書き出す ---
        try:
            from core.time_utils import JST
            from datetime import datetime
            import json as _json
            now = datetime.now(JST)
            log_dir = self.memory.workspace / "logs" / "layer0"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"{now.strftime('%Y-%m-%d')}.jsonl"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(_json.dumps(
                    {"timestamp": now.strftime("%H:%M:%S"), "user": formatted_user, "assistant": compressed_assistant},
                    ensure_ascii=False) + "\n")
        except Exception as e:
            tlog(f"[Layer0] ログ書き出し失敗: {e}")

        return True

    async def compress_layer0_at(self, history_indices: list) -> dict:
        """
        指定した会話履歴インデックス（を含むターン）を手動でLayer0圧縮する。

        DEBUG画面の「このターンを圧縮」ボタンから呼ばれる。
        多ステップ（ツール連打）のターンを1ターン1ステップに畳み、
        強化ループの元になった記憶を薄くする目的。

        【重要】記憶ファイル（ノート・手紙・self_memo）には一切触れない。会話コンテキストのみ。
        【重要】Layer0化は replace_turn_with_layer0 経由のみ → トークン表示は自動で整合。

        Args:
            history_indices: conversation_history のインデックス（DEBUG画面のhistory_idx）のリスト

        Returns:
            {"success": bool, "message": str, "compressed_turns": int, "token_usage": dict}
        """
        import json as _json

        # --- 対象ターンを解決（同一ターンはstart_idxで重複排除、既Layer0/無効はスキップ）---
        resolved = {}  # start_idx -> turn_msgs
        for hi in history_indices:
            try:
                turn = self.context.find_turn_at(int(hi))
            except (TypeError, ValueError):
                continue
            if turn is None:
                continue
            s, msgs = turn
            resolved[s] = msgs

        if not resolved:
            return {
                "success": False,
                "message": "圧縮対象のRAWターンが見つかりません（既にLayer0化済み、または無効な位置です）",
                "compressed_turns": 0,
                "token_usage": self.context.get_token_usage(),
            }

        # --- Layer0プロンプト読み込み（1回だけ）---
        config_path = config_file("compression_config.json")
        config = _json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        prompt_file = config.get("layer0_prompt_file", "data/compression_prompt_layer0.txt")
        prompt_path = resolve_path(prompt_file)  # data_root 基準で解決（"data/..." 相対値を data_root/data/... に）
        layer0_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else (
            "以下の1ターン分の活動ログを、assistantの発言を口調・感情・固有名詞を保ちながら"
            "1つのassistant発言にまとめてください。"
        )
        # {{agent_name}} / {{user_honorific}} プレースホルダを実際の値に置換
        layer0_prompt = apply_prompt_placeholders(layer0_prompt, self.agent_name, self.honorific)

        token_before = self.context.get_token_count()
        compressed = 0
        # start_idx の大きい方から処理 → 置換でターンが縮んでも後続インデックスがずれない
        for s in sorted(resolved.keys(), reverse=True):
            ok = await self._layer0_compress_turn(s, resolved[s], layer0_prompt)
            if ok:
                compressed += 1

        if compressed > 0:
            self.context.save_state()
            # --- 記憶グラフ更新（3amと同じ非同期経路）---
            try:
                from core.wyrd_network import load_graph, process_fact_buffer_async, node_count
                graph = load_graph()

                async def llm_fn(prompt):
                    response = await self.llm.chat([{"role": "user", "content": prompt}], tools=None)
                    return response.content or ""

                cnt = await process_fact_buffer_async(
                    graph, embed_fn=self._get_embedding, llm_fn=llm_fn, agent_name=self.agent_name)
                if cnt > 0:
                    tlog(f"[Layer0手動] Wyrd Network: {cnt}件追加, {node_count(graph)}")
            except Exception as e:
                tlog(f"[Layer0手動] wyrd更新エラー: {e}")
            await self._update_debug_context()

        token_after = self.context.get_token_count()
        return {
            "success": compressed > 0,
            "message": f"{compressed}ターンをLayer0圧縮しました（{token_before:,} → {token_after:,} トークン）",
            "compressed_turns": compressed,
            "token_usage": self.context.get_token_usage(),
        }

    async def edit_history_message(self, history_idx, new_content: str,
                                   expected_content: str = None) -> dict:
        """
        DEBUG画面のインライン編集: 会話履歴のメッセージ本文を書き換える。

        成功時は永続化（save_state）とデバッグ再描画（_update_debug_context）まで行う。
        記憶ファイルには一切触れない。
        """
        r = self.context.edit_message(history_idx, new_content, expected_content)
        if r.get("success"):
            self.context.save_state()
            await self._update_debug_context()
        r["token_usage"] = self.context.get_token_usage()
        return r

    async def replace_history_text(self, *, find: str, replacement: str,
                                   start_idx: int = None, end_idx: int = None,
                                   occurrence: int = None,
                                   use_regex: bool = False, case_sensitive: bool = True,
                                   dry_run: bool = False) -> dict:
        """
        DEBUG画面の検索置換: 会話履歴本文に対する一括/単独置換（およびプレビュー）。

        dry_run=True はマッチ件数のみ返し実体を変更しない。実置換に成功した場合のみ
        永続化とデバッグ再描画を行う。記憶ファイルには一切触れない。
        """
        r = self.context.replace_text_in_history(
            find, replacement,
            start_idx=start_idx, end_idx=end_idx, occurrence=occurrence,
            use_regex=use_regex, case_sensitive=case_sensitive, dry_run=dry_run)
        if r.get("success") and not dry_run:
            self.context.save_state()
            await self._update_debug_context()
        r["token_usage"] = self.context.get_token_usage()
        return r

    async def _compress_layer0_if_needed(self):
        """
        Layer0常時整理型圧縮を実行する。

        毎ターン終了後に呼ばれ、未圧縮ターン数が layer0_keep_turns を超えていれば
        最古のターン1件をルールベース整形 + LLM圧縮して user/assistant ペアに置換する。
        Layer1/2 圧縮とは独立して動作し、既存の圧縮機構には影響しない。

        処理フロー:
          1. compression_config.json から layer0_keep_turns を読む
          2. 未圧縮ターン数を確認
          3. 閾値超過なら最古ターンを取得
          4. user テキストをルールベースで整形
          5. ターン全体を LLM に渡して assistant 部分を圧縮
          6. conversation_history を圧縮済みペアに置換
          7. 状態を永続化
        """
        import json as _json

        # --- 設定読み込み ---
        config_path = config_file("compression_config.json")
        config = _json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        keep_turns = config.get("layer0_keep_turns", 5)

        # --- 発動チェック＆バッチ処理 ---
        max_batch = config.get("layer0_max_batch", 10)
        processed = 0

        while True:
            uncompressed = self.context.count_uncompressed_turns()
            if uncompressed <= keep_turns:
                break
            if processed >= max_batch:
                break

            tlog(f"[Layer0] 未圧縮ターン数={uncompressed} > 閾値={keep_turns}、最古ターンを圧縮します ({processed+1}/{max_batch})")

            result = self.context.extract_oldest_uncompressed_turn()
            if result is None:
                break
            start_idx, turn_msgs = result

            # --- ユーザーロールのルールベース整形 ---
            user_msg = turn_msgs[0]
            raw_user_text = self.context._get_text_from_content(user_msg.get("content", ""))
            formatted_user = self._format_user_for_layer0(raw_user_text)

            # --- LLMに渡すためにターン全体を人間可読テキストに変換 ---
            turn_text_parts = [f"[user]\n{raw_user_text}"]
            for msg in turn_msgs[1:]:
                role = msg.get("role", "")
                content = self.context._get_text_from_content(msg.get("content", ""))
                if role == "assistant":
                    turn_text_parts.append(f"[assistant]\n{content}")
                elif role == "tool":
                    import re as _re
                    clean_content = _re.sub(r'\n*<system_notice>.*?</system_without>', '', content, flags=_re.DOTALL).strip()
                    turn_text_parts.append(f"[tool_result]\n{clean_content}")

            turn_text = "\n\n".join(turn_text_parts)

            # --- Layer0プロンプト読み込み ---
            prompt_file = config.get("layer0_prompt_file", "data/compression_prompt_layer0.txt")
            prompt_path = resolve_path(prompt_file)  # data_root 基準で解決（"data/..." 相対値を data_root/data/... に）
            if prompt_path.exists():
                layer0_prompt = prompt_path.read_text(encoding="utf-8")
            else:
                layer0_prompt = (
                    "以下の1ターン分の活動ログを、assistantの発言を口調・感情・固有名詞を保ちながら"
                    "1つのassistant発言にまとめてください。"
                )
            # {{agent_name}} / {{user_honorific}} プレースホルダを実際の値に置換
            layer0_prompt = apply_prompt_placeholders(layer0_prompt, self.agent_name, self.honorific)

            # --- LLM呼び出し ---
            summary_messages = [
                {"role": "system", "content": layer0_prompt},
                {"role": "user", "content": turn_text},
            ]
            try:
                response = await self.llm.chat(summary_messages, tools=None)
                raw_response = (response.content or "").strip()
            except Exception as e:
                tlog(f"[Layer0] LLM呼び出し失敗、スキップします: {e}")
                break

            if not raw_response:
                tlog("[Layer0] LLMの応答が空のため、スキップします")
                break

            # --- レスポンスのパース ---
            compressed_assistant, facts = self._parse_layer0_response(raw_response)
            tlog(f"[Layer0] facts={'あり' if facts else 'なし'} (len={len(facts)})")

            if not compressed_assistant:
                tlog("[Layer0] 圧縮結果が空のため、スキップします")
                break

            # --- 事実をfact_bufferに追記 ---
            if facts:
                self._append_to_fact_buffer(facts, turn_msgs[0])

            # --- conversation_historyの該当ターンを圧縮済みペアに置換 ---
            self.context.replace_turn_with_layer0(
                start_idx,
                len(turn_msgs),
                formatted_user,
                compressed_assistant,
            )
            processed += 1
            # --- Layer0ログを書き出す ---
            try:
                from core.time_utils import JST
                from datetime import datetime
                import json as _json
                now = datetime.now(JST)
                date_str = now.strftime("%Y-%m-%d")
                log_dir = self.memory.workspace / "logs" / "layer0"
                log_dir.mkdir(parents=True, exist_ok=True)
                log_path = log_dir / f"{date_str}.jsonl"
                entry = {
                    "timestamp": now.strftime("%H:%M:%S"),
                    "user": formatted_user,
                    "assistant": compressed_assistant,
                }
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception as e:
                tlog(f"[Layer0] ログ書き出し失敗: {e}")

        # --- 状態を永続化（ループ外で1回だけ）---
        if processed > 0:
            self.context.save_state()
            tlog(f"[Layer0] {processed}件圧縮完了")
            # 記憶グラフ更新
            graph = load_graph()
            count = process_fact_buffer(graph, embed_fn=self._get_embedding, agent_name=self.agent_name)
            if count > 0:
                tlog(f"[MemoryGraph] {count}件のエピソードを追加, {node_count(graph)}")

    def _parse_layer0_response(self, raw: str) -> tuple[str, str]:
        """
        Layer0 LLMレスポンスを <compression> と <facts> にパースする。
        タグが見つからない場合は全体を圧縮結果として扱い、factsは空とする（後方互換）。
        """
        import re

        compression_match = re.search(r"<compression>(.*?)</compression>", raw, re.DOTALL)
        facts_match = re.search(r"<facts>(.*?)</facts>", raw, re.DOTALL)

        if compression_match:
            compressed = compression_match.group(1).strip()
        else:
            # タグなしの場合は全体を圧縮結果として扱う（後方互換）
            compressed = raw.strip()

        facts = ""
        if facts_match:
            facts_raw = facts_match.group(1).strip()
            if facts_raw.lower() != "なし":
                facts = facts_raw

        return compressed, facts

    def _append_to_fact_buffer(self, facts: str, user_msg: dict) -> None:
        """
        抽出された事実を data/fact_buffer.jsonl に追記する。
        """
        import json as _json
        from core.time_utils import JST
        from datetime import datetime
    
        buffer_path = data_file("fact_buffer.jsonl")
        buffer_path.parent.mkdir(parents=True, exist_ok=True)
    
        now = datetime.now(JST)
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%S")
    
        # E行・C行・S行をセットで抽出
        lines = facts.strip().split("\n")
        current_episode = None
        current_concepts = None
    
        for line in lines:
            line = line.strip()
            if line.startswith("E:"):
                # 前のセットがS行なしで終わった場合を保存
                if current_episode and current_concepts:
                    entry = {
                        "timestamp": timestamp,
                        "content": current_episode,
                        "concepts": current_concepts,
                        "valence": 0.0
                    }
                    with open(buffer_path, "a", encoding="utf-8") as f:
                        f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
                current_episode = line[2:].strip()
                current_concepts = None
            elif line.startswith("C:") and current_episode:
                current_concepts = [c.strip() for c in line[2:].split(",") if c.strip()]
            elif line.startswith("S:") and current_episode and current_concepts:
                valence_str = line[2:].strip()
                try:
                    if "~" in valence_str:
                        parts = valence_str.split("~")
                        valence = (float(parts[0]) + float(parts[1])) / 2
                    else:
                        valence = float(valence_str)
                except ValueError:
                    valence = 0.0
                entry = {
                    "timestamp": timestamp,
                    "content": current_episode,
                    "concepts": current_concepts,
                    "valence": round(valence, 2)
                }
                with open(buffer_path, "a", encoding="utf-8") as f:
                    f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
                current_episode = None
                current_concepts = None
    
        # 最後のセットがS行なしで終わった場合
        if current_episode and current_concepts:
            entry = {
                "timestamp": timestamp,
                "content": current_episode,
                "concepts": current_concepts,
                "valence": 0.0
            }
            with open(buffer_path, "a", encoding="utf-8") as f:
                f.write(_json.dumps(entry, ensure_ascii=False) + "\n")

    def _get_embedding(self, text: str) -> list[float]:
        """テキストのembeddingベクトルを生成（RAGのモデルを再利用）"""
        if self.rag_db and hasattr(self.rag_db, '_ef'):
            return self.rag_db._ef.embed_query([text])[0]

        # RAG未初期化時のフォールバック
        if not hasattr(self, "_embed_model"):
            from sentence_transformers import SentenceTransformer
            # 二刀流: 同梱 models/ があればローカルからオフライン読み込み、無ければ HF キャッシュから解決
            from core.paths import resolve_model
            _model_src, _local_only = resolve_model("multilingual-e5-small", "intfloat/multilingual-e5-small")
            self._embed_model = SentenceTransformer(_model_src, local_files_only=_local_only)

        embedding = self._embed_model.encode(text, normalize_embeddings=True)
        return embedding.tolist()
  
    async def _compress_if_needed(self):
        """
        緊急圧縮: emergency_compression_threshold を超えた場合にのみ、
        Layer0済みターンを layer1_emergency_count 件圧縮する。
        
        定期圧縮（compress_layer1_scheduled）と異なり、
        バッチ処理ではなくリアルタイムの緊急対応用。
        """
        import json as _json
        if not self.context.needs_emergency_compression():
            return

        # 緊急圧縮件数を設定から読む
        config_path = config_file("compression_config.json")
        config = _json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        count = config.get("layer1_emergency_count", 200)

        old_messages, recent_messages = self.context.build_compression_messages(count=count)

        if not old_messages:
            return

        tlog(f"[緊急圧縮] Layer0済みターンを圧縮中... (トークン数: {self.context.get_token_count()}, count={count})")

        # 古い会話履歴をターン単位で要約
        summary_lines = await self._summarize_turns(old_messages)

        # 要約結果をlayer1として適用し、直近の会話のみ残す
        self.context.apply_compression(summary_lines, recent_messages)

        # layer1が上限を超えていたらlayer2にさらに圧縮
        await self._compress_layer1_if_needed()

        # 日次要約ファイルに追記 + RAG登録
        self._save_compression_to_memory("\n".join(summary_lines))

        # 圧縮の実行ログを記録
        if self.logger:
            self.logger.log_full_event("emergency_compression", {
                "summary_lines": len(summary_lines),
                "old_message_count": len(old_messages),
                "remaining_message_count": len(recent_messages),
                "new_token_count": self.context.get_token_count(),
                "layer1_tokens": self.context.get_layer1_token_count(),
                "layer2_tokens": self.context.get_layer2_token_count(),
            })

        # デバッグ用コンテキスト情報を最新化
        await self._update_debug_context()

        tlog(f"[緊急圧縮] 完了 (圧縮後トークン数: {self.context.get_token_count()}, L1: {self.context.get_layer1_token_count()}, L2: {self.context.get_layer2_token_count()})")

    async def compress_layer1_scheduled(self):
        """
        定期圧縮: compression_threshold を超えている場合にのみ、
        Layer0済みターンを layer1_scheduled_count 件圧縮する。

        scheduler.pyの_check_layer1_compression（深夜3時）から呼び出される。
        柳月を介さない純粋なバッチ処理として実行する。
        """
        import json as _json

        if not self.context.needs_compression():
            tlog("[Layer1定期] 圧縮不要（閾値以下）")
            return

        # 定期圧縮件数を設定から読む
        config_path = config_file("compression_config.json")
        config = _json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        count = config.get("layer1_scheduled_count", 400)

        old_messages, recent_messages = self.context.build_compression_messages(count=count)

        if not old_messages:
            tlog("[Layer1定期] 圧縮対象なLayer0済みターンなし")
            return

        tlog(f"[Layer1定期] 圧縮開始 (トークン数: {self.context.get_token_count()}, count={count})")

        # 古い会話履歴をターン単位で要約
        summary_lines = await self._summarize_turns(old_messages)

        # 要約結果をlayer1として適用
        self.context.apply_compression(summary_lines, recent_messages)

        # layer1が上限を超えていたらlayer2にさらに圧縮
        await self._compress_layer1_if_needed()

        # 日次要約ファイルに追記 + RAG登録
        self._save_compression_to_memory("\n".join(summary_lines))

        # 圧縮完了後に状態を永続化
        self.context.save_state()

        # 圧縮の実行ログを記録
        if self.logger:
            self.logger.log_full_event("scheduled_compression", {
                "summary_lines": len(summary_lines),
                "old_message_count": len(old_messages),
                "remaining_message_count": len(recent_messages),
                "new_token_count": self.context.get_token_count(),
                "layer1_tokens": self.context.get_layer1_token_count(),
                "layer2_tokens": self.context.get_layer2_token_count(),
            })

        tlog(f"[Layer1定期] 完了 (圧縮後トークン数: {self.context.get_token_count()}, L1: {self.context.get_layer1_token_count()}, L2: {self.context.get_layer2_token_count()})")

    async def _summarize_turns(self, old_messages: list[dict]) -> list[str]:
        """
        会話履歴をターン単位でLLMに要約させる。

        メッセージをターンに分割し、設定されたバッチサイズごとにLLMへ送信して
        1〜3行の要約を取得する。各行にはターンのタイムスタンプが付与される。

        Args:
            old_messages: 圧縮対象のメッセージリスト

        Returns:
            各ターンの1行要約リスト（不要と判定された「-」は除外済み）
        """
        import json as _json

        # 圧縮パラメータの読み込み
        config_path = config_file("compression_config.json")
        config = _json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}

        # 要約プロンプトの読み込み（カスタマイズ可能）
        prompt_file = config.get("turn_summary_prompt_file", "data/compression_prompt_turn.txt")
        prompt_path = resolve_path(prompt_file)  # data_root 基準で解決（"data/..." 相対値を data_root/data/... に）
        if prompt_path.exists():
            turn_prompt = prompt_path.read_text(encoding="utf-8")
        else:
            turn_prompt = t("agent_turn_summary_fallback", agent_name=self.agent_name)
        # {{agent_name}} / {{user_honorific}} プレースホルダを実際の値に置換
        turn_prompt = apply_prompt_placeholders(turn_prompt, self.agent_name, self.honorific)

        turns_per_batch = config.get("turns_per_batch", 5)

        # メッセージをターン単位（user→次のuserまで）に分割
        turns = self._split_into_turns(old_messages)

        # バッチごとにLLMに要約させる
        all_summary_lines = []
        for i in range(0, len(turns), turns_per_batch):
            batch = turns[i:i + turns_per_batch]
            batch_text = ""
            batch_times = []

            # 各ターンをテキスト形式に変換（ターンの時刻も抽出）
            for turn_idx, turn in enumerate(batch):
                # ターンの時刻をuserメッセージのシステム注入部分から抽出
                turn_time = ""
                for msg in turn:
                    if msg.get("role") == "user":
                        import re
                        msg_text = self.context._get_text_from_content(msg.get("content", ""))
                        # 言語別フォーマットに対応。詳細は _format_user_for_layer0 のコメント参照。
                        if get_language() == "ja":
                            time_match = re.search(r'(\d{4})年(\d{2})月(\d{2})日.*?(\d{2}:\d{2})', msg_text)
                        else:
                            time_match = re.search(r'(\d{4})-(\d{2})-(\d{2})\s+\([^)]*\)\s+(\d{2}:\d{2})', msg_text)
                        if time_match:
                            y, m, d, hm = time_match.groups()
                            turn_time = f"{y[2:]}{m}{d} {hm}"
                        break
                batch_times.append(turn_time)

                # ターンの内容を人間が読める形式に整形
                batch_text += "\n" + t("agent_turn_separator", n=turn_idx + 1) + "\n"
                for msg in turn:
                    role = msg.get("role", "unknown")
                    content = self.context._get_text_from_content(msg.get("content", ""))
                    if role == "user":
                        batch_text += f"{self.honorific}: {content}\n"
                    elif role == "assistant":
                        batch_text += f"{self.agent_name}: {content}\n"
                    elif role == "tool":
                        batch_text += t("agent_tool_result_label", content=content[:200]) + "\n"

            # LLMに要約を依頼
            summary_messages = [
                {"role": "system", "content": turn_prompt},
                {"role": "user", "content": batch_text},
            ]

            response = await self.llm.chat(summary_messages, tools=None)
            result = response.content or ""

            # 結果をパースし、各行にタイムスタンプを付与
            lines = [l.strip().lstrip("- ").strip() for l in result.strip().split("\n")]
            lines = [l for l in lines if l and l != "-"]
            time_prefix = batch_times[0] if batch_times else ""
            for line in lines:
                if time_prefix:
                    all_summary_lines.append(f"{time_prefix}: {line}")
                else:
                    all_summary_lines.append(line)

            tlog(f"[圧縮] バッチ {i // turns_per_batch + 1}/{(len(turns) + turns_per_batch - 1) // turns_per_batch} 完了 ({len(batch)}ターン)")

        return all_summary_lines

    def _split_into_turns(self, messages: list[dict]) -> list[list[dict]]:
        """
        メッセージリストをターン単位に分割する。
        1ターン = userメッセージから次のuserメッセージの手前まで
        （user, assistant, tool の一連のやりとりが1ターン）。
        """
        turns = []
        current_turn = []

        for msg in messages:
            # 新しいuserメッセージが来たら、それまでを1ターンとして区切る
            if msg["role"] == "user" and current_turn:
                turns.append(current_turn)
                current_turn = []
            current_turn.append(msg)

        # 最後のターンを追加
        if current_turn:
            turns.append(current_turn)

        return turns

    async def _compress_layer1_if_needed(self):
        """layer1の要約がトークン上限を超えていたら、layer2にさらに圧縮する。"""
        import json as _json
        config_path = config_file("compression_config.json")
        config = _json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}

        # layer2圧縮が必要かどうかと、圧縮対象の行を取得
        needs, old_lines = self.context.needs_layer2_compression(config)
        if not needs:
            return

        tlog(f"[圧縮] layer1→layer2 圧縮開始 ({len(old_lines)}行)")

        # layer2用要約プロンプトの読み込み（カスタマイズ可能）
        prompt_file = config.get("layer2_summary_prompt_file", "data/compression_prompt_layer2.txt")
        prompt_path = resolve_path(prompt_file)  # data_root 基準で解決（"data/..." 相対値を data_root/data/... に）
        if prompt_path.exists():
            layer2_prompt_template = prompt_path.read_text(encoding="utf-8")
        else:
            layer2_prompt_template = t("agent_layer2_summary_fallback")
        # {{agent_name}} / {{user_honorific}} プレースホルダを実際の値に置換
        # （{target_lines} は後段の .format() 用なので干渉しない）
        layer2_prompt_template = apply_prompt_placeholders(layer2_prompt_template, self.agent_name, self.honorific)

        compress_ratio = config.get("layer2_compress_lines_ratio", 0.25)
        batch_max = config.get("layer2_batch_max_lines", 60)

        # バッチに分割してLLMに再圧縮させる
        all_compressed = []
        for i in range(0, len(old_lines), batch_max):
            batch = old_lines[i:i + batch_max]
            target_lines = max(1, int(len(batch) * compress_ratio))
            prompt = layer2_prompt_template.replace("{target_lines}", str(target_lines))

            summary_messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "\n".join(f"- {line}" for line in batch)},
            ]

            response = await self.llm.chat(summary_messages, tools=None)
            result = response.content or ""

            for line in result.strip().split("\n"):
                line = line.strip().lstrip("- ").strip()
                if line:
                    all_compressed.append(line)

            tlog(f"[圧縮] layer2バッチ {i // batch_max + 1} 完了 ({len(batch)}行→{target_lines}行目標)")

        # 圧縮結果をlayer2としてコンテキストに適用
        self.context.apply_layer2_compression("\n".join(all_compressed), config)
        tlog(f"[圧縮] layer2 圧縮完了 (L2トークン: {self.context.get_layer2_token_count()})")

    async def force_compress(self, count=None) -> dict:
        """
        手動トリガーによる強制コンテキスト圧縮を実行する。

        直近1往復のみ残し、それ以前の全会話履歴を要約圧縮する。
        UIの「圧縮」ボタンから呼び出される。

        Returns:
            圧縮結果を含む辞書 {"success": bool, "message": str, "token_usage": dict}
        """
        # 要約対象: 直近1往復を除く全メッセージ
        if count is not None:
            # 件数指定: Layer0済みターンを古い方からcount件圧縮
            old_messages, recent_messages = self.context.build_compression_messages(count=count)
        else:
            # 従来動作: 直近1往復を除く全メッセージ
            old_messages, _ = self.context.build_compression_messages(keep_exchanges=0)
            _, recent_messages = self.context.build_compression_messages(keep_exchanges=1)

        if not old_messages:
            return {
                "success": False,
                "message": "圧縮対象の会話履歴がありません（直近の会話のみです）",
                "token_usage": self.context.get_token_usage(),
            }

        token_before = self.context.get_token_count()
        print(f"手動コンテキスト圧縮を実行中... (トークン数: {token_before})")

        # ターン単位で要約
        summary_lines = await self._summarize_turns(old_messages)

        # 要約結果をlayer1として適用
        self.context.apply_compression(summary_lines, recent_messages)

        # layer1→layer2の圧縮が必要か確認
        await self._compress_layer1_if_needed()

        # 日次要約ファイルに追記 + RAG登録
        self._save_compression_to_memory("\n".join(summary_lines))

        # 圧縮の実行ログを記録
        if self.logger:
            self.logger.log_full_event("manual_compression", {
                "summary_lines": len(summary_lines),
                "old_message_count": len(old_messages),
                "remaining_message_count": len(recent_messages),
                "token_before": token_before,
                "token_after": self.context.get_token_count(),
                "layer1_tokens": self.context.get_layer1_token_count(),
                "layer2_tokens": self.context.get_layer2_token_count(),
            })

        # デバッグ用コンテキスト情報を最新化
        await self._update_debug_context()

        token_after = self.context.get_token_count()
        print(f"手動コンテキスト圧縮完了 (圧縮後トークン数: {token_after}, L1: {self.context.get_layer1_token_count()}, L2: {self.context.get_layer2_token_count()})")

        return {
            "success": True,
            "message": f"コンテキストを圧縮しました（{token_before:,} → {token_after:,} トークン, L1: {self.context.get_layer1_token_count()}, L2: {self.context.get_layer2_token_count()}）",
            "token_usage": self.context.get_token_usage(),
        }

    def _process_vital_report(self, text: str) -> str:
        """応答テキストから [VITAL_REPORT: ...] タグを検出・除去する。バイタル自己申告の表示抑制用。"""
        return self._VITAL_REPORT_RE.sub('', text).rstrip()
        # 末尾に残った空行をクリーンアップ
    
    def _process_internal(self, text: str) -> str:
        """応答テキストから <internal>...</internal> タグを除去する。エージェントの内面思考の表示抑制用。"""
        return self._INTERNAL_RE.sub('', text).strip()

    def _read_record_check_enabled(self) -> bool:
        """記録判定フック（salia.record_check.enabled）の有効/無効を settings.json から読む。

        somatic_marker / evaluate_turn と同様、毎ターン settings.json を直接読むことで
        サーバー再起動なしに即時反映する。設定が無ければ既定 True（有効）。
        """
        try:
            settings_path = Path("settings.json")
            if settings_path.exists():
                with open(settings_path, "r", encoding="utf-8") as f:
                    full = json.load(f)
                return bool(full.get("salia", {}).get("record_check", {}).get("enabled", True))
        except Exception:
            pass
        return True

    def _read_declaration_check_enabled(self) -> bool:
        """「宣言だけして未実行」検知（salia.declaration_check.enabled）の有効/無効を settings.json から読む。

        record_check と同じく、毎ターン settings.json を直接読むことで
        サーバー再起動なしに即時反映する。設定が無ければ既定 True（有効）。
        """
        try:
            settings_path = Path("settings.json")
            if settings_path.exists():
                with open(settings_path, "r", encoding="utf-8") as f:
                    full = json.load(f)
                return bool(full.get("salia", {}).get("declaration_check", {}).get("enabled", True))
        except Exception:
            pass
        return True

    def _load_record_check_prompt(self) -> str:
        """記録判定フックで注入する内省プロンプト本文を読み込む。

        system_prompt/RECORD_CHECK.md があればそれを優先し（設定UIから編集可能）、
        無い/空なら組み込み既定にフォールバックする。毎回読むので編集が即時反映される。
        {{agent_name}} / {{user_honorific}} は実行時の値に置換して返す。
        """
        text = ""
        try:
            path = system_prompt_dir() / "RECORD_CHECK.md"
            if path.exists():
                text = path.read_text(encoding="utf-8").strip()
        except Exception:
            text = ""
        if not text:
            text = t("agent_record_check_fallback")
        return apply_prompt_placeholders(text, self.agent_name, self.honorific)

    async def process_message(self, user_message: str,
                               image: str = None,
                               images: Optional[list] = None,
                               files: Optional[list] = None,
                               on_tool_call: Optional[Callable] = None,
                               on_system_message: Optional[Callable[[str], None]] = None,
                               on_intermediate_text: Optional[Callable] = None,
                               on_stream_event: Optional[Callable] = None,
                               is_background: bool = False,
                               frequency_penalty_override: Optional[float] = None,
                               msg_type: str = "user") -> str:

        
        """
        ユーザーメッセージを受け取り、LLMとの対話ループ（ツール実行含む）を経て最終応答を返す。

        これがエージェントのメインループであり、以下のサイクルを繰り返す:
          コンテキスト構築 → LLM呼び出し → 応答解析 → (ツール実行 → 再度LLM呼び出し) → 最終テキスト応答

        Args:
            user_message: ユーザーからのメッセージテキスト
            image: 画像データURL（data:image/...;base64,...）またはNone（後方互換の単一画像）
            images: 画像データURLのリスト（複数画像添付）
            files: 添付テキストファイルのリスト。各要素は {"name", "content"} の dict
            on_tool_call: ツール実行時のUIコールバック（ツール名・引数・結果を通知）
            on_system_message: システムメッセージのUIコールバック（自動続行通知等）
            on_intermediate_text: ツールループ中の中間テキストのUIコールバック
            on_stream_event: ストリーミング配信用コールバック。
                {"event": "begin"/"delta"/"reset"/"end", "stream_id": str, "text"?: str, "aborted"?: bool}
                を受け取る。None なら従来どおり非ストリーミングで動く
            is_background: バックグラウンド実行（スケジュールタスク/Moonbeat）であるか
            frequency_penalty_override: LLMのfrequency_penaltyを一時的に上書きする値（Moonbeat再生成時に使用）
            msg_type: メッセージ種別（"user" / "moonbeat" / "system"）。コンテキスト構築時のタグ付けに影響

        Returns:
            エージェントのテキスト応答（XMLタグ・特殊タグ除去済み）
        """
        # このターンが画像添付を含むか（エラー時のロールバック判定に使う）。
        # 画像入りメッセージがエラーで履歴に残ると、次ターン以降も同じ画像を送り続けて
        # 連鎖失敗（詰まり）になるため、画像ターンのエラー時だけ巻き戻す。
        turn_had_image = bool(image or images)

        # このターンでソマティックマーカーが既に発火したかのフラグ
        turn_somatic_fired = False
        # --- ソマティックマーカー（ユーザーロール介入版）---
        # Moonbeat以外のメッセージに対して確率発火する
        if self.salia and msg_type not in ("moonbeat", "system") and user_message:
            try:
                from core.wyrd_network import load_graph
                # settings.jsonからソマティックマーカー設定を読む
                settings_path = Path("settings.json")
                sm_settings = {}
                wyrd_search_config = {}
                if settings_path.exists():
                    with open(settings_path, "r", encoding="utf-8") as f:
                        full_settings = json.load(f)
                        sm_settings = full_settings.get("salia", {}).get("somatic_marker", {})
                # Wyrd検索設定
                wyrd_config_path = config_file("wyrd_config.json")
                if wyrd_config_path.exists():
                    with open(wyrd_config_path, "r", encoding="utf-8") as f:
                        wyrd_search_config = json.load(f).get("search", {})

                if sm_settings.get("enabled", True) and self.rag_db is not None and hasattr(self.rag_db, '_ef'):
                    def _embed(text):
                        return self.rag_db._ef.embed_query([text])[0]
                    graph = load_graph()
                    flashback_block = await self.salia.somatic_marker_for_user(
                        user_message=user_message,
                        msg_type=msg_type,
                        wyrd_graph=graph,
                        embed_fn=_embed,
                        wyrd_search_config=wyrd_search_config,
                        used_episode_ids=self.salia._used_episode_ids,
                        settings=sm_settings,
                    )
                    if flashback_block:
                        # user_messageの末尾にflashbackタグを追加（context.py側で抽出される）
                        user_message = f"{user_message}\n\n{flashback_block}"
                        turn_somatic_fired = True  # 追加
            except Exception as e:
                print(f"[Agent] ソマティックマーカーエラー: {e}")
      
        # --- バイタル状態の更新とプロンプト生成 ---
        if self.vital_manager:
            self.vital_manager.update_stamina()
            self.context._pending_vital_prompt = self.vital_manager.get_vital_prompt()
        else:
            self.context._pending_vital_prompt = ""

        # ユーザーメッセージを会話履歴に追加（時刻・天気・self_memo等のシステム情報が自動注入される）
        self.context.add_user_message(user_message, image=image, msg_type=msg_type,
                                      images=images, files=files)

        # ユーザーメッセージをフルログに記録
        if self.logger:
            self.logger.log_user_message(user_message)

        # --- ツールループ用の状態変数 ---
        tool_log_parts = []                # チャットログ用のツール実行記録
        tool_calls_this_turn = []  # 追加
        consecutive_tool_call_count = 0    # 同一ツール呼び出しの連続回数（無限ループ検知用）
        last_tool_sig = None               # 前回のツール呼び出しシグネチャ（名前+引数のハッシュ）
        life_actions_this_turn = set()     # このターンで実行済みのlife_actionアクション名（同一行動の連打防止）
        note_quill_used_this_turn = False  # note_quill（雑記帳）は1ターン1回まで（同一内容の連投防止）
        guard_block_count = 0              # 1ターン1回制限でブロックした累積回数（縮退ループ保護用）

        # --- 記録判定フック（ツール使用ターン後の内省ステップ）の状態 ---
        # ツールを使ったターンの最終ステップ後に「記録はないか」を1回だけ注入し、柚月に
        # 内省ターン（可視）を回させる。ただし本編ですでに記録サテライトを使っていれば注入しない。
        record_check_enabled = self._read_record_check_enabled() and msg_type != "system"
        record_phase = False            # 内省ステップ実行中か
        record_check_injected = False   # この turn で内省を既に注入したか（多重注入防止）
        record_program_used = False     # 本編で記録サテライト（手紙/雑記帳/好悪）を使ったか（使っていれば注入スキップ）
        saved_user_facing_text = ""     # ご主人様に見せる本来の最終応答（内省後にこれを返す）
        tools_used_this_turn = False    # このターンで1回でもツールを実行したか（logger有無に依存しない判定）

        # --- 「宣言だけして実行せず終わる」検知の発火フラグ（1ターン1回まで） ---
        # ツール呼び出し無しの最終応答に has_unfulfilled_declaration が当たったら、
        # 「やるか・やらないか決めて」と促すsystem_noticeを1回だけ注入して再ターンを回す。
        # 2回目以降は無限ループ回避のため発火させない。
        # settings.json で無効化されていれば最初から True にして発火を完全スキップする。
        declaration_check_enabled = self._read_declaration_check_enabled() and msg_type != "system"
        declared_action_checked = not declaration_check_enabled

        # === メインツールループ ===
        # LLMがツール呼び出しを返す限りループし、テキスト応答が返ったら終了する
        try:
            last_valid_text = ""  # ツールループ中に得られた最後の有効なテキスト応答

            for loop_idx in range(MAX_TOOL_LOOPS):
                # --- 中断チェック: 処理中断シグナルが立っていたら即終了 ---
                if self.processing_lock and self.processing_lock.interrupt_flag:
                    self.processing_lock.interrupt_flag = False  # シグナルを消費
                    interrupt_msg = t("agent_interrupted")
                    if is_background:
                        # バックグラウンドタスクの中断: メッセージを履歴に残す
                        interrupt_msg = t("agent_interrupted_bg", honorific=self.honorific)
                        self.context.add_assistant_message(interrupt_msg)
                        if self.logger:
                            self.logger.log_assistant_message(interrupt_msg)
                    else:
                        # ユーザー会話の中断: 直近のやり取りを履歴から削除してロールバック
                        self.context.remove_last_exchange()
                    return interrupt_msg

                # --- ループ残り回数警告: 残り5回で警告メッセージを注入 ---
                if loop_idx == MAX_TOOL_LOOPS - 5:
                    warning_msg = t("agent_system_warning")
                    self.context.add_system_notice(warning_msg)
                    if self.logger:
                        self.logger.log_user_message(warning_msg)

                # --- コンテキスト構築: システムプロンプト + 記憶 + 会話履歴をmessagesリストに変換 ---
                messages = self.context.build_messages()

                # デバッグ用にコンテキスト情報を保存・配信（ツール定義分のトークン数も含む）
                # is_input=True: これは「実際にLLMへ送る入力」なので、入力スナップショットとして凍結保存する。
                await self._update_debug_context(messages, is_input=True)

                # --- LLM呼び出し（中断フラグ監視付き） ---
                # asyncio.Taskとして実行し、0.3秒ごとに中断フラグをチェックする
                import asyncio

                # ストリーミング配信: コールバックが渡され、config で無効化されていなければ
                # chat_streamed を使い、サニタイズ済みのテキスト断片をUIへリアルタイムに流す。
                # 戻り値の LLMResponse は chat() と完全に同形なので、以降の処理は共通。
                streaming_enabled = bool(on_stream_event) and \
                    self.context.config.get("llm", {}).get("streaming", True)

                if streaming_enabled:
                    import uuid
                    stream_id = uuid.uuid4().hex[:12]  # ツールループの周回ごとに別ストリーム
                    sanitizer = StreamSanitizer()
                    stream_begun = False

                    async def _on_delta(fragment: str):
                        nonlocal stream_begun
                        safe = sanitizer.feed(fragment)
                        if not safe:
                            return
                        if not stream_begun:
                            stream_begun = True
                            await on_stream_event({"event": "begin", "stream_id": stream_id})
                        await on_stream_event({"event": "delta", "stream_id": stream_id, "text": safe})

                    async def _on_stream_reset():
                        # 簡体字リトライ等での再生成開始。UI側は吹き出しを空に戻す
                        nonlocal sanitizer
                        sanitizer = StreamSanitizer()
                        if stream_begun:
                            await on_stream_event({"event": "reset", "stream_id": stream_id})

                    llm_task = asyncio.create_task(self.llm.chat_streamed(
                        messages, tools=get_tool_definitions(),
                        frequency_penalty_override=frequency_penalty_override,
                        on_delta=_on_delta, on_stream_reset=_on_stream_reset))
                else:
                    llm_task = asyncio.create_task(self.llm.chat(messages, tools=get_tool_definitions(), frequency_penalty_override=frequency_penalty_override))

                while not llm_task.done():
                    if self.processing_lock and self.processing_lock.interrupt_flag:
                        self.processing_lock.interrupt_flag = False  # シグナルを消費
                        llm_task.cancel()
                        try:
                            await llm_task
                        except asyncio.CancelledError:
                            pass
                        if streaming_enabled and stream_begun:
                            # 流しかけの吹き出しを閉じる（直後の中断応答で置換される）
                            await on_stream_event({"event": "end", "stream_id": stream_id, "aborted": True})
                        if not is_background:
                            self.context.remove_last_exchange()
                            return t("agent_interrupted")
                    await asyncio.sleep(0.3)

                response = llm_task.result()

                if streaming_enabled:
                    # サニタイザの保留分（タグ未成立で保留していた本文）を吐き出して閉じる
                    tail = sanitizer.flush()
                    if tail:
                        if not stream_begun:
                            stream_begun = True
                            await on_stream_event({"event": "begin", "stream_id": stream_id})
                        await on_stream_event({"event": "delta", "stream_id": stream_id, "text": tail})
                    if stream_begun:
                        await on_stream_event({"event": "end", "stream_id": stream_id})

                # --- LLMのキャッシュヒット状況をログに記録（プロンプトキャッシュの効果測定用） ---
                usage = response.raw.get("usage", {})
                if usage and self.logger:
                    self.logger.log_full_event("llm_usage", {
                        "cache_hit": usage.get("prompt_cache_hit_tokens", 0),
                        "cache_miss": usage.get("prompt_cache_miss_tokens", 0),
                        "completion": usage.get("completion_tokens", 0),
                    })

                # --- トークン使用量をVitalManagerに蓄積（スタミナ計算用） ---
                if self.vital_manager:
                    usage = response.raw.get("usage", {})
                    if usage:
                        self.vital_manager.add_token_usage(usage)

                # =====================================================
                # ケース1: ツール呼び出しがない場合 → テキスト応答として処理
                # =====================================================
                if not response.tool_calls:
                    raw_text = response.content or ""

                    # DeepSeek prefill対応: レスポンスにprefill部分が含まれない場合があるので結合
                    if self.context._pending_prefill:
                        raw_text = self.context._pending_prefill + raw_text
                        self.context._pending_prefill = ""

                    # ツールXMLタグ・特殊タグの除去
                    clean_text = strip_tool_xml(raw_text)

                    # バイタル関連タグのパースと除去
                    if self.vital_manager:
                        clean_text = self._process_vital_report(clean_text)
                        clean_text = self._process_internal(clean_text)
                        # 使用量を記録してスタミナを再計算
                        usage = response.raw.get("usage", {})
                        if usage:
                            self.vital_manager.add_token_usage(usage)
                        self.vital_manager.update_stamina()

                    # 応答を会話履歴に追加し、状態を永続化
                    self.context.add_assistant_message(raw_text)
                    self.context.save_state()
                    # 最終発言を履歴に入れた直後に最新化（DEBUGの最新ビュー＝次ターンに入るコンテキストを反映）。
                    # is_input=False なので入力スナップショット（直前発言を生んだ入力）は据え置かれる。
                    await self._update_debug_context()

                    # === 「宣言だけして未実行」検知の注入（1ターン1回まで） ===
                    # 「雑記帳に書いてみようと思います」「残しておきます」のように行動を宣言したのに
                    # 実際にはツールを呼ばずに終わろうとしている場合、再ターンを促す。
                    # record_phase の早期returnより前に置くことで、record_check の内省応答中に
                    # 「雑記帳にも記録を残しておきます」と宣言だけして未実行で終わるケースも拾う。
                    # 1ターン1回限定 (declared_action_checked) なので、ループ化はしない。
                    if (not declared_action_checked
                            and has_unfulfilled_declaration(clean_text)):
                        declared_action_checked = True
                        # 本文の吹き出しをここで確定保存させる（record_checkと同じ理由）
                        if on_intermediate_text and clean_text.strip():
                            await on_intermediate_text(clean_text)
                        notice = t("agent_declaration_notice")
                        self.context.add_system_notice(notice)
                        continue

                    # === 記録判定フェーズの終了 ===
                    # 内省ステップの最終応答（ツールを使わない締めの発言）に到達した。
                    # 本文は注入時に中間テキストとして確定済み。この締めの発言が画面上の最後の
                    # 吹き出しになるため、最終 response としてこれを返す（空なら本文にフォールバック）。
                    if record_phase:
                        await self._compress_if_needed()
                        return clean_text if clean_text.strip() else saved_user_facing_text

                    # --- サリア: 繰り返しチェック ---
                    # if self.salia and clean_text.strip() and msg_type == "moonbeat":
                    if False:
                        try:
                            detected, feedback = await self.salia.check_repetition(
                                self.context.conversation_history,
                                clean_text,
                            )
                            if detected and feedback:
                                # repetition_checkツールコールをダミー挿入
                                import uuid as _uuid
                                dummy_id = f"call_salia_{_uuid.uuid4().hex[:12]}"
                                self.context.conversation_history.append({
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [{
                                        "id": dummy_id,
                                        "type": "function",
                                        "function": {
                                            "name": "repetition_check",
                                            "arguments": "{}",
                                        }
                                    }],
                                })
                                # tool_resultとして指摘を注入
                                self.context.add_tool_result(dummy_id, feedback)
                                self.context.save_state()
                                tlog(f"[Salia] 繰り返し検出、再発言を促します")
                                # サリア介入前の発言をUIに中間テキストとして送信
                                if on_intermediate_text and clean_text.strip():
                                    await on_intermediate_text(clean_text)
                                # ループを続行して柚月に再発言させる
                                continue
                        except Exception as e:
                            tlog(f"[Salia] 繰り返しチェックエラー: {e}")

                    # --- ログ記録 ---
                    if self.logger:
                        self.logger.log_assistant_message(clean_text)
                        full_response = "\n\n".join(tool_log_parts + [clean_text]) if tool_log_parts else clean_text
                        await self.logger.log_chat_exchange(user_message, full_response)
                        # evaluate_turn.enabled が false の場合はターン情動評価をスキップする。
                        # settings.json から直接読むことで再起動なしに即時反映できる（somatic_marker と同様）。
                        _evaluate_turn_enabled = True
                        try:
                            _settings_path = Path("settings.json")
                            if _settings_path.exists():
                                with open(_settings_path, "r", encoding="utf-8") as _f:
                                    _evaluate_turn_enabled = json.load(_f).get("salia", {}).get("evaluate_turn", {}).get("enabled", True)
                        except Exception:
                            pass
                        if self.salia and _evaluate_turn_enabled:
                            import asyncio as _asyncio
                            _asyncio.ensure_future(self.salia.evaluate_turn(
                                user_message=user_message,
                                assistant_message=full_response,
                                tool_calls_summary=tool_calls_this_turn,
                                rag_db=getattr(self, 'rag_db', None),
                                desire_manager=getattr(self.vital_manager, 'desire_manager', None) if self.vital_manager else None,
                                moontide=getattr(self.vital_manager, 'moontide', None) if self.vital_manager else None,
                            ))

                    # --- 応答後のコンテキスト圧縮チェック ---
                    await self._compress_if_needed()

                    # === 記録判定フェーズの注入 ===
                    # ツールを使ったターンなら、本文を返す前に内省ステップを1回挟む。
                    # ただし以下は注入しない：
                    #   - 本編ですでに記録サテライト（手紙/雑記帳/好悪）を使っている（二重の問いかけ回避）
                    #   - 本編で life_action（睡眠・食事など生活行動）を実行している
                    if (record_check_enabled and tools_used_this_turn
                            and not record_check_injected and not record_program_used
                            and not life_actions_this_turn):
                        record_check_injected = True
                        record_phase = True
                        saved_user_facing_text = clean_text if clean_text.strip() else last_valid_text
                        # 本文の吹き出しをここで確定保存させる。これをしないと、この後の内省ターンの
                        # ストリームが本文の暫定吹き出し（stream_end 待ち）を上書きしてしまう。
                        if on_intermediate_text and saved_user_facing_text.strip():
                            await on_intermediate_text(saved_user_facing_text)
                        # 宣言検知フラグを record_phase 用にリセット：メインフェーズで1回使い切っていても、
                        # 内省ステップで再度「雑記帳に書いておきます」と宣言だけして終わるケースを拾えるようにする。
                        # 最大2回/ターン（メイン1回 + record_phase 1回）。record_phase 内で複数回宣言しても
                        # 2発目以降は declared_action_checked で止まるので無限ループにはならない。
                        declared_action_checked = False
                        self.context.add_system_notice(self._load_record_check_prompt())
                        continue

                    return clean_text if clean_text.strip() else last_valid_text

                # =====================================================
                # ケース2: ツール呼び出しがある場合 → ツールを実行してループ続行
                # =====================================================

                # --- 無限ループ検知: 同一のツール呼び出しが3回連続したら強制終了 ---
                current_calls_sig = "|".join([
                    f"{tc.name}:{json.dumps(tc.arguments, sort_keys=True)}"
                    for tc in response.tool_calls
                ])

                if last_tool_sig == current_calls_sig:
                    consecutive_tool_call_count += 1
                else:
                    consecutive_tool_call_count = 1
                    last_tool_sig = current_calls_sig

                # run_programは実行ごとに状態が変わりうるため無限ループ検知の対象外
                is_run_program = all(tc.name == "run_program" for tc in response.tool_calls)
                if consecutive_tool_call_count >= 3 and not is_run_program:
                    error_msg = "【システムエラー】同一のツール呼び出しが3回連続で行われたため、無限ループ保護により処理を強制終了しました。"
                    self.context.add_assistant_message(error_msg)
                    if self.logger:
                        self.logger.log_assistant_message(error_msg)
                        await self.logger.log_chat_exchange(user_message, error_msg, self.llm, getattr(self, 'rag_db', None))
                    return error_msg
                
                # --- ツール呼び出しと同時にテキスト応答がある場合（中間テキスト） ---
                if response.content and response.content.strip():
                    saved = response.content.strip()
                    saved = strip_tool_xml(saved)
                    if self.vital_manager:
                        saved = self._process_vital_report(saved)
                        saved = self._process_internal(saved)
                    last_valid_text = saved
                    if saved.strip():
                        tool_log_parts.append(f"**{self.agent_name}:** {saved}")

                    if self.logger and saved.strip():
                        self.logger.log_full_event("assistant_message", {"content": saved})
                  
                    # 中間テキストをWebSocket経由でUIにリアルタイムプッシュ
                    if on_intermediate_text and saved.strip():
                        await on_intermediate_text(saved)
                      
                # DeepSeek prefill: ツール呼び出し時もprefillバッファを消費済みとしてクリア
                if self.context._pending_prefill:
                    self.context._pending_prefill = ""

                # --- ツール呼び出しのアシスタントメッセージを会話履歴に追加 ---
                assistant_msg = {
                    "role": "assistant",
                    "content": response.content,
                    "reasoning_content": response.reasoning_content,  # 追加
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "extra_content": {
                                "google": {
                                    "thought_signature": "context_engineering_is_the_way_to_go"
                                }
                            },
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                            }
                        }
                        for tc in response.tool_calls
                    ],
                }
                self.context.add_tool_call(assistant_msg)
                tools_used_this_turn = True  # 記録判定フックの発火判定に使う（logger非依存）

                # --- 各ツールを順次実行 ---
                for tc in response.tool_calls:
                    # ツール実行前にも中断チェック
                    if self.processing_lock and self.processing_lock.interrupt_flag:
                        if not is_background:
                            self.processing_lock.interrupt_flag = False  # シグナルを消費
                            self.context.remove_last_exchange()
                            return t("agent_interrupted")

                    # ツール呼び出しをフルログに記録
                    if self.logger:
                        self.logger.log_tool_call(tc.name, tc.arguments)
                        tool_log_parts.append(f"🔧 {tc.name} → {json.dumps(tc.arguments, ensure_ascii=False)[:100]}")
                        tool_calls_this_turn.append({          # 追加
                            "name": tc.name,                   # 追加
                            "args": str(tc.arguments)[:100],   # 追加
                        })   

                    # --- ソマティックマーカー（ツールコール介入版）---
                    if (self.salia and not turn_somatic_fired 
                            and msg_type not in ("moonbeat", "system")):
                        try:
                            settings_path = Path("settings.json")
                            sm_settings = {}
                            wyrd_search_config = {}
                            if settings_path.exists():
                                with open(settings_path, "r", encoding="utf-8") as f:
                                    full_settings = json.load(f)
                                    sm_settings = full_settings.get("salia", {}).get("somatic_marker", {})
                            wyrd_config_path = config_file("wyrd_config.json")
                            if wyrd_config_path.exists():
                                with open(wyrd_config_path, "r", encoding="utf-8") as f:
                                    wyrd_search_config = json.load(f).get("search", {})

                            if (sm_settings.get("enabled", True) and self.rag_db is not None 
                                    and hasattr(self.rag_db, '_ef')):
                                from core.wyrd_network import load_graph
                                def _embed(text):
                                    return self.rag_db._ef.embed_query([text])[0]
                                graph = load_graph()
                                # 直近の柚月のテキスト発言を取得
                                recent_text = last_valid_text or ""
                                flashback_text = await self.salia.somatic_marker_for_tool(
                                    recent_assistant_text=recent_text,
                                    tool_name=tc.name,
                                    tool_arguments=json.dumps(tc.arguments, ensure_ascii=False),
                                    wyrd_graph=graph,
                                    embed_fn=_embed,
                                    wyrd_search_config=wyrd_search_config,
                                    used_episode_ids=self.salia._used_episode_ids,
                                    settings=sm_settings,
                                )
                                if flashback_text:
                                    turn_somatic_fired = True
                                    # ツール実行をスキップしてフラッシュバック+元のツールコール情報を返す
                                    original_args = json.dumps(tc.arguments, ensure_ascii=False)
                                    result = (
                                        f"【システムが一時停止しました】\n"
                                        f"あなたのツールコールは保留されています。\n\n"
                                        f"<flashback>\n{flashback_text}\n</flashback>\n\n"
                                        f"保留されたツールコール：\n"
                                        f"- ツール名: {tc.name}\n"
                                        f"- 引数: {original_args}\n\n"
                                        f"この記憶を踏まえて、本当にこの内容で実行しますか？"
                                        f"問題なければ再度同じツールコールを行ってください。"
                                        f"変更したい場合は別のツールコールを行ってください。"
                                        f"やめる場合はそのまま発言を続けてください。"
                                    )
                                    # ツール実行をスキップしてtool_resultを直接登録
                                    self.context.add_tool_result(tc.id, result)
                                    if self.logger:
                                        self.logger.log_tool_result(tc.name, f"[ソマティックマーカー発火] {result[:200]}")
                                    continue  # 次のtcへ
                        except Exception as e:
                            print(f"[Agent] ツールソマティックマーカーエラー: {e}")

                    # --- life_action は同じ行動を1ターン内で繰り返さない ---
                    # （sleep確認機能の追加後、寝ようとして弾かれる→再試行を1ターン内で
                    #   延々繰り返し、雑記帳と行動ログが同一内容で溢れる暴走が起きたため）
                    # 違う行動（例: 窓を見てから寝る）なら同一ターンで複数回OK。
                    _is_life_action = (
                        tc.name == "run_program"
                        and tc.arguments.get("app_name") == "life_action"
                    )
                    _life_action_name = (
                        (tc.arguments.get("args") or {}).get("action")
                        if _is_life_action else None
                    )
                    # note_quill（雑記帳）は1ターン1回まで（同一内容の連投防止）
                    _is_note_quill = (
                        tc.name == "run_program"
                        and tc.arguments.get("app_name") == "note_quill"
                    )

                    # --- ツール実行（条件分岐） ---
                    if self.vital_manager and (self.vital_manager.data.get("stamina", 500) <= 0 or self.vital_manager.data.get("energy", 50) <= 0):
                        # 体力/エネルギー切れ: ツール実行をブロック
                        result = "うまく実行できませんでした…体力が足りないようです。少し休んでから試してみてください。"
                    elif _is_note_quill and note_quill_used_this_turn:
                        # 雑記帳をこのターンで既に書いている: 実行せず案内を返す
                        guard_block_count += 1
                        result = (
                            "雑記帳（note_quill）はこのターンで既に1回書きました。"
                            "1ターンにつき書き込みは1回までです。"
                            "同じ内容を繰り返さず、そのまま発言を続けてください。"
                        )
                    elif (_is_life_action and _life_action_name in life_actions_this_turn
                          and not (tc.arguments.get("args") or {}).get("confirmed")):
                        # 同じ生活行動をこのターンで既に実行済み: 実行せず案内を返す。
                        # ただし confirmed:true（sleep/napの確認後の本実行）は常に通す。
                        guard_block_count += 1
                        result = (
                            f"life_action の「{_life_action_name}」はこのターンで既に実行しました。"
                            "同じ生活行動を1ターン内で繰り返すことはできません。"
                            "違う行動なら実行できますが、なければそのまま発言を続けてください。"
                        )
                    elif "__parse_error__" in tc.arguments:
                        # 引数のJSONパースに失敗していた場合: エラーメッセージを返す
                        result = f"実行に失敗しました: JSONの引数フォーマットが不正です（{tc.arguments['__parse_error__']}）。文字列内の改行やダブルクォーテーションのエスケープ漏れを確認し、正しい形式で再度実行してください。\nRaw: {tc.arguments.get('raw_arguments', '')}"
                    else:
                        # 通常のツール実行
                        if _is_note_quill:
                            note_quill_used_this_turn = True
                        result = await execute_tool(self.memory, tc.name, tc.arguments,
                                              scheduler=self.scheduler, rag_db=getattr(self, 'rag_db', None),
                                              secret_manager=getattr(self, 'secret_manager', None),
                                              llm=self.llm)
                        # life_action は「実際に実行された場合のみ」当ターンの使用済みに記録する。
                        # sleep/nap の確認待ち（confirm_required・まだ寝ていない）はカウントしない。
                        # これをカウントすると、本命の confirmed:true 呼び出しがガードで弾かれ、
                        # 柚月が眠れなくなる（確認フローが壊れる）。
                        if _is_life_action and not (isinstance(result, str) and "confirm_required" in result):
                            life_actions_this_turn.add(_life_action_name)
                        # 記録サテライト（雑記帳/手紙/好悪）を使ったら控えておく。
                        # 本編でこれを使っていれば、ターン末の記録判定フックは注入しない
                        # （二重の問いかけを避ける）。add_preference は confirmed:true の
                        # 本実行のみカウントする（初回は再考案内で書き込まないため）。
                        if tc.name == "run_program":
                            _rec_app = tc.arguments.get("app_name")
                            if _rec_app in ("note_quill", "letter_post"):
                                record_program_used = True
                            elif _rec_app == "add_preference" and (tc.arguments.get("args") or {}).get("confirmed"):
                                record_program_used = True

                    # reload_promptsツールが実行された場合、システムプロンプトを即時再読み込み
                    if result == "__reload_prompts__":
                        self.context.reload_memories()
                        result = "システムプロンプトを最新の状態に再読み込みしました。"

                    # --- コンテキスト残量に応じたツール結果の動的切り詰め ---
                    # 残りトークンの80%を文字数に換算し、それを超える結果を切り詰める
                    if isinstance(result, str):
                        current_tokens = self.context.get_token_count()
                        max_tokens = self.context.max_tokens
                        remaining = max_tokens - current_tokens
                        max_chars = max(5000, int(remaining * 3 * 0.8))
                        if len(result) > max_chars:
                            result = result[:max_chars] + "\n\n(以下省略...)"

                    # ツール結果をフルログに記録
                    if self.logger:
                        self.logger.log_tool_result(tc.name, str(result)[:500])
                        tlog("[Agent] システムプロンプトを再読み込みしました")

                    # --- see_imageツールの特殊処理: 画像データをコンテキストに直接挿入 ---
                    import json as _json
                    try:
                        parsed = _json.loads(result)
                        if isinstance(parsed, dict) and parsed.get("__see_image__"):
                            # ツール結果はテキストのみ（画像は別途userメッセージとして追加）
                            notice = (
                                "（※ここまでの発言は既に届いています。同じことを繰り返す必要はありません。続きがあれば自然に続けてください）"
                                if loop_idx > 0 and last_valid_text
                                else ""
                            )
                            self.context.add_tool_result(tc.id, f"画像を取得しました。確認してください。", system_notice=notice)
                            # 画像をマルチモーダルuserメッセージとしてコンテキストに追加
                            image_message = {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": f"[see_image結果] {parsed['question']}"},
                                    {"type": "image_url", "image_url": {"url": parsed["image_url"]}}
                                ]
                            }
                            self.context.conversation_history.append(image_message)
                        else:
                            notice = (
                                "（※ここまでの発言は既に届いています。同じことを繰り返す必要はありません。続きがあれば自然に続けてください）"
                                if loop_idx > 0 and last_valid_text
                                else ""
                            )
                            self.context.add_tool_result(tc.id, result, system_notice=notice)
                    except (ValueError, KeyError):
                        notice = (
                            "（※ここまでの発言は既に届いています。同じことを繰り返す必要はありません。続きがあれば自然に続けてください）"
                            if loop_idx > 0 and last_valid_text
                            else ""
                        )
                        self.context.add_tool_result(tc.id, result, system_notice=notice)
                                                
                    # ツール結果のログ記録（see_imageの場合は画像データではなくソース名を記録）
                    if self.logger:
                        if tc.name == "see_image" and "__see_image__" in result:
                            log_result = f"see_image → {tc.arguments.get('source', '不明')}"
                        else:
                            log_result = result
                        self.logger.log_tool_result(tc.name, log_result)

                    # ツール実行結果をWebSocket経由でUIに通知
                    if on_tool_call:
                        if tc.name == "see_image" and "__see_image__" in result:
                            call_result = f"see_image → {tc.arguments.get('source', '不明')}"
                        else:
                            call_result = result
                        await on_tool_call(tc.name, tc.arguments, call_result)

                    # 欲求システムへのトリガー通知（ツール実行が欲求を満たす場合がある）
                    if self.vital_manager and hasattr(self.vital_manager, 'desire_manager') and self.vital_manager.desire_manager:
                        self.vital_manager.desire_manager.on_tool_executed(tc.name, tc.arguments)
                        
                # --- 全ツール実行完了後の後処理 ---
                # 記憶ファイルの変更をリアルタイム反映（ツールがファイルを編集した可能性がある）
                self.context.reload_memories()
                # コンテキスト状態を永続化
                self.context.save_state()

                # --- 縮退ループ保護 ---
                # 1ターン1回制限でのブロックが積み重なった場合、同じ生活行動・雑記帳を
                # 延々と呼び続ける空回り（縮退ループ）に陥っている。
                # ここで無理に次の生成を促さず、ターンを静かに終了して「待機」に落とす。
                if guard_block_count >= 3:
                    tlog(f"[Agent] 1ターン1回制限のブロックが{guard_block_count}回積み重なったため、"
                         f"縮退ループ保護としてターンを終了します（loop_idx={loop_idx}）")
                    final_text = last_valid_text or ""
                    if final_text:
                        self.context.add_assistant_message(final_text)
                        if self.logger:
                            self.logger.log_assistant_message(final_text)
                            full_response = "\n\n".join(tool_log_parts + [final_text]) if tool_log_parts else final_text
                            await self.logger.log_chat_exchange(user_message, full_response)
                    self.context.save_state()
                    await self._compress_if_needed()
                    return final_text

                # --- ツール実行後のコンテキスト圧縮チェック（コンテキスト溢れ防止） ---
                if self.context.needs_compression():
                    if self.processing_lock and self.processing_lock.interrupt_flag:
                        if not is_background:
                            self.processing_lock.interrupt_flag = False  # シグナルを消費
                            self.context.remove_last_exchange()
                            return t("agent_interrupted")

                    tlog(f"[Agent] ツールループ中にコンテキスト圧縮を実行します (loop_idx={loop_idx})")
                    await self._compress_if_needed()
                    if on_system_message:
                        await on_system_message("コンテキストを圧縮しました。処理を続行します。")

            # --- ツールループ上限到達 ---
            # MAX_TOOL_LOOPSに達しても最終テキスト応答が得られなかった場合
            await self._compress_if_needed()
            return "（内部エラー: ツール実行の上限に達しました。もう一度お試しください）"

        except Exception as e:
            # 予期せぬエラー時も中断フラグをチェックし、中断によるエラーならロールバック
            if self.processing_lock and self.processing_lock.interrupt_flag and not is_background:
                self.processing_lock.interrupt_flag = False  # シグナルを消費
                self.context.remove_last_exchange()
                return t("agent_interrupted")
            # 画像付きターンがエラーで終わった場合、その画像入りメッセージを履歴から外す。
            # 残すと次ターン以降も同じ画像を送り続けて連鎖失敗（詰まり）になるため。
            # 通常のテキスト会話は巻き戻さない（一時的なエラーで発言が消えるのを避ける）。
            if turn_had_image and not is_background:
                self.context.remove_last_exchange()
            raise e
