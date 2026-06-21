# core/routes/settings_api.py
"""設定API群（APIキー・セキュリティ・LLM・一般・config/ 配下の各種設定ファイル）。"""

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core import app_state
from core.i18n import t
from core.config_loader import load_config as core_load_config
from core.env_manager import EnvManager
from core.paths import data_file, config_file, system_prompt_dir

router = APIRouter()


# =============================================================================
# 言語設定エンドポイント
# =============================================================================

SUPPORTED_LANGUAGES = {"ja", "en"}


@router.get("/api/settings/language")
async def get_language():
    """現在の言語設定を返す。"""
    config = core_load_config()
    return {"language": config.get("language", "ja")}


@router.post("/api/settings/language")
async def update_language(req: dict):
    """言語設定を保存する（反映にはサーバー再起動が必要）。"""
    from core.config_loader import save_settings
    lang = req.get("language", "").strip().lower()
    if lang not in SUPPORTED_LANGUAGES:
        raise HTTPException(status_code=400, detail=t("lang_unsupported"))
    save_settings({"language": lang})
    return {"status": "success"}


# =============================================================================
# APIキー管理エンドポイント
# .envファイルへの環境変数の登録・削除・一覧取得を行う。
# =============================================================================

class KeyRegistrationReq(BaseModel):
    """APIキー登録リクエストのボディスキーマ。"""
    name: str
    value: str
    is_llm: bool = False


@router.get("/api/keys")
async def get_keys():
    """登録済み環境変数（APIキー）のリストを返す。値はマスクされる。"""
    return EnvManager.get_all_keys()


@router.post("/api/keys")
async def register_key(req: KeyRegistrationReq):
    """APIキーを.envに登録・更新し、プロセスの環境変数にも即時反映する。"""
    try:
        success = EnvManager.set_key(req.name, req.value, is_llm=req.is_llm)
        if not success:
            raise HTTPException(status_code=400, detail="Invalid key name")
        return {"status": "success"}
    except Exception as e:
        print(f"API Key Reg Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/keys/{name}")
