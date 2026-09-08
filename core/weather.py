import requests
import time
from core.i18n import t

# 旭川の設定
LAT = 43.7707
LON = 142.3650
JMA_CITY_CODE = "012010"

METEO_URL = "https://api.open-meteo.com/v1/jma"
JMA_URL = f"https://weather.tsukumijima.net/api/forecast/city/{JMA_CITY_CODE}"

# WMO 気象コード → i18n キー名のマッピング。表示文字列は t() で言語別に解決する。
_WMO_KEYS = {
    0: "wx_clear", 1: "wx_sunny", 2: "wx_partly_cloudy", 3: "wx_cloudy",
    45: "wx_fog", 48: "wx_freezing_fog",
    51: "wx_drizzle_light", 53: "wx_drizzle", 55: "wx_drizzle_heavy",
    61: "wx_rain_light", 63: "wx_rain", 65: "wx_rain_heavy",
    71: "wx_snow_light", 73: "wx_snow", 75: "wx_snow_heavy", 77: "wx_snow_grains",
    80: "wx_shower_light", 81: "wx_shower", 82: "wx_shower_violent",
    85: "wx_snow_shower_light", 86: "wx_snow_shower_violent",
    95: "wx_thunderstorm", 96: "wx_thunderstorm_hail", 99: "wx_thunderstorm_hail_heavy",
}

def _wmo_label(code) -> str:
    key = _WMO_KEYS.get(code)
    return t(key) if key else t("wx_unknown")

_cache = {"data": None, "updated_at": 0}
CACHE_TTL = 1800  # 30分


def fetch_weather() -> dict:
    """天気情報を取得して辞書で返す"""
    result = {}

    try:
        meteo_res = requests.get(METEO_URL, params={
            "latitude": LAT, "longitude": LON,
            "current_weather": True,
            "daily": "temperature_2m_max,temperature_2m_min,weathercode",
            "timezone": "Asia/Tokyo",
            "forecast_days": 1,
        }, timeout=5)
        meteo_res.raise_for_status()
        meteo = meteo_res.json()

        current = meteo["current_weather"]
        daily = meteo["daily"]

        result["current_temp"] = current["temperature"]
        result["current_weather"] = _wmo_label(current["weathercode"])
        result["today_max"] = daily["temperature_2m_max"][0]
        result["today_min"] = daily["temperature_2m_min"][0]
    except Exception as e:
        print(f"[Weather] Open-Meteo取得失敗: {e}")

    try:
        jma_res = requests.get(JMA_URL, timeout=5)
        jma_res.raise_for_status()
        jma = jma_res.json()
        result["today_forecast"] = jma["forecasts"][0]["telop"]
    except Exception as e:
        print(f"[Weather] JMA取得失敗: {e}")

    return result


def get_weather_string() -> str:
    """キャッシュ付きで天気情報文字列を返す"""
    now = time.time()
    if _cache["data"] is None or now - _cache["updated_at"] > CACHE_TTL:
        new_data = fetch_weather()
        if new_data:  # 取得成功時のみキャッシュ更新
            _cache["data"] = new_data
            _cache["updated_at"] = now
        elif _cache["data"] is None:
            return ""  # 初回取得失敗時は空文字
        # 失敗時は前回キャッシュをそのまま使う
    w = _cache["data"]
    if not w:
        return ""
    parts = []
    if "today_forecast" in w:
        parts.append(t("wx_today", weather=w['today_forecast']))
    elif "current_weather" in w:
        parts.append(t("wx_today", weather=w['current_weather']))
    if "today_max" in w and "today_min" in w:
        parts.append(t("wx_high_low", high=w['today_max'], low=w['today_min']))
    if "current_temp" in w:
        parts.append(t("wx_current_temp", temp=w['current_temp']))
    if "current_weather" in w:
        parts.append(t("wx_current_sky", sky=w['current_weather']))
    return "  ".join(parts)