"""
life_action - 生活行動サテライト

柚月が「ぼーっとする」「寝る」「仮眠する」「窓の外を見る」「何もしない」
などの生活行動を実行する。
"""
import json
import sys
import os
import random
from datetime import datetime, timedelta

from _i18n import t, get_language

STATE_FILE = ".life_action_state.json"

# ACTIONS の name は内部キーなので不変。description は LLM が見る = i18n キーから引く
ACTIONS_DEFS = [
    ("idle", "life_action_act_idle_desc"),
    ("sleep", "life_action_act_sleep_desc"),
    ("nap", "life_action_act_nap_desc"),
    ("look_outside", "life_action_act_look_outside_desc"),
    ("nothing", "life_action_act_nothing_desc"),
]

DEFAULT_DURATIONS = {
    "nap": 90,
}

TIME_SLOTS = [
    ("early_morning", 5, 7),
    ("morning", 7, 10),
    ("daytime", 10, 15),
    ("afternoon", 15, 18),
    ("evening", 18, 21),
    ("night", 21, 29),  # 29 = 翌5時（21-24 + 0-5）
]

# WEATHER_MAP は context.py/weather.py の ja 出力フォーマットと1対1。
# 翻訳すると壊れるので構造マーカー扱いで ja 固定（en では weather_text は空入力
# として `cloudy` にフォールバックする）。
WEATHER_MAP = {
    "快晴": "clear", "晴れ": "clear", "一部曇り": "clear",
    "曇り": "cloudy",
    "弱い霧雨": "rain", "霧雨": "rain", "強い霧雨": "rain",
    "弱い雨": "rain", "雨": "rain", "強い雨": "rain",
    "弱いにわか雨": "heavy_rain", "にわか雨": "heavy_rain",
    "激しいにわか雨": "heavy_rain",
    "弱い雪": "snow", "雪": "snow", "強い雪": "snow", "霧雪": "snow",
    "霧": "fog", "着氷性の霧": "fog",
    "雷雨": "thunder", "雹を伴う雷雨": "thunder",
    "激しい雹を伴う雷雨": "thunder",
}


def get_time_slot(sunrise_hour=6.0, sunset_hour=18.0):
    now = datetime.now()
    hour = now.hour + now.minute / 60
    if hour < 5:
        hour += 24

    if hour < sunrise_hour:
        return "early_morning"
    elif hour < sunrise_hour + 3:
        return "morning"
    elif hour < sunset_hour - 3:
        return "daytime"
    elif hour < sunset_hour - 1:
        return "afternoon"
    elif hour < sunset_hour + 1:
        return "evening"
    else:
        return "night"


def get_weather_key(weather_text):
    for jp, key in WEATHER_MAP.items():
        if jp in weather_text:
            return key
    return "cloudy"