async def delete_key(name: str):
    """指定された名前のAPIキーを.envから削除する。"""
    try:
        success = EnvManager.delete_key(name)
        if not success:
            raise HTTPException(status_code=400, detail="Invalid key name")
        return {"status": "success"}
    except Exception as e:
        print(f"API Key Del Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# セキュリティ設定APIエンドポイント（ドメインホワイトリスト管理）
# =============================================================================

class DomainReq(BaseModel):
    """ドメイン操作リクエストのボディスキーマ。"""
    domain: str


@router.get("/api/settings/security/domains")
async def get_security_domains():
    """許可済みドメインの一覧を返す。Webアクセスツールのホワイトリストに対応。"""
    config = core_load_config()
    domains = config.get("security", {}).get("allowed_domains", [])
    return {"domains": domains}


@router.post("/api/settings/security/domains")
async def add_security_domain(req: DomainReq):
    """許可ドメインリストに新しいドメインを追加する。IPアドレスは拒否。"""
    from core.config_loader import save_settings
    domain = req.domain.strip().lower()

    # バリデーション: 空白文字やIPアドレスは許可しない
    if " " in domain:
        raise HTTPException(status_code=400, detail=t("domain_no_whitespace"))
    if __import__("re").match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", domain):
        raise HTTPException(status_code=400, detail=t("domain_no_ip"))

    config = core_load_config()
    security = config.get("security", {})
    domains = security.get("allowed_domains", [])

    # 重複チェックの上で追加
    if domain not in domains:
        domains.append(domain)
        security["allowed_domains"] = domains
        config["security"] = security
        save_settings({"security": security})

    return {"status": "success"}


@router.delete("/api/settings/security/domains")
async def delete_security_domain(req: DomainReq):
    """許可ドメインリストから指定されたドメインを削除する。"""
    from core.config_loader import save_settings
    domain = req.domain.strip().lower()

    config = core_load_config()
    security = config.get("security", {})
    domains = security.get("allowed_domains", [])

    if domain in domains:
        domains.remove(domain)
        security["allowed_domains"] = domains
        config["security"] = security
        save_settings({"security": security})

    return {"status": "success"}


# =============================================================================
# LLM設定APIエンドポイント
# =============================================================================

class LLMConfigReq(BaseModel):
    """LLM設定更新リクエストのボディスキーマ。"""
    provider: str
    model: str = ""
    base_url: str = ""
    temperature: float = 1.0
    max_tokens: int = 4096
    supports_images: bool = True    # このモデルが画像認識に対応しているか（falseなら画像を送らない）
    thinking: str = "auto"          # メインの思考モード auto/off/low/medium/high
    salia_model: str = ""           # サリア専用モデル（""=メイン追従）
    salia_thinking: str = ""        # サリアの思考モード（""=メイン追従）
    context_max_tokens: int = 65536
    compression_threshold: float = 0.70
    emergency_compression_threshold: float = 0.90
    keep_recent_exchanges: int = 4


@router.get("/api/settings/llm")
async def get_llm_config():
    """現在のLLM設定（プロバイダー・モデル・パラメータ・コンテキスト設定）を返す。"""
    config = core_load_config()
    llm = config.get("llm", {})
    context = config.get("context", {})
    salia = config.get("salia", {})
    return {
        **llm,
        "supports_images": llm.get("supports_images", True),
        "thinking": llm.get("thinking", "auto"),
        "salia_model": salia.get("model", ""),
        "salia_thinking": salia.get("thinking", ""),
        "context_max_tokens": context.get("max_tokens", 65536),
        "compression_threshold": context.get("compression_threshold", 0.70),
        "emergency_compression_threshold": context.get("emergency_compression_threshold", 0.90),
        "keep_recent_exchanges": context.get("keep_recent_exchanges", 4)
    }


@router.post("/api/settings/llm")
async def update_llm_config(req: LLMConfigReq):
    """LLM設定を更新してsettings.jsonに保存する。"""
    from core.config_loader import save_settings
    config = core_load_config()
    llm = config.get("llm", {})

    llm["provider"] = req.provider
    llm["temperature"] = req.temperature
    llm["max_tokens"] = req.max_tokens
    llm["supports_images"] = req.supports_images
    llm["thinking"] = req.thinking or "auto"

    if req.model:
        llm["model"] = req.model
    elif "model" in llm:
        del llm["model"]

    # 過去の settings.json に誤って保存された api_key があれば消去する（キーは.envで管理すべき）
    if "api_key" in llm:
        del llm["api_key"]

    if req.base_url:
        llm["base_url"] = req.base_url
    elif "base_url" in llm:
        del llm["base_url"]

    # コンテキストウィンドウ設定
    context = config.get("context", {})
    context["max_tokens"] = req.context_max_tokens
    context["compression_threshold"] = req.compression_threshold
    context["emergency_compression_threshold"] = req.emergency_compression_threshold
    context["keep_recent_exchanges"] = req.keep_recent_exchanges

    # サリアの LLM 設定（モデル/思考モード）。空文字＝「メインと同じ」（解決側でフォールバック）。
    salia = config.get("salia", {})
    salia["model"] = req.salia_model or ""
    salia["thinking"] = req.salia_thinking or ""

    save_settings({"llm": llm, "context": context, "salia": salia})

    # --- 再起動なしでの即時反映 ---
    # provider / model / base_url の変更はLLMプロバイダの作り直しが必要なため、
    # ここでは反映せず「要再起動」を返す。temperature / max_tokens / コンテキスト
    # ウィンドウ設定は稼働中オブジェクトに直接反映できる。
    restart_required = False
    global_agent = app_state.global_agent
    if global_agent is not None:
        running = getattr(global_agent, "startup_llm", {}) or {}
        def _norm(v):
            return v if v else None
        if (_norm(req.provider) != _norm(running.get("provider"))
                or _norm(req.model) != _norm(running.get("model"))
                or _norm(req.base_url) != _norm(running.get("base_url"))):
            restart_required = True

        # 安全に即反映できるサンプリング系パラメータを稼働中プロバイダに反映する
        try:
            if global_agent.llm is not None:
                global_agent.llm.temperature = req.temperature
                global_agent.llm.max_tokens = req.max_tokens
        except Exception as e:
            print(f"警告: 稼働中LLMへのパラメータ反映に失敗しました: {e}")

        # コンテキストウィンドウ設定を稼働中の config に即反映する
        try:
            live_config = global_agent.context.config
            if "context" not in live_config or not isinstance(live_config["context"], dict):
                live_config["context"] = {}
            live_config["context"]["max_tokens"] = req.context_max_tokens
            live_config["context"]["compression_threshold"] = req.compression_threshold
            live_config["context"]["emergency_compression_threshold"] = req.emergency_compression_threshold
            live_config["context"]["keep_recent_exchanges"] = req.keep_recent_exchanges
            live_config.setdefault("llm", {})
            live_config["llm"]["temperature"] = req.temperature
            live_config["llm"]["max_tokens"] = req.max_tokens
            # 画像認識フラグは送信時に context.config から読むため、稼働中configへ即反映
            live_config["llm"]["supports_images"] = req.supports_images

            # ContextBuilder は __init__ でこれらをインスタンス属性にキャッシュするため、
            # config dict だけ書き換えても反映されない。属性も直接更新して即時反映する。
            ctx = global_agent.context
            ctx.max_tokens = req.context_max_tokens
            ctx.compression_threshold = req.compression_threshold
            ctx.emergency_compression_threshold = req.emergency_compression_threshold
            ctx.keep_recent_exchanges = req.keep_recent_exchanges
        except Exception as e:
            print(f"警告: 稼働中configへのコンテキスト設定反映に失敗しました: {e}")

    return {"status": "success", "restart_required": restart_required}


# =============================================================================
# 一般設定APIエンドポイント
# =============================================================================

class GeneralConfigReq(BaseModel):
    """一般設定更新リクエストのボディスキーマ。"""
    profile: dict
    workspace: dict
    logs: dict
    time: dict = {}
    memory_compression: dict = {}
    letter_file: str = "memory/letter_for_me.md"
    boot_memories: list = []
    system_prompts: dict = {}
    post_prompts: dict = {}
    tokenizer: dict = {}
    salia: dict = {}
    search: dict = {}


@router.get("/api/settings/general")
async def get_general_config():
    """一般設定（プロファイル・ワークスペース・ログ・プロンプト・記憶圧縮等）の現在値を返す。"""
    config = core_load_config()

    sp = config.get("system_prompts", {
        "directory": "system_prompt",
        "files": ["TOP_PROMPT.md", "TOOL_INSTRUCTIONS.md", "SAFETY_PROMPT.md"]
    })

    # ポストプロンプト（真・最下部に置くBOTTOM_PROMPT.md 等）。
    # enabled が無い旧設定には既定 False（無効）を補う。エキスパート向け機能。
    pp = config.get("post_prompts", {
        "directory": "system_prompt",
        "files": ["BOTTOM_PROMPT.md"]
    })
    if "enabled" not in pp:
        pp = {**pp, "enabled": False}

    # --- boot_memories のマイグレーション ---
    # 旧設定に letter_for_me.md が含まれていたら自動で除去する（専用設定に分離済み）
    boot_memories_raw = config.get("boot_memories", [
        "IDENTITY.md", "SOUL.md", "USER.md", "MEMORY.md", "memory/compressed.md"
    ])
    boot_memories = [f for f in boot_memories_raw if f != "memory/letter_for_me.md"]

    return {
        "profile": config.get("profile", {}),
        "workspace": config.get("workspace", {}),
        "logs": config.get("logs", {}),
        # タイムゾーン（コンテキストに注入する時刻表示のみに影響）。未設定時は JST(+9)。
        "time": config.get("time", {"tz_offset": 9, "tz_label": "JST"}),
        "memory_compression": config.get("memory_compression", {
            "max_tokens": 2000,
            "max_event_tokens": 30,
            "decay_coeff": 1.0,
            "max_details": 8,
            "max_events_per_day": 10,
            "event_db": "memory/event_db.json",
            "compressed_file": "memory/compressed.md"
        }),
        "letter_file": config.get("letter_file", "memory/letter_for_me.md"),
        "boot_memories": boot_memories,
        "system_prompts": sp,
        "post_prompts": pp,
        "tokenizer": config.get("tokenizer", {
            "type": "deepseek",
            "path": "data/tokenizer_deepseek.json"
        }),
        "salia": config.get("salia", {
            "enabled": True,
            "somatic_marker": {
                "enabled": True,
                "probability": 0.3,
                "valence_threshold": 0.5,
                "candidate_count": 5
            },
            "evaluate_turn": {"enabled": True},
            "record_check": {"enabled": True}
        }),
        # Web検索プロバイダー（"ddgs" 既定 / "tavily"）。未設定時は ddgs。
        "search": config.get("search", {"provider": "ddgs"})
    }


@router.post("/api/settings/general")
async def update_general_config(req: GeneralConfigReq):
    """一般設定を更新してsettings.jsonに保存する。"""
    from core.config_loader import save_settings
    config = core_load_config()

    # --- プロファイル設定（user/agentの各フィールドを保存） ---
    if "profile" not in config: config["profile"] = {}
    # honorific は user のみが持つ呼称フィールド（agent 側ペイロードには無いが、
    # if field in role_data ガードで安全に無視される）。
    for role in ["user", "agent"]:
        if role not in config["profile"]: config["profile"][role] = {}
        role_data = req.profile.get(role, {})
        for field in ["name", "honorific", "avatar", "size", "layout"]:
            if field in role_data:
                config["profile"][role][field] = role_data[field]

    # --- ワークスペースパス ---
    if "workspace" not in config: config["workspace"] = {}
    config["workspace"]["path"] = req.workspace.get("path", "")

    # --- ログディレクトリ ---
    # 空文字や未指定でリクエストが来た場合に既存値を消さない（消すとPath("")が"."解決され
    # ログがプロジェクトルートに散らばる事故が起きるため）。既存値も無ければデフォルトを使う。
    if "logs" not in config: config["logs"] = {}
    _existing_logs = config.get("logs", {}) or {}
    _new_chat = (req.logs.get("chat_log_directory") or "").strip()
    _new_full = (req.logs.get("full_log_directory") or "").strip()
    config["logs"]["chat_log_directory"] = _new_chat or _existing_logs.get("chat_log_directory") or "workspace/logs/chat"
    config["logs"]["full_log_directory"] = _new_full or _existing_logs.get("full_log_directory") or "workspace/logs/full"

    # --- タイムゾーン設定（コンテキストに注入する時刻表示のみに影響・即時反映） ---
    # tz_offset は UTC からの時差（時間単位の小数可。+9 / +5.5 等）。
    # 常識的範囲（UTC-12〜+14）にクランプし、不正値は JST(+9) に丸める。
    if "time" not in config: config["time"] = {}
    if req.time:
        try:
            _off = float(req.time.get("tz_offset", 9))
        except (TypeError, ValueError):
            _off = 9.0
        config["time"]["tz_offset"] = max(-12.0, min(14.0, _off))
        _label = str(req.time.get("tz_label", "JST")).strip()
        config["time"]["tz_label"] = _label or "JST"

    # --- 記憶圧縮（LETHE）設定 ---
    if "memory_compression" not in config: config["memory_compression"] = {}
    mc_req = req.memory_compression
    config["memory_compression"]["max_tokens"] = mc_req.get("max_tokens", 2000)
    config["memory_compression"]["max_event_tokens"] = mc_req.get("max_event_tokens", 30)
    config["memory_compression"]["decay_coeff"] = float(mc_req.get("decay_coeff", 1.0))
    config["memory_compression"]["max_details"] = mc_req.get("max_details", 8)
    config["memory_compression"]["max_events_per_day"] = mc_req.get("max_events_per_day", 10)
    config["memory_compression"]["event_db"] = mc_req.get("event_db", "memory/event_db.json")
    config["memory_compression"]["compressed_file"] = mc_req.get("compressed_file", "memory/compressed.md")

    # --- 手紙ファイルパス（ダッシュボード連携） ---
    config["letter_file"] = req.letter_file.strip() if req.letter_file.strip() else "memory/letter_for_me.md"

    # --- 起動記憶ファイルリスト（コンテキストに注入するmdファイルの一覧） ---
    config["boot_memories"] = [p for p in req.boot_memories if p.strip()]

    # --- システムプロンプト設定（コンテキスト最上部に配置されるプロンプトファイル群） ---
    if "system_prompts" not in config: config["system_prompts"] = {}
    config["system_prompts"]["directory"] = req.system_prompts.get("directory", "system_prompt")
    config["system_prompts"]["files"] = [f for f in req.system_prompts.get("files", []) if f.strip()]

    # --- ポストプロンプト設定（会話履歴の後＝真・最下部に配置されるプロンプトファイル群） ---
    # enabled はエキスパート向けトグル。OFF（既定）ならコンテキストに一切入らない。
    if "post_prompts" not in config: config["post_prompts"] = {}
    config["post_prompts"]["directory"] = req.post_prompts.get("directory", "system_prompt")
    config["post_prompts"]["files"] = [f for f in req.post_prompts.get("files", []) if f.strip()]
    config["post_prompts"]["enabled"] = bool(req.post_prompts.get("enabled", False))

    # --- トークナイザー設定（トークン数見積もりに使用。変更は要再起動） ---
    if "tokenizer" not in config: config["tokenizer"] = {}
    if req.tokenizer:
        config["tokenizer"]["type"] = req.tokenizer.get("type", "deepseek")
        _tk_path = (req.tokenizer.get("path") or "").strip()
        config["tokenizer"]["path"] = _tk_path or config["tokenizer"].get("path", "data/tokenizer_deepseek.json")

    # --- Salia（情動評価）設定 ---
    # somatic_marker / evaluate_turn は settings.json から毎ターン直接読まれるため即時反映。
    # salia.enabled（機能全体ON/OFF）のみ初期化時に評価されるため要再起動。
    if "salia" not in config: config["salia"] = {}
    if req.salia:
        config["salia"]["enabled"] = bool(req.salia.get("enabled", True))
        sm_req = req.salia.get("somatic_marker", {})
        sm_cfg = config["salia"].get("somatic_marker", {})
        if not isinstance(sm_cfg, dict): sm_cfg = {}
        sm_cfg["enabled"] = bool(sm_req.get("enabled", True))
        sm_cfg["probability"] = float(sm_req.get("probability", 0.3))
        sm_cfg["valence_threshold"] = float(sm_req.get("valence_threshold", 0.5))
        sm_cfg["candidate_count"] = int(sm_req.get("candidate_count", 5))
        config["salia"]["somatic_marker"] = sm_cfg
        et_req = req.salia.get("evaluate_turn", {})
        et_cfg = config["salia"].get("evaluate_turn", {})
        if not isinstance(et_cfg, dict): et_cfg = {}
        et_cfg["enabled"] = bool(et_req.get("enabled", True))
        config["salia"]["evaluate_turn"] = et_cfg
        # 記録判定フック（ツール使用ターン後の内省ステップ）。process_message が毎ターン
        # settings.json を直接読むため即時反映。
        rc_req = req.salia.get("record_check", {})
        rc_cfg = config["salia"].get("record_check", {})
        if not isinstance(rc_cfg, dict): rc_cfg = {}
        rc_cfg["enabled"] = bool(rc_req.get("enabled", True))
        config["salia"]["record_check"] = rc_cfg

    # --- Web検索プロバイダー設定 ---
    # provider は core/web_tools.py が毎回 load_config で読むため即時反映（再起動不要）。
    # "ddgs"（DuckDuckGo・無料）/ "tavily"（要APIキー）のみ許可し、不正値は ddgs に丸める。
    if "search" not in config: config["search"] = {}
    if req.search:
        _provider = str(req.search.get("provider", "ddgs")).lower()
        config["search"]["provider"] = _provider if _provider in ("ddgs", "tavily") else "ddgs"
        # 複数クエリ同時検索の調整値。数値以外・範囲外は既定にクランプ（1〜10）。
        def _clamp_int(value, default, lo=1, hi=10):
            try:
                return max(lo, min(hi, int(value)))
            except (TypeError, ValueError):
                return default
        if "max_parallel" in req.search:
            config["search"]["max_parallel"] = _clamp_int(req.search.get("max_parallel"), 3)
        if "max_queries" in req.search:
            config["search"]["max_queries"] = _clamp_int(req.search.get("max_queries"), 5)

    save_settings({
        "profile": config["profile"],
        "workspace": config["workspace"],
        "logs": config["logs"],
        "time": config["time"],
        "memory_compression": config["memory_compression"],
        "letter_file": config["letter_file"],
        "boot_memories": config["boot_memories"],
        "system_prompts": config["system_prompts"],
        "post_prompts": config["post_prompts"],
        "tokenizer": config["tokenizer"],
        "salia": config["salia"],
        "search": config["search"]
    })

    # 保存した設定を稼働中のプロセスに即反映する（パス系を除き再起動不要）
    reloaded = app_state.reload_runtime_config()
    return {"status": "success", "reloaded": reloaded}


# =============================================================================
# システムプロンプト本文の編集APIエンドポイント（固定スロット）
#
# 人格・ツール説明・安全ルール等のプロンプトmdは、従来 system_prompt/ 配下の
# ファイルをエディタで直接開いて編集するしかなかった（＝workspace外ファイルを
# ユーザーに触らせていた）。これを設定UIから編集できるようにする。
#
# 設計（完全固定）:
#   - 編集できるファイルは下記の固定スロットのみ。追加・削除・パス指定は一切なし。
#   - クライアントは id（"top" 等）だけを送り、実ファイル名はサーバ側のマッピングで
#     決める。任意パスを受け取らないため、ディレクトリ外への書き込みが構造的に不可能。
#   - 保存のたびに直前版を <file>.bak へ1世代退避する（壊しても戻せる）。
#   - 空文字の保存も許可する（BOTTOM_PROMPT 等は空が正常）。
#
# 即時反映:
#   - top/tool/safety/bottom … ContextBuilder が起動時にキャッシュするため
#     reload_memories()（_rebuild_all）で再構築する。
#   - salia            … Salia インスタンスが起動時にキャッシュするため
#     reload_system_prompt() で再読込する。
# =============================================================================

# 編集可能なプロンプトの固定スロット。id → 実ファイル名のマッピングはここが唯一の出所。
SYSTEM_PROMPT_SLOTS = [
    {"id": "top", "file": "TOP_PROMPT.md", "label": "人格 (TOP_PROMPT)",
     "desc": "LLMへ送る最初の指示。キャラクターの中心人格。"},
    {"id": "tool", "file": "TOOL_INSTRUCTIONS.md", "label": "ツール説明 (TOOL_INSTRUCTIONS)",
     "desc": "各ツールの使い方の説明。"},
    {"id": "safety", "file": "SAFETY_PROMPT.md", "label": "安全ルール (SAFETY_PROMPT)",
     "desc": "安全に関する指示。編集は自己責任で。"},
    {"id": "salia", "file": "SALIA.md", "label": "サリア (SALIA)",
     "desc": "情動評価サブエージェント(サリア)のシステムプロンプト。{{agent_name}} / {{user_honorific}} は実行時に置換される。"},
    {"id": "record_check", "file": "RECORD_CHECK.md", "label": "記録判定 (RECORD_CHECK)",
     "desc": "ツールを使ったターンの後に注入する内省プロンプト（雑記帳・手紙・好悪の問いかけ）。記録判定フックが有効なときのみ使用。{{user_honorific}} は実行時に置換される。"},
    {"id": "bottom", "file": "BOTTOM_PROMPT.md", "label": "最下部 (BOTTOM_PROMPT)",
     "desc": "会話履歴の後(真・最下部)に置く指示。一般設定でポストプロンプトを有効にしたときのみ使用。空でも可。"},
]

_SLOT_BY_ID = {s["id"]: s for s in SYSTEM_PROMPT_SLOTS}


class SystemPromptSaveReq(BaseModel):
    """システムプロンプト保存リクエスト。id は固定スロットのいずれか。"""
    id: str
    content: str


@router.get("/api/settings/system-prompts")
async def get_system_prompts():
    """固定スロットの一覧と各ファイルの現在の本文を返す。"""
    directory = system_prompt_dir()
    slots = []
    for s in SYSTEM_PROMPT_SLOTS:
        path = directory / s["file"]
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            content = ""
        slots.append({**s, "content": content})
    return {"slots": slots}


@router.post("/api/settings/system-prompts")
async def update_system_prompt(req: SystemPromptSaveReq):
    """指定スロットの本文を保存し、稼働中プロセスへ即時反映する。

    id は固定スロットのみ許可（不明なidは400）。クライアントからの任意パスは受け取らない。
    保存前に直前版を <file>.bak へ1世代退避し、空文字の保存も許可する。
    """
    slot = _SLOT_BY_ID.get(req.id)
    if slot is None:
        raise HTTPException(status_code=400, detail=t("prompt_unknown_slot"))

    directory = system_prompt_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / slot["file"]

    # 直前版を1世代だけバックアップする（存在する場合のみ・失敗しても保存は続行）。
    # 改行コードを変えずにそのまま退避したいのでバイト単位でコピーする。
    try:
        if path.exists():
            backup = directory / (slot["file"] + ".bak")
            backup.write_bytes(path.read_bytes())
    except Exception as e:
        print(f"警告: プロンプトのバックアップに失敗しました: {e}")

    # 改行は LF に正規化し、OS による CRLF 変換を避けて書き込む。
    # （write_text は Windows で \n→\r\n 変換するため、毎回の保存で改行コードが
    #   揺れて差分が出る。ブラウザが送る \r\n もここで LF に吸収して安定させる。）
    content = req.content.replace("\r\n", "\n").replace("\r", "\n")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)

    # --- 稼働中プロセスへの即時反映（再起動不要） ---
    agent = app_state.global_agent
    if agent is not None:
        if req.id == "salia":
            # サリアは自前でプロンプトをキャッシュしているため専用リロード。
            try:
                if getattr(agent, "salia", None) is not None:
                    agent.salia.reload_system_prompt()
            except Exception as e:
                print(f"警告: サリアのプロンプト即時反映に失敗しました: {e}")
        else:
            # TOP/TOOL/SAFETY/BOTTOM は ContextBuilder がキャッシュしているため再構築。
            try:
                if getattr(agent, "context", None) is not None:
                    agent.context.reload_memories()
            except Exception as e:
                print(f"警告: システムプロンプトの即時反映に失敗しました: {e}")

    return {"status": "success"}


