# reset_password.py
"""
パスワード再設定ツール（パスワードを忘れたとき用）。

役割:
    ログインパスワードを忘れた場合に、ローカルで新しいパスワードを設定し直す。
    Web画面ではなく「このPCで直接実行する」ことを必須にすることで、
    PCの持ち主（＝正当な利用者）だけがリセットできるようにしている。
    リモートからはこのスクリプトを実行できないため、認証の安全性を損なわない。

使い方:
    1) サーバー（Crescent Grove）を停止する
    2) このスクリプトを実行する
         venv\\Scripts\\python.exe reset_password.py
       （または reset_password.bat をダブルクリック）
    3) 画面の指示に従って新しいパスワードを2回入力する
    4) サーバーを起動し直して、新しいパスワードでログインする

補足:
    パスワードのハッシュは .env の CG_AUTH_PASSWORD_HASH に保存される。
    このスクリプトはそれを新しい値で上書きするだけで、会話・記憶などには一切触れない。
"""

import sys
import getpass


def _early_data_root_from_argv():
    """server.py と同じく --data-root の値だけを軽量に取り出す（配布版対応）。"""
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--data-root":
            if i + 1 < len(argv):
                return argv[i + 1]
        elif a.startswith("--data-root="):
            return a.split("=", 1)[1]
    return None


def main() -> int:
    # .env の場所を確定させる（dev では従来パス、--data-root 指定時はそちら）。
    from core.paths import set_data_root, env_path
    set_data_root(_early_data_root_from_argv())

    # 現在の .env を読み込む（既存設定の有無を表示するため）。
    from core.env_manager import EnvManager
    EnvManager.load_env()

    from core.auth import is_password_set, hash_password, save_password_hash

    print("=" * 50)
    print(" Crescent Grove パスワード再設定ツール")
    print("=" * 50)
    print(f" 設定ファイル: {env_path()}")
    if is_password_set():
        print(" 現在の状態: パスワードは設定済みです（これを上書きします）")
    else:
        print(" 現在の状態: パスワードは未設定です（新規に設定します）")
    print("-" * 50)

    # bcrypt が無いと設定できないので先に確認する。
    try:
        import bcrypt  # noqa: F401
    except ImportError:
        print("エラー: bcrypt がインストールされていません。")
        print("  venv を有効にした上で、pip install bcrypt を実行してください。")
        return 1

    # 新しいパスワードを2回入力させて一致を確認する。
    try:
        pw1 = getpass.getpass("新しいパスワード（8文字以上・入力は表示されません）: ")
        pw2 = getpass.getpass("確認のためもう一度入力してください: ")
    except (KeyboardInterrupt, EOFError):
        print("\n中止しました。パスワードは変更されていません。")
        return 1

    if pw1 != pw2:
        print("エラー: 2回の入力が一致しませんでした。やり直してください。")
        return 1
    if len(pw1) < 8:
        print("エラー: パスワードは8文字以上にしてください。")
        return 1
    if len(pw1.encode("utf-8")) > 1024:
        print("エラー: パスワードが長すぎます。")
        return 1

    # ハッシュ化して .env に保存する。
    hashed = hash_password(pw1)
    save_password_hash(hashed)

    print("-" * 50)
    print("✓ 新しいパスワードを設定しました。")
    print("  サーバーが起動中の場合は、一度停止してから起動し直してください。")
    print("  起動後、新しいパスワードでログインできます。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
