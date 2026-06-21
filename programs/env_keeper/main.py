"""
env_keeper - 環境変数管理サテライト
"""
import json
import sys
import os

from _i18n import t


def main():
    args = json.loads(sys.stdin.read())

    action = args.get("action")
    key = args.get("key")
    value = args.get("value")

    # パスの解決
    workspace = os.environ.get("CG_WORKSPACE", ".")
    base_dir = os.path.abspath(os.path.join(workspace, ".."))
    sys.path.insert(0, base_dir)
    from core.env_manager import EnvManager

    # 引数なし: 一覧と使い方
    if not action:
        keys = EnvManager.get_all_keys()
        key_names = [k["name"] for k in keys] if keys else []
        if key_names:
            key_list = "\n".join(f"  - {k}" for k in key_names)
        else:
            key_list = t("env_keeper_no_keys_yet")
        # usage 文には JSON 例の { } が含まれるので lb / rb で素通しさせる
        msg = t("env_keeper_usage", key_list=key_list, lb="{", rb="}")
        print(json.dumps({"status": "ok", "message": msg}, ensure_ascii=False))
        return

    if action == "set":
        if not key or not value:
            print(json.dumps({"status": "error", "message": t("env_keeper_err_key_value_required")}, ensure_ascii=False))
            return
        if not key.startswith("CG_"):
            print(json.dumps({"status": "error", "message": t("env_keeper_err_prefix")}, ensure_ascii=False))
            return
        success = EnvManager.set_key(key, value)
        if success:
            EnvManager.load_env()  # 即時反映
            print(json.dumps({"status": "success", "message": t("env_keeper_set_ok", key=key)}, ensure_ascii=False))
        else:
            print(json.dumps({"status": "error", "message": t("env_keeper_set_fail", key=key)}, ensure_ascii=False))
        return

    print(json.dumps({"status": "error", "message": t("env_keeper_err_unknown_action", action=action)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
