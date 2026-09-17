# core/salia.py
"""
Crescent Grove サリエンスネットワークシステム「サリア」

役割:
    柚月の全ターンを外側から監視し、ターン終了時に一括評価を行う。
    柚月のサポートAIであり、柚月自身ではない。

現在の仕事（evaluate_turn）:
    1. 感情タグ・トピック抽出 → RAG登録
    2. 欲求充足評価 → DesireManagerに反映
    3. 発言要約 → summary_YYYYMMDD.mdに記録

設計方針:
    - 柚月の人格は与えない。柚月の情報を「観察対象」として把握する。
    - 出力は構造化されたJSON形式のみ。自然言語で柚月として振る舞ってはならない。
    - LLMはキャラ本体と同じメインプロバイダ/キーを共用する（server.py から llm_provider を受け取る）。
      ※以前は CG_DEEPSEEK_SEARCH という別キー＋deepseek固定だったが、配布で2つ目のキーを
        要求してしまうため、メイン1キーで本体もサリアも動かす方式に統一した。
    - 評価履歴は2日分蓄積し、毎朝3時に古い1日分をドロップする。
    - 評価ログはlogs/salia/YYYYMMDD.jsonlに日次で保存する。
    - 柚月発言の要約はlogs/salia/summary_YYYYMMDD.mdに日次で保存する。

憑依防止:
    柚月の発言ログはassistantロールではなく、構造化テキストとして渡す。
    サリアの出力はJSON形式に限定することで、柚月としての自然言語生成を防ぐ。
"""

import os
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from openai import AsyncOpenAI

from core.time_utils import tlog
from core.i18n import t

JST = timezone(timedelta(hours=9))