# =============================================================================
# Moonbeat設定APIエンドポイント
# =============================================================================

def _deep_merge_dict(base: dict, override: dict) -> dict:
    """base に override を再帰的にマージする（dict同士のみ深くマージ）。

    UIが送ってこなかったキー（例: 将来追加された項目）を消さずに保持するため、
    moonbeat_config.json の保存はファイル全体置換ではなく既存値へのマージで行う。
    """
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge_dict(base[key], value)
        else:
            base[key] = value
    return base


@router.get("/api/settings/moonbeat")
async def get_moonbeat_config():
    """Moonbeat（月動）設定 config/moonbeat_config.json の現在値を返す。"""
    config_path = config_file("moonbeat_config.json")
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # ファイルが無い場合は空dictを返し、UI側のデフォルト値で埋める
        return {}
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=t("config_parse_error").replace("{name}", "moonbeat_config.json").replace("{error}", str(e)))


@router.post("/api/settings/moonbeat")
async def update_moonbeat_config(req: dict):
    """Moonbeat設定を config/moonbeat_config.json に保存する。

    スケジューラはパルス判定時に毎回このファイルを読み直すため、保存内容は
    サーバー再起動なしで次回のMoonbeat判定から反映される（類似度モデルの差し替えを除く）。
    """
    config_path = config_file("moonbeat_config.json")

    # 既存ファイルを読み込み、UIが送ってこなかったキーを保持したままマージする
    existing: dict = {}
    try:
        existing = json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        existing = {}

    merged = _deep_merge_dict(existing, req)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    return {"status": "success"}