def load_scenes():
    """言語別の scenes ファイルを読む。
    scenes_<lang>.json があればそれ、なければ既定の scenes.json（ja 用）を読む。
    存在しなければ None。"""
    here = os.path.dirname(__file__)
    lang = get_language()
    candidates = [
        os.path.join(here, f"scenes_{lang}.json"),
        os.path.join(here, "scenes.json"),
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                continue
    return None


def save_state(workspace, state):
    path = os.path.join(workspace, STATE_FILE)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def action_help():
    actions = [{"name": name, "description": t(desc_key)} for name, desc_key in ACTIONS_DEFS]
    return {
        "status": "success",
        "message": t("life_action_help_message"),
        "data": {"actions": actions}
    }


# 月の描写 flavor 文。ja 用に厚みのある描写を持ち、en では空にして
# 「Tonight is <phase>.」だけの素朴な返答にフォールバックする（後フェーズで充実）。
MOON_VISIBLE_CLEAR_JA = [
    "月が澄んだ空に浮かんでいる。",
    "月明かりが地面まで届いている。",
    "月がくっきりと輝いている。",
    "夜空に月が白く光っている。",
]

MOON_VISIBLE_CLOUDY_JA = [
    "雲間からぼんやりと月が光っている。",
    "厚い雲の向こうに月の気配がある。",
    "雲を透かして月がうっすらと見える。",
    "雲の隙間に月が顔を出している。",
]

MOON_VISIBLE_FOG_JA = [
    "霧の向こうに月がぼんやりと滲んでいる。",
    "霧に包まれた夜空の奥に月の光がある。",
    "霧越しに月がうっすらと光っている。",
    "霧が月の輪郭を溶かして、淡い光だけが残っている。",
]


def _moon_flavor(weather_key):
    """言語別の月描写 flavor 文リストを返す。en では空。"""
    if get_language() != "ja":
        return []
    if weather_key == "clear":
        return MOON_VISIBLE_CLEAR_JA
    if weather_key == "fog":
        return MOON_VISIBLE_FOG_JA
    return MOON_VISIBLE_CLOUDY_JA


# 月相の i18n キー対応
_PHASE_KEYS = {
    "new_moon": "life_action_phase_new",
    "crescent": "life_action_phase_crescent",
    "first_quarter": "life_action_phase_first_quarter",
    "waxing_gibbous": "life_action_phase_waxing_gibbous",
    "full_moon": "life_action_phase_full",
    "waning_gibbous": "life_action_phase_waning_gibbous",
    "last_quarter": "life_action_phase_last_quarter",
    "waning_crescent": "life_action_phase_waning_crescent",
}


def get_moon_phase(date=None):
    """月齢を計算して月相 (i18n 後の表示文字列, 月齢) を返す"""
    if date is None:
        date = datetime.now()
    # 既知の新月日から日数を計算（2000-01-06を基準）
    known_new_moon = datetime(2000, 1, 6, 18, 14)
    diff = (date - known_new_moon).total_seconds()
    lunar_cycle = 29.53058867 * 24 * 3600
    age = (diff % lunar_cycle) / (24 * 3600)

    if age < 1.5:
        phase_key = "new_moon"
    elif age < 7.4:
        phase_key = "crescent"
    elif age < 8.5:
        phase_key = "first_quarter"
    elif age < 13.5:
        phase_key = "waxing_gibbous"
    elif age < 15.5:
        phase_key = "full_moon"
    elif age < 21.5:
        phase_key = "waning_gibbous"
    elif age < 22.5:
        phase_key = "last_quarter"
    else:
        phase_key = "waning_crescent"

    return t(_PHASE_KEYS[phase_key]), age, phase_key


def action_look_outside(weather_text=""):
    scenes = load_scenes()
    if not scenes:
        return {
            "status": "success",
            "message": t("life_action_look_fallback"),
            "data": {}
        }

    # 天気をAPIから直接取得
    sunrise_hour = 6.0  # フォールバック
    sunset_hour = 18.0  # フォールバック
    current_weather = weather_text  # フォールバック
    try:
        import requests
        LAT, LON = 43.7707, 142.3650
        WMO_WEATHER = {
            0: "快晴", 1: "晴れ", 2: "一部曇り", 3: "曇り",
            45: "霧", 48: "着氷性の霧",
            51: "弱い霧雨", 53: "霧雨", 55: "強い霧雨",
            61: "弱い雨", 63: "雨", 65: "強い雨",
            71: "弱い雪", 73: "雪", 75: "強い雪", 77: "霧雪",
            80: "弱いにわか雨", 81: "にわか雨", 82: "激しいにわか雨",
            95: "雷雨", 96: "雹を伴う雷雨", 99: "激しい雹を伴う雷雨",
        }
        res = requests.get(
            "https://api.open-meteo.com/v1/jma",
            params={
                "latitude": LAT, "longitude": LON,
                "current_weather": True,
                "daily": "sunrise,sunset",
                "timezone": "Asia/Tokyo",
            }, timeout=5
        )
        res.raise_for_status()
        data = res.json()
        code = data["current_weather"]["weathercode"]
        current_weather = WMO_WEATHER.get(code, "曇り")
        # 日の出・日没を取得
        sunrise_str = data["daily"]["sunrise"][0]  # "2026-04-13T04:45"
        sunset_str = data["daily"]["sunset"][0]    # "2026-04-13T18:32"
        sunrise_hour = int(sunrise_str[11:13]) + int(sunrise_str[14:16]) / 60
        sunset_hour = int(sunset_str[11:13]) + int(sunset_str[14:16]) / 60

    except Exception:
        pass

    time_slot = get_time_slot(sunrise_hour=sunrise_hour, sunset_hour=sunset_hour)
    weather_key = get_weather_key(current_weather)

    texts = scenes.get(time_slot, {}).get(weather_key, [])
    if not texts:
        texts = scenes.get(time_slot, {}).get("cloudy", [t("life_action_look_fallback")])

    message = random.choice(texts)

    # 夜間のみ月情報を追加
    if time_slot in ("night", "early_morning"):
        phase_text, age, phase_key = get_moon_phase()
        flavor = _moon_flavor(weather_key)
        if weather_key == "clear":
            if phase_key != "new_moon" and flavor:
                message += random.choice(flavor)
            moon_line = t("life_action_moon_visible", phase=phase_text)
        elif weather_key in ("cloudy", "fog"):
            if phase_key != "new_moon" and flavor:
                message += random.choice(flavor)
            moon_line = t("life_action_moon_inferred", phase=phase_text)
        else:
            moon_line = t("life_action_moon_inferred", phase=phase_text)
        message += "\n" + moon_line

    return {
        "status": "success",
        "message": message,
        "data": {"time_slot": time_slot, "weather": weather_key}
    }


def action_rest(workspace, action, duration_minutes=None):
    now = datetime.now()

    if action == "sleep" and duration_minutes is None:
        # 翌朝7:00まで
        tomorrow_7am = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if now.hour >= 7:
            tomorrow_7am += timedelta(days=1)
        until = tomorrow_7am
        duration_minutes = int((until - now).total_seconds() / 60)
    else:
        if duration_minutes is None:
            duration_minutes = DEFAULT_DURATIONS.get(action, 60)
        until = now + timedelta(minutes=duration_minutes)

    state = {
        "action": action,
        "started_at": now.isoformat(),
        "until": until.isoformat(),
        "wake_on_user": True,
        "wake_on_schedule": True,
    }
    save_state(workspace, state)

    action_label_keys = {
        "idle": "life_action_label_idle",
        "sleep": "life_action_label_sleep",
        "nap": "life_action_label_nap",
        "nothing": "life_action_label_nothing",
    }
    label = t(action_label_keys.get(action, action_label_keys.get("idle")))

    return {
        "status": "success",
        "message": t("life_action_rest_entered", label=label, minutes=duration_minutes),
        "data": {
            "action": action,
            "duration_minutes": duration_minutes,
            "until": until.isoformat(),
        }
    }


def main():
    args = json.loads(sys.stdin.read())
    workspace = os.environ.get("CG_WORKSPACE", ".")

    action = args.get("action")

    if not action:
        result = action_help()
    elif action == "look_outside":
        weather_text = args.get("weather_text", "")
        result = action_look_outside(weather_text)
    elif action in ("idle", "nothing"):
        # 瞬間的な行動（状態ファイルを作らない）
        moment_keys = {
            "idle": "life_action_label_idle",
            "nothing": "life_action_label_nothing",
        }
        label = t(moment_keys[action])
        result = {
            "status": "success",
            "message": t("life_action_moment_message", label=label),
            "data": {"action": action}
        }
    elif action in ("sleep", "nap"):
        duration = args.get("duration_minutes")
        # 睡眠は「必ず時刻を見て判断する」ステップを挟ませる。
        # confirmed:true でも、直前に confirm_required を出していなければ即寝させない
        # （いきなり confirmed:true で時刻判断を飛ばして即寝するのを防ぐ＝確認機能の本来の目的）。
        if action == "sleep":
            now = datetime.now()
            pending_path = os.path.join(workspace, ".sleep_pending.json")
            CONFIRM_WINDOW_SEC = 300  # confirm_required の有効時間（秒）。ターン内のみ有効、翌ターンには持ち越さない

            # 直前に confirm_required を出していたか（最近の pending があるか）
            recent_pending = False
            try:
                if os.path.exists(pending_path):
                    with open(pending_path, "r", encoding="utf-8") as f:
                        ts = datetime.fromisoformat(json.load(f).get("at", ""))
                    if (now - ts).total_seconds() <= CONFIRM_WINDOW_SEC:
                        recent_pending = True
            except Exception:
                recent_pending = False

            if args.get("confirmed") and recent_pending:
                # 正規フロー: 時刻を確認した上での confirmed:true → 実際に寝る。pending を消す。
                try:
                    os.remove(pending_path)
                except Exception:
                    pass
                result = action_rest(workspace, action, duration)
            else:
                # 未確認、または「いきなり confirmed:true（時刻判断を飛ばした）」 → 必ず確認を求める。
                # pending を記録し、次の confirmed:true で初めて寝られるようにする。
                try:
                    with open(pending_path, "w", encoding="utf-8") as f:
                        json.dump({"at": now.isoformat()}, f, ensure_ascii=False)
                except Exception:
                    pass
                result = {
                    "status": "confirm_required",
                    "message": t("life_action_sleep_confirm", hour=now.hour, minute=f"{now.minute:02d}"),
                    "data": {
                        "action": "sleep",
                        "current_time": now.strftime("%H:%M"),
                    }
                }
        else:
            # nap は確認なし
            result = action_rest(workspace, action, duration)
    else:
        result = {
            "status": "error",
            "message": t("life_action_err_unknown", action=action),
            "data": {}
        }

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
