# core/filter.py
"""
コンテンツフィルターモジュール

役割:
    外部から取得したコンテンツ（Web検索結果やURL取得内容）に対して、
    特定のNGワードや共起ワードを含む文を自動的に削除する。
"""

import os
import re
from pathlib import Path

# プロジェクトルート
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_FILTER_DIR = _PROJECT_ROOT / "filters"

class ContentFilter:
    def __init__(self):
        self.solo_patterns = []
        self.combo_filters = []  # list of {"a": [], "b": []}
        self.load_filters()

    def load_filters(self):
        """filters/ ディレクトリからフィルター定義を読み込む"""
        if not _FILTER_DIR.exists():
            return

        # Solo Blacklist
        solo_path = _FILTER_DIR / "solo_blacklist.txt"
        if solo_path.exists():
            with open(solo_path, "r", encoding="utf-8") as f:
                raw_keywords = [line.strip().lower() for line in f if line.strip()]
            # 単語境界付き正規表現にコンパイル
            self.solo_patterns = [re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE) for kw in raw_keywords]

        # Combo Filters
        for combo_file in _FILTER_DIR.glob("combo_*.txt"):
            combo_data = {"a": [], "b": []}
            current_section = None
            try:
                with open(combo_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("#"):
                            if "keywords_a" in line.lower():
                                current_section = "a"
                            elif "keywords_b" in line.lower():
                                current_section = "b"
                            continue
                        
                        if current_section:
                            combo_data[current_section].append(
                                re.compile(r'\b' + re.escape(line.lower()) + r'\b', re.IGNORECASE)
                            )
                
                if combo_data["a"] and combo_data["b"]:
                    self.combo_filters.append(combo_data)
            except Exception as e:
                print(f"[ContentFilter] Failed to load {combo_file.name}: {e}")

    def apply(self, text: str) -> str:
        """
        テキストにフィルターを適用する。
        文単位で分割し、NGワードが含まれる文を削除して再結合する。
        """
        if not text:
            return text

        # 分割パターン: 。 | . (スペース/改行) | ！ | ! | ？ | ? | \n
        # 肯定後向き言及などは使わず、re.split でセパレータも保持する
        # パターン: ([。！!？?]|(?:\.\s)|(?:\n))
        pattern = r'([。！!？?\n]|\.\s+)'
        parts = re.split(pattern, text)
        
        # parts は [文1, セパレータ1, 文2, セパレータ2, ...] となる
        # 文とセパレータをペアにする
        sentences = []
        for i in range(0, len(parts) - 1, 2):
            sentences.append({"content": parts[i], "sep": parts[i+1]})
        
        # 最後に余った部分（セパレータなしの文）
        if len(parts) % 2 != 0:
            last_content = parts[-1]
            if last_content:
                sentences.append({"content": last_content, "sep": ""})

        # 区切り文字が一切見つからない場合（sentencesが空、または1つでsepが空）
        # 文分割の結果が元テキストと同一かチェック
        is_single_sentence = (len(sentences) == 0) or (len(sentences) == 1 and not sentences[0]["sep"])
        
        if is_single_sentence and not sentences:
            # 念のため
            sentences = [{"content": text, "sep": ""}]

        result_sentences = []
        removed_count = 0

        for s in sentences:
            content_lower = s["content"].lower()
            if not content_lower:
                result_sentences.append(s)
                continue

            # レベル1判定: Solo Blacklist
            is_removed = False
            for pat in self.solo_patterns:
                if pat.search(content_lower):
                    is_removed = True
                    break
            
            if is_removed:
                removed_count += 1
                continue

            # レベル2判定: Combo
            for combo in self.combo_filters:
                hit_a = any(pat.search(content_lower) for pat in combo["a"])
                hit_b = any(pat.search(content_lower) for pat in combo["b"])
                if hit_a and hit_b:
                    is_removed = True
                    break
            
            if is_removed:
                removed_count += 1
                continue

            result_sentences.append(s)

        if removed_count > 0:
            print(f"[ContentFilter] {removed_count} sentences removed")

        # 再結合
        return "".join(s["content"] + s["sep"] for s in result_sentences)

# シングルトン的に再利用
_global_filter = None

def get_filter():
    global _global_filter
    if _global_filter is None:
        _global_filter = ContentFilter()
    return _global_filter