# =============================================================================
# config/ 配下の各種設定ファイルのGET/POST
#
# 共通方針:
#   - 項目別フォーム系（self-memo / vital / compression）は _deep_merge_dict で
#     既存値へマージ保存し、UIが送らなかったキー（将来追加項目）を保持する。
#   - 投げっぱなしJSON系（desire / openclaw）は全置換保存。受信ボディがJSONとして
#     妥当であることだけ FastAPI 側で保証される（不正JSONはそもそも到達しない）。
#   - tips は JSON ではなくテキストなので、配列 ⇔ "---" 区切りテキストを変換する。
# =============================================================================

def _read_config_json(name: str) -> dict:
    """config/<name> を読み込んで dict で返す。無ければ {}、壊れていれば500。"""
    config_path = config_file(name)
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=t("config_parse_error").replace("{name}", name).replace("{error}", str(e)))


def _write_config_json(name: str, data: dict) -> None:
    """config/<name> に整形済みJSONで書き込む（親ディレクトリも作成）。"""
    config_path = config_file(name)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def _merge_save_config_json(name: str, req: dict) -> None:
    """既存値へ req を深くマージして config/<name> に保存する（項目別フォーム用）。"""
    existing = _read_config_json(name)
    merged = _deep_merge_dict(existing, req)
    _write_config_json(name, merged)


