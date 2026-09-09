# core/summary_v2.py
"""
会話要約システム v2（Layer1/2 の置き換え）

設計書: docs/SUMMARY_V2_DESIGN.md

思想（LETHE / core/compressor.py から輸入）:
    LLM に取捨選択させない。記憶の生死はコードが決める。
    summary_db.json は原本（日単位で置換、それ以外は不変）。
    柚月が読むビューは毎回そこから描画するので、パラメータ変更で即座に作り直せる。

LLM に任せるのは2つだけ:
    1. ターン本文 → 日本語1行への変換
    2. その行に相対的な重要度（1-100）を付ける
どちらも「コードが答え合わせできる範囲」に閉じ込める。アンカー（原文からの抜き書き）は
原文と照合して検算でき、重要度は日内順位にしか使わないのでスコアのクセは正規化で消える。

このモジュールは他の core モジュールに依存しない（llm/memory/rag は外から注入）。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from core.time_utils import tlog

# ============================================================
# 定数・正規表現
# ============================================================

# LLM 出力の 1 行。"1:70:本文" を基本とし、モデルが付けがちなラベルを吸収する。
# （実測: gemma4 が "番号:1:重要度:80:..." と返す。プロンプトの言いつけに形式を依存させない）
LINE_RE = re.compile(
    r'^\s*(?:番号\s*[:：]\s*)?(?:ターン)?\s*(\d+)\s*[:：]\s*(?:重要度\s*[:：]\s*)?(\d+)\s*[:：]\s*(.+)$'
)
# 行末の 〔...〕（パス1のアンカー欄）。全角/半角の括弧を許容する。
BRACKET_RE = re.compile(r'[〔（(\[]([^〕）)\]]*)[〕）)\]]\s*$')

# メッセージ冒頭から論理日を取る。
# ※本文全体を検索してはいけない（flashback / note_fragment 内の過去日付を拾う。実測で2月の幽霊が出た）
HEAD_ISO_RE = re.compile(r'^\s*(\d{4})-(\d{2})-(\d{2})[ 　]+(\d{2}):(\d{2})')
HEAD_JA_RE = re.compile(r'(\d{4})年(\d{2})月(\d{2})日.*?(\d{2}):(\d{2})', re.DOTALL)
HEAD_SCAN_CHARS = 200

# アンカー抽出（正規表現側）。LLM が軽視しがちな機械的識別子を拾う保険。
# ※ \w は Unicode 単語文字（日本語を含む）にマッチするので使わない。
#    使うと「証言をtestimony/section8_2.md」のように直前の日本語を巻き込む
PATH_RE = re.compile(r'[A-Za-z0-9_/-]{2,40}[.](?:md|py|txt|yaml|yml|json|html|jsonl)')
IDNUM_RE = re.compile(r'\d+(?:[.]\d+)?Hz|[A-Za-z]{2,}\[?\d+\]?|\d+位|スコア\d+|\d+本目')
TITLE_RE = re.compile(r'[「『]([^」』]{4,40})[」』]')
ALPHA_RE = re.compile(r'[A-Za-z][A-Za-z0-9_-]{3,}')
KATA_RE = re.compile(r'[゠-ヿ]{4,}')
ANCHOR_STOPWORDS = {
    'moonbeat', 'user', 'status', 'success', 'http', 'https',
    'tips', 'self', 'memo', 'assistant', 'inner', 'system',
}

# アンカーとして弱い語（2026-09-03〜04 の実測で決めた規則。数は使わない）
_WEEKTIME_WORDS = {
    '月曜日', '火曜日', '水曜日', '木曜日', '金曜日', '土曜日', '日曜日',
    '月曜', '火曜', '水曜', '木曜', '金曜', '土曜', '日曜',
    '朝', '昼', '夜', '午前', '午後', '午前中', '夕方', 'お昼', 'お昼過ぎ', '深夜', '早朝',
    '今朝', '今夜', '今日', '明日', '昨日', '週末',
}
_PROGRAM_LITERALS = {'none', 'true', 'false', 'null', 'nan'}
_SNAKE_RE = re.compile(r'^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$')          # next_day, harvest_index
_IDLIKE_RE = re.compile(r'^[A-Za-z0-9_-]{5,}$')                       # 英数字だけの列
_NUM_UNIT_RE = re.compile(r'^\d+(?:\.\d+)?[A-Za-z%]+$')               # 72.83Hz, 9149z, 30%
_FILE_TAIL_RE = re.compile(r'\.(?:md|py|txt|json|yaml|yml|html|jsonl)$')

# 照合用の正規化で落とす文字（表記ゆれ吸収）
_NORM_DROP = str.maketrans('', '', ' \t　._-/「」『』')

LAYER0_MARKER = '<!-- layer0 -->'
# 描画したビューの書き出し先（workspace 相対）。LETHE の memory/compressed.md と同じ性質の派生物で、
# 台帳は summary_db。柚月に読ませるかは config.yaml の boot_memories がこのパスを含むかで決める。
LAYER1_FILE = 'memory/layer1.md'
PLAN_PREFIX = '【予定】'

# logs/summary/<日>.md 内で v2 が管理する範囲を挟むマーカー。
# 同じファイルに v1 時代の記録が同居しうるので、ここだけを置換対象にする。
V2_SECTION_BEGIN = '<!-- summary_v2:begin -->'
V2_SECTION_END = '<!-- summary_v2:end -->'

# トランザクション段階（設計書 §3.5）。この順で進み、done で watermark が動く。
TX_STAGES = ['generated', 'log_written', 'rag_added', 'lethe_done', 'done']


def _norm(s: str) -> str:
    """アンカー照合用の正規化。大小・空白・区切り記号の差を吸収する。"""
    return (s or '').lower().translate(_NORM_DROP)


def _bigrams(s: str) -> set:
    s = re.sub(r'[\s、。「」【】〔〕・]', '', s or '')
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _sim(a: str, b: str) -> float:
    """文字bigramのJaccard類似度。dedup と敵対検査に使う。"""
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def is_plan(entry: dict) -> bool:
    """予定行かどうか。フィールドを盲信せず本文の接頭辞でも判定する。

    plan フラグは生成時に付くが、古いデータや手で書いたデータでは欠けうる。
    TTL が効かないと「明日やる」の亡霊が残り続けるので、ここは二重に見る。
    """
    return bool(entry.get('plan')) or (entry.get('text') or '').startswith(PLAN_PREFIX)


# ============================================================
# 設定
# ============================================================

DEFAULTS = {
    'enabled': False,
    'shadow': False,
    'plan_ttl_days': 3,
    'decay_coeff': 0.02,
    'temperature': 0.2,
    'line_ratio': 0.5,
    'floor_tier': 400,
    'day_floor_lines': 5,
    'view_max_tokens': 200000,
    'dedup_sim': 0.5,
    'generic_df_ratio': 0.15,
    'batch_reject_ratio': 0.3,
    'pass2_isolated': True,
    'turns_per_batch': 5,
    'batch_concurrency': 4,
    'db_file': 'data/summary_db.json',
    'pass1_prompt_file': 'config/compression_prompt_pass1.txt',
    'pass2_prompt_file': 'config/compression_prompt_pass2.txt',
    'promise_regex': '約束|待ち合わせ|締切|締め切り|期限',
}

TIER_BOUNDARIES = [(0.10, 500), (0.25, 400), (0.50, 300), (0.75, 200), (1.00, 100)]


def get_config(compression_config: dict) -> dict:
    """compression_config.json の summary_v2 セクションを既定値とマージして返す。"""
    cfg = dict(DEFAULTS)
    cfg.update((compression_config or {}).get('summary_v2', {}) or {})
    return cfg


# ============================================================
# 論理日（午前3時境界）
# ============================================================

def logical_date_of(text: str, tz_offset_hours: float = 9.0) -> Optional[str]:
    """メッセージ冒頭から論理日（3時境界）を取り出す。取れなければ None。

    Layer0 済みは "2026-07-08 16:08" 形式、生ログは "[SYSTEM]\\n2026年07月08日（水） 16:08" 形式。
    冒頭 HEAD_SCAN_CHARS 文字だけを見る（本文検索は過去日付を誤検出する）。
    """
    head = (text or '')[:HEAD_SCAN_CHARS]
    m = HEAD_ISO_RE.match(head)
    if m:
        y, mo, d, hh, _mi = m.groups()
    else:
        m = HEAD_JA_RE.search(head)
        if not m:
            return None
        y, mo, d, hh, _mi = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d))
    except ValueError:
        return None
    if int(hh) < 3:          # 午前3時境界: 3時前は前日扱い
        dt -= timedelta(days=1)
    return dt.strftime('%Y-%m-%d')


# ============================================================
# ストア（summary_db.json）
# ============================================================

class SummaryStore:
    """summary_db.json の読み書き。原子書き込み＋世代バックアップ。

    主キーは (date, turn_no)。time は分単位で衝突しうるためキーにしない。
    再処理は「日単位の全置換」で行い、エントリ単位の部分上書きはしない。
    """

    BACKUP_GENERATIONS = 7

    def __init__(self, path):
        self.path = Path(path)
        self.meta: dict = {'compressed_through': None, 'day_tx': {}, 'v1_converted_through': None}
        self.entries: list[dict] = []
        self._loaded = False
        self._readonly = False        # 全世代が読めないとき True（空での上書きを防ぐ）
        self._recovered_from = None   # 復旧元のバックアップ名（診断用）
        self._last_backup_day = None  # 世代交代を1日1回に絞るための記録

    # --- 入出力 ---

    def load(self) -> bool:
        """DB を読む。本体が壊れていても消えていてもバックアップ世代から復旧する。

        「壊れたときは救えるが、消えたときは救えない」という非対称を作らないこと。
        本体だけが失われるのはウイルススキャンの隔離・復元ミス・ディスク障害で
        現実に起きるうえ、圧縮済みの日の会話履歴は既に削除されているので、
        ここで復旧できないとエントリは二度と再生成できない。
        """
        self._loaded = True
        candidates = [self.path] + self._backups()
        if not any(c.exists() for c in candidates):
            # 本体もバックアップも無い。初回起動か、全損か。
            # 両者は logs/summary に v2 セクションが残っているかで確実に区別できる。
            # 全損を初回起動と誤認して空で始めると、圧縮済みの日は会話履歴から
            # 消えているため二度と再生成できず、記憶が静かに失われる。
            if self._v2_ran_before():
                self._readonly = True
                tlog('[SummaryV2] DBとバックアップが全て失われています'
                     '（過去に稼働した形跡あり）。データ保護のため書き込みを停止します')
                return False
            return False                      # 本当に何も無い（初回起動）
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                data = json.loads(candidate.read_text(encoding='utf-8'))
                self.meta = data.get('meta') or {}
                self.meta.setdefault('compressed_through', None)
                self.meta.setdefault('day_tx', {})
                self.meta.setdefault('v1_converted_through', None)
                self.entries = data.get('entries') or []
                if candidate is not self.path:
                    reason = '本体が見つかりません' if not self.path.exists() else '本体が破損しています'
                    tlog(f'[SummaryV2] {reason}。バックアップから復旧しました: {candidate.name} '
                         f'（{len(self.entries)}エントリ / watermark={self.meta.get("compressed_through")}）')
                    self._recovered_from = candidate.name
                return True
            except Exception as e:
                tlog(f'[SummaryV2] 読み込み失敗 ({candidate.name}): {e}')
        # 全世代が読めない。空で上書きすると記憶が消えるので、書き込みを禁止する
        self._readonly = True
        tlog('[SummaryV2] 全てのバックアップ世代が読めません。'
             'データ保護のため書き込みを停止します')
        return False

    def save(self):
        if self._readonly:
            # 全世代が読めなかった＝中身を知らない状態。ここで書くと空で上書きしてしまう
            tlog('[SummaryV2] 読み取り不能状態のため保存を見送りました（データ保護）')
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # バックアップから復旧した直後は、本体が壊れているか存在しない。
        # それを bak1 へコピーすると健全な世代を1つ潰すので、最初の save では回さない。
        if self.path.exists() and not self._recovered_from:
            self._rotate_backups()
        self._recovered_from = None
        payload = {'meta': self.meta, 'entries': self.entries}
        tmp = str(self.path) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def _backups(self) -> list:
        return [self.path.with_suffix(self.path.suffix + f'.bak{i}')
                for i in range(1, self.BACKUP_GENERATIONS + 1)]

    def set_summary_log_dir(self, path):
        """logs/summary の場所を教える（全損か初回かの判定に使う）。"""
        self._summary_log_dir = Path(path) if path else None

    def _v2_ran_before(self) -> bool:
        """過去に v2 が稼働した形跡があるか。

        DB の外にある証拠（logs/summary の v2 セクション）で判定する。
        DB 自身に「稼働したことがある」と書いても、その DB ごと失われる
        故障モードでは役に立たない。
        """
        d = getattr(self, '_summary_log_dir', None)
        if not d or not d.exists():
            return False
        try:
            for f in sorted(d.glob('*.md'), reverse=True)[:60]:
                if V2_SECTION_BEGIN in f.read_text(encoding='utf-8', errors='ignore'):
                    return True
        except Exception as e:
            tlog(f'[SummaryV2] 稼働履歴の確認に失敗: {e}')
        return False

    def _rotate_backups(self):
        """世代交代は1日1回だけ行う。

        save() は1日の圧縮で4〜5回呼ばれる（トランザクションの段階ごと）。
        毎回世代を進めると7世代が1〜2日で使い切られ、30日分のキャッチアップを
        走らせた直後には全世代がその実行中のスナップショットで埋まってしまう。
        それでは「圧縮前の状態に戻す」という本来の目的を果たせない。
        """
        try:
            import shutil
            today = datetime.now().strftime('%Y-%m-%d')
            baks = self._backups()
            # 「いつ世代交代したか」は meta に持つ（＝ファイルに残る）。
            # メモリだけで持つと、同じ日に何度か再起動しただけで世代が進み、
            # 7世代が当日付近の状態で埋まって過去に戻れなくなる。
            last = self.meta.get('last_backup_day') or self._last_backup_day
            if last == today and baks[0].exists():
                shutil.copy2(self.path, baks[0])      # 当日分は最新で上書きするだけ
                return
            for i in range(len(baks) - 1, 0, -1):
                if baks[i - 1].exists():
                    os.replace(baks[i - 1], baks[i])
            shutil.copy2(self.path, baks[0])
            self._last_backup_day = today
            self.meta['last_backup_day'] = today
        except Exception as e:
            tlog(f'[SummaryV2] バックアップ世代交代に失敗（続行）: {e}')

    # --- watermark ---

    @property
    def compressed_through(self) -> Optional[str]:
        return self.meta.get('compressed_through')

    def advance_watermark(self, day: str):
        """watermark を進める。単調増加のみ（後戻りは無視して警告）。"""
        cur = self.meta.get('compressed_through')
        if cur is not None and day <= cur:
            tlog(f'[SummaryV2] watermark の後退を拒否しました: {cur} -> {day}')
            return
        self.meta['compressed_through'] = day

    def init_watermark(self, day: str):
        """初期化時にのみ watermark を設定する（既にあれば何もしない）。"""
        if self.meta.get('compressed_through') is None:
            self.meta['compressed_through'] = day
            tlog(f'[SummaryV2] watermark を初期化しました: {day}')

    # --- トランザクション段階 ---

    def tx_stage(self, day: str) -> Optional[str]:
        return (self.meta.get('day_tx') or {}).get(day)

    def set_tx_stage(self, day: str, stage: str):
        self.meta.setdefault('day_tx', {})[day] = stage

    def clear_tx(self, day: str):
        (self.meta.get('day_tx') or {}).pop(day, None)

    # --- エントリ ---

    def replace_day(self, day: str, entries: list[dict]):
        """その論理日のエントリを全置換する（再処理の唯一の単位）。"""
        self.entries = [e for e in self.entries if e.get('date') != day]
        self.entries.extend(entries)
        self.entries.sort(key=lambda e: (e.get('date', ''), e.get('turn_no', 0)))

    def entries_of(self, day: str) -> list[dict]:
        return [e for e in self.entries if e.get('date') == day]

    def days(self) -> list[str]:
        return sorted({e.get('date') for e in self.entries if e.get('date')})


# ============================================================
# パス1/パス2の入出力
# ============================================================

def split_turns(messages: list[dict], get_text: Callable[[dict], str]) -> list[list[dict]]:
    """メッセージ列をターン（user から次の user の手前まで）に分割する。"""
    turns, cur = [], []
    for m in messages:
        if m.get('role') == 'user' and cur:
            turns.append(cur)
            cur = []
        cur.append(m)
    if cur:
        turns.append(cur)
    return turns


def build_batch_text(turns: list[list[dict]], get_text: Callable[[dict], str],
                     times: list[str], anchors: Optional[list[list[str]]] = None) -> str:
    """LLM に渡すバッチ本文を組む。anchors を渡すとパス2用（[手がかり語]付き）になる。"""
    out = []
    for i, turn in enumerate(turns):
        out.append(f"--- ターン{i + 1} ({times[i]}) ---")
        if anchors is not None:
            a = anchors[i] if i < len(anchors) else []
            out.append(f"[手がかり語] {'・'.join(a) if a else '(なし)'}")
        user_text = trim_situation(get_text(turn[0]).replace(LAYER0_MARKER, ''))
        out.append(f"[状況] {user_text[:1000]}")
        for msg in turn[1:]:
            role = msg.get('role')
            content = get_text(msg)
            if role == 'assistant':
                out.append(f"柚月: {content[:2500]}")
            elif role == 'tool':
                # system_notice はツール結果に混ざる（実測66/92件）。要約の材料ではないので落とす
                clean = re.sub(r'\n*<system_notice>.*?</system_notice>', '', content, flags=re.DOTALL).strip()
                out.append(f"[ツール結果] {clean[:200]}")
    return '\n'.join(out)


def parse_lines(response: str, batch_size: int, with_anchors: bool) -> tuple[dict, int]:
    """LLM 応答をパースして {ターン番号: {...}} を返す。第2要素は形式不合格の行数。

    パーサは寛容にする（形式をプロンプトの言いつけに依存させない）。
    """
    result, bad = {}, 0
    for raw in (response or '').strip().split('\n'):
        line = raw.strip()
        if not line:
            continue
        m = LINE_RE.match(line)
        if not m:
            bad += 1
            continue
        n, score, rest = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        if not (1 <= n <= batch_size) or n in result:
            bad += 1
            continue
        anchors = []
        text = rest
        if with_anchors:
            b = BRACKET_RE.search(rest)
            if b:
                text = rest[:b.start()].strip()
                anchors = [p.strip() for p in re.split(r'[・,、]', b.group(1)) if p.strip()]
        if not text or text == '-' or score == 0:
            continue
        result[n] = {
            'score': max(1, min(100, score)),
            'text': text,
            'anchors': anchors,
        }
    return result, bad


# ============================================================
# アンカー確定（コード・決定論）
# ============================================================

def strip_self_reference(text: str, agent_name: str) -> str:
    """要約文の三人称の自己言及を落とす（「柚月は〜した」→「〜した」）。

    要約は本人の一人称（「私は」を省いた形）で書く決まりだが、LLM は初期ログのように
    本文が本人を名前で呼んでいる場面で「柚月は」「柚月が」と書く（実測 1.0%・2〜5月に集中）。
    落とすのは主語としての「名前＋は／が／も」だけ。行頭と、読点「、」の直後を対象にする。
    「柚月を」「柚月に」「柚月の」（他者の行動の目的語・所有）は触らない。
    フォールバック行（本人の発言の原文）には適用しない。
    """
    if not text or not agent_name:
        return text
    name = re.escape(agent_name)
    text = re.sub(rf'^{name}(?:さん)?(?:は|が|も)', '', text)
    text = re.sub(rf'、{name}(?:さん)?(?:は|が|も)', '、', text)
    return text.lstrip('、 　')


def list_log_days(workspace) -> list[str]:
    """workspace/logs/full にある日次ログの日付（ファイル名の YYYY-MM-DD）を昇順で返す。"""
    base = Path(workspace) / 'logs' / 'full'
    if not base.exists():
        return []
    out = []
    for f in base.glob('*_full.jsonl'):
        d = f.name[:10]
        try:
            date.fromisoformat(d)
            out.append(d)
        except ValueError:
            continue
    return sorted(set(out))


def turns_from_raw_log(workspace, day: str) -> tuple[list[list[dict]], list[str]]:
    """logs/full/<日>.jsonl から、その論理日のターン列を復元する（生ログからの作り直しの入口）。

    論理日は午前3時境界なので、前日のファイル（3時以降ぶん）と当日のファイル
    （3時より前を除く）の両方を読む。戻り値は (ターン列, 各ターンの時刻)。
    ターンは [{'role':'user',...}, {'role':'assistant',...}, {'role':'tool',...}...] の列で、
    user の本文冒頭に「YYYY-MM-DD HH:MM」を付けて論理日の判定に使えるようにする。

    OpenClaw 期（2026-02-06〜18）の心拍ターン（「HEARTBEAT.md を読め」→「HEARTBEAT_OK」だけの往復）は
    情報が無いので落とす。CG 側の Moonbeat は本文を持つのでこの条件には当たらない。
    """
    from datetime import timedelta
    base = Path(workspace) / 'logs' / 'full'
    d = date.fromisoformat(day)
    rows = []
    for f in (base / f'{d.isoformat()}_full.jsonl',
              base / f'{(d + timedelta(days=1)).isoformat()}_full.jsonl'):
        if not f.exists():
            continue
        for line in f.read_text(encoding='utf-8', errors='replace').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    rows.sort(key=lambda r: r.get('timestamp', ''))

    turns, times, cur = [], [], None
    for r in rows:
        ts = r.get('timestamp', '')
        typ = r.get('type', '')
        if typ == 'user_message':
            if logical_date_of(f'{ts[:10]} {ts[11:16]}') != day:
                cur = None
                continue
            cur = [{'role': 'user', 'content': f"{ts[:10]} {ts[11:16]}\n{r.get('content') or ''}"}]
            turns.append(cur)
            times.append(ts[11:16])
        elif cur is not None and typ == 'assistant_message':
            cur.append({'role': 'assistant', 'content': r.get('content') or ''})
        elif cur is not None and typ == 'tool_result':
            c = r.get('content')
            cur.append({'role': 'tool', 'content': c if isinstance(c, str) else str(c)})

    kept_turns, kept_times = [], []
    for t, tm in zip(turns, times):
        replies = [m for m in t[1:] if m['role'] == 'assistant']
        heartbeat_only = (
            'HEARTBEAT' in t[0]['content']
            and replies
            and all(m['content'].strip() == 'HEARTBEAT_OK' for m in replies)
            and not any(m['role'] == 'tool' for m in t[1:])
        )
        if not heartbeat_only:
            kept_turns.append(t)
            kept_times.append(tm)
    return kept_turns, kept_times


def write_layer1_file(workspace, view: str) -> Path:
    """ビューを workspace/memory/layer1.md に書く（tmp に書いてから置き換える）。戻り値は書いたパス。

    コンテキストへ直接差し込むのではなくファイルに書くのは、LETHE の compressed.md と揃えるため。
    「作る（summary_db）」「書き出す（このファイル）」「読ませる（boot_memories）」を分けておくと、
    見せる・見せないの切替とロールバックが config.yaml の1行で済み、中身も目で確かめられる。
    """
    path = Path(workspace) / LAYER1_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.md.tmp')
    tmp.write_text(view or '', encoding='utf-8')
    os.replace(tmp, path)
    return path


def is_weak_anchor(a: str, own_text_norm: str = '') -> bool:
    """手がかりとして弱い語か。own_text_norm は柚月自身の発言（正規化済み）。

    - 曜日・時間帯の語: どのターンにもあるので手がかりにならない
    - 小文字だけ／大文字だけの英単語: 引用文の中の一般語（into・DATA 等）がほとんど
    - プログラムの値（None 等）と snake_case の識別子（next_day 等）: ツール結果の JSON 由来が
      ほとんど。柚月自身がその語を口にした場合だけ残す（self_memo のような道具名は残る）
    - ファイル名でも数値+単位でもない、数字入りの英数字列: ID の類（KTCrD4 等）
    実測: この規則を入れる前は 31B が「Noneの状態で就寝した」と書いた。
    """
    if a in _WEEKTIME_WORDS:
        return True
    if re.fullmatch(r'[a-z]+', a) or re.fullmatch(r'[A-Z]{2,}', a):
        return True
    if a.lower() in _PROGRAM_LITERALS or _SNAKE_RE.match(a):
        return _norm(a) not in own_text_norm
    if _IDLIKE_RE.match(a) and re.search(r'\d', a) and not _FILE_TAIL_RE.search(a) \
            and not _NUM_UNIT_RE.match(a):
        return True
    return False


# 発端（user 側）から落とすもの。柚月の記憶の材料ではなく、仕組みの決まり文句や手順書。
# - Moonbeat の投げかけ「[Moonbeat] 朝ですね。何をしますか？」（Layer0 後は "moonbeat" の1語）。
#   <note_fragment>（過去ノートの抜粋）は柚月が読んで反応する材料なので残す
# - スケジュールタスクの指示書本文（<task_instruction>〜、旧形式は「--- 〜 ---」）。
#   「タスク名: 〜」の行は残る。畳まれた部（短い一文）はそのまま
# 実測: 指示書の見本の見出し20個がアンカーになり、31B がそれを全部文に押し込んだ（7/13 08:00）
_MOONBEAT_LINE_RE = re.compile(r'^[ \t]*(?:\[Moonbeat\][^\n]*|moonbeat)[ \t]*$', re.IGNORECASE | re.MULTILINE)
_TASK_INSTRUCTION_RE = re.compile(
    r'(?:以下の指示書に従って行動してください:\s*\n)?<task_instruction>\s*.*?\s*</task_instruction>', re.DOTALL)
_TASK_INSTRUCTION_LEGACY_RE = re.compile(r'以下の指示書に従って行動してください:\s*\n---\n.*\n---\s*$', re.DOTALL)


def trim_situation(text: str) -> str:
    """LLM に渡す発端の文から、仕組みの決まり文句と指示書本文を落とす。日付・天気の行は残る。"""
    t = _TASK_INSTRUCTION_RE.sub('', text)
    t = _TASK_INSTRUCTION_LEGACY_RE.sub('', t)
    t = _MOONBEAT_LINE_RE.sub('', t)
    return re.sub(r'\n{3,}', '\n\n', t).strip()


def _regex_anchors(seg: str) -> list[tuple[int, str]]:
    out = []
    for m in PATH_RE.findall(seg):
        out.append((0, m))
    for m in IDNUM_RE.findall(seg):
        if not m.isdigit():
            out.append((1, m))
    for m in TITLE_RE.findall(seg):
        if re.search(r'[A-Za-z0-9]', m):     # 英数字を含む引用だけ（汎用句を排除）
            out.append((2, m[:32]))
    for m in ALPHA_RE.findall(seg):
        if m.lower() in ANCHOR_STOPWORDS or len(m) > 20:
            continue
        out.append((3, m))
    return out


def verify_anchors(raw_anchors: list[str], seg: str) -> list[str]:
    """LLM が挙げたアンカーを原文と照合する。実在しないものは捨てる（捏造の構造的排除）。

    まず全体で照合し、ダメなら '/' で分割して各部を照合する
    （実測: モデルが "A/B/C" と連結して返すことがある。ファイルパスを壊さないためこの順）。
    """
    seg_n = _norm(seg)
    ok = []
    for a in raw_anchors:
        a = (a or '').strip()
        if not a:
            continue
        if _norm(a) and _norm(a) in seg_n:
            ok.append(a)
            continue
        parts = [p.strip() for p in a.split('/') if p.strip()]
        if len(parts) > 1 and all(_norm(p) in seg_n for p in parts):
            ok.extend(parts)
    return ok


def finalize_anchors(llm_anchors: list[str], seg: str, df: Callable[[str], int],
                     total_segs: int, cfg: dict, own_text: str = '') -> list[str]:
    """検算済みLLMアンカー + 正規表現アンカーの和集合を、優先順位で並べて返す。

    - 数の上限は設けない（2026-09-04 撤廃）。ターンの固有名詞の数はまちまちなので、
      固定の上限は短いターンで余り、長いターンで足りなかった。要約の長さは
      内容に追従させる（パス2の指示も「内容に合わせる」）
    - 汎用語（バッチ内の出現率が generic_df_ratio 超）は却下
      → そのターンを特定できないので手がかりにならない
    - 固定の禁止語（ANCHOR_STOPWORDS）と弱い語（is_weak_anchor）は、LLM が挙げた語にも掛ける
    - LLM は日本語固有名詞に強く、正規表現は機械的識別子に強い。和集合で互いの穴を埋める
    own_text は柚月自身の発言（正規化前でよい）。プログラムの値や snake_case の識別子を
    残すかどうかの判定に使う。
    """
    limit = max(1, int(total_segs * cfg['generic_df_ratio']))
    own_norm = _norm(own_text or '')
    cand = [(1, a) for a in verify_anchors(llm_anchors, seg)]
    cand += _regex_anchors(seg)
    seen, out = [], []
    for _prio, a in sorted(cand, key=lambda x: (x[0], -len(x[1]))):
        key = _norm(a)
        if not key or df(a) > limit:
            continue
        if a.lower() in ANCHOR_STOPWORDS or is_weak_anchor(a, own_norm):
            continue
        if any(key in s or s in key for s in seen):   # 包含関係は情報量の多い方だけ残す
            continue
        seen.append(key)
        out.append(a)
    return out


# ============================================================
# ゲート（コード・決定論）
# ============================================================

def name_violations(text: str, batch_text: str) -> list[str]:
    """要約本文の固有語が入力バッチに実在するか。実在しない語＝捏造の疑い。"""
    in_norm = _norm(batch_text)
    in_words = {_norm(w) for w in ALPHA_RE.findall(batch_text)}
    in_words_nodigit = {re.sub(r'\d+$', '', w) for w in in_words}
    bad = []
    for w in ALPHA_RE.findall(text):
        n = _norm(w)
        if n in in_words or n in in_norm:
            continue
        if re.sub(r'\d+$', '', n) in in_words_nodigit:   # "Grok 4.5" → "Grok4.5" の表記差を吸収
            continue
        bad.append(w)
    return bad


def is_misattributed(text: str, own_seg: str, other_segs: list[str]) -> bool:
    """帰属エラー判定: 手がかり語が自ターンに無く、他ターンにだけある場合。

    日時の紐付けごと壊れるので、事実の欠落より深刻。疑わしきは計上する（緩め）。
    """
    probes = set(ALPHA_RE.findall(text)) | set(IDNUM_RE.findall(text)) | set(KATA_RE.findall(text))
    probes = [p for p in probes if len(p) >= 4 and p.lower() not in ('moonbeat', 'user')]
    if not probes:
        return False
    own = _norm(own_seg)
    if any(_norm(p) in own for p in probes):
        return False
    others = [_norm(s) for s in other_segs]
    return any(any(_norm(p) in o for o in others) for p in probes)


# ============================================================
# 正規化・減衰・描画
# ============================================================

def normalize_day(entries: list[dict], cfg: dict) -> None:
    """日内パーセンタイルで tier(100-500) を付け、コード側フロアを適用する。

    LLM のスコアは絶対値を使わず順位だけ使うので、甘口/辛口モデルの差が消える
    （実測: 生スコア中央値 flash 60 / gemma 85 でも正規化後の選別は8割一致）。
    """
    if not entries:
        return
    scores = sorted((e['score'] for e in entries), reverse=True)
    n = len(scores)
    for e in entries:
        rank = sum(1 for s in scores if s > e['score'])
        pct = rank / n if n > 1 else 0.0
        for boundary, tier in TIER_BOUNDARIES:
            if pct < boundary:
                e['tier'] = tier
                break
        # フロアはコードが判定する（ご主人様・約束はLLMの採点に依存させない）
        if e.get('master') or e.get('promise'):
            e['tier'] = max(e['tier'], cfg['floor_tier'])


def layer0_pending(turns: list[list[dict]], get_text: Callable[[dict], str]) -> int:
    """その日のターンのうち Layer0 未圧縮のものを数える。

    Layer0 は user メッセージ末尾に LAYER0_MARKER を残す。無ければ生のターンで、
    <system_notice> や生のツール結果がそのまま入っている（Layer0 が取り除く前）。
    v2 の要約は Layer0 済みの入力を前提に設計・実測している。
    """
    return sum(1 for t in turns if t and LAYER0_MARKER not in get_text(t[0]))


def _accepts_kwarg(fn, name: str) -> bool:
    """fn がキーワード引数 name（または **kwargs）を受け取れるか。"""
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD
                                 for p in params.values())


def decay_factor(rank: int, coeff: float) -> float:
    """双曲減衰の係数。rank はビュー内の序数（最新日 = 1）。

    起点を「今日」ではなく「ビューの最新日」に置くのが要点。
    ビューが担当するのは会話履歴より古い日なので、絶対日齢を使うと
    ビューに入った瞬間から既に大きく減衰した状態で始まってしまう。
    序数にすると、会話履歴から出てきたばかりの日が満額（rank=1）で、
    そこから遡るほど薄くなる。会話履歴とビューが滑らかに繋がる。

    式は双曲線型（1/n 系）。実データで比較した結果:
      - 対数 1/(1+ln(1+n)) は最初の1週間で急落した後ほぼ平坦になり、
        2ヶ月前と半年前がほぼ同じ分量になってしまった（傾斜が付かない）
      - 指数（半減期）は落ち始めると止まらず、半年前が1日1行まで潰れた
      - 双曲線型は両者の中間で、月ごとに単調な傾斜が付いた
    """
    return 1 / (1 + coeff * max(0, rank - 1))


def decayed_score(tier: int, rank: int, coeff: float) -> float:
    return tier * decay_factor(rank, coeff)


def render_view(store: SummaryStore, today: date, cfg: dict,
                count_tokens: Callable[[str], int], heading: str) -> str:
    """summary_db から柚月に見せるビューを描画する（LLM 不使用・毎回再生成）。

    設計書 §4。順序: 範囲決定 → 正規化 → 減衰 → 予定TTL → dedup →
    「全日1行の最低保証」→ 残予算で行数を増やす → 日付順に出力。
    """
    through = store.compressed_through
    if not through:
        return ''
    by_day: dict[str, list[dict]] = {}
    for e in store.entries:
        d = e.get('date')
        if not d or d > through:      # 会話履歴が持っている日はビューに出さない（二重計上の排除）
            continue
        by_day.setdefault(d, []).append(dict(e))
    if not by_day:
        return ''

    # 減衰の起点はビュー内の最新日（＝直近に圧縮された日）。今日ではない。
    newest = max(by_day)
    newest_d = date.fromisoformat(newest)

    prepared = {}
    for d, es in by_day.items():
        normalize_day(es, cfg)
        age = (today - date.fromisoformat(d)).days          # 予定TTLの判定に使う実日齢
        rank = (newest_d - date.fromisoformat(d)).days + 1  # 減衰に使う序数（最新日=1）
        for e in es:
            e['age'] = age
            e['rank'] = rank
            e['decayed'] = decayed_score(e['tier'], rank, cfg['decay_coeff'])
        # 予定は TTL 超過で非表示（データは残す）。「明日やる」の亡霊を構造的に防ぐ
        vis = [e for e in es if not (is_plan(e) and age > cfg['plan_ttl_days'])]
        kept: list[dict] = []
        for e in sorted(vis, key=lambda x: x.get('turn_no', 0)):
            dup = next((k for k in kept if _sim(e['text'], k['text']) > cfg['dedup_sim']), None)
            if dup is None:
                kept.append(e)
            elif e['decayed'] > dup['decayed']:
                kept[kept.index(dup)] = e
        if kept:
            prepared[d] = sorted(kept, key=lambda x: -x['decayed'])

    if not prepared:
        return ''

    # --- 段階1: 全ての日に最低1行を確保（生涯カバーの不変条件） ---
    # 予算はトークン数の増分で積算する。1行足すたびに全文を組み直して
    # トークン化すると、日数×行数に比例して描画が重くなる（実測: 200日で7秒）。
    chosen = {d: es[:1] for d, es in prepared.items()}
    line_cost = {d: [count_tokens(f'- {e["text"]}\n') for e in es] for d, es in prepared.items()}
    head_cost = count_tokens(heading + '\n') if heading else 0
    day_cost = {d: count_tokens(f'## {d}（{_WEEKDAYS[date.fromisoformat(d).weekday()]}）\n') + 1
                for d in prepared}
    total = head_cost + sum(day_cost[d] + line_cost[d][0] for d in chosen)

    budget = cfg['view_max_tokens']
    if total > budget:
        # 最低保証すら入らない＝設定ミスか数年後。日を落とさず超過を許容し、上位に知らせる
        tlog('[SummaryV2] 予算が最低保証（全日1行）を下回っています。'
             'view_max_tokens の見直しが必要です')
        return _compose(chosen, heading, cfg.get('agent_name', ''))

    # --- 段階2: 残予算で各日の行数を減衰式の上限まで増やす（新しい日から） ---
    for d in sorted(prepared, reverse=True):
        es = prepared[d]
        # 基準はその日の全行ではなく line_ratio 倍。実データを見ると、
        # スコア下位の半分は「穏やかに過ごすことにした」系の低情報行が占める
        # （低情報行の出現率は tier500 で 2.5%、tier100 で 18.1%）。
        # 全行を基準にすると、その層まで含めて減らしていくことになる。
        want = max(cfg['day_floor_lines'],
                   round(len(es) * cfg['line_ratio']
                         * decay_factor(es[0]['rank'], cfg['decay_coeff'])))
        want = min(want, len(es))
        while len(chosen[d]) < want:
            nxt = line_cost[d][len(chosen[d])]
            if total + nxt > budget:
                break
            chosen[d].append(es[len(chosen[d])])
            total += nxt
        if total >= budget:
            break        # 予算を使い切ったら残りの日を試すまでもない

    # 加算はトークナイザの境界結合を考慮しない近似なので、最終文字列で検算する。
    # 超過していたら新しい日から順に1行ずつ削って収める
    # （最低1行は §4-8 の生涯カバー保証として必ず残す）。
    view = _compose(chosen, heading, cfg.get('agent_name', ''))
    if count_tokens(view) > budget:
        for d in sorted(chosen, reverse=True):
            while len(chosen[d]) > 1 and count_tokens(view) > budget:
                chosen[d].pop()
                view = _compose(chosen, heading, cfg.get('agent_name', ''))
            if count_tokens(view) <= budget:
                break
    return view


_WEEKDAYS = '月火水木金土日'


def _compose(chosen: dict, heading: str, agent_name: str = '') -> str:
    parts = [heading] if heading else []
    for d in sorted(chosen):
        es = sorted(chosen[d], key=lambda x: x.get('turn_no', 0))
        if not es:
            continue
        wd = _WEEKDAYS[date.fromisoformat(d).weekday()]
        parts.append(f'## {d}（{wd}）')
        # 描画時にも三人称の自己言及を落とす（既存の DB を書き換えずに直るように）
        parts.extend(f'- {e["text"] if e.get("fallback") else strip_self_reference(e["text"], agent_name)}'
                     for e in es)
        parts.append('')
    return '\n'.join(parts).rstrip() + '\n' if len(parts) > (1 if heading else 0) else ''


def view_coverage(store: SummaryStore) -> tuple[Optional[str], Optional[str]]:
    """ビューが覆う日付範囲（最古, 最新）。描画ガードの判定に使う。"""
    days = [d for d in store.days() if store.compressed_through and d <= store.compressed_through]
    return (days[0], days[-1]) if days else (None, None)


class GuardBaseline:
    """描画ガードの基準値を summary_db の外に持つ小さなファイル。

    基準値を守る対象と同じファイルに置くと、ファイルごと巻き戻る・失われる
    故障モードで基準値も一緒に巻き戻り、ガードが原理的に働かなくなる。
    「守るもの」と「守れているか測る物差し」は別の場所に置くこと。
    """

    def __init__(self, path):
        self.path = Path(path)
        self.data: dict = {}
        try:
            if self.path.exists():
                self.data = json.loads(self.path.read_text(encoding='utf-8'))
        except Exception as e:
            tlog(f'[SummaryV2] ガード基準値の読み込みに失敗（初期化して続行）: {e}')
            self.data = {}

    @property
    def oldest(self) -> Optional[str]:
        return self.data.get('view_oldest')

    @property
    def days(self) -> Optional[int]:
        return self.data.get('view_days')

    def update(self, oldest: Optional[str], days: int):
        if not oldest:
            return
        if self.data.get('view_oldest') == oldest and self.data.get('view_days') == days:
            return
        self.data['view_oldest'] = oldest
        self.data['view_days'] = days
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(self.path) + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception as e:
            tlog(f'[SummaryV2] ガード基準値の保存に失敗: {e}')


# ============================================================
# 生成パイプライン（1論理日ぶん）
# ============================================================

class DayCompressor:
    """1論理日ぶんのターン群 → summary_db エントリ列。

    パス1（要約+アンカー候補）→ コードでアンカー確定 → パス2（アンカーを使って再要約）
    → ゲート。LLM が全滅してもフォールバックで必ず完遂する（F6）。
    """

    def __init__(self, llm, cfg: dict, prompts: tuple[str, str],
                 get_text: Callable[[dict], str]):
        self.llm = llm
        self.cfg = cfg
        self.pass1_prompt, self.pass2_prompt = prompts
        self.get_text = get_text
        self.promise_re = re.compile(cfg['promise_regex'])
        self.stats = {'fallback': 0, 'name_reject': 0, 'attr_reject': 0,
                      'format_bad': 0, 'batch_reject': 0, 'llm_error': 0,
                      'pass2_missing': 0}
        # 温度は呼び出し単位で渡す（本体の会話用温度を書き換えない）。
        # 受け取れない LLM 実装（テストのモック・他プロバイダ）にはそのまま呼ぶ。
        # シャドー9日分（2026-09-01〜02）で、本体の温度1.0のまま走った結果
        # フォールバック率が 12.7%（設計想定 0.3%）に跳ねたのがこの穴の実測。
        self._temperature_kw = _accepts_kwarg(llm.chat, 'temperature_override')

    # --- LLM ---

    async def _chat(self, system: str, user: str) -> str:
        messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]
        if self._temperature_kw:
            resp = await self.llm.chat(messages, tools=None,
                                       temperature_override=self.cfg['temperature'])
        else:
            resp = await self.llm.chat(messages, tools=None)
        return (resp.content or '') if resp else ''

    # --- ターン属性（コードが判定する。LLM に聞かない） ---

    def _turn_attrs(self, turn: list[dict]) -> dict:
        user_text = self.get_text(turn[0])
        whole = '\n'.join(self.get_text(m) for m in turn)
        # ご主人様の発言があるか（Layer0 整形後は "user: 内容" の形で残る）
        master = ('user: ' in user_text) or ('<user_message>' in user_text)
        return {'master': master, 'promise': bool(self.promise_re.search(whole))}

    def _fallback_text(self, turn: list[dict]) -> str:
        """LLM に頼らない代替行。柚月自身の発言の冒頭を切り出すので捏造が起きえない。"""
        for msg in turn[1:]:
            if msg.get('role') == 'assistant':
                t = re.sub(r'\s+', ' ', self.get_text(msg)).strip()
                if t:
                    return t[:60]
        t = re.sub(r'\s+', ' ', self.get_text(turn[0])).strip()
        return t[:60] or '（記録なし）'

    # --- 本体 ---

    async def compress_day(self, day: str, turns: list[list[dict]],
                           times: list[str]) -> list[dict]:
        """1論理日ぶんを処理してエントリ列を返す。例外は投げない（必ず何か返す）。"""
        per_batch = self.cfg['turns_per_batch']
        batches = [(i, turns[i:i + per_batch], times[i:i + per_batch])
                   for i in range(0, len(turns), per_batch)]
        # アンカーの汎用語判定はこの日のバッチ内で自己完結させる（外部状態を持たない）
        all_segs = [self._seg_text(t) for t in turns]

        def df(word: str) -> int:
            k = _norm(word)
            return sum(1 for s in all_segs if k in _norm(s)) if k else 999

        # バッチ同士は独立（アンカーの df はこの日の全ターンで先に確定している）ので
        # 並列に処理する。逐次だと1バッチ約50秒×8バッチで1日13分かかり、
        # 緊急圧縮で走ったときに柚月がその間ずっと待たされる。
        import asyncio
        sem = asyncio.Semaphore(max(1, int(self.cfg.get('batch_concurrency', 4))))

        async def run(start, bturns, btimes):
            async with sem:
                return await self._run_batch(day, start, bturns, btimes, df, len(all_segs))

        results = await asyncio.gather(*[run(s, bt, tm) for s, bt, tm in batches])
        entries = [e for r in results for e in r]
        entries.sort(key=lambda e: e['turn_no'])
        return entries

    def _seg_text(self, turn: list[dict]) -> str:
        """検査とアンカーの材料。LLM が実際に見る本文（build_batch_text の描画）と同じにする。

        以前はターンの全文（ツール結果も全文）を使っていたため、LLM が見ていない部分
        （ツール結果の200字より後ろ・指示書・Moonbeat の決まり文句）から正規表現アンカーが
        生まれ、「必ず使え」で文に押し込まれていた（実測: Cosmic・Harvest・None・見出し20個）。
        """
        return build_batch_text([turn], self.get_text, ['--'])

    def _own_text(self, turn: list[dict]) -> str:
        """柚月自身の発言だけ（弱いアンカーの判定に使う）。"""
        return '\n'.join(self.get_text(m) for m in turn[1:] if m.get('role') == 'assistant')

    async def _run_batch(self, day: str, start: int, turns: list[list[dict]],
                         times: list[str], df, total_segs: int) -> list[dict]:
        size = len(turns)
        attrs = [self._turn_attrs(t) for t in turns]
        segs = [self._seg_text(t) for t in turns]

        def fallback_all(reason: str) -> list[dict]:
            self.stats['fallback'] += size
            tlog(f'[SummaryV2] {day} batch{start}: フォールバック（{reason}）')
            return [self._entry(day, start + i, times[i], self._fallback_text(turns[i]),
                                1, [], attrs[i], fallback=True) for i in range(size)]

        # --- パス1: 要約 + アンカー候補 ---
        text1 = build_batch_text(turns, self.get_text, times)
        try:
            resp1 = await self._chat(self.pass1_prompt, text1)
        except Exception as e:
            self.stats['llm_error'] += 1
            return fallback_all(f'パス1のLLM呼び出し失敗: {e}')
        parsed1, bad1 = parse_lines(resp1, size, with_anchors=True)
        self.stats['format_bad'] += bad1

        # --- アンカー確定（コード） ---
        anchors = [finalize_anchors(parsed1.get(i + 1, {}).get('anchors', []),
                                    segs[i], df, total_segs, self.cfg,
                                    own_text=self._own_text(turns[i]))
                   for i in range(size)]

        # --- パス2: 確定アンカーを渡して書き直し ---
        isolated = bool(self.cfg.get('pass2_isolated', False))
        if isolated:
            # 1ターンずつ、そのターンの本文だけを見せて書かせる。
            # 隣のターンの本文が目の前に無ければ、モデルの行儀に関係なく借りようがない
            # （2026-09-03 実測: 5ターン一括だと flash は隣の内容を書く。31B は書かない。
            #  この差をモデルの癖に任せず、仕組みで消す）。
            import asyncio as _aio

            async def _one(i: int):
                t2 = build_batch_text([turns[i]], self.get_text, [times[i]], anchors=[anchors[i]])
                try:
                    r = await self._chat(self.pass2_prompt, t2)
                except Exception:
                    self.stats['llm_error'] += 1
                    return i, {}, 0
                p, bad = parse_lines(r, 1, with_anchors=False)
                return i, p, bad

            parsed2, bad2 = {}, 0
            for i, p, bad in await _aio.gather(*[_one(i) for i in range(size)]):
                bad2 += bad
                if 1 in p:
                    parsed2[i + 1] = p[1]
            text2 = None
        else:
            text2 = build_batch_text(turns, self.get_text, times, anchors=anchors)
            try:
                resp2 = await self._chat(self.pass2_prompt, text2)
            except Exception as e:
                self.stats['llm_error'] += 1
                resp2 = ''
            parsed2, bad2 = parse_lines(resp2, size, with_anchors=False)
        self.stats['format_bad'] += bad2

        # --- ゲート ---
        accepted, unparsed = {}, 0
        from_pass1 = set()
        for i in range(size):
            cand = parsed2.get(i + 1)
            if cand is None:
                # パス2が落ちた行はパス1で代替するが、狙った品質ではない
                # （パス1は50字以内・帰結なし、パス2は45〜60字で出来事＋帰結）。
                # そのまま正常データにすると後日の作り直し対象から漏れるので印を付ける。
                cand = parsed1.get(i + 1)
                if cand is not None:
                    from_pass1.add(i)
            if not cand:
                unparsed += 1          # どちらのパスも読める行を返さなかった（形式の異常）
                continue
            text = cand['text']
            if isolated and i not in from_pass1:
                # パス2は自分のターンしか見ていないので、自分のターンに無い固有名詞は捏造
                if name_violations(text, segs[i]):
                    self.stats['name_reject'] += 1
                    continue
            else:
                if name_violations(text, text2 if text2 is not None else segs[i]):
                    self.stats['name_reject'] += 1
                    continue
                if is_misattributed(text, segs[i], [s for j, s in enumerate(segs) if j != i]):
                    self.stats['attr_reject'] += 1
                    continue
            accepted[i] = cand

        # 束ごと捨てるのは「行の形式が読めない」系統的異常だけ（LLM の返答そのものが壊れている）。
        # 名前・帰属で弾いた行は上で1行ずつ落としてあり、検査を通った行は残す。
        # 以前は名前・帰属の破棄も数えて 3割超で5行まとめて捨てていたが、正しい行まで
        # 道連れにしていた（実測: Layer0 済み45日で9回、2月13日で24回）。
        if size and unparsed / size > self.cfg['batch_reject_ratio']:
            self.stats['batch_reject'] += 1
            return fallback_all(f'形式不良{unparsed}/{size}が閾値超過')

        out = []
        for i in range(size):
            if i in accepted:
                c = accepted[i]
                if i in from_pass1:
                    self.stats['pass2_missing'] += 1
                # 重要度は「5ターンの中での相対」で付けさせている。隔離モードのパス2は
                # 1ターンしか見ていないので相対評価が成り立たない。点数はパス1（5ターンを
                # 見て付けた）のものを使い、文だけパス2から取る。パス1に無ければパス2の点数。
                score = c['score']
                if isolated and (i + 1) in parsed1:
                    score = parsed1[i + 1]['score']
                out.append(self._entry(day, start + i, times[i],
                                       strip_self_reference(c['text'], self.cfg.get('agent_name', '')),
                                       score, anchors[i], attrs[i],
                                       degraded=(i in from_pass1)))
            else:
                self.stats['fallback'] += 1
                out.append(self._entry(day, start + i, times[i],
                                       self._fallback_text(turns[i]), 1, anchors[i],
                                       attrs[i], fallback=True))
        return out

    @staticmethod
    def _entry(day: str, turn_no: int, time: str, text: str, score: int,
               anchors: list[str], attrs: dict, fallback: bool = False,
               degraded: bool = False) -> dict:
        return {
            'date': day,
            'turn_no': turn_no,
            'time': time,
            'text': text.strip(),
            'score': score,
            'plan': text.strip().startswith(PLAN_PREFIX),
            'master': bool(attrs.get('master')),
            'promise': bool(attrs.get('promise')),
            'anchors': anchors,
            'fallback': fallback,      # 決定論フォールバック（冒頭の切り出し）
            'degraded': degraded,      # パス1で代替した行（狙った品質ではない）
        }


# ============================================================
# 履歴の日単位スライス
# ============================================================

def slice_history_by_day(history: list[dict], get_text: Callable[[dict], str]) -> list[dict]:
    """会話履歴を論理日ごとに区切る。

    Returns: [{'date','start','end','turns','times','partial_head'}] を古い順で。
      start/end は history のインデックス（end は排他）。
    日付が取れないメッセージは直前のターンの論理日を継承する。
    """
    days: list[dict] = []
    cur = None
    orphan_start = None      # 先頭にある日付不明ターンの開始位置
    for i, m in enumerate(history):
        if m.get('role') != 'user':
            continue
        d = logical_date_of(get_text(m))
        if d is None:
            d = cur['date'] if cur else None
            if d is None:
                # 先頭から日付不明。まだ日が決まらないので位置だけ覚えておき、
                # 最初の日が決まったらその日に取り込む。
                # ここで捨てると、後で drop_history_before がこの範囲ごと削除し、
                # 要約されないまま会話が消える（実測で確認済み）。
                if orphan_start is None:
                    orphan_start = i
                continue
        if cur is None or d != cur['date']:
            if cur is not None:
                cur['end'] = i
                days.append(cur)
            start = i
            starts = []
            if not days and orphan_start is not None:
                # 先頭の日付不明ターンを最初の日に合流させる（欠落させない）
                start = orphan_start
                starts = [j for j, mm in enumerate(history[orphan_start:i], orphan_start)
                          if mm.get('role') == 'user']
                orphan_start = None
            cur = {'date': d, 'start': start, 'end': len(history), 'turn_starts': starts}
        cur['turn_starts'].append(i)
    if cur is not None:
        days.append(cur)
    # 各日のターン群と時刻を組み立てる
    for k, day in enumerate(days):
        end = day['end'] if k < len(days) - 1 else len(history)
        day['end'] = end
        starts = day['turn_starts']
        turns, times = [], []
        for j, s in enumerate(starts):
            e = starts[j + 1] if j + 1 < len(starts) else end
            turns.append(history[s:e])
            m = HEAD_ISO_RE.match(get_text(history[s])[:HEAD_SCAN_CHARS])
            if m:
                times.append(f'{m.group(4)}:{m.group(5)}')
            else:
                jm = HEAD_JA_RE.search(get_text(history[s])[:HEAD_SCAN_CHARS])
                times.append(f'{jm.group(4)}:{jm.group(5)}' if jm else '')
        day['turns'] = turns
        day['times'] = times
        day.pop('turn_starts', None)
    return days


# ============================================================
# トランザクション（1論理日 = 1トランザクション・設計書 §3.5）
# ============================================================

class DayTransaction:
    """1論理日の圧縮を5段階で進め、段階を summary_db に永続化する。

    どの段階で落ちても会話履歴は無傷、watermark も進まない。
    各段階が冪等（日単位置換／セクション置換／ID upsert／同日置換）なので、
    同じ段階をやり直しても重複・矛盾が生じない。
    """

    def __init__(self, store: SummaryStore, memory=None, rag_db=None,
                 lethe=None, workspace_path: Optional[str] = None):
        self.store = store
        self.memory = memory
        self.rag_db = rag_db
        self.lethe = lethe                 # MemoryCompressor（LETHE）
        self.workspace_path = workspace_path

    def _reached(self, day: str, stage: str) -> bool:
        cur = self.store.tx_stage(day)
        if cur is None:
            return False
        return TX_STAGES.index(cur) >= TX_STAGES.index(stage)

    async def run(self, day: str, entries: list[dict]) -> bool:
        """段階1〜4を実行する（段階5=履歴削除は呼び出し元が行う）。成功なら True。"""
        try:
            if not self._reached(day, 'generated'):
                self.store.replace_day(day, entries)
                self.store.set_tx_stage(day, 'generated')
                self.store.save()
                # 読み戻し検証（書けたつもりで書けていない事故を防ぐ）
                verify = SummaryStore(self.store.path)
                verify.load()
                if len(verify.entries_of(day)) != len(entries):
                    tlog(f'[SummaryV2] {day}: 読み戻し検証に失敗しました')
                    return False

            if not self._reached(day, 'log_written'):
                self._write_daily_log(day)
                self.store.set_tx_stage(day, 'log_written')
                self.store.save()

            if not self._reached(day, 'rag_added'):
                self._add_to_rag(day)
                self.store.set_tx_stage(day, 'rag_added')
                self.store.save()

            if not self._reached(day, 'lethe_done'):
                await self._run_lethe(day)
                self.store.set_tx_stage(day, 'lethe_done')
                self.store.save()

            return True
        except Exception as e:
            tlog(f'[SummaryV2] {day}: トランザクション失敗（段階 {self.store.tx_stage(day)}）: {e}')
            return False

    def commit(self, day: str):
        """段階5。履歴削除が完了した後に呼ぶ。watermark を進めて tx を閉じる。"""
        self.store.set_tx_stage(day, 'done')
        self.store.advance_watermark(day)
        self.store.clear_tx(day)
        self.store.save()

    # --- 各段階 ---

    def _day_body(self, day: str) -> str:
        """その日の要約本文（見出しなし）。LETHE / RAG にはこれを渡す。"""
        lines = []
        for e in sorted(self.store.entries_of(day), key=lambda x: x.get('turn_no', 0)):
            lines.append(f'- {e["text"]}')
        return '\n'.join(lines) + '\n'

    def _day_section(self, day: str) -> str:
        """logs/summary に書く v2 セクション（マーカー付き）。"""
        return f'{V2_SECTION_BEGIN}\n{self._day_body(day)}{V2_SECTION_END}\n'

    def _write_daily_log(self, day: str):
        """logs/summary/<論理日>.md に v2 セクションだけを差し替えて書く。

        ファイル全体を上書きしてはいけない。同じ日付のファイルには v1 時代の
        要約が既に入っていることがあり（実測: 2026-08-05.md に373行）、
        全上書きすると柚月の記録が復元不能に失われる。
        マーカーで挟んだ v2 セクションだけを置換し、他の内容はそのまま残す。
        セクション置換なので再実行しても重複しない（冪等）。
        書き込み先が実際の論理日になるため、LETHE の日付ずれ（A-2）が構造的に解消する。
        """
        if not self.memory:
            return
        rel = f'logs/summary/{day}.md'
        # read_file はファイルが無いとき None を返す仕様なので、例外は
        # 「存在するのに読めない」場合にしか起きない（ロック・権限など）。
        # そこで既存内容を空とみなして書くと v1 の記録を消してしまうため、
        # 例外は握り潰さず伝播させ、トランザクションを未完にして翌日再試行させる。
        existing = self.memory.read_file(rel)

        section = self._day_section(day)
        if existing is None:
            self.memory.write_file(rel, f'# {day} の記憶\n\n{section}')
            return

        # 既存の v2 セクションがあれば差し替え、無ければ末尾に足す（v1 の内容は保持）
        pattern = re.compile(
            re.escape(V2_SECTION_BEGIN) + r'.*?' + re.escape(V2_SECTION_END) + r'\n?',
            re.DOTALL)
        if pattern.search(existing):
            merged = pattern.sub(section, existing, count=1)
        else:
            merged = existing.rstrip('\n') + '\n\n' + section
        self.memory.write_file(rel, merged)

    def _add_to_rag(self, day: str):
        """決定論IDで upsert する（再実行しても重複登録されない）。"""
        if not self.rag_db:
            return
        body = self._day_body(day)      # マーカーは検索対象に入れない
        try:
            self.rag_db.add('daily_memories', body,
                            {'date': day, 'source': f'logs/summary/{day}.md'},
                            doc_id=f'daily_summary_{day}')
        except TypeError:
            # doc_id 非対応の実装。IDなしで追加すると再試行や作り直しのたびに
            # 同じ日の文書が積み重なり、検索結果が重複で埋まる。
            # 冪等性を保証できないので、ここでは登録せず記録だけ残す。
            tlog(f'[SummaryV2] {day}: RAG が doc_id 非対応のため登録を見送りました'
                 f'（重複登録を避けるため）')

    async def _run_lethe(self, day: str):
        """LETHE の compress_day を正しい論理日で呼ぶ。同日置換仕様なので再実行は冪等。"""
        if not self.workspace_path:
            return
        if not self.lethe:
            # LETHE を作れなかった（設定不備・import失敗）。ここを黙って通すと
            # 履歴は削除されるのに LETHE だけその日を欠落し、後から気づけない。
            # 圧縮自体は止めず、欠落した日を meta に控えて後で補える形にする。
            missing = self.store.meta.setdefault('lethe_missing_days', [])
            if day not in missing:
                missing.append(day)
            tlog(f'[SummaryV2] {day}: LETHE が利用できないため連携をスキップしました'
                 f'（meta.lethe_missing_days に記録）')
            return
        await self.lethe.compress_day(self._day_body(day),
                                      date.fromisoformat(day), self.workspace_path)
