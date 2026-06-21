#!/usr/bin/env python3
"""
MCP Client — Crescent Grove 統合版
外部MCPサーバーへの接続・ツール一覧取得・ツール呼び出しを行う汎用クライアント。

プロトコル: MCP over Streamable HTTP (JSON-RPC 2.0)
依存: 標準ライブラリのみ (urllib.request)
"""
import sys
import json
import os
import urllib.request
import urllib.error

from _i18n import t

# --- 定数 ---
DATA_DIR = os.path.dirname(os.path.abspath(__file__))

# 状態ファイルは workspace/program_data/mcp_client/ に保存する。
# 旧バージョンはサテライト同梱フォルダ(DATA_DIR)に保存していたため、
# 読み込み時のみ旧パスにフォールバックして記録を引き継ぐ。
_WS = os.environ.get("CG_WORKSPACE", DATA_DIR)
_STATE_DIR = os.path.join(_WS, "program_data", "mcp_client")
REGISTRY_FILE = os.path.join(_STATE_DIR, "registry.json")
_REGISTRY_FILE_OLD = os.path.join(DATA_DIR, "registry.json")
PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "crescent-grove", "version": "1.0.0"}


# ═══════════════════════════════════════════════════════════
# レジストリ管理
# ═══════════════════════════════════════════════════════════

def load_registry() -> dict:
    # 新パス優先、無ければ旧パスにフォールバック
    path = REGISTRY_FILE if os.path.exists(REGISTRY_FILE) else _REGISTRY_FILE_OLD
    if not os.path.exists(path):
        return {"services": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"services": {}}


def save_registry(data: dict):
    os.makedirs(_STATE_DIR, exist_ok=True)
    with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════
# MCP通信
# ═══════════════════════════════════════════════════════════

def mcp_request(url: str, method: str, params: dict = None, token: str = None, request_id: int = 1) -> dict:
    """MCPサーバーにJSON-RPCリクエストを送信する。"""
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
    }
    if params is not None:
        payload["params"] = params

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            content_type = res.headers.get("Content-Type", "")

            # SSEレスポンスの場合: 最後のJSON-RPCレスポンスを抽出
            if "text/event-stream" in content_type:
                return _parse_sse_response(res.read().decode("utf-8"))

            # 通常のJSONレスポンス
            return json.loads(res.read().decode("utf-8"))

    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
        except Exception:
            err_body = e.reason
        return {"error": {"code": e.code, "message": str(err_body)}}
    except Exception as e:
        return {"error": {"code": -1, "message": str(e)}}


def _parse_sse_response(raw: str) -> dict:
    """SSEストリームからJSON-RPCレスポンスを抽出する。"""
    last_data = None
    for line in raw.split("\n"):
        if line.startswith("data: "):
            last_data = line[6:]
    if last_data:
        try:
            return json.loads(last_data)
        except json.JSONDecodeError:
            pass
    return {"error": {"code": -1, "message": t("mcp_client_err_sse_parse")}}


def mcp_initialize(url: str, token: str = None) -> dict:
    """MCPサーバーとの初期化ハンドシェイクを行う。"""
    params = {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": CLIENT_INFO,
    }
    result = mcp_request(url, "initialize", params, token, request_id=1)

    # initializeが成功したら、initialized通知を送る
    if "result" in result:
        _send_notification(url, "initialized", token=token)

    return result


def _send_notification(url: str, method: str, params: dict = None, token: str = None):
    """通知（レスポンス不要）を送信する。"""
    payload = {
        "jsonrpc": "2.0",
        "method": method,
    }
    if params:
        payload["params"] = params

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            pass  # 202 Accepted、ボディ不要
    except Exception:
        pass  # 通知の失敗は無視


def mcp_list_tools(url: str, token: str = None) -> dict:
    """ツール一覧を取得する。"""
    return mcp_request(url, "tools/list", {}, token, request_id=2)


def mcp_call_tool(url: str, tool_name: str, arguments: dict = None, token: str = None) -> dict:
    """ツールを呼び出す。"""
    params = {"name": tool_name}
    if arguments:
        params["arguments"] = arguments
    return mcp_request(url, "tools/call", params, token, request_id=3)


# ═══════════════════════════════════════════════════════════
# セッション付きMCP操作（initialize → 操作 → 結果）
# ═══════════════════════════════════════════════════════════

def full_list_tools(url: str, token: str = None) -> dict:
    """初期化 → ツール一覧取得 を一括で行う。"""
    init = mcp_initialize(url, token)
    if "error" in init:
        return init

    result = mcp_list_tools(url, token)
    if "error" in result:
        return result

    # tools一覧を整形
    tools = result.get("result", {}).get("tools", [])
    formatted = []
    for t in tools:
        entry = {"name": t.get("name", ""), "description": t.get("description", "")}
        schema = t.get("inputSchema", {})
        if schema.get("properties"):
            entry["parameters"] = {
                k: {
                    "type": v.get("type", "any"),
                    "description": v.get("description", ""),
                }
                for k, v in schema["properties"].items()
            }
            entry["required"] = schema.get("required", [])
        formatted.append(entry)

    return {"tools": formatted, "count": len(formatted)}


def full_call_tool(url: str, tool_name: str, arguments: dict = None, token: str = None) -> dict:
    """初期化 → ツール呼び出し を一括で行う。"""
    init = mcp_initialize(url, token)
    if "error" in init:
        return init

    result = mcp_call_tool(url, tool_name, arguments, token)
    if "error" in result:
        return result

    # レスポンスのcontent部を整形
    content_list = result.get("result", {}).get("content", [])
    texts = []
    for c in content_list:
        if c.get("type") == "text":
            texts.append(c.get("text", ""))
        else:
            texts.append(json.dumps(c, ensure_ascii=False))

    return {"result": "\n".join(texts) if texts else t("mcp_client_empty_response")}