# --- self_memo（user_memo_config.json）---

@router.get("/api/settings/self-memo")
async def get_self_memo_config():
    """self_memo設定 config/user_memo_config.json の現在値を返す。"""
    return _read_config_json("user_memo_config.json")


@router.post("/api/settings/self-memo")
async def update_self_memo_config(req: dict):
    """self_memo設定を保存する。context が毎ターン読み直すため即時反映される。"""
    _merge_save_config_json("user_memo_config.json", req)
    return {"status": "success"}


# --- 気分（moontide_v2_config.json）---

def _load_mood_emotions() -> list:
    """data/moontide_inner.jsonl から60感情を（出現順・重複排除で）読み込む。

    各要素は {"id": <state>, "name": <日本語名>}。設定UIのトグルラベル
    「english（日本語）」生成に使う。日本語名は jsonl の name フィールドを参照する。
    """
    emotions = []
    seen = set()
    path = data_file("moontide_inner.jsonl")
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                state = entry.get("state")
                if not state or state in seen:
                    continue
                seen.add(state)
                emotions.append({"id": state, "name": entry.get("name", state)})
    except FileNotFoundError:
        pass
    return emotions


@router.get("/api/settings/mood")
async def get_mood_config():
    """気分設定 config/moontide_v2_config.json の現在値＋60感情マスタを返す。

    返却: {"enabled": bool, "excluded_landmarks": [...], "emotions": [{"id","name"}...]}
    excluded_landmarks に含まれる感情が「無効化」されているもの。
    """
    cfg = _read_config_json("moontide_v2_config.json")
    return {
        "enabled": cfg.get("enabled", True),
        "excluded_landmarks": cfg.get("excluded_landmarks", []),
        "emotions": _load_mood_emotions(),
    }


