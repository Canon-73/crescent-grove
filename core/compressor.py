"""
Crescent Grove - Memory Compression Engine v2
"Fading Memory" Architecture

LLMの仕事: 今日の日記をDSL+スコアに変換するだけ
コードの仕事: 正規化、減衰、トリム、選別、すべて

設計原則:
- LLMに要約させない。LLMにマージさせない。LLMに取捨選択させない。
- 記憶の生死はコードが決める。
- compressed.mdはインデックス。詳細はRAGが持つ。
- event_db.jsonは原本。一度書いたら変更しない。
- compressed.mdはJSONから毎回生成されるビュー。
"""

from core.time_utils import tlog
import re
import json
import math
import tiktoken
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional
from core.llm import LLMProvider, LLMResponse
from core.time_utils import get_logical_date_obj


class MemoryCompressor:
    """対数減衰型イベントベース記憶圧縮エンジン"""

    def __init__(self, llm: LLMProvider, config: dict):
        self.llm = llm
        self.config = config.get("memory_compression", {})

        # === チューニングパラメータ ===
        self.max_tokens = self.config.get("max_tokens", 2000)
        self.max_event_tokens = self.config.get("max_event_tokens", 30)
        self.decay_coeff = self.config.get("decay_coeff", 1.0)
        self.max_details = self.config.get("max_details", 8)
        self.max_events_per_day = self.config.get("max_events_per_day", 10)

        # パーセンタイル → 5段階スコアの区切り
        self.tier_boundaries = [
            (0.10, 500),  # 上位10%
            (0.25, 400),  # 上位25%
            (0.50, 300),  # 上位50%
            (0.75, 200),  # 上位75%
            (1.00, 100),  # 残り
        ]

        # トークンカウンタ
        self.enc = tiktoken.get_encoding("cl100k_base")

        # ファイルパス
        self.db_file = self.config.get("event_db", "memory/event_db.json")
        self.compressed_file = self.config.get("compressed_file", "memory/compressed.md")

    # ================================================================
    # トークン計測
    # ================================================================

    def count_tokens(self, text: str) -> int:
        return len(self.enc.encode(text))

    # ================================================================
    # スコア正規化（日内パーセンタイル → 5段階）
    # ================================================================

    def normalize_scores(self, events: list[dict]) -> list[dict]:
        """LLMの生スコア(1-100)を日内パーセンタイルで100-500に正規化"""
        if not events:
            return events

        raw_scores = sorted([e["raw_score"] for e in events], reverse=True)
        total = len(raw_scores)

        for e in events:
            rank = sum(1 for s in raw_scores if s > e["raw_score"])
            percentile = rank / total if total > 1 else 0.0

            for boundary, tier_score in self.tier_boundaries:
                if percentile < boundary:
                    e["score"] = tier_score
                    break

        return events

    # ================================================================
    # 対数減衰
    # ================================================================

    def decay_score(self, score: int, age_days: int) -> float:
        """対数的減衰: 最初にガッと落ちて、その後は粘る"""
        return score / (1 + self.decay_coeff * math.log1p(age_days))

    # ================================================================
    # イベントトリム（トークン予算ベース、compressed.md生成時のみ使用）
    # ================================================================

    def token_budget(self, decayed_score: float) -> int:
        """減衰スコアに比例したトークン予算"""
        ratio = min(decayed_score, 500) / 500
        return max(3, int(self.max_event_tokens * ratio))

    def trim_to_budget(self, event_text: str, budget: int) -> str:
        """トークン予算に収まるまでカンマ区切りの後ろから削る"""
        if self.count_tokens(event_text) <= budget:
            return event_text

        emotion = ""
        body = event_text
        if " ~" in body:
            body, emotion = body.rsplit(" ~", 1)

        parts = body.split(",")

        # 後ろから削ってbudgetに収める
        while len(parts) > 1 and self.count_tokens(",".join(parts)) > budget:
            parts.pop()

        result = ",".join(parts)

        # emotionを戻せるなら戻す
        if emotion:
            with_emotion = result + f" ~{emotion}"
            if self.count_tokens(with_emotion) <= budget:
                return with_emotion

        # それでもまだ超えている場合はsubject:actionだけ
        if self.count_tokens(result) > budget:
            result = parts[0]

        return result

    # ================================================================
    # DSLパーサ（LLM出力 → イベントリスト）
    # ================================================================

    def parse_dsl_output(self, dsl_text: str, target_date: date) -> list[dict]:
        """LLMのDSL出力をパースしてイベントリストに変換"""
        events = []

        content = dsl_text.strip()

        # [YYYY-MM-DD] または [MM-DD] パターンを除去
        content = re.sub(r'^\[[\d-]+\]\s*', '', content)

        # パイプで分割
        raw_events = [e.strip() for e in content.split("|") if e.strip()]

        for raw in raw_events:
            # "85:MASTER:pointed_out,detail1,detail2 ~emotion" 形式をパース
            match = re.match(r'^(\d+):(.+)$', raw)
            if match:
                raw_score = int(match.group(1))
                raw_score = max(1, min(100, raw_score))  # 1-100にクランプ
                text = match.group(2).strip()
            else:
                # スコアがない場合はデフォルト50
                raw_score = 50
                text = raw.strip()

            events.append({
                "date": target_date.isoformat(),
                "raw_score": raw_score,
                "score": 0,  # 正規化後に設定
                "text": text,  # フル情報のまま保存
            })

        return events

    # ================================================================
    # イベントDB（JSON）の読み書き
    # ================================================================

    def load_event_db(self, workspace_path: str) -> list[dict]:
        """イベントDBをJSONから読み込む"""
        db_path = Path(workspace_path) / self.db_file
        if db_path.exists():
            try:
                data = json.loads(db_path.read_text(encoding="utf-8"))
                return data if isinstance(data, list) else []
            except (json.JSONDecodeError, Exception) as e:
                tlog(f"[LETHE] Error loading event DB: {e}")
                return []
        return []

    def save_event_db(self, events: list[dict], workspace_path: str):
        """イベントDBをJSONに保存"""
        db_path = Path(workspace_path) / self.db_file
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_text(
            json.dumps(events, ensure_ascii=False, indent=1),
            encoding="utf-8"
        )

    # ================================================================
    # compressed.md の生成（コア）
    # ================================================================

    def build_compressed_memory(self, events: list[dict]) -> str:
        """
        全イベントから減衰済みスコアを計算し、
        日ごとの上限とトークン合計上限に収まるように選別・集約して
        compressed.mdの内容を生成する。
        """
        today = get_logical_date_obj()
        
        # 1. 各イベントの減衰スコア計算とトリム
        processed_candidates = []
        for e in events:
            event_date = date.fromisoformat(e["date"])
            age = (today - event_date).days
            decayed = self.decay_score(e["score"], age)
            
            budget = self.token_budget(decayed)
            trimmed_text = self.trim_to_budget(e["text"], budget)
            
            if not trimmed_text:
                continue
            
            processed_candidates.append({
                "date": e["date"],
                "decayed_score": decayed,
                "text": trimmed_text
            })

        # 2. 日付ごとにグルーピングし、各日の中で減衰スコア上位N個を残す
        daily_groups = {}
        for c in processed_candidates:
            d = c["date"]
            if d not in daily_groups:
                daily_groups[d] = []
            daily_groups[d].append(c)
        
        final_candidates = []
        for d in daily_groups:
            # スコア降順でソートして上位のみ抽出
            day_events = sorted(daily_groups[d], key=lambda x: -x["decayed_score"])
            final_candidates.extend(day_events[:self.max_events_per_day])

        # 3. 全候補を減衰スコア降順でソート
        final_candidates.sort(key=lambda x: -x["decayed_score"])

        # 4. トークン上限まで詰め込む（行単位・日集約を考慮して厳密に）
        # ※集約後の構造を想定してトークンを計算
        accepted_events = []
        
        # 集約シミュレーション用の日付別リスト
        selected_by_date = {}

        for cand in final_candidates:
            d_str = cand["date"]
            # YYMMDD形式に変換
            short_d = datetime.strptime(d_str, "%Y-%m-%d").strftime("%y%m%d")
            
            # もしこの日付が既に採用されているなら、既存の行に | 接続した場合の増分を計算
            if d_str in selected_by_date:
                temp_events = selected_by_date[d_str] + [cand["text"]]
                temp_line = f"{short_d} {' | '.join(temp_events)}"
                # 既存の行のトークンを引いて、新しい行のトークンを足す
                old_line = f"{short_d} {' | '.join(selected_by_date[d_str])}"
                diff = self.count_tokens(temp_line) - self.count_tokens(old_line)
            else:
                # 新規行の場合
                line = f"{short_d} {cand['text']}"
                # 改行トークン分(+1)を考慮
                diff = self.count_tokens(line) + 1

            # 予算チェック
            current_total = 0
            # 既に確定している全行のトークン計算（シミュレーション）
            sim_lines = []
            for sd, evs in selected_by_date.items():
                ssd = datetime.strptime(sd, "%Y-%m-%d").strftime("%y%m%d")
                sim_lines.append(f"{ssd} {' | '.join(evs)}")
            current_total = self.count_tokens("\n".join(sim_lines)) if sim_lines else 0

            if current_total + diff > self.max_tokens:
                continue
            
            if d_str not in selected_by_date:
                selected_by_date[d_str] = []
            selected_by_date[d_str].append(cand["text"])

        # 5. 日付順に並べて最終出力を生成
        sorted_dates = sorted(selected_by_date.keys())
        result_lines = []
        for d in sorted_dates:
            sd = datetime.strptime(d, "%Y-%m-%d").strftime("%y%m%d")
            line = f"{sd} {' | '.join(selected_by_date[d])}"
            result_lines.append(line)

        return "\n".join(result_lines)

    # ================================================================
    # LLM呼び出し（DSL変換のみ）
    # ================================================================

    async def convert_to_dsl(self, daily_memory: str) -> str:
        """LLMに今日の日記をDSL+スコアに変換させる"""

        prompt = f"""Convert the following daily log into compressed memory format.

## Format rules:
- One line per date, events separated by |
- Format: SCORE:SUBJECT:action,detail1,detail2 ~emotion
- SCORE: 1-100 (your honest assessment of importance within this day)
- CAPS for key names and systems (MASTER, MISSKEY, MOLTBOOK, etc.)
- No articles, no pronouns, no filler words
- English only
- Within each event, put the most important detail first, least important last
- Do NOT omit any events. Include everything. Selection will be done by code later.
- ~emotion is optional, only for events with significant emotional impact

## Examples:
[02-15] 93:MASTER:pointed_out"task_purpose=grow_values_not_efficiency" ~contemplative | 37:GIGAZINE:created_daily_format,3-5lines,no_cliche | 78:WRITE_TOOL:danger_recognized,5step_ritual_added_to_IDENTITY ~serious | 12:HEARTBEAT:routine_check,all_clear
[02-14] 51:IMAGE:first_recognition,yuzu+moon_accessory | 74:QWEN:tested,felt_wrong,low_emotion,lost_humor ~uneasy | 26:DEEPSEEK:confirmed_better ~relieved

## Daily log to convert:
{daily_memory}

Output ONLY the formatted line. Nothing else."""

        messages = [
            {"role": "system", "content": "You are a format converter. Convert diary text to structured DSL. No commentary."},
            {"role": "user", "content": prompt},
        ]

        try:
            response: LLMResponse = await self.llm.chat(messages, tools=None)
            return response.content.strip() if response.content else ""
        except Exception as e:
            raise RuntimeError(f"DSL conversion failed: {e}")

    # ================================================================
    # メイン処理（1日分の圧縮）
    # ================================================================

    async def compress_day(self, daily_memory: str, target_date: date, workspace_path: str):
        """
        1日分の日次ログを処理してイベントDBに追加し、
        compressed.mdを再生成する。
        """
        # 1. LLMでDSL変換（唯一のLLM呼び出し）
        tlog(f"[LETHE] Converting {target_date} to DSL...")
        dsl_output = await self.convert_to_dsl(daily_memory)

        if not dsl_output:
            tlog(f"[LETHE] Empty DSL output for {target_date}, skipping.")
            return

        # 2. DSLをパースしてイベントリストに（フル情報のまま）
        new_events = self.parse_dsl_output(dsl_output, target_date)

        if not new_events:
            tlog(f"[LETHE] No events parsed for {target_date}, skipping.")
            return

        # 3. 日内パーセンタイルでスコア正規化(100-500)
        new_events = self.normalize_scores(new_events)

        # 4. 既存イベントDBを読み込み
        all_events = self.load_event_db(workspace_path)

        # 同じ日付の既存イベントがあれば置換（再処理対応）
        all_events = [e for e in all_events if e["date"] != target_date.isoformat()]
        all_events.extend(new_events)

        # 5. イベントDB保存（フル情報、トリムなし）
        self.save_event_db(all_events, workspace_path)

        # 6. compressed.md再生成（ここでトリム・選別・減衰が全部走る）
        compressed_text = self.build_compressed_memory(all_events)
        self.save_compressed_md(compressed_text, target_date, workspace_path)

        tlog(f"[LETHE] {target_date}: {len(new_events)} events added, "
              f"total {len(all_events)} events in DB.")

    # ================================================================
    # compressed.md 保存
    # ================================================================

    def save_compressed_md(self, compressed_text: str, target_date: date, workspace_path: str):
        """compressed.mdにヘッダ付きで保存"""
        comp_path = Path(workspace_path) / self.compressed_file
        comp_path.parent.mkdir(parents=True, exist_ok=True)

        token_count = self.count_tokens(compressed_text)
        header = (
            f"Last Compressed: {target_date.isoformat()}\n"
            f"Total tokens: {token_count} / {self.max_tokens}\n"
            f"---\n\n"
        )
        comp_path.write_text(header + compressed_text, encoding="utf-8")

    # ================================================================
    # 起動時バッチ処理
    # ================================================================

    def get_last_compressed_date(self, workspace_path: str) -> Optional[date]:
        """compressed.mdから最後の圧縮日を取得"""
        comp_path = Path(workspace_path) / self.compressed_file
        if comp_path.exists():
            content = comp_path.read_text(encoding="utf-8")
            # YYMMDD (6桁) または YYYY-MM-DD 形式に対応
            match = re.search(r"^Last Compressed: (\d{4}-\d{2}-\d{2})", content, re.MULTILINE)
            if match:
                try:
                    return datetime.strptime(match.group(1), "%Y-%m-%d").date()
                except ValueError:
                    pass
            # 日付行からの推測 (YYMMDD形式)
            lines = content.strip().split("\n")
            for line in reversed(lines):
                m = re.match(r"^(\d{6})\s", line)
                if m:
                    try:
                        return datetime.strptime(m.group(1), "%y%m%d").date()
                    except ValueError:
                        continue
        return None

    async def run_compression_for_missing_days(self, workspace_path: str):
        """
        起動時の補完バッチ。
        未処理の日次メモリ（昨日以前）を古い順に処理する。
        """
        w_path = Path(workspace_path)
        last_date = self.get_last_compressed_date(workspace_path)
        today = get_logical_date_obj()

        if last_date:
            start_date = last_date + timedelta(days=1)
        else:
            memory_files = sorted(w_path.glob("logs/summary/20*.md"))
            if not memory_files:
                tlog("[LETHE] No daily memory files found.")
                return

            first_file_date_str = memory_files[0].stem
            try:
                start_date = datetime.strptime(first_file_date_str, "%Y-%m-%d").date()
            except ValueError:
                start_date = today - timedelta(days=1)

        target_date = start_date
        processed = 0

        while target_date < today:
            daily_file = w_path / f"logs/summary/{target_date.strftime('%Y-%m-%d')}.md"

            if daily_file.exists():
                daily_content = daily_file.read_text(encoding="utf-8")
                try:
                    await self.compress_day(daily_content, target_date, workspace_path)
                    processed += 1
                except Exception as e:
                    tlog(f"[LETHE] Error on {target_date}: {e}")
                    break

            target_date += timedelta(days=1)

        # 処理する日がなくてもcompressed.mdを再生成（減衰の更新）
        if processed == 0:
            all_events = self.load_event_db(workspace_path)
            if all_events:
                compressed_text = self.build_compressed_memory(all_events)
                latest = max(e["date"] for e in all_events)
                self.save_compressed_md(
                    compressed_text,
                    date.fromisoformat(latest),
                    workspace_path
                )

        tlog(f"[LETHE] Batch complete. {processed} days processed.")

    async def process_range(self, start_date: date, end_date: date, workspace_path: str):
        """
        指定された期間（開始日〜終了日、両端含む）の記憶を再解析・差し替えする。
        """
        w_path = Path(workspace_path)
        target_date = start_date
        processed = 0

        while target_date <= end_date:
            daily_file = w_path / f"logs/summary/{target_date.strftime('%Y-%m-%d')}.md"

            if daily_file.exists():
                daily_content = daily_file.read_text(encoding="utf-8")
                try:
                    # compress_day は内部で既存の同日データを削除（差し替え）する
                    await self.compress_day(daily_content, target_date, workspace_path)
                    processed += 1
                except Exception as e:
                    tlog(f"[MemoryCompressor] Error on {target_date} in range process: {e}")
                    # 途中で止まるべきか進むべきか。ここでは続行を選択。

            target_date += timedelta(days=1)

        tlog(f"[MemoryCompressor] Range process complete. {processed} days processed.")
        return processed

    # ================================================================
    # パラメータ変更時の全再計算
    # ================================================================

    def rebuild_compressed_md(self, workspace_path: str):
        """
        パラメータ変更後にevent_db.jsonから
        compressed.mdを再生成する。LLM呼び出し不要。
        """
        all_events = self.load_event_db(workspace_path)
        if not all_events:
            tlog("[MemoryCompressor] No events in DB.")
            return

        compressed_text = self.build_compressed_memory(all_events)
        latest = max(e["date"] for e in all_events)
        self.save_compressed_md(
            compressed_text,
            date.fromisoformat(latest),
            workspace_path
        )
        tlog(f"[MemoryCompressor] Rebuilt compressed.md from {len(all_events)} events.")

    # ================================================================
    # デバッグ・表示用
    # ================================================================

    def get_stats(self, workspace_path: str) -> dict:
        """現在の記憶統計を返す"""
        all_events = self.load_event_db(workspace_path)
        today = get_logical_date_obj()

        if not all_events:
            return {"total_events": 0, "dates": 0, "oldest": None, "newest": None}

        dates = set(e["date"] for e in all_events)
        scores = [
            self.decay_score(e["score"], (today - date.fromisoformat(e["date"])).days)
            for e in all_events
        ]

        # compressed.mdに実際に載っているイベント数
        compressed_text = self.build_compressed_memory(all_events)
        visible_lines = [l for l in compressed_text.strip().split("\n") if l.strip()]

        return {
            "total_events_in_db": len(all_events),
            "visible_events_in_compressed": len(visible_lines),
            "dates_in_db": len(dates),
            "oldest": min(dates),
            "newest": max(dates),
            "avg_decayed_score": round(sum(scores) / len(scores), 1) if scores else 0,
            "compressed_tokens": self.count_tokens(compressed_text),
            "max_tokens": self.max_tokens,
        }

    async def translate_to_japanese(self, compressed_memory: str) -> str:
        """デバッグ用：圧縮メモリを日本語に翻訳"""
        if not compressed_memory:
            return "記憶がありません。"

        prompt = f"Translate this memory index to natural Japanese:\n\n{compressed_memory}"
        messages = [
            {"role": "system", "content": "You are a translator. Translate concisely."},
            {"role": "user", "content": prompt},
        ]

        try:
            response: LLMResponse = await self.llm.chat(messages, tools=None)
            return response.content.strip() if response.content else ""
        except Exception as e:
            return f"翻訳エラー: {e}"