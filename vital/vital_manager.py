# vital/vital_manager.py
"""
VITAL/MENTAL 統合管理クラス

役割:
    Stamina（体力）とMental（精神力）を管理し、
    閾値に応じたシステムプロンプト注入文を生成する。
    - Stamina: API使用量に連動（日次リセット + 自然回復）
    - Mental: 会話内容に連動（日をまたいで持続 + 自然回復）
"""

from core.time_utils import tlog
import os
import json
import tempfile
from datetime import datetime, timedelta

from vital.cost_tracker import CostTracker
from vital.token_tracker import TokenTracker
from core.time_utils import get_logical_date, JST

# vital.json 等のパスは data_root 基準で「遅延解決」する。
# モジュール import 時に定数で固定すると set_data_root() より前にパスが確定し、
# 配布版で install_root（読み取り専用になり得る）を向く罠になる。
# そのため関数で都度解決する。dev（data_root == bundle_root）では従来と同一パス。
def _vital_json_path() -> str:
    """data/vital.json のパス（実行時書き込み対象）。"""
    from core.paths import data_file
    return str(data_file("vital.json"))


def _data_dir() -> "Path":
    """data/ ディレクトリ（data_root 基準）。"""
    from core.paths import data_dir
    return data_dir()


def _workspace_dir() -> "Path":
    """workspace/ ディレクトリ（data_root 基準）。"""
    from core.paths import workspace_root
    return workspace_root()

# 初期データ
_DEFAULT_DATA = {
    "stamina": 100,
    "mental": 100,
    "previous_ratio": 0.0,
    "last_updated": None,
    "daily_start_balance": None,
    "config": {
        "stamina_mode": "auto",
        "daily_budget_usd": 0.50,
        "daily_token_limit": 500000
    }
}


