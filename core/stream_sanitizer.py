# core/stream_sanitizer.py
"""
ストリーミング応答の隠しタグ除去フィルタ

役割:
    LLMのストリーミング delta には、ユーザーに見せてはいけない特殊タグが
    含まれうる（<internal>内面思考</internal>、[VITAL_REPORT:...]、
    ツール呼び出しXML）。本モジュールはそれらをストリーム途中で
    「ほぼ完璧に」除去する。

設計:
    - feed(fragment) に delta 断片を渡すと、外に出して安全なテキストだけ返す。
      タグの開始かもしれない接尾辞（例: "<inter" で終わる断片）は内部に保留し、
      次の feed で判定が付いてから出力する（ホールドバック方式）。
    - flush() でストリーム終端処理。保留分を最終判定して返す。
      抑制タグの途中で終わった場合は「隠す側に倒して」破棄する。
    - 抑制対象は core/agent.py の最終クリーン処理
      （strip_tool_xml / _VITAL_REPORT_RE / _INTERNAL_RE）と同一仕様。
      最終的な response でクライアントの吹き出しは正規テキストに置換されるため、
      ここでの判定誤差は恒久的には残らない（二段構え）。

使い方:
    sanitizer = StreamSanitizer()   # LLM呼び出し1回につき1インスタンス
    async for delta in stream:
        safe = sanitizer.feed(delta)
        if safe:
            await send_to_ui(safe)
    tail = sanitizer.flush()
    if tail:
        await send_to_ui(tail)
"""

import re

# DSML形式のツール呼び出しXML（agent.py strip_tool_xml と同じパターン）
# 開きタグは「タグ全体」との完全一致で判定する
_DSML_OPEN_RE = re.compile(r'<\s*\|?\s*DSML\s*\|?\s*function_calls?\s*>')
_DSML_CLOSE_RE = re.compile(r'<\s*/\s*\|?\s*DSML\s*\|?\s*function_calls?\s*>')

# 閉じタグがチャンク境界をまたぐ場合に備えて保持する尻尾の長さ。
# DSML閉じタグは空白を含んでも実用上これより短い。
_TAIL_KEEP = 64


class StreamSanitizer:
    """ストリーム断片から隠しタグを除去するステートマシン"""

    # '<' から '>' が現れないままこの長さを超えたら「タグではない」と判定して解放する
    TAG_MAX = 64

    # バイタル自己申告タグの開始文字列（閉じは ']'）
    VITAL_OPEN = "[VITAL_REPORT:"

    # 固定文字列の抑制タグ → 対応する閉じタグ
    _OPENERS = {
        "<internal>": "</internal>",
        "<function_call>": "</function_call>",
    }

    def __init__(self):
        self._buf = ""           # 判定保留中のテキスト
        self._suppress = False   # 抑制タグの中身を捨てている最中か
        self._closer = None      # 抑制終了の目印（文字列、または "DSML"）

    def feed(self, fragment: str) -> str:
        """delta断片を受け取り、出力して安全なテキストのみ返す（空文字あり）"""
        self._buf += fragment
        out = []
        while True:
            if self._suppress:
                if not self._consume_suppressed():
                    break
                continue

            # --- 通常状態: 次のタグ候補（'<' または '['）まで出力 ---
            i_lt = self._buf.find('<')
            i_br = self._buf.find('[')
            candidates = [i for i in (i_lt, i_br) if i >= 0]
            if not candidates:
                out.append(self._buf)
                self._buf = ""
                break
            i = min(candidates)
            out.append(self._buf[:i])
            self._buf = self._buf[i:]

            if self._buf[0] == '<':
                gt = self._buf.find('>')
                if gt >= 0:
                    tag = self._buf[:gt + 1]
                    closer = self._match_opener(tag)
                    if closer:
                        self._suppress = True
                        self._closer = closer
                        self._buf = self._buf[gt + 1:]
                        continue
                    # 抑制対象ではない '<'。先頭1文字だけ出して再走査する
                    # （"<a><internal>" のような連接も正しく処理するため）
                    out.append(self._buf[0])
                    self._buf = self._buf[1:]
                    continue
                if len(self._buf) > self.TAG_MAX:
                    # '>' が来ないまま長すぎる → タグではない普通の '<' と判定
                    out.append(self._buf[0])
                    self._buf = self._buf[1:]
                    continue
                break  # タグかもしれないので保留して次の feed を待つ

            else:  # '['
                if self._buf.startswith(self.VITAL_OPEN):
                    self._suppress = True
                    self._closer = "]"
                    self._buf = self._buf[len(self.VITAL_OPEN):]
                    continue
                if self.VITAL_OPEN.startswith(self._buf):
                    break  # まだ VITAL_OPEN の途中かもしれないので保留
                out.append(self._buf[0])
                self._buf = self._buf[1:]
                continue

        return "".join(out)

    def flush(self) -> str:
        """ストリーム終端処理。保留バッファの最終判定結果を返す"""
        if self._suppress:
            # 閉じタグが来ないまま終了 → リーク防止優先で破棄
            # （本文だった場合は最終 response の置換で復元される）
            self._buf = ""
            return ""
        tail = self._buf
        self._buf = ""
        return tail  # タグ未成立＝本文だったので解放

    # --- 内部処理 ---

    def _match_opener(self, tag: str):
        """タグ文字列が抑制対象の開きタグなら、対応する閉じ目印を返す"""
        closer = self._OPENERS.get(tag)
        if closer:
            return closer
        if _DSML_OPEN_RE.fullmatch(tag):
            return "DSML"
        return None

    def _consume_suppressed(self) -> bool:
        """抑制中のバッファを消費する。閉じタグが見つかったら True（通常状態へ復帰）"""
        if self._closer == "DSML":
            m = _DSML_CLOSE_RE.search(self._buf)
            if m:
                self._buf = self._buf[m.end():]
                self._suppress = False
                self._closer = None
                return True
            # 閉じタグがチャンク境界をまたぐ場合に備えて尻尾だけ残し、中身は捨てる
            self._buf = self._buf[-_TAIL_KEEP:]
            return False

        idx = self._buf.find(self._closer)
        if idx >= 0:
            self._buf = self._buf[idx + len(self._closer):]
            self._suppress = False
            self._closer = None
            return True
        keep = len(self._closer) - 1
        self._buf = self._buf[-keep:] if keep > 0 else ""
        return False