@router.post("/api/settings/mood")
async def update_mood_config(req: dict):
    """気分設定（enabled / excluded_landmarks）を保存する。

    他のパラメータ（alpha, speed 等）は維持したいので merge 保存。
    enabled・excluded_landmarks は MoonTide 初期化時に読まれるため、反映には
    サーバー再起動が必要（UI側で要再起動バッジを表示）。
    """
    payload = {}
    if "enabled" in req:
        payload["enabled"] = bool(req["enabled"])
    if "excluded_landmarks" in req:
        excluded = req["excluded_landmarks"]
        if not isinstance(excluded, list):
            raise HTTPException(status_code=400, detail=t("field_must_be_array").replace("{field}", "excluded_landmarks"))
        # 既知の感情IDのみ受け付ける（不正値の混入を防ぐ）
        valid_ids = {e["id"] for e in _load_mood_emotions()}
        payload["excluded_landmarks"] = [str(x) for x in excluded if str(x) in valid_ids]
    _merge_save_config_json("moontide_v2_config.json", payload)
    return {"status": "success"}


# --- バイタル（vital_config.json）---

@router.get("/api/settings/vital")
async def get_vital_config():
    """バイタル設定 config/vital_config.json の現在値を返す。"""
    return _read_config_json("vital_config.json")