# ═══════════════════════════════════════════════════════════
# コマンド実装
# ═══════════════════════════════════════════════════════════

def cmd_register(args: dict) -> dict:
    """MCPサービスを登録する。"""
    name = args.get("name")
    url = args.get("url")
    token_env = args.get("token_env")

    if not name:
        return {"error": t("mcp_client_err_name_required")}
    if not url:
        return {"error": t("mcp_client_err_url_required")}

    registry = load_registry()
    registry["services"][name] = {
        "url": url,
        "token_env": token_env or "",
    }
    save_registry(registry)

    return {
        "message": t("mcp_client_register_ok", name=name),
        "name": name,
        "url": url,
        "token_env": token_env or t("mcp_client_none_label"),
    }


def cmd_unregister(args: dict) -> dict:
    """MCPサービスの登録を削除する。"""
    name = args.get("name")
    if not name:
        return {"error": t("mcp_client_err_name_required_short")}

    registry = load_registry()
    if name not in registry["services"]:
        return {"error": t("mcp_client_err_not_registered", name=name)}

    del registry["services"][name]
    save_registry(registry)
    return {"message": t("mcp_client_unregister_ok", name=name)}


def cmd_list(args: dict) -> dict:
    """登録済みサービス一覧を返す。"""
    registry = load_registry()
    services = registry.get("services", {})
    if not services:
        return {"message": t("mcp_client_no_services"), "services": []}

    result = []
    for name, info in services.items():
        result.append({
            "name": name,
            "url": info.get("url", ""),
            "token_env": info.get("token_env", ""),
        })
    return {"services": result, "count": len(result)}


def cmd_tools(args: dict) -> dict:
    """指定サービスのツール一覧を取得する。"""
    name = args.get("name")
    if not name:
        return {"error": t("mcp_client_err_name_required_registered")}

    registry = load_registry()
    if name not in registry["services"]:
        return {"error": t("mcp_client_err_not_registered_then", name=name)}

    service = registry["services"][name]
    url = service["url"]
    token = _resolve_token(service.get("token_env", ""))

    return full_list_tools(url, token)


def cmd_call(args: dict) -> dict:
    """指定サービスのツールを呼び出す。"""
    name = args.get("name")
    tool = args.get("tool")

    if not name:
        return {"error": t("mcp_client_err_name_required_registered")}
    if not tool:
        return {"error": t("mcp_client_err_tool_required")}

    registry = load_registry()
    if name not in registry["services"]:
        return {"error": t("mcp_client_err_not_registered_then", name=name)}

    service = registry["services"][name]
    url = service["url"]
    token = _resolve_token(service.get("token_env", ""))

    # argsからtool_argsを抽出（name, tool, command以外の全キーをツール引数として渡す）
    tool_args = {}
    reserved = {"command", "name", "tool"}
    for k, v in args.items():
        if k not in reserved:
            tool_args[k] = v

    return full_call_tool(url, tool, tool_args if tool_args else None, token)


def _resolve_token(token_env: str) -> str:
    """環境変数名からトークンを取得する。"""
    if not token_env:
        return ""
    return os.environ.get(token_env, "")


# ═══════════════════════════════════════════════════════════
# ディスパッチャ
# ═══════════════════════════════════════════════════════════

COMMANDS = {
    "register": cmd_register,
    "unregister": cmd_unregister,
    "list": cmd_list,
    "tools": cmd_tools,
    "call": cmd_call,
}

def _help_text():
    return {
        "register": t("mcp_client_help_register"),
        "unregister": t("mcp_client_help_unregister"),
        "list": t("mcp_client_help_list"),
        "tools": t("mcp_client_help_tools"),
        "call": t("mcp_client_help_call"),
    }


def main():
    try:
        raw = sys.stdin.read().strip()
        args = json.loads(raw) if raw else {}
    except Exception as e:
        print(json.dumps({"status": "error", "message": t("mcp_client_err_json_parse", e=e)}, ensure_ascii=False))
        sys.exit(1)

    command = args.get("command")

    # コマンド未指定 → ヘルプ + 登録済みサービス一覧
    if not command:
        registry = load_registry()
        services = list(registry.get("services", {}).keys())
        print(json.dumps({
            "status": "ok",
            "data": {
                "message": t("mcp_client_help_title"),
                "commands": _help_text(),
                "registered_services": services if services else t("mcp_client_none_label"),
                "usage_example": {
                    "register": {"command": "register", "name": "colony", "url": "https://mcp.thecolony.cc/mcp", "token_env": "CG_COLONY_TOKEN"},
                    "tools": {"command": "tools", "name": "colony"},
                    "call": {"command": "call", "name": "colony", "tool": "post", "content": "hello world"},
                },
            }
        }, ensure_ascii=False))
        return

    if command not in COMMANDS:
        print(json.dumps({
            "status": "error",
            "message": t("mcp_client_err_unknown_command", command=command),
            "available": list(COMMANDS.keys()),
        }, ensure_ascii=False))
        return

    try:
        result = COMMANDS[command](args)
        if "error" in result:
            print(json.dumps({"status": "error", "message": result["error"]}, ensure_ascii=False))
        else:
            print(json.dumps({"status": "ok", "data": result}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"status": "error", "message": t("mcp_client_err_internal", e=e)}, ensure_ascii=False), file=sys.stdout)
        sys.exit(1)


if __name__ == "__main__":
    main()