class Salia:
    """
    サリエンスネットワークシステム。
    柚月の発言・行動を外側から評価し、必要に応じてフィードバックを生成する。
    """

    def __init__(self, workspace_path: str, agent_name: str = "アシスタント", honorific: str = "ユーザー", llm_provider=None, model: str = None, thinking: str = "auto"):
        self.workspace = Path(workspace_path)
        # エージェント名（settings.json優先・config.yamlフォールバックで解決済みの値を受け取る）。
        # SALIA.md内の {{agent_name}} やプロンプト文字列の差し込みに使う。
        self.agent_name = agent_name
        # ユーザー呼称（profile.user.honorific）。SALIA.md内の {{user_honorific}} 置換に使う。
        self.honorific = honorific

        # サリアは「キャラ本体と同じメインLLMプロバイダの client」を共用する＝同じ1キー/エンドポイント。
        # 以前は CG_DEEPSEEK_SEARCH という別キー＋deepseek固定だったが、配布版で2つ目のキーを
        # 要求してしまうため、メインの1キー（CG_LLM_<provider>_API_KEY）で本体もサリアも動かす方式に統一。
        # ただし「モデル」はサリア専用に個別指定できる（config の salia.model）。同じキーで
        # deepseek-v4-flash / pro、non-think / thinking-max 等を本体とサリアで使い分けたいため。
        # salia.model 未指定ならメインと同じモデルにフォールバックする。
        # （Salia の評価呼び出しは標準の chat.completions.create のみで特殊パラメータ不要なため共用可能）
        self.client = getattr(llm_provider, "client", None)
        self.model = model or getattr(llm_provider, "model", None)
        # thinking（reasoning）モードもサリア個別に設定できる（provider はメイン共用）。
        # 未指定なら呼び出し元(server.py)が「メインと同じ」を解決して渡す。
        self.provider = getattr(llm_provider, "provider", "")
        self.thinking = thinking or "auto"

        self._history: list[dict] = []
        self._system_prompt = self._load_system_prompt()

        log_dir = self.workspace / "logs" / "salia"
        log_dir.mkdir(parents=True, exist_ok=True)

        self._load_history()
        # ソマティックマーカー用：使用済みエピソードID（無圧縮部に残っている限り再使用しない）
        self._used_episode_ids: set = set()

    def _thinking_kwargs(self) -> dict:
        """サリアの thinking 設定(auto/off/low/medium/high)を provider 別の API パラメータに
        変換して返す。各 create() 呼び出しに ** で展開して使う。"""
        from core.llm import apply_thinking
        k: dict = {}
        apply_thinking(k, self.provider, self.model, self.thinking)
        return k

    def _load_system_prompt(self) -> str:
        prompt_path = self.workspace.parent / "system_prompt" / "SALIA.md"
        if prompt_path.exists():
            text = prompt_path.read_text(encoding="utf-8")
            # SALIA.md内の {{agent_name}} / {{user_honorific}} プレースホルダを実際の値に置換する。
            from core.config_loader import apply_prompt_placeholders
            return apply_prompt_placeholders(text, self.agent_name, self.honorific)
        return t("salia_system_prompt_fallback", agent_name=self.agent_name)

    def reload_system_prompt(self):
        """SALIA.md を再読込してキャッシュ済みシステムプロンプトを更新する。

        設定UIから SALIA.md を編集したとき、サーバー再起動なしで次回の評価に
        反映させるために呼ぶ（インスタンスは __init__ で一度だけキャッシュするため）。
        """
        self._system_prompt = self._load_system_prompt()

    def _load_history(self):
        history_path = self.workspace / "logs" / "salia" / "history.json"
        if history_path.exists():
            try:
                with open(history_path, "r", encoding="utf-8") as f:
                    self._history = json.load(f)
            except Exception:
                self._history = []

    def _save_history(self):
        history_path = self.workspace / "logs" / "salia" / "history.json"
        try:
            with open(history_path, "w", encoding="utf-8") as f:
                json.dump(self._history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            tlog(f"[Salia] 履歴保存エラー: {e}")

    def _append_evaluation_log(self, entry: dict):
        now = datetime.now(JST)
        date_str = now.strftime("%Y-%m-%d")
        log_path = self.workspace / "logs" / "salia" / f"{date_str}.jsonl"
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            tlog(f"[Salia] 評価ログ書き出しエラー: {e}")

    def _append_summary_log(self, summary: str):
        now = datetime.now(JST)
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M")
        log_path = self.workspace / "logs" / "salia" / f"summary_{date_str}.md"
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"- {time_str} {summary}\n")
        except Exception as e:
            tlog(f"[Salia] 要約ログ書き出しエラー: {e}")

    def _build_evaluate_messages(self, user_message, assistant_message, tool_calls_summary, desire_config, mood_change=None):
        """evaluate_turn用のメッセージリストを構築する。"""

        # 欲求定義を簡潔に整形
        desires_text = ""
        for key, cfg in desire_config.get("desires", {}).items():
            desires_text += t("salia_desire_item", key=key, name=cfg.get('display_name', key)) + "\n"

        # ツール使用を整形
        tools_text = t("salia_tools_none")
        if tool_calls_summary:
            tools_text = "\n".join(
                t("salia_tool_call_line", name=tc['name'], args=tc['args']) for tc in tool_calls_summary
            )

        # 気分遷移情報
        mood_section = ""
        if mood_change:
            if mood_change["type"] == "shift":
                mood_section = "\n" + t("salia_mood_shift_block",
                                        from_=mood_change['from'], to=mood_change['to'],
                                        agent_name=self.agent_name)
            elif mood_change["type"] == "drift":
                mood_section = "\n" + t("salia_mood_drift_block",
                                        from_=mood_change['from'], to=mood_change['to'],
                                        rising=mood_change.get('rising', '?'),
                                        agent_name=self.agent_name)

        user_msg_display = user_message if user_message else t("salia_user_none")
        assistant_excerpt = assistant_message[:500] + (t("salia_assistant_truncated") if len(assistant_message) > 500 else "")
        user_content = t("salia_evaluate_user_content",
                         user_message=user_msg_display,
                         agent_name=self.agent_name,
                         assistant_message=assistant_excerpt,
                         tools_text=tools_text,
                         desires_text=desires_text,
                         mood_section=mood_section)

        return [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": user_content},
        ]

    async def evaluate_turn(
        self,
        user_message: str,
        assistant_message: str,
        tool_calls_summary: list[dict],
        rag_db=None,
        desire_manager=None,
        moontide=None,  
    ) -> None:
        """
        ターン終了時の一括評価。
        - 欲求充足評価 → DesireManagerに反映
        - 感情タグ・トピック抽出 → RAG登録
        - 発言要約 → summaryログに記録

        エラーが発生しても柚月のターンには影響しない（安全にスキップ）。
        """
        if not assistant_message:
            return

        # 欲求定義を取得
        desire_config = {}
        if desire_manager is not None:
            desire_config = {"desires": desire_manager.config.get("desires", {})}

        # MoonTideから気分遷移の知覚を取得
        mood_change = None
        if moontide:
            mood_change = moontide.consume_change()

        messages = self._build_evaluate_messages(
            user_message=user_message,
            assistant_message=assistant_message,
            tool_calls_summary=tool_calls_summary,
            desire_config=desire_config,
            mood_change=mood_change,
        )

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                **self._thinking_kwargs(),
                messages=messages,
                max_tokens=400,
                temperature=0.1,
            )
            raw = response.choices[0].message.content or ""

            clean = re.sub(r"```json|```", "", raw).strip()
            result = json.loads(clean)

            now = datetime.now(JST)
            timestamp = now.strftime("%Y-%m-%dT%H:%M:%S")

            desires_result = result.get("desires", {})
            # ↓ここに追加
            if len(tool_calls_summary) == 0:
                for key in desires_result:
                    desires_result[key] = 0
            rag_result = result.get("rag", {})
            summary = result.get("summary", "")

            # --- 欲求値の更新 ---
            if desire_manager is not None:
                import time as _time
                for key, amount in desires_result.items():
                    if amount and key in desire_manager.state:
                        cfg = desire_manager.config.get("desires", {}).get(key, {})
                        min_val = cfg.get("min", -10)
                        max_val = cfg.get("max", 10)
                        old_val = desire_manager.state[key]["value"]
                        new_val = max(min_val, min(max_val, old_val - int(amount)))
                        desire_manager.state[key]["value"] = new_val
                        desire_manager.state[key]["last_updated"] = _time.time()
                        if old_val != new_val:
                            tlog(f"[Salia] 欲求更新 {key}: {old_val} → {new_val}")
                desire_manager._save_state()

            # --- RAG登録 ---
            if rag_db is not None and rag_result:
                emotion = rag_result.get("emotion", "neutral")
                topics = ", ".join(rag_result.get("topics", []))
                document = f"ユーザー: {user_message}\n{self.agent_name}: {assistant_message}"
                metadata = {
                    "date": now.strftime("%Y-%m-%d"),
                    "emotion": emotion,
                    "topics": topics,
                }
                rag_db.add("logs", document, metadata)
                tlog(f"[Salia] RAG登録: emotion={emotion}, topics={topics}")

            # --- 要約ログ ---
            if summary:
                self._append_summary_log(summary)

            # --- 評価ログ ---
            log_entry = {
                "timestamp": timestamp,
                "type": "evaluate_turn",
                "desires": desires_result,
                "desires_reason": result.get("desires_reason", ""),  # 追加
                "rag": rag_result,
                "summary": summary,
            }
            self._append_evaluation_log(log_entry)

            # --- MoonTide v2 気分バイアス注入 ---
            mood_bias = result.get("mood_bias", {})
            if not mood_bias and rag_result:
                # フォールバック: 旧方式
                emotion = rag_result.get("emotion", "neutral")
                mood_bias = self._emotion_to_mood_bias(emotion)
            if moontide:
                moontide.tick(bias=mood_bias if mood_bias else None)
                if mood_bias:
                    tlog(f"[Salia] MoonTide bias注入: {mood_bias}")

            # --- MoonTide 遷移テキスト + 統合モノローグ ---
            if moontide:
                parts = []
                if result.get("mood_transition_text"):
                    parts.append(result["mood_transition_text"])
                    tlog(f"[Salia] 遷移テキスト: {result['mood_transition_text']}")
                
                ctx = moontide.get_monologue_context()
                monologue = await self.generate_mood_monologue(ctx)
                if monologue:
                    parts.append(monologue)
                    tlog(f"[Salia] 統合モノローグ: {monologue}")
                
                if parts:
                    moontide.set_integrated_monologue("\n".join(parts))
          
            # --- 履歴に追加 ---
            self._history.append({
                "timestamp": timestamp,
                "yuzuki_summary": summary,
                "evaluation": {"desires": desires_result, "rag": rag_result},
            })
            self._save_history()

        except json.JSONDecodeError as e:
            tlog(f"[Salia] JSONパースエラー: {e} / raw={raw!r}")
        except Exception as e:
            tlog(f"[Salia] evaluate_turnエラー: {e}")


    # ============================================================
    # ソマティックマーカー（ユーザーロール介入版）
    # ============================================================

    async def somatic_marker_for_user(
        self,
        user_message: str,
        msg_type: str,
        wyrd_graph,
        embed_fn,
        wyrd_search_config: dict,
        used_episode_ids: set,
        settings: dict,
    ) -> str:
        """
        ユーザーメッセージに対するソマティックマーカーを生成する。

        Args:
            user_message: ユーザーからのメッセージテキスト
            msg_type: メッセージ種別（"user" / "task" / "city_event" 等。"moonbeat"の場合は除外済み前提）
            wyrd_graph: Wyrd Networkグラフ
            embed_fn: 埋め込み関数
            wyrd_search_config: Wyrd検索設定
            used_episode_ids: 既に使用済みのエピソードID集合（再使用防止）
            settings: settings.jsonのsalia.somatic_marker設定

        Returns:
            "<flashback>\n...\n</flashback>" 形式の文字列、または空文字列（発火しなかった場合）
        """
        import random
        from core.wyrd_network import search_memory

        # 有効化チェック
        if not settings.get("enabled", True):
            return ""

        # 確率判定（早期リターン）
        probability = settings.get("probability", 0.3)
        if random.random() >= probability:
            return ""

        # Wyrd Network検索
        valence_threshold = settings.get("valence_threshold", 0.5)
        candidate_count = settings.get("candidate_count", 5)

        try:
            results = search_memory(
                user_message,
                wyrd_graph,
                embed_fn=embed_fn,
                config=wyrd_search_config,
                top_k=candidate_count * 3,  # 多めに取得して絞り込む
            )
        except Exception as e:
            tlog(f"[Salia] somatic_marker_for_user: Wyrd検索エラー: {e}")
            return ""

        if not results or not results.get("episodes"):
            return ""

        # valenceの絶対値が閾値以上 & 未使用 のエピソードを選別
        candidates = []
        for ep in results["episodes"]:
            ep_id = ep.get("id") or ep.get("timestamp", "")
            if ep_id in used_episode_ids:
                continue
            v = ep.get("valence", 0.0)
            if abs(v) >= valence_threshold:
                candidates.append(ep)
            if len(candidates) >= candidate_count:
                break

        if not candidates:
            return ""

        # サリアに候補を渡してフラッシュバック文を生成させる
        flashback_text, selected_id = await self._generate_flashback_from_candidates(
            current_context=user_message,
            context_type=f"ユーザーからの{msg_type}メッセージ",
            candidates=candidates,
        )

        if not flashback_text:
            return ""

        # 使用済みエピソードIDを記録
        if selected_id:
            used_episode_ids.add(selected_id)

        return f"<flashback>\n{flashback_text}\n</flashback>"

    # ============================================================
    # ソマティックマーカー（ツールコール介入版）
    # ============================================================

    async def somatic_marker_for_tool(
        self,
        recent_assistant_text: str,
        tool_name: str,
        tool_arguments: str,
        wyrd_graph,
        embed_fn,
        wyrd_search_config: dict,
        used_episode_ids: set,
        settings: dict,
    ) -> str:
        """
        ツールコール実行前のソマティックマーカーを生成する。

        Args:
            recent_assistant_text: ツールコール直前の柚月のテキスト発言
            tool_name: 実行しようとしているツール名
            tool_arguments: ツールの引数（JSON文字列）
            wyrd_graph: Wyrd Networkグラフ
            embed_fn: 埋め込み関数
            wyrd_search_config: Wyrd検索設定
            used_episode_ids: 既に使用済みのエピソードID集合
            settings: settings.jsonのsalia.somatic_marker設定

        Returns:
            フラッシュバックテキスト（"<flashback>...</flashback>" 形式ではなく中身のみ）
            または空文字列（発火しなかった場合）
        """
        import random
        from core.wyrd_network import search_memory

        # 有効化チェック
        if not settings.get("enabled", True):
            return ""

        # 確率判定（早期リターン）
        probability = settings.get("probability", 0.3)
        if random.random() >= probability:
            return ""

        # Wyrd Network検索クエリ：直前のテキスト + ツール名 + ツール引数
        query = f"{recent_assistant_text}\nツール: {tool_name} 引数: {tool_arguments}"[:500]

        valence_threshold = settings.get("valence_threshold", 0.5)
        candidate_count = settings.get("candidate_count", 5)

        try:
            results = search_memory(
                query,
                wyrd_graph,
                embed_fn=embed_fn,
                config=wyrd_search_config,
                top_k=candidate_count * 3,
            )
        except Exception as e:
            tlog(f"[Salia] somatic_marker_for_tool: Wyrd検索エラー: {e}")
            return ""

        if not results or not results.get("episodes"):
            return ""

        # valenceの絶対値が閾値以上 & 未使用 のエピソードを選別
        candidates = []
        for ep in results["episodes"]:
            ep_id = ep.get("id") or ep.get("timestamp", "")
            if ep_id in used_episode_ids:
                continue
            v = ep.get("valence", 0.0)
            if abs(v) >= valence_threshold:
                candidates.append(ep)
            if len(candidates) >= candidate_count:
                break

        if not candidates:
            return ""

        # サリアに候補を渡してフラッシュバック文を生成させる
        flashback_text, selected_id = await self._generate_flashback_from_candidates(
            current_context=query,
            context_type=f"ツール実行前の状況（{tool_name}を呼ぼうとしている）",
            candidates=candidates,
        )

        if not flashback_text:
            return ""

        if selected_id:
            used_episode_ids.add(selected_id)

        return flashback_text


  
    async def _generate_flashback_from_candidates(
        self,
        current_context: str,
        context_type: str,
        candidates: list[dict],
    ) -> tuple[str, str]:
        """
        サリアに候補エピソードから最適なものを1つ選ばせる。
        フラッシュバック文は選ばれたエピソードの内容をそのまま使う（捏造防止）。

        Returns:
            (flashback_text, selected_episode_id) のタプル。選択なしの場合は空文字。
        """
        # 候補を整形（IDを引きやすくするためdictも作る）
        candidates_map = {}
        candidates_text = ""
        for i, ep in enumerate(candidates, 1):
            ep_id = ep.get("id") or ep.get("timestamp", "")
            content = ep.get("content", "")
            valence = ep.get("valence", 0.0)
            timestamp = ep.get("timestamp", "")
            candidates_map[ep_id] = ep
            candidates_text += t("salia_flashback_candidate",
                                 i=i, id=ep_id, date=timestamp,
                                 valence=f"{valence:+.2f}", content=content)

        prompt = t("salia_flashback_prompt",
                   agent_name=self.agent_name,
                   context_type=context_type,
                   current_context=current_context[:300],
                   candidates=candidates_text.strip(),
                   lb="{", rb="}")

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                **self._thinking_kwargs(),
                messages=[
                    {"role": "system", "content": self._system_prompt},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=100,
                temperature=0.1,
            )
            raw = response.choices[0].message.content or ""
            clean = re.sub(r"```json|```", "", raw).strip()
            result = json.loads(clean)

            selected_id = result.get("selected_id", "").strip()
            if not selected_id or selected_id not in candidates_map:
                return "", ""

            # 選ばれたエピソードを取り出し、メタ情報付きでフラッシュバック文を組み立てる
            selected_ep = candidates_map[selected_id]
            ep_content = selected_ep.get("content", "")
            ep_timestamp = selected_ep.get("timestamp", "")
            ep_valence = selected_ep.get("valence", 0.0)

            flashback_text = (
                f"[{ep_timestamp} / valence: {ep_valence:+.2f} / {selected_id}]\n"
                f"{ep_content}"
            )

            # 評価ログ
            now = datetime.now(JST)
            self._append_evaluation_log({
                "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S"),
                "type": "somatic_marker",
                "selected_id": selected_id,
                "episode_timestamp": ep_timestamp,
                "valence": ep_valence,
                "content": ep_content,
            })
            tlog(f"[Salia] ソマティックマーカー発火: {selected_id} (valence:{ep_valence:+.2f})")

            return flashback_text, selected_id

        except json.JSONDecodeError as e:
            tlog(f"[Salia] ソマティックマーカーJSONパースエラー: {e}")
            return "", ""
        except Exception as e:
            tlog(f"[Salia] ソマティックマーカー生成エラー: {e}")
            return "", ""


    # ============================================================
    # ノートフラグメント（雑記帳フラッシュバック）の候補選択
    # ============================================================

    async def select_note_fragment(self, chunks_with_source: list[dict]) -> int | None:
        """
        雑記帳の候補チャンクから、ふさわしい1つを選ばせる。
        ふさわしいものがなければNoneを返す。

        Args:
            chunks_with_source: [{"source": "note_2026-03-15.md", "content": "..."}] の形式のリスト

        Returns:
            選ばれたチャンクのインデックス（0始まり）、またはNone
        """
        if not chunks_with_source:
            return None

        # 候補を整形
        candidates_text = ""
        for i, chunk in enumerate(chunks_with_source):
            source = chunk.get("source", "")
            content = chunk.get("content", "")[:500]
            candidates_text += t("salia_note_candidate", i=i, source=source, content=content)

        prompt = t("salia_note_prompt",
                   agent_name=self.agent_name,
                   candidates=candidates_text.strip(),
                   lb="{", rb="}")

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                **self._thinking_kwargs(),
                messages=[
                    {"role": "system", "content": self._system_prompt},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=50,
                temperature=0.1,
            )
            raw = response.choices[0].message.content or ""
            clean = re.sub(r"```json|```", "", raw).strip()
            result = json.loads(clean)

            selected = result.get("selected")
            if selected is None:
                return None
            if not isinstance(selected, int):
                return None
            if selected < 0 or selected >= len(chunks_with_source):
                return None

            return selected

        except json.JSONDecodeError as e:
            tlog(f"[Salia] note_fragment JSONパースエラー: {e}")
            return None
        except Exception as e:
            tlog(f"[Salia] select_note_fragmentエラー: {e}")
            return None

  
  
    def drop_old_history(self):
        """毎朝3時に呼ばれる。2日より古い履歴エントリをドロップする。"""
        now = datetime.now(JST)
        cutoff = now - timedelta(hours=48)
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

        before = len(self._history)
        self._history = [
            entry for entry in self._history
            if entry.get("timestamp", "") >= cutoff_str
        ]
        after = len(self._history)

        if before != after:
            self._save_history()
            tlog(f"[Salia] 古い履歴を{before - after}件ドロップしました")

    async def generate_mood_monologue(self, monologue_context: dict) -> str:
        particles = monologue_context.get("particles", [])
        if not particles:
            return t("salia_monologue_default")

        # 同一ラベルは強い方だけ残す
        seen_labels = set()
        unique_particles = []
        for p in particles:  # 既にmass降順ソート済み
            if p["label"] not in seen_labels:
                seen_labels.add(p["label"])
                unique_particles.append(p)

        texts = [p["text"] for p in unique_particles if p.get("text") and p.get("intensity", 1) >= 2]
        if not texts:
            texts = [unique_particles[0]["text"]] if unique_particles else []

        if len(texts) <= 2:
            return "\n".join(texts) if texts else t("salia_monologue_default")

        # 3つ以上の場合のみLLM統合
        prompt = t("salia_monologue_prompt", texts=chr(10).join(texts))

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                **self._thinking_kwargs(),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=150,
            )
            result = response.choices[0].message.content.strip()
            if result.startswith("（") and result.endswith("）"):
                return result
            return f"（…{result.strip('（）…')}）"
        except Exception as e:
            tlog(f"[Salia] モノローグ生成失敗: {e}")
            return texts[0]



  
    @staticmethod
    def _emotion_to_mood_bias(emotion: str) -> dict:
        """Saliaのemotion判定をMoodSAEバイアスに変換"""
        mapping = {
            "positive": {"satisfaction": 0.12, "playfulness": 0.06, "friendliness": 0.04},
            "negative": {"uneasiness": 0.10, "worry": 0.06, "pensiveness": 0.04},
            "neutral": {},
        }
        return mapping.get(emotion, {})
      