@router.post("/api/settings/vital")
async def update_vital_config(req: dict):
    """バイタル設定を保存し、VitalManager に再ロードさせて即時反映する。"""
    _merge_save_config_json("vital_config.json", req)
    # 稼働中の VitalManager に新configを反映（再起動不要）
    try:
        if app_state.global_agent is not None and getattr(app_state.global_agent, "vital_manager", None) is not None:
            app_state.global_agent.vital_manager.reload_config()
    except Exception as e:
        print(f"警告: vital_config の稼働中反映に失敗しました: {e}")
    return {"status": "success"}


# --- 記憶圧縮（compression_config.json）---

@router.get("/api/settings/compression")
async def get_compression_config():
    """記憶圧縮設定 config/compression_config.json の現在値を返す。"""
    return _read_config_json("compression_config.json")


@router.post("/api/settings/compression")
async def update_compression_config(req: dict):
    """記憶圧縮設定を保存する。各圧縮処理が実行時に読み直すため即時反映される。"""
    _merge_save_config_json("compression_config.json", req)
    return {"status": "success"}


# --- Tips（tips.txt / "---" 区切りテキスト）---

@router.get("/api/settings/tips")
async def get_tips_config():
    """tips.txt を "---" 区切りでパースし、{"enabled": bool, "tips": [...]} で返す。

    オンオフは平文 tips.txt とは別の config/tips_config.json で管理する。
    """
    config_path = config_file("tips.txt")
    try:
        raw = config_path.read_text(encoding="utf-8")
        blocks = [b.strip() for b in raw.split("---")]
        tips = [b for b in blocks if b]
    except FileNotFoundError:
        tips = []
    enabled = _read_config_json("tips_config.json").get("enabled", True)
    return {"enabled": enabled, "tips": tips}


