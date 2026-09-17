import os
import re
import sys
import yaml
from pathlib import Path

# scripts/ から見て1段上がリポジトリルート。core.* を import するため sys.path に追加。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.rag import RAGDB

from core.config_loader import load_config

def import_memories(rag_db, workspace_path):
    print("--- 日次メモリのインポート ---")
    memory_dir = Path(workspace_path) / "memory"
    if not memory_dir.exists():
        print(f"ディレクトリが見つかりません: {memory_dir}")
        return

    count = 0
    # YYYY-MM-DD.md などを対象にする
    for filepath in memory_dir.glob("*.md"):
        filename = filepath.name
        # 日付形式のファイル名かチェック
        match = re.search(r'(\d{4}-\d{2}-\d{2})', filename)
        date_str = match.group(1) if match else "不明"
        
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
            
        print(f"インポート中: {filename} (日付: {date_str})")
        rag_db.add("daily_memories", content, {"date": date_str, "source": filename})
        count += 1
        
    print(f"合計 {count} 件の日次メモリをインポートしました。")
        
def import_logs(rag_db, chat_log_dir):
    print("\n--- 会話ログのインポート ---")
    log_dir_path = Path(chat_log_dir)
    if not log_dir_path.exists():
        print(f"ディレクトリが見つかりません: {log_dir_path}")
        return

    count = 0
    for filepath in log_dir_path.glob("*_chat.md"):
        filename = filepath.name
        match = re.search(r'(\d{4}-\d{2}-\d{2})', filename)
        date_str = match.group(1) if match else "不明"
        
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
            
        print(f"インポート中: {filename} (日付: {date_str})")
        
        # 過去ログはLLMによる感情抽出をスキップ（コスト・時間節約のため）
        metadata = {
            "date": date_str,
            "emotion": "imported",
            "topics": "imported"
        }
        rag_db.add("logs", content, metadata)
        count += 1
        
    print(f"合計 {count} 件の会話ログファイルをインポートしました。")

def import_notes(rag_db, workspace_path):
    print("\n--- 雑記帳のインポート ---")
    notes_dir = Path(workspace_path) / "notes"
    if not notes_dir.exists():
        print(f"ディレクトリが見つかりません: {notes_dir}")
        return

    # やり直しのための既存データ削除
    try:
        count_before = rag_db.collections["notes"].count()
        if count_before > 0:
            print(f"既存の 'notes' コレクションをクリアしています... ({count_before}件)")
            # コレクションを削除して再作成
            rag_db.client.delete_collection("notes")
            rag_db.collections["notes"] = rag_db.client.create_collection("notes")
    except Exception as e:
        print(f"コレクションのクリアに失敗しました（無視して続行します）: {e}")

    count = 0
    for filepath in notes_dir.glob("*.md"):
        filename = filepath.name
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
            
        print(f"インポート中: {filename}")
        # 雑記帳はメタデータとしてファイル名のみ保持
        rag_db.add("notes", content, {"source": filename})
        count += 1
        
    print(f"合計 {count} 件の雑記帳をインポートしました。")

def main():
    print("RAGデータ一括インポートスクリプトを開始します...")
    try:
        config = load_config()
    except FileNotFoundError:
        print("エラー: config.yaml が見つかりません。")
        return

    rag_config = config.get("rag")
    if not rag_config or not rag_config.get("db_directory"):
        print("エラー: config.yaml に rag.db_directory が設定されていません。")
        return
        
    db_dir = rag_config["db_directory"]
    embedding_model = rag_config.get("embedding_model", "default")
    
    print(f"RAGDBを初期化しています... (ディレクトリ: {db_dir})")
    rag_db = RAGDB(db_dir, embedding_model)
    
    workspace_path = config.get("workspace", {}).get("path", "")
    
    # ログディレクトリの取得
    chat_log_dir = config.get("logs", {}).get("chat_log_directory", "")
    
    # workspace.path がある場合、相対パスなどの補完
    if not chat_log_dir and workspace_path:
        chat_log_dir = str(Path(workspace_path) / "logs" / "chat")

    # インポートの実行
    if workspace_path:
        import_memories(rag_db, workspace_path)
    else:
        print("workspace.path が設定されていないため、日次メモリのインポートをスキップします。")
        
    if chat_log_dir:
        import_logs(rag_db, chat_log_dir)
    else:
        print("logs.chat_log_directory が設定されていないため、会話ログのインポートをスキップします。")

    # 雑記帳のインポート
    if workspace_path:
        import_notes(rag_db, workspace_path)

    print("\nすべてのインポート処理が完了しました！")

if __name__ == "__main__":
    main()