class VitalManager:
    def __init__(self, api_key: str = None):
        """
        Args:
            api_key: DeepSeek API キー（balance API に使用）
        """
        self.api_key = api_key
        self.data = {}
        self.tracker: CostTracker = None
        self.on_day_reset = None


        # data/ ディレクトリが存在しない場合は作成（data_root 基準）
        data_dir = str(_data_dir())
        os.makedirs(data_dir, exist_ok=True)

        # vital.json を読み込む（存在しなければ初期値で生成）
        self._load()

        # MoodPhase / MoonTide を初期化
        self.moodphase = None
        self.moontide = None
        
        # vital.json の config から mood_system を読む（デフォルト: moodsae）
        mood_system = self.config.get("mood_system", "moodsae")
        
        if mood_system == "moodsae":
            try:
                from vital.moontide_v2 import MoonTideV2, load_landmarks, load_inner_texts, load_config
                from core.paths import config_file
                # 参照データ（mood_graph.json 等）は data_root 基準。
                # moontide_v2_config.json は config/ 配下の編集可能設定。
                data_dir = _data_dir()
                config_path = config_file("moontide_v2_config.json")

                landmarks = load_landmarks(
                    str(data_dir / "mood_graph.json"),
                    str(data_dir / "mood_transition_matrix.csv")
                )
                inner_texts = load_inner_texts(str(data_dir / "moontide_inner.jsonl"))
                params = load_config(str(config_path))

                # 気分システムのオンオフ。moontide_v2_config.json の "enabled" が false の場合、
                # MoonTide を一切初期化せず self.moontide=None のままにする。これにより
                #   - get_vital_prompt() の気分テキスト注入（if self.moontide）
                #   - salia.evaluate_turn() に渡る moontide（if moontide: で tick/モノローグ生成）
                # の両方が自動的にスキップされ、エージェントへの気分テキスト送信と
                # サリア側の気分モノローグLLM処理がまとめて止まる（欲求評価・RAGは継続）。
                # デフォルトは true（キー未指定でも従来通り有効）。
                if not params.get("enabled", True):
                    self.moontide = None
                    tlog("[VitalManager] MoonTide v2 は無効化されています (enabled=false)")
                else:
                    self.moontide = MoonTideV2(landmarks, params, inner_texts)

                    if "moontide_state" in self.data:
                        self.moontide.load_state_snapshot(self.data["moontide_state"])
                    else:
                        self.moontide.morning_init()

                    tlog(f"[VitalManager] MoonTide v2 初期化完了")
            except Exception as e:
                self.moontide = None
                tlog(f"[VitalManager] MoonTide v2 初期化失敗: {e}")
        else:
            try:
                from vital.moodphase import MoodPhase
                self.moodphase = MoodPhase()
                tlog(f"[VitalManager] MoodPhase 初期化完了: {self.moodphase.get_status_summary()}")
            except Exception as e:
                self.moodphase = None
                tlog(f"[VitalManager] MoodPhase 初期化失敗: {e}")

        # 日付変更チェック
        self._check_day_reset()

        # CostTracker を初期化
        self._init_tracker()
        # DesireManager を初期化
        try:
            from vital.desire_manager import DesireManager
            self.desire_manager = DesireManager()
            tlog(f"[VitalManager] DesireManager 初期化完了")
        except Exception as e:
            self.desire_manager = None
            tlog(f"[VitalManager] DesireManager 初期化失敗: {e}")

    def _load_config_file(self) -> dict:
        """config/vital_config.json（読み取り専用）を読む。

        無い/壊れている場合は {} を返す（config 各キーはコード側にフォールバックがあるため
        空でも従来デフォルトで動く）。このファイルは _save() の書き込み対象ではない。
        """
        from core.paths import config_file
        try:
            p = config_file("vital_config.json")
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, IOError, OSError):
            pass
        return {}

    def reload_config(self) -> None:
        """config/vital_config.json を読み直して self.config を差し替える。

        設定UIからの保存後にサーバー再起動なしで反映するためのフック。各 get_* メソッドは
        self.config を都度参照するため、ここを入れ替えるだけで次回計算から新値が効く
        （stamina/energy の現在値such as state は維持される）。
        """
        self.config = self._load_config_file()

    def _load(self):
        """vital.json（state）を読み込み、config は config/vital_config.json から分離して読む"""
        vital_json = _vital_json_path()
        need_save = False
        if os.path.exists(vital_json):
            try:
                with open(vital_json, 'r', encoding='utf-8') as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, IOError):
                self.data = dict(_DEFAULT_DATA)
                need_save = True
        else:
            self.data = dict(_DEFAULT_DATA)
            need_save = True

        # --- config を state から分離 ---
        # config は config/vital_config.json（読み取り専用）から読む。旧 vital.json や
        # _DEFAULT_DATA に残る "config" キーは state から剥がす（後方互換）。これにより
        # _save() が書く self.data には config が含まれず、手編集した config が消えない。
        self.config = self._load_config_file()
        self.data.pop("config", None)

        # 新規生成/破損時のみ state を保存（config を pop 済みなので config は書かれない）
        if need_save:
            self._save()

    def _save(self):
        """vital.json にアトミック書き込み"""
        # MoonTide の状態を保存データに含める
        if hasattr(self, 'moontide') and self.moontide:
            self.data["moontide_state"] = self.moontide.get_state_snapshot()

        vital_json = _vital_json_path()
        data_dir = os.path.dirname(vital_json)
        os.makedirs(data_dir, exist_ok=True)
        try:
            fd, tmp_path = tempfile.mkstemp(dir=data_dir, suffix='.tmp')
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, vital_json)
        except Exception as e:
            tlog(f"[VitalManager] vital.json の保存に失敗: {e}")
            if 'tmp_path' in locals() and os.path.exists(tmp_path):
                os.remove(tmp_path)

    def _check_day_reset(self):
        """日付が変わっていたら stamina をリセット（mental はリセットしない）"""
        today = get_logical_date()
        last = self.data.get("last_updated")

        if last:
            last_date = last.split("T")[0] if "T" in last else last.split(" ")[0]
            if last_date != today:
                # 日付変更: stamina リセット、daily_start_balance リセット、previous_ratio リセット
                self.data["stamina"] = self.config.get("stamina_max", 500)
                self.data["energy"] = self.config.get("energy_max", 50)
                # 今日の気分をランダム選択
                import random
                # MoodPhase / MoonTide の日付リセット
                if self.moodphase:
                    self.moodphase.check_day_reset(today)
                    tlog(f"[VitalManager] MoodPhase リセット: {self.moodphase.get_status_summary()}")
                if self.moontide:
                    # MoonTide は日次リセット不要（自然遷移で変化するため）
                    tlog(f"[VitalManager] MoonTide 状態: {self.moontide}")
                self.data["previous_ratio"] = 0.0
                self.data["daily_start_balance"] = None
                tlog(f"[VitalManager] 日付変更を検知: stamina={self.data['stamina']}, energy={self.data['energy']} にリセット")
                # today.md をアーカイブ
                import re as _re
                workspace = str(_workspace_dir())
                today_md = os.path.join(workspace, "memory", "today.md")
                if os.path.exists(today_md):
                    with open(today_md, "r", encoding="utf-8") as f:
                        first_line = f.readline().strip()
                    date_match = _re.search(r'(\d{4}-\d{2}-\d{2})', first_line)
                    if date_match:
                        file_date = date_match.group(1)
                        archive_dir = os.path.join(workspace, "memory", "today")
                        os.makedirs(archive_dir, exist_ok=True)
                        archive_path = os.path.join(archive_dir, f"{file_date}.md")
                        os.replace(today_md, archive_path)
                        tlog(f"[VitalManager] today.md を {file_date}.md にアーカイブ")
                # システムプロンプト再読み込み
                if self.on_day_reset:
                    self.on_day_reset()
                    tlog("[VitalManager] システムプロンプトを再読み込みしました")
        self._save()

    def _init_tracker(self):
        """stamina_mode に基づき CostTracker を初期化"""
        config = self.config
        mode = config.get("stamina_mode", "auto")
        daily_budget = config.get("daily_budget_usd", 0.50)
        daily_token_limit = config.get("daily_token_limit", 500000)

        if mode == "token":
            self.tracker = TokenTracker(daily_token_limit)
            tlog("[VitalManager] TokenTracker を使用")
            return

        if mode in ("auto", "balance"):
            if self.api_key:
                try:
                    from vital.deepseek_tracker import DeepSeekTracker
                    start_balance = self.data.get("daily_start_balance")
                    self.tracker = DeepSeekTracker(
                        self.api_key, daily_budget, start_balance
                    )
                    # 初期残高を保存
                    if self.data.get("daily_start_balance") is None:
                        self.data["daily_start_balance"] = self.tracker.daily_start_balance
                        self._save()
                    tlog(f"[VitalManager] DeepSeekTracker を使用 (残高: ${self.tracker.daily_start_balance:.4f})")
                    return
                except Exception as e:
                    if mode == "balance":
                        raise  # 強制モードでは例外を伝播
                    tlog(f"[VitalManager] DeepSeekTracker 初期化失敗、TokenTracker にフォールバック: {e}")

        # フォールバック
        self.tracker = TokenTracker(daily_token_limit)
        tlog("[VitalManager] TokenTracker を使用（フォールバック）")

    def _apply_natural_recovery(self):
        """
        自然回復を適用する。
        - stamina: 1時間アクセスなしにつき +20（上限100）
        - mental: 1時間アクセスなしにつき +5（上限100）
        """
        last = self.data.get("last_updated")
        if not last:
            return

        try:
            last_time = datetime.fromisoformat(last)
        except (ValueError, TypeError):
            return

        now = datetime.now()
        elapsed_hours = (now - last_time).total_seconds() / 3600.0
        recovery_hours = int(elapsed_hours)

        if recovery_hours >= 1:
            # Stamina: 自然回復なし（翌日リセットのみ）
            # Mental の自然回復: +5/時間
            mental_recovery = recovery_hours * 5
            self.data["mental"] = min(self.data.get("mental", 100) + mental_recovery, 100)

        # Energy の自然回復: 分単位で計算
        elapsed_minutes = (now - last_time).total_seconds() / 60.0
        config = self.config
        energy_max = config.get("energy_max", 50)
        recovery_per_min = config.get("energy_recovery_per_minute", 2)
        stamina_max = config.get("stamina_max", 500)

        if elapsed_minutes >= 1:
            energy_recovery = round(int(elapsed_minutes) * recovery_per_min)
            if energy_recovery > 0:
                old_energy = self.data.get("energy", energy_max)
                self.data["energy"] = min(energy_max, old_energy + energy_recovery)
                if self.data["energy"] != old_energy:
                    tlog(f"[VitalManager] エナジー回復: {int(elapsed_minutes)}分経過 → energy +{energy_recovery}")

        # スタミナ0ならエナジー強制0
        if self.data.get("stamina", stamina_max) <= 0:
            self.data["energy"] = 0

    def add_token_usage(self, usage: dict):
        """LLM使用トークンをペンディングに蓄積（inputは増加分のみ）"""
        pending = self.data.get("pending_tokens", {"input": 0, "output": 0})
        
        # input: 前回との差分のみ計上
        current_input = usage.get("prompt_tokens", 0)
        prev_input = self.data.get("_last_prompt_tokens", 0)
        input_delta = max(0, current_input - prev_input)
        self.data["_last_prompt_tokens"] = current_input
        
        pending["input"] += input_delta
        pending["output"] += usage.get("completion_tokens", 0)
        self.data["pending_tokens"] = pending


    def update_stamina(self):
        """
        Stamina を更新する（累積方式）。
        """
        # 日付変更チェック（today.mdアーカイブ + staminaリセット）
        self._check_day_reset()
        # MoodPhase / MoonTide の気分変動
        if hasattr(self, 'moodphase') and self.moodphase:
            self.moodphase.tick()
        # if hasattr(self, 'moontide') and self.moontide:
        #    self.moontide.tick()
        # 1. 自然回復
        self._apply_natural_recovery()
        
        # 変化前のstaminaを記録
        old_stamina = self.data.get("stamina", 100)

        # 2. トークン消費からstaminaを減算
        try:
            config = self.config
            input_w = config.get("stamina_input_weight", 1.0)
            output_w = config.get("stamina_output_weight", 1.0)
            multiplier = config.get("stamina_cost_multiplier", 0.001)

            pending = self.data.get("pending_tokens", {"input": 0, "output": 0})
            import math
            softcap = config.get("input_softcap", 2500)
            effective_input = softcap * math.log(1 + pending["input"] / softcap) if pending["input"] > 0 else 0
            weighted = effective_input * input_w + pending["output"] * output_w
            stamina_cost = round(weighted * multiplier)

            if stamina_cost > 0:
                config = self.config
                stamina_max = config.get("stamina_max", 500)
                energy_max = config.get("energy_max", 50)

                current_stamina = self.data.get("stamina", stamina_max)
                self.data["stamina"] = max(0, min(stamina_max, current_stamina - stamina_cost))

                current_energy = self.data.get("energy", energy_max)
                self.data["energy"] = max(0, min(energy_max, current_energy - stamina_cost))

                tlog(f"[VitalManager] [{datetime.now(JST).strftime('%H:%M:%S')}] トークン消費: input={pending['input']}, output={pending['output']} → stamina -{stamina_cost}, energy -{stamina_cost}")

                # スタミナ0ならエナジー強制0
                if self.data["stamina"] <= 0:
                    self.data["energy"] = 0

            self.data["_last_consumed_tokens"] = self.data.get("_last_consumed_tokens", 0) + pending["input"] + pending["output"]
            self.data["pending_tokens"] = {"input": 0, "output": 0}
        except Exception as e:
            tlog(f"[VitalManager] スタミナ計算に失敗: {e}")

        # 段階変化の検知とtoday.mdへの記録
        try:
            import json as _json
            from core.paths import data_file
            messages_path = str(data_file("vital_messages.json"))
            with open(messages_path, 'r', encoding='utf-8') as f:
                vital_messages = _json.load(f)

            def _get_stage(value, entries):
                for entry in entries:
                    if value <= entry["threshold"]:
                        return entry["threshold"]
                return None
            
            old_stage = _get_stage(old_stamina, vital_messages.get("stamina", []))
            new_stage = _get_stage(self.data["stamina"], vital_messages.get("stamina", []))
            
            if old_stage != new_stage:
                now = datetime.now(JST)
                time_str = now.strftime("%H:%M")
                date_str = get_logical_date()
                line = f"- {time_str} 体力変化: {old_stamina} → {self.data['stamina']}\n"
                today_path = os.path.join(str(_workspace_dir()), 'memory', 'today.md')
                if os.path.exists(today_path):
                    with open(today_path, 'a', encoding='utf-8') as f:
                        f.write(line)
                else:
                    with open(today_path, 'w', encoding='utf-8') as f:
                        f.write(f"# {date_str} の活動\n\n{line}")
                tlog(f"[VitalManager] 体力段階変化を記録: {old_stamina} → {self.data['stamina']}")
        except Exception as e:
            tlog(f"[VitalManager] today.md記録失敗: {e}")

        # タイムスタンプ更新と保存
        self.data["last_updated"] = datetime.now().isoformat()
        self._save()

    def update_mental(self, delta: int):
        """
        Mental を更新する。
        Args:
            delta: 差分値（+5, -15 等）
        """
        current = self.data.get("mental", 100)
        self.data["mental"] = max(0, min(100, current + delta))
        self.data["last_updated"] = datetime.now().isoformat()
        self._save()
        tlog(f"[VitalManager] mental 更新: {current} → {self.data['mental']} (delta: {delta:+d})")

    def get_vital_prompt(self) -> str:
        """
        Stamina/Mental の現在値に応じたシステムプロンプト注入文を返す。
        メッセージは data/vital_messages.json から読み込む。
        VITAL_REPORT 出力ルールは常に含める。
        順序: VITAL_REPORTルール → 体調/気持ちメモ（最後）
        """
        import json as _json

        stamina = self.data.get("stamina", 100)
        mental = self.data.get("mental", 100)

        parts = []

        # MoodPhase / MoonTide 気分メッセージ
        if hasattr(self, 'moontide') and self.moontide:
            mood_text = self.moontide.get_prompt_text()
            if mood_text:
                parts.append(mood_text)
        elif hasattr(self, 'moodphase') and self.moodphase:
            mood_texts = self.moodphase.get_mood_text()
            parts.extend(mood_texts)

        # 欲求メッセージ
        if hasattr(self, 'desire_manager') and self.desire_manager:
            self.desire_manager.update_time()
            desire_prompt = self.desire_manager.get_desire_prompt()
            if desire_prompt:
                parts.append(desire_prompt)

        # vital_messages.json からメッセージを読み込む（data_root 基準）
        from core.paths import data_file
        messages_path = str(data_file("vital_messages.json"))
        try:
            with open(messages_path, 'r', encoding='utf-8') as f:
                vital_messages = _json.load(f)
        except (FileNotFoundError, _json.JSONDecodeError):
            vital_messages = {"stamina": [], "mental": []}

        # エナジーとスタミナのメッセージ（より深刻な方を優先）
        # vital_config.json の "enabled" が false の場合、体力/エナジーの体調文は注入しない
        # （気分・欲求は別系統なのでここでは止めない）。デフォルト true で後方互換。
        if self.config.get("enabled", True):
            energy = self.data.get("energy", self.config.get("energy_max", 50))
            stamina_max = self.config.get("stamina_max", 500)
            energy_max = self.config.get("energy_max", 50)

            # 割合で比較（どちらがより深刻か）
            stamina_ratio = stamina / stamina_max if stamina_max > 0 else 1.0
            energy_ratio = energy / energy_max if energy_max > 0 else 1.0

            if energy_ratio <= stamina_ratio:
                # エナジーの方が深刻 → エナジーメッセージ
                for entry in vital_messages.get("energy", []):
                    if energy <= entry["threshold"]:
                        if entry.get("message"):
                            parts.append(entry["message"])
                        break
            else:
                # スタミナの方が深刻 → スタミナメッセージ
                for entry in vital_messages.get("stamina", []):
                    if stamina <= entry["threshold"]:
                        if entry.get("message"):
                            parts.append(entry["message"])
                        break
        if parts:
            return "\n".join(parts)
        return ""

    def get_status(self) -> dict:
        """現在のバイタル状態を辞書で返す（UI表示用）"""
        config = self.config
        return {
            "stamina": self.data.get("stamina", 500),
            "stamina_max": config.get("stamina_max", 500),
            "energy": self.data.get("energy", 50),
            "energy_max": config.get("energy_max", 50),
            "mental": self.data.get("mental", 100),
            "last_updated": self.data.get("last_updated"),
        }

