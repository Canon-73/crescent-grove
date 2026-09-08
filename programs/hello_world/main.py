"""
hello_world - 動作確認用サンプルサテライト

名前を受け取って挨拶メッセージを返す。
run_program ツールの動作確認に使用。
i18n フェーズ4-A 基盤の使い方サンプルでもある（programs/_i18n.t() の参照例）。
"""
import json
import sys

from _i18n import t


def main():
    # 標準入力からJSON引数を受け取る
    args = json.loads(sys.stdin.read())

    name = args.get("name", "World")

    # デバッグ出力は stderr を使う（LLM には届かない・人間用）
    print(f"[hello_world] 受信引数: {args}", file=sys.stderr)

    # 標準出力にJSON結果を返す。message は LLM が見るので i18n 対象
    result = {
        "status": "success",
        "message": t("hello_world_message", name=name),
        "data": {
            "greeted": name
        }
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