@router.post("/api/settings/tips")
async def update_tips_config(req: dict):
    """{"enabled": bool, "tips": [...]} を受け取り、tips本文と有効フラグを保存する。

    tips本文は "---" 区切りテキストとして tips.txt に、enabled は tips_config.json に
    保存する。scheduler が発火のたびに両方を読み直すため即時反映される。
    """
    tips = req.get("tips", [])
    if not isinstance(tips, list):
        raise HTTPException(status_code=400, detail=t("field_must_be_array").replace("{field}", "tips"))
    cleaned = [str(item).strip() for item in tips if str(item).strip()]
    text = "\n---\n".join(cleaned)
    if text:
        text += "\n"
    config_path = config_file("tips.txt")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text, encoding="utf-8")
    # オンオフフラグを保存（指定があるときのみ）
    if "enabled" in req:
        _merge_save_config_json("tips_config.json", {"enabled": bool(req["enabled"])})
    return {"status": "success"}


# --- 欲求（desire_config.json / 投げっぱなしJSON・全置換）---

@router.get("/api/settings/desire")
async def get_desire_config():
    """欲求設定 config/desire_config.json の現在値を返す。"""
    return _read_config_json("desire_config.json")


@router.post("/api/settings/desire")
async def update_desire_config(req: dict):
    """欲求設定を全置換で保存し、DesireManager に再ロードさせて即時反映する。"""
    _write_config_json("desire_config.json", req)
    try:
        if (app_state.global_agent is not None
                and getattr(app_state.global_agent, "vital_manager", None) is not None
                and getattr(app_state.global_agent.vital_manager, "desire_manager", None) is not None):
            app_state.global_agent.vital_manager.desire_manager.reload_config()
    except Exception as e:
        print(f"警告: desire_config の稼働中反映に失敗しました: {e}")
    return {"status": "success"}


# --- OpenClaw（openclaw_config.json / 投げっぱなしJSON・全置換・要再起動）---

@router.get("/api/settings/openclaw")
async def get_openclaw_config():
    """OpenClaw設定 config/openclaw_config.json の現在値を返す。"""
    return _read_config_json("openclaw_config.json")


@router.post("/api/settings/openclaw")
async def update_openclaw_config(req: dict):
    """OpenClaw設定を全置換で保存する。

    接続は起動時に確立されるため、反映にはサーバー再起動が必要（UI側で明示警告）。
    """
    _write_config_json("openclaw_config.json", req)
    return {"status": "success"}
