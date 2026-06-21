"""
MoodPhase - 気分位相システム

4軸の気分状態を管理する:
    H (Hedonic): 快不快 (1=不快, 4=普通, 7=快)
    S (Sociality): 社交性 (1=一人でいたい, 4=普通, 7=人と関わりたい)
    T (Tension): 緊張/不安度 (1=弛緩, 4=普通, 7=緊張)
    A (Absorption): 思考の鋭さ (1=ぼんやり, 4=普通, 7=冴えている)

起動時にH,S,T,Aを正規分布(平均4, σ=1)で初期化。
1ターンごとにtick()で変動:
    40% 変化なし
    40% どれか1つが4に1歩近づく
    20% どれか1つが4から1歩遠のく
"""

from core.time_utils import tlog
import json
import os
import random

# moodphase_state.json（実行時書き込み）と mood_hs/mood_ta（参照データ）のパスは
# data_root 基準で「遅延解決」する。モジュール import 時に定数化すると set_data_root()
# より前に確定し、配布版で install_root（読み取り専用になり得る）を向く罠になるため、
# 関数で都度解決する。dev（data_root == bundle_root）では従来と同一パス。
def _moodphase_state_path() -> str:
    from core.paths import data_file
    return str(data_file("moodphase_state.json"))


def _mood_hs_path() -> str:
    from core.paths import data_file
    return str(data_file("mood_hs.jsonl"))


def _mood_ta_path() -> str:
    from core.paths import data_file
    return str(data_file("mood_ta.jsonl"))


def _clamp(value, lo=1, hi=7):
    return max(lo, min(hi, value))


def _random_center():
    """正規分布(平均4, σ=1)で1-7の整数を生成"""
    v = round(random.gauss(4, 1))
    return _clamp(v)


def _load_mood_table(path):
    """jsonlファイルを読み込み、座標→テキストの辞書を返す"""
    table = {}
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    entry = json.loads(line)
                    table[entry["coord"]] = entry["text"]
    return table


class MoodPhase:
    def __init__(self):
        self.h = 4
        self.s = 4
        self.t = 4
        self.a = 4
        self.date = None
        self._hs_table = _load_mood_table(_mood_hs_path())
        self._ta_table = _load_mood_table(_mood_ta_path())
        self._load()

    def _load(self):
        state_path = _moodphase_state_path()
        if os.path.exists(state_path):
            try:
                with open(state_path, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                self.h = state.get("h", 4)
                self.s = state.get("s", 4)
                self.t = state.get("t", 4)
                self.a = state.get("a", 4)
                self.date = state.get("date", None)
            except (json.JSONDecodeError, IOError):
                self._reset()
        else:
            self._reset()

    def _save(self):
        state = {
            "h": self.h,
            "s": self.s,
            "t": self.t,
            "a": self.a,
            "date": self.date,
        }
        try:
            state_path = _moodphase_state_path()
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            tmp = state_path + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, state_path)
        except Exception as e:
            tlog(f"[MoodPhase] 保存失敗: {e}")

    def _reset(self):
        """新しい日の気分を正規分布で初期化"""
        self.h = _random_center()
        self.s = _random_center()
        self.t = _random_center()
        self.a = _random_center()
        tlog(f"[MoodPhase] 初期化: H={self.h} S={self.s} T={self.t} A={self.a}")
        self._save()

    def check_day_reset(self, today_str: str):
        """日付が変わっていたらリセット"""
        if self.date != today_str:
            self.date = today_str
            self._reset()

    def tick(self):
        """1ターンごとの気分変動"""
        roll = random.random()
        axes = ['h', 's', 't', 'a']

        if roll < 0.4:
            # 変化なし
            pass
        elif roll < 0.8:
            # 40%: どれか1つが4に近づく
            axis = random.choice(axes)
            val = getattr(self, axis)
            if val < 4:
                setattr(self, axis, val + 1)
            elif val > 4:
                setattr(self, axis, val - 1)
        else:
            # 20%: どれか1つが4から遠のく
            axis = random.choice(axes)
            val = getattr(self, axis)
            if val <= 4:
                setattr(self, axis, _clamp(val - 1))
            else:
                setattr(self, axis, _clamp(val + 1))

        self._save()

    def get_mood_text(self) -> list[str]:
        """現在の気分テキストを返す（HS, TAの2つ）"""
        hs_coord = f"{self.h}{self.s}"
        ta_coord = f"{self.t}{self.a}"

        texts = []
        hs_text = self._hs_table.get(hs_coord)
        if hs_text:
            texts.append(hs_text)
        ta_text = self._ta_table.get(ta_coord)
        if ta_text:
            texts.append(ta_text)

        return texts

    def get_status_summary(self) -> str:
        """デバッグ用: 現在の状態を文字列で返す"""
        return f"H={self.h} S={self.s} T={self.t} A={self.a}"


    def recover_from_nap(self) -> dict:
        """仮眠終了時の気分回復。回復した軸を {axis: '変化前→変化後'} で返す。
        H/S/T: 3以下なら+1、4以上は変化なし
        A（思考）: 6以下なら+1、7以上は変化なし
        """
        caps = {"h": 3, "s": 3, "t": 3, "a": 6}
        changed = {}
        for axis, cap in caps.items():
            current = getattr(self, axis, 4)
            if current <= cap:
                setattr(self, axis, current + 1)
                changed[axis] = f"{current}→{current+1}"
        if changed:
            self._save()
            tlog(f"[MoodPhase] 仮眠回復: {changed}")
        return changed