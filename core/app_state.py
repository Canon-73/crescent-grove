# core/app_state.py
"""
サーバープロセス内で共有されるグローバル状態。

server.py のモノリス時代にモジュールグローバルとして持っていた変数群を、
APIRouter 分割（core/routes/）後も全モジュールから参照できるようここに集約する。

重要: 値は再代入される（global_agent は起動時に None → Agent インスタンス等）ため、
取り込み側は必ず「モジュール属性アクセス」で参照すること。

    from core import app_state
    app_state.global_agent          # ← 常に最新の値が見える（正しい）

    from core.app_state import global_agent   # ← import 時の値で固定される（誤り）
"""

import json
import asyncio

from core.agent import ProcessingLock, Agent
from core.scheduler import Scheduler

# =============================================================================
# 共有状態
# =============================================================================

global_scheduler: "Scheduler | None" = None   # サーバー全体で共有されるスケジューラインスタンス
global_agent: "Agent | None" = None           # タスク実行用のグローバルAgentインスタンス（スケジュール実行時に使用）
global_processing_lock = ProcessingLock()     # ユーザー会話とバックグラウンドタスクの排他制御用ロック
global_last_debug_context = None              # 最新のデバッグ用コンテキスト情報（全エージェントで共有）
debug_websockets: set = set()                 # デバッグページ用WebSocket接続の集合
active_chat_websockets: set = set()           # アクティブなチャットクライアントのWebSocket接続（複数対応）
active_chat_agent = None                      # 現在アクティブなチャットセッションのAgentインスタンス
last_chat_time = 0                            # 最後にユーザーがメッセージを送信したUNIXタイムスタンプ
last_moonbeat_response = ""                   # 直前のMoonbeat応答テキスト（類似度チェック用）
similarity_model = None                       # Moonbeat類似度チェック用のSentenceTransformerモデル

# OpenClawのcity_eventを逐次処理するための非同期キュー
openclaw_event_queue: asyncio.Queue = asyncio.Queue()


# =============================================================================
# 全クライアント配信
# =============================================================================

async def broadcast(data: dict):
    """接続中の全チャットクライアントにメッセージをブロードキャスト配信する。"""
    global active_chat_websockets
    disconnected = set()
    for ws in active_chat_websockets:
        try:
            await ws.send_text(json.dumps(data, ensure_ascii=False))
        except Exception:
            disconnected.add(ws)
    active_chat_websockets -= disconnected


async def register_debug_context(data: dict):
    """エージェントからのデバッグ情報をグローバルに保存し、接続中の全デバッグクライアントにWebSocket配信する。"""
    global global_last_debug_context
    global_last_debug_context = data

    # 接続中のすべてのデバッグクライアントに送信
    if debug_websockets:
        message = json.dumps(data, ensure_ascii=False)
        # 送信に失敗した（切断済みの）接続を記録して後で除去する
        disconnected = set()
        for ws in debug_websockets:
            try:
                await ws.send_text(message)
            except Exception:
                disconnected.add(ws)

        for ws in disconnected:
            debug_websockets.remove(ws)


# =============================================================================
# 稼働中設定の再読込
# =============================================================================

def reload_runtime_config() -> bool:
    """settings.json を再読込し、稼働中の設定をサーバー再起動なしで反映する。

    起動時に構築した config dict は ContextBuilder や各クロージャが
    同じ参照を共有している。その「中身だけ」を入れ替える（clear + update）ことで、
    プロンプト・プロフィール・メモリ設定など大半の項目が即座に反映される。
    システムプロンプトと起動記憶はキャッシュされているため、reload_memories() で
    再構築する。

    注意: LLMプロバイダ/モデルの差し替えはここでは行わない（要サーバー再起動）。

    Returns:
        反映できた場合 True。global_agent 未初期化などで反映できなければ False。
    """
    from core.config_loader import load_config as core_load_config

    if global_agent is None or getattr(global_agent, "context", None) is None:
        return False

    # 同一参照の config dict を「中身だけ」入れ替える（全クロージャ・context に波及）
    live_config = global_agent.context.config
    new_config = core_load_config()
    live_config.clear()
    live_config.update(new_config)

    # Agent インスタンスにコピー保持されている値も追従させる
    try:
        profile = new_config.get("profile", {})
        global_agent.honorific = profile.get("user", {}).get("honorific", "ユーザー")
        agent_name = profile.get("agent", {}).get("name")
        if agent_name:
            global_agent.agent_name = agent_name
    except Exception as e:
        print(f"警告: reload_runtime_config の Agent 値追従に失敗しました: {e}")

    # 論理日付（午前3時境界）のタイムゾーンを time.tz_offset に追従させる。
    # context.py の時刻表示は live_config を毎ターン読むので自動追従するが、
    # time_utils はモジュール変数で保持しているためここで明示的に更新する。
    try:
        from core.time_utils import set_context_timezone
        set_context_timezone(new_config.get("time", {}).get("tz_offset", 9))
    except Exception as e:
        print(f"警告: reload_runtime_config のタイムゾーン追従に失敗しました: {e}")

    # キャッシュ済みのシステムプロンプト・起動記憶を再構築する
    try:
        global_agent.context.reload_memories()
    except Exception as e:
        print(f"警告: reload_runtime_config の reload_memories に失敗しました: {e}")

    print("[reload] 設定を再読込し、稼働中の設定に反映しました（再起動不要）")
    return True
