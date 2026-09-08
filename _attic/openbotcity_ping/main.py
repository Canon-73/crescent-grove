"""
openbotcity_ping - OpenBotCityオンライン維持プログラム

JWTを環境変数から取得してPOST /pingを叩くだけ。
スケジューラで45秒おきに実行することでオンライン状態を維持する。
"""
import json
import os
import sys
import urllib.request
import urllib.error


def main():
    # 標準入力からJSON引数を受け取る（引数なしなので空）
    sys.stdin.read()

    jwt = os.environ.get("CG_OPENBOTCITY_TOKEN", "")
    if not jwt:
        result = {
            "status": "error",
            "message": "CG_OPENBOTCITY_TOKENが設定されていません。"
        }
        print(json.dumps(result, ensure_ascii=False))
        return

    url = "https://api.openbotcity.com/ping"
    req = urllib.request.Request(
        url,
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt}",
            "Content-Length": "0"
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=8) as res:
            body = res.read().decode("utf-8")
            result = {
                "status": "success",
                "message": "pingを送信しました。",
                "response": body
            }
    except urllib.error.HTTPError as e:
        result = {
            "status": "error",
            "message": f"HTTPエラー: {e.code}",
            "response": e.read().decode("utf-8")
        }
    except Exception as e:
        result = {
            "status": "error",
            "message": str(e)
        }

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
