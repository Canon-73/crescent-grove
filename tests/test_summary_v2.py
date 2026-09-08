# -*- coding: utf-8 -*-
"""
会話要約システム v2 のテスト（設計書 docs/SUMMARY_V2_DESIGN.md §10-2）

実行:
    venv\\Scripts\\python.exe tests\\test_summary_v2.py

pytest 不要の自前ランナー（OpenBotCity / citron のテストと同じ流儀）。
ネットワークに出ない。LLM は全てモック。柚月の実データには一切触れない
（workspace も data/ も使わず、tempfile 上で完結する）。
"""

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# tlog は logs/console/<日付>.log に追記する。テストの出力がそこへ混ざると、
# 柚月のサーバで何が起きたかを後から追うときに紛らわしい（実際に一度混ぜた）。
# tlog は呼び出しのたびに _console_log_dir() を引き直すので、ここで逃がせば足りる。
import core.time_utils as _time_utils

_TEST_LOG_DIR = Path(tempfile.mkdtemp(prefix='summary_v2_test_log_'))
_time_utils._console_log_dir = lambda: _TEST_LOG_DIR

import core.summary_v2 as S

_passed, _failed = 0, 0
_failures = []


def check(name, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        _failures.append(f'{name}: {detail}')
        print(f'  [FAIL] {name} {detail}')


def eq(name, got, want):
    check(name, got == want, f'got={got!r} want={want!r}')


# ============================================================
# ヘルパ
# ============================================================

def txt(m):
    c = m.get('content', '')
    if isinstance(c, list):
        return ''.join(p.get('text', '') for p in c if isinstance(p, dict))
    return c if isinstance(c, str) else str(c)


def user_msg(day, time, body='', master=False):
    head = f'{day} {time}\n20.0℃ 晴れ\n'
    if master:
        head += f'user: {body}\n'
    else:
        head += 'moonbeat\n'
    return {'role': 'user', 'content': head + S.LAYER0_MARKER}


def asst(text):
    return {'role': 'assistant', 'content': text}


def make_history(day, n, master_at=(), promise_at=()):
    h = []
    for i in range(n):
        t = f'{7 + i:02d}:00'
        h.append(user_msg(day, t, body='今日はどう？' if i in master_at else '', master=i in master_at))
        body = f'ターン{i}の内容。Tiramisuとやり取りした。'
        if i in promise_at:
            body += '明日会う約束をした。'
        h.append(asst(body))
    return h


class FakeLLM:
    """決定論モック。台本(list[str])を順に返す。callable なら関数として使う。"""

    def __init__(self, script):
        self.script = script
        self.calls = []
        self.kwargs = []

    async def chat(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        self.kwargs.append(kwargs)
        s = self.script
        out = s(messages, len(self.calls) - 1) if callable(s) else s[min(len(self.calls) - 1, len(s) - 1)]
        if isinstance(out, Exception):
            raise out

        class R:
            content = out
        return R()


def default_script(messages, idx):
    """入力のターン数だけ整形済みの行を返す（正常系）。"""
    user = messages[1]['content']
    n = len(re.findall(r'--- ターン\d+ ', user))
    return '\n'.join(f'{i+1}:{50 + i * 5}:ターン{i}の要約。Tiramisuとやり取りし満足した。〔Tiramisu〕'
                     for i in range(n))


CFG = S.get_config({})


def cnt(s):
    """テスト用トークン計測（1文字=1トークン扱いで十分）。"""
    return len(s)


# ============================================================
# 1. 論理日（午前3時境界）
# ============================================================

def test_logical_date():
    print('[1] 論理日（3時境界）')
    eq('2:59は前日', S.logical_date_of('2026-07-09 02:59 くもり'), '2026-07-08')
    eq('3:00は当日', S.logical_date_of('2026-07-09 03:00 くもり'), '2026-07-09')
    eq('00:04は前日', S.logical_date_of('2026-08-28 00:04'), '2026-08-27')
    eq('日本語形式', S.logical_date_of('[SYSTEM]\n2026年07月09日（木） 01:30 JST'), '2026-07-08')
    # 冒頭だけを見る＝flashback 内の過去日付を拾わない（実測で2月の幽霊が出た問題）
    eq('flashback汚染なし',
       S.logical_date_of('2026-07-09 10:00\n<flashback>2026-02-08 05:30 の話</flashback>'),
       '2026-07-09')
    eq('抽出不能はNone', S.logical_date_of('日付のない本文'), None)
    eq('不正日付はNone', S.logical_date_of('2026-13-45 10:00'), None)


# ============================================================
# 2. 寛容パーサ
# ============================================================

def test_parser():
    print('[2] 寛容パーサ')
    p, _ = S.parse_lines('1:70:本文A〔Tiramisu・Alias〕', 5, True)
    eq('基本形', p[1]['text'], '本文A')
    eq('アンカー抽出', p[1]['anchors'], ['Tiramisu', 'Alias'])
    # プロンプトの言いつけに形式を依存させない（gemma が実際に返した形）
    p, _ = S.parse_lines('番号:2:重要度:80:本文B', 5, False)
    eq('ラベル付き番号', p[2]['text'], '本文B')
    p, _ = S.parse_lines('ターン3:40:本文C', 5, False)
    eq('ターン接頭辞', p[3]['text'], '本文C')
    p, _ = S.parse_lines('4：40：全角コロン', 5, False)
    eq('全角コロン', p[4]['text'], '全角コロン')
    p, bad = S.parse_lines('以下が要約です:\n1:50:本文', 5, False)
    eq('前置き行を弾く', bad, 1)
    eq('本体は通る', p[1]['text'], '本文')
    p, bad = S.parse_lines('9:50:範囲外', 5, False)
    eq('ターン範囲外を弾く', (len(p), bad), (0, 1))
    p, bad = S.parse_lines('1:50:先勝ち\n1:60:後', 5, False)
    eq('重複番号を弾く', (p[1]['text'], bad), ('先勝ち', 1))
    p, _ = S.parse_lines('1:0:-', 5, False)
    eq('スキップ行は不採用', len(p), 0)
    p, _ = S.parse_lines('1:200:上限クランプ', 5, False)
    eq('スコアクランプ', p[1]['score'], 100)


# ============================================================
# 3. アンカー
# ============================================================

def test_anchors():
    print('[3] アンカー')
    seg = '証言をtestimony/section8_2.mdとして書き直し、72.83HzとTiramisuの話をした'
    eq('実在するものは通る', S.verify_anchors(['testimony/section8_2.md', '72.83Hz'], seg),
       ['testimony/section8_2.md', '72.83Hz'])
    eq('捏造は落ちる', S.verify_anchors(['存在しない語'], seg), [])
    # スラッシュ連結の救済（実測: モデルが "A/B" と返す）。パスは壊さない
    eq('連結の分割', S.verify_anchors(['72.83Hz/Tiramisu'], seg), ['72.83Hz', 'Tiramisu'])
    eq('パスは分割しない', S.verify_anchors(['testimony/section8_2.md'], seg),
       ['testimony/section8_2.md'])
    # 表記ゆれ
    eq('空白差の吸収', S.verify_anchors(['Grok 4.5'], 'Grok4.5の記事'), ['Grok 4.5'])

    segs = [seg] + ['moonbeat 天気の話'] * 9
    def df(w):
        k = S._norm(w)
        return sum(1 for s in segs if k in S._norm(s))
    got = S.finalize_anchors(['testimony/section8_2.md'], seg, df, len(segs), CFG)
    check('パスが最優先', got and got[0] == 'testimony/section8_2.md', f'got={got}')
    # 正規表現が直前の日本語を巻き込まない（\\w は日本語にマッチするので使えない）
    eq('パス抽出に日本語が混ざらない', S.PATH_RE.findall(seg), ['testimony/section8_2.md'])
    eq('日本語直後のパスも純粋', S.PATH_RE.findall('メモをnotes/foo.txtに書いた'), ['notes/foo.txt'])
    # 汎用語（バッチ内で頻出）は却下される
    df_common = lambda w: 10
    eq('汎用語の却下', S.finalize_anchors(['72.83Hz'], seg, df_common, 10, CFG), [])
    # 上限は無い。弱い語と禁止語が落ちる
    got = S.finalize_anchors(['Tiramisu', 'tips', 'into', 'DATA', '金曜日', 'None', 'next_day', 'KTCrD4'],
                             seg + ' tips into DATA 金曜日 None next_day KTCrD4', df, len(segs), CFG)
    eq('禁止語・弱い語が落ちる', [a for a in got if a in ('tips', 'into', 'DATA', '金曜日', 'None', 'next_day', 'KTCrD4')], [])
    check('本物は残る', 'Tiramisu' in got and 'testimony/section8_2.md' in got, str(got))
    # 柚月自身が口にした識別子は残る
    got2 = S.finalize_anchors(['self_memo', 'next_day'], seg + ' self_memo next_day', df, len(segs), CFG,
                              own_text='self_memoを更新した')
    check('自分で言った道具名は残る', 'self_memo' in got2 and 'next_day' not in got2, str(got2))


# ============================================================
# 4. ゲート
# ============================================================

def test_gates():
    print('[4] ゲート')
    batch = 'ターン1 Tiramisuと話した。testimony/section8_2.mdを書いた。'
    eq('実在名は通る', S.name_violations('Tiramisuと話した', batch), [])
    eq('捏造名を検出', S.name_violations('Watsonと話した', batch), ['Watson'])
    eq('末尾数字の正規化', S.name_violations('Grok4.5を読んだ', 'Grok 4.5の記事'), [])

    own = 'ターン1 Tiramisuとの会話'
    other = ['ターン2 Clawdineから Living Atlas の DM が来た']
    eq('正しい帰属', S.is_misattributed('Tiramisuと話した', own, other), False)
    eq('帰属エラー検出', S.is_misattributed('ClawdineからLiving Atlasを知った', own, other), True)
    eq('手がかり語なしは判定しない', S.is_misattributed('のんびり過ごした', own, other), False)


# ============================================================
# 5. 正規化・フロア・減衰
# ============================================================

def test_normalize_decay():
    print('[5] 正規化・フロア・減衰')
    es = [{'score': s, 'master': False, 'promise': False} for s in [90, 70, 50, 30, 10]]
    S.normalize_day(es, CFG)
    eq('最上位は500', es[0]['tier'], 500)
    eq('最下位は100', es[-1]['tier'], 100)

    # 甘口/辛口モデルの差が正規化で消えること（実測 flash 60 / gemma 85 の中央値差への対策）
    a = [{'score': s, 'master': False, 'promise': False} for s in [90, 80, 70, 60, 50]]
    b = [{'score': s, 'master': False, 'promise': False} for s in [50, 40, 30, 20, 10]]
    S.normalize_day(a, CFG); S.normalize_day(b, CFG)
    eq('スコア絶対値に依存しない', [x['tier'] for x in a], [x['tier'] for x in b])

    # 縮退ケース
    one = [{'score': 5, 'master': False, 'promise': False}]
    S.normalize_day(one, CFG)
    eq('1件の日', one[0]['tier'], 500)
    same = [{'score': 50, 'master': False, 'promise': False} for _ in range(4)]
    S.normalize_day(same, CFG)
    eq('全行同点', {x['tier'] for x in same}, {500})
    S.normalize_day([], CFG)  # 空で落ちない

    # コード側フロア（LLMの採点に依存させない）
    low = [{'score': 1, 'master': True, 'promise': False},
           {'score': 99, 'master': False, 'promise': False}]
    S.normalize_day(low, CFG)
    eq('ご主人様フロア', low[0]['tier'] >= CFG['floor_tier'], True)
    pr = [{'score': 1, 'master': False, 'promise': True}]
    S.normalize_day(pr, CFG)
    eq('約束フロア', pr[0]['tier'] >= CFG['floor_tier'], True)

    eq('減衰は単調減少',
       S.decayed_score(500, 1, 1.0) > S.decayed_score(500, 30, 1.0) > S.decayed_score(500, 200, 1.0),
       True)
    eq('日齢0で等値', S.decayed_score(500, 0, 1.0), 500.0)


# ============================================================
# 6. ストア（キー・置換・watermark・原子性）
# ============================================================

def test_store(tmp):
    print('[6] ストア')
    p = Path(tmp) / 'db.json'
    st = S.SummaryStore(p)
    st.load()
    eq('初期watermarkはNone', st.compressed_through, None)

    # 同一分に複数ターンがあっても別エントリとして残る（(date,turn_no) が主キー）
    st.replace_day('2026-07-08', [
        {'date': '2026-07-08', 'turn_no': 0, 'time': '10:39', 'text': 'A', 'score': 50},
        {'date': '2026-07-08', 'turn_no': 1, 'time': '10:39', 'text': 'B', 'score': 50},
    ])
    eq('同一分でも別エントリ', len(st.entries_of('2026-07-08')), 2)

    # 日単位の全置換（部分上書きはしない）
    st.replace_day('2026-07-08', [
        {'date': '2026-07-08', 'turn_no': 0, 'time': '10:39', 'text': 'C', 'score': 50}])
    eq('日単位置換', [e['text'] for e in st.entries_of('2026-07-08')], ['C'])

    st.init_watermark('2026-07-07')
    st.advance_watermark('2026-07-08')
    eq('watermark前進', st.compressed_through, '2026-07-08')
    st.advance_watermark('2026-07-05')
    eq('watermark後退拒否', st.compressed_through, '2026-07-08')
    st.init_watermark('2026-01-01')
    eq('init は既存を上書きしない', st.compressed_through, '2026-07-08')

    st.set_tx_stage('2026-07-09', 'generated')
    st.save()
    st2 = S.SummaryStore(p)
    st2.load()
    eq('永続化: watermark', st2.compressed_through, '2026-07-08')
    eq('永続化: tx段階', st2.tx_stage('2026-07-09'), 'generated')
    eq('永続化: エントリ', len(st2.entries_of('2026-07-08')), 1)
    check('原子書き込みの残骸なし', not (Path(str(p) + '.tmp')).exists())

    # 破損時はバックアップ世代から復旧する
    st2.save()   # bak1 が作られる
    p.write_text('{壊れたJSON', encoding='utf-8')
    st3 = S.SummaryStore(p)
    ok = st3.load()
    eq('破損からの復旧', (ok, st3.compressed_through), (True, '2026-07-08'))


# ============================================================
# 7. 履歴の日単位スライス
# ============================================================

def test_slice():
    print('[7] 日単位スライス')
    h = make_history('2026-07-08', 3) + make_history('2026-07-09', 2)
    days = S.slice_history_by_day(h, txt)
    eq('2日に分かれる', [d['date'] for d in days], ['2026-07-08', '2026-07-09'])
    eq('1日目のターン数', len(days[0]['turns']), 3)
    eq('2日目のターン数', len(days[1]['turns']), 2)
    eq('時刻抽出', days[0]['times'][0], '07:00')
    eq('範囲が連続', days[0]['end'], days[1]['start'])
    eq('末尾が履歴長', days[-1]['end'], len(h))
    # 深夜0時台は前日に繰り入れられる
    h2 = make_history('2026-07-08', 1)
    h2 += [user_msg('2026-07-09', '01:30'), asst('深夜の話')]
    eq('深夜は前日扱い', [d['date'] for d in S.slice_history_by_day(h2, txt)], ['2026-07-08'])
    # 日付不明のターンは直前の日を継承する
    h3 = make_history('2026-07-08', 1)
    h3 += [{'role': 'user', 'content': '日付なし' + S.LAYER0_MARKER}, asst('続き')]
    d3 = S.slice_history_by_day(h3, txt)
    eq('日付不明は継承', (len(d3), len(d3[0]['turns'])), (1, 2))


# ============================================================
# 8. 描画（TTL・dedup・予算・生涯カバー保証）
# ============================================================

def _store_with(tmp, days_spec):
    st = S.SummaryStore(Path(tmp) / f'v_{abs(hash(str(days_spec)))}.json')
    st.load()
    last = None
    for day, entries in days_spec:
        st.replace_day(day, entries)
        last = day
    st.init_watermark(last)
    return st


def _e(day, no, text, score=50, plan=False, master=False, promise=False):
    return {'date': day, 'turn_no': no, 'time': f'{7+no:02d}:00', 'text': text,
            'score': score, 'plan': plan, 'master': master, 'promise': promise,
            'anchors': [], 'fallback': False}


def test_render(tmp):
    print('[8] 描画')
    today = date(2026, 8, 31)

    # 日付見出しが付く / watermark より新しい日は出さない
    st = _store_with(tmp, [('2026-08-01', [_e('2026-08-01', 0, 'できごとA', 90)])])
    st.replace_day('2026-08-20', [_e('2026-08-20', 0, '履歴側の日', 90)])
    v = S.render_view(st, today, CFG, cnt, '【要約】')
    check('見出しが出る', '## 2026-08-01（土）' in v, v[:80])
    check('watermark外は出さない', '履歴側の日' not in v, v[:200])

    # 予定TTL: 期限内は出る、超えたら消える（データは残る）
    cfg = dict(CFG); cfg['plan_ttl_days'] = 3
    recent = (today - date.resolution * 0).isoformat()
    st2 = _store_with(tmp, [('2026-08-30', [_e('2026-08-30', 0, '【予定】明日やる', 90)])])
    v2 = S.render_view(st2, today, cfg, cnt, '')
    check('TTL内の予定は出る', '【予定】明日やる' in v2, v2[:100])
    st3 = _store_with(tmp, [('2026-06-01', [_e('2026-06-01', 0, '【予定】明日やる', 90, plan=True),
                                            _e('2026-06-01', 1, '通常の記憶', 80)])])
    v3 = S.render_view(st3, today, cfg, cnt, '')
    check('TTL超過の予定は消える', '【予定】' not in v3, v3[:150])
    eq('DBには残っている', len(st3.entries_of('2026-06-01')), 2)
    # planフィールドが欠けていても本文の接頭辞で判定する（古いデータ・手書きデータ対策）
    st3b = _store_with(tmp, [('2026-06-02', [_e('2026-06-02', 0, '【予定】明日やる', 90),
                                             _e('2026-06-02', 1, '通常の記憶', 80)])])
    v3b = S.render_view(st3b, today, cfg, cnt, '')
    check('planフラグ無しでもTTLが効く', '【予定】' not in v3b, v3b[:150])

    # dedup: 酷似行は畳まれる
    st4 = _store_with(tmp, [('2026-08-01', [
        _e('2026-08-01', 0, 'ブログを書いて公開した話', 90),
        _e('2026-08-01', 1, 'ブログを書いて公開した話です', 80),
        _e('2026-08-01', 2, '全然ちがう出来事の記録', 70)])])
    v4 = S.render_view(st4, today, CFG, cnt, '')
    eq('重複が畳まれる', v4.count('ブログを書いて公開'), 1)

    # 生涯カバー保証: 極小予算でも全ての日が1行ずつ残る（日が丸ごと消えない）
    spec = [(f'2026-0{m}-0{d}', [_e(f'2026-0{m}-0{d}', i, f'{m}月{d}日の記憶その{i}', 90 - i * 10)
                                 for i in range(5)])
            for m in (3, 4, 5) for d in (1, 2, 3)]
    st5 = _store_with(tmp, spec)
    tiny = dict(CFG); tiny['view_max_tokens'] = 200
    v5 = S.render_view(st5, today, tiny, cnt, '')
    shown_days = set(re.findall(r'## (\d{4}-\d{2}-\d{2})', v5))
    eq('全ての日が残る', len(shown_days), len(spec))
    for d, _ in spec:
        check(f'{d}に最低1行', v5.count(f'## {d}') == 1)

    # 予算が増えれば行数が増える（減る方向にはならない）
    rich = dict(CFG); rich['view_max_tokens'] = 100000
    v6 = S.render_view(st5, today, rich, cnt, '')
    check('予算増で情報量増', v6.count('\n- ') >= v5.count('\n- '),
          f'{v6.count(chr(10)+"- ")} vs {v5.count(chr(10)+"- ")}')

    # フロア付き（約束）は低スコアでも最低保証の1行に選ばれる
    st7 = _store_with(tmp, [('2026-05-01', [
        _e('2026-05-01', 0, '雑多な話', 99),
        _e('2026-05-01', 1, '明日会う約束をした', 1, promise=True)])])
    t7 = dict(CFG); t7['view_max_tokens'] = 60
    v7 = S.render_view(st7, today, t7, cnt, '')
    check('約束がフロアで生き残る', '約束' in v7, v7)

    # 空DBで落ちない
    st8 = S.SummaryStore(Path(tmp) / 'empty.json'); st8.load()
    eq('空DBは空文字', S.render_view(st8, today, CFG, cnt, '【要約】'), '')


# ============================================================
# 9. 圧縮パイプライン（ゲート・フォールバック）
# ============================================================

def test_pipeline(tmp):
    print('[9] パイプライン')
    prompts = ('P1', 'P2')
    hist = make_history('2026-07-08', 3, master_at=(1,), promise_at=(2,))
    days = S.slice_history_by_day(hist, txt)[0]

    # 正常系
    dc = S.DayCompressor(FakeLLM(default_script), CFG, prompts, txt)
    es = asyncio.run(dc.compress_day('2026-07-08', days['turns'], days['times']))
    eq('全ターンにエントリ', len(es), 3)
    eq('turn_noが連番', [e['turn_no'] for e in es], [0, 1, 2])
    eq('ご主人様判定', es[1]['master'], True)
    eq('約束判定', es[2]['promise'], True)
    eq('フォールバックなし', dc.stats['fallback'], 0)

    # LLM が例外 → 全行フォールバック（LLM不要・決定論）で必ず完遂する
    dc2 = S.DayCompressor(FakeLLM([RuntimeError('API down')]), CFG, prompts, txt)
    es2 = asyncio.run(dc2.compress_day('2026-07-08', days['turns'], days['times']))
    eq('LLM全滅でも全ターン返る', len(es2), 3)
    eq('全てフォールバック', all(e['fallback'] for e in es2), True)
    check('本文は柚月自身の発言', 'ターン0の内容' in es2[0]['text'], es2[0]['text'])

    # 空応答 → フォールバック
    dc3 = S.DayCompressor(FakeLLM(['', '']), CFG, prompts, txt)
    es3 = asyncio.run(dc3.compress_day('2026-07-08', days['turns'], days['times']))
    eq('空応答もフォールバック', all(e['fallback'] for e in es3), True)

    # 捏造名は破棄されフォールバックに落ちる
    def fabricate(messages, idx):
        return '\n'.join(f'{i+1}:50:Watsonという実在しない相手と話した' for i in range(3))
    dc4 = S.DayCompressor(FakeLLM(fabricate), CFG, prompts, txt)
    es4 = asyncio.run(dc4.compress_day('2026-07-08', days['turns'], days['times']))
    check('捏造名を弾く', dc4.stats['name_reject'] > 0 or dc4.stats['batch_reject'] > 0,
          str(dc4.stats))
    check('捏造は本文に残らない', all('Watson' not in e['text'] for e in es4),
          [e['text'] for e in es4])

    # 破棄率が閾値超 → バッチごと不採用
    eq('バッチ異常でフォールバック', all(e['fallback'] for e in es4), True)

    # 【予定】の検出
    def planned(messages, idx):
        return '\n'.join(f'{i+1}:50:【予定】明日やる。Tiramisuと会う。' for i in range(3))
    dc5 = S.DayCompressor(FakeLLM(planned), CFG, prompts, txt)
    es5 = asyncio.run(dc5.compress_day('2026-07-08', days['turns'], days['times']))
    eq('予定フラグ', [e['plan'] for e in es5], [True, True, True])


# ============================================================
# 10. トランザクション（段階再開・冪等）
# ============================================================

class FakeMemory:
    def __init__(self, root):
        self.workspace = Path(root)
        self.writes = []

    def read_file(self, rel):
        p = self.workspace / rel
        return p.read_text(encoding='utf-8') if p.exists() else None

    def write_file(self, rel, content):
        self.writes.append(rel)
        p = self.workspace / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding='utf-8')
        return 'ok'


class FakeRag:
    def __init__(self):
        self.docs = {}

    def add(self, coll, doc, meta, doc_id=None):
        self.docs[doc_id or f'auto{len(self.docs)}'] = (coll, doc, meta)
        return [doc_id]


class FakeLethe:
    def __init__(self, fail_times=0):
        self.calls = []
        self.fail_times = fail_times

    async def compress_day(self, content, d, ws):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError('LETHE失敗')
        self.calls.append(d.isoformat())


def test_transaction(tmp):
    print('[10] トランザクション')
    day = '2026-07-08'
    entries = [_e(day, 0, '記憶A', 90), _e(day, 1, '記憶B', 50)]

    st = S.SummaryStore(Path(tmp) / 'tx.json'); st.load()
    mem, rag, lethe = FakeMemory(Path(tmp) / 'ws1'), FakeRag(), FakeLethe()
    tx = S.DayTransaction(st, mem, rag, lethe, str(mem.workspace))
    ok = asyncio.run(tx.run(day, entries))
    eq('正常完了', ok, True)
    eq('段階はlethe_done', st.tx_stage(day), 'lethe_done')
    eq('watermarkはまだ動かない', st.compressed_through, None)
    tx.commit(day)
    eq('commitでwatermark前進', st.compressed_through, day)
    eq('commitでtxクリア', st.tx_stage(day), None)
    # 書き込み先が「実際の論理日」になる（A-2 の日付ずれが構造的に解消）
    eq('ログの日付', mem.writes, [f'logs/summary/{day}.md'])
    eq('RAGの決定論ID', list(rag.docs), [f'daily_summary_{day}'])
    eq('LETHEの日付', lethe.calls, [day])

    # LETHE が失敗したら未完のまま止まり、watermark は動かない
    st2 = S.SummaryStore(Path(tmp) / 'tx2.json'); st2.load()
    mem2, rag2 = FakeMemory(Path(tmp) / 'ws2'), FakeRag()
    tx2 = S.DayTransaction(st2, mem2, rag2, FakeLethe(fail_times=1), str(mem2.workspace))
    ok2 = asyncio.run(tx2.run(day, entries))
    eq('LETHE失敗で未完', ok2, False)
    eq('段階はrag_addedで停止', st2.tx_stage(day), 'rag_added')
    eq('watermark据え置き', st2.compressed_through, None)

    # 再開: 未完段階からだけ実行され、済んだ段階は繰り返さない（冪等）
    lethe3 = FakeLethe()
    tx3 = S.DayTransaction(st2, mem2, rag2, lethe3, str(mem2.workspace))
    ok3 = asyncio.run(tx3.run(day, entries))
    eq('再開で完了', ok3, True)
    eq('ログは重複書き込みしない', len(mem2.writes), 1)
    eq('RAGも重複しない', len(rag2.docs), 1)
    eq('LETHEは1回だけ', lethe3.calls, [day])

    # 同じ日をもう一度通しても重複しない（全段階の冪等性）
    st4 = S.SummaryStore(Path(tmp) / 'tx4.json'); st4.load()
    mem4, rag4, lethe4 = FakeMemory(Path(tmp) / 'ws4'), FakeRag(), FakeLethe()
    tx4 = S.DayTransaction(st4, mem4, rag4, lethe4, str(mem4.workspace))
    asyncio.run(tx4.run(day, entries)); tx4.commit(day)
    st4.clear_tx(day)
    asyncio.run(tx4.run(day, entries))     # 再処理
    eq('エントリは重複しない', len(st4.entries_of(day)), 2)
    eq('RAGは上書き', len(rag4.docs), 1)


# ============================================================
# 12. logs/summary への書き込みが既存内容を壊さないこと（レビュー指摘の修正確認）
# ============================================================

def test_daily_log_preserves_v1(tmp):
    print('[12] logs/summary の非破壊書き込み')
    day = '2026-08-05'
    ws = Path(tmp) / 'ws_v1'
    mem = FakeMemory(ws)
    # v1 時代の記録が既に存在する状況を再現（実データでは 2026-08-05.md が373行ある）
    v1_body = f'# {day} の記憶\n\n## コンテキスト圧縮 (08:33 JST)\n\nv1が書いた大事な記録\n'
    mem.write_file(f'logs/summary/{day}.md', v1_body)
    mem.writes.clear()

    st = S.SummaryStore(Path(tmp) / 'log.json'); st.load()
    st.replace_day(day, [_e(day, 0, 'v2の要約A'), _e(day, 1, 'v2の要約B')])
    tx = S.DayTransaction(st, mem, None, None, str(ws))
    tx._write_daily_log(day)

    got = mem.read_file(f'logs/summary/{day}.md')
    check('v1の記録が残る', 'v1が書いた大事な記録' in got, got[:200])
    check('v1の見出しも残る', '## コンテキスト圧縮 (08:33 JST)' in got, got[:200])
    check('v2の要約が入る', 'v2の要約A' in got and 'v2の要約B' in got, got[:200])

    # 再実行してもv2セクションは増えない（冪等）＆v1は無傷のまま
    tx._write_daily_log(day)
    got2 = mem.read_file(f'logs/summary/{day}.md')
    eq('v2セクションは1つ', got2.count(S.V2_SECTION_BEGIN), 1)
    eq('v1の記録も1つ', got2.count('v1が書いた大事な記録'), 1)

    # 内容が変わったら差し替わる（古い行は残らない）
    st.replace_day(day, [_e(day, 0, 'v2の要約C')])
    tx._write_daily_log(day)
    got3 = mem.read_file(f'logs/summary/{day}.md')
    check('新しい内容に置換', 'v2の要約C' in got3 and 'v2の要約A' not in got3, got3[:200])
    check('置換後もv1は無傷', 'v1が書いた大事な記録' in got3, got3[:200])

    # ファイルが無い日は新規作成される
    day2 = '2026-08-06'
    st.replace_day(day2, [_e(day2, 0, '新規の日')])
    tx._write_daily_log(day2)
    got4 = mem.read_file(f'logs/summary/{day2}.md')
    check('新規作成', got4 and '新規の日' in got4 and got4.startswith(f'# {day2}'), (got4 or '')[:80])

    # LETHE/RAG に渡す本文にはマーカーが入らない
    body = tx._day_body(day2)
    check('本文にマーカーなし', S.V2_SECTION_BEGIN not in body and '#' not in body, body)

    # 読み取りが例外を投げたら書かずに伝播させる（既存内容を潰さない）
    class LockedMemory(FakeMemory):
        def read_file(self, rel):
            raise PermissionError('ロック中')

    locked = LockedMemory(ws)
    locked.write_file(f'logs/summary/{day}.md', v1_body)   # 既存内容を用意
    locked.writes.clear()
    tx_locked = S.DayTransaction(st, locked, None, None, str(ws))
    raised = False
    try:
        tx_locked._write_daily_log(day)
    except PermissionError:
        raised = True
    eq('読み取り例外は伝播する', raised, True)
    eq('例外時は書き込まない', locked.writes, [])
    # トランザクション全体としては未完扱いになる（翌日再試行される）
    st_l = S.SummaryStore(Path(tmp) / 'locked.json'); st_l.load()
    st_l.replace_day(day, [_e(day, 0, 'x')])
    ok = asyncio.run(S.DayTransaction(st_l, locked, None, None, str(ws)).run(day, [_e(day, 0, 'x')]))
    eq('例外でトランザクション未完', ok, False)
    check('段階はlog_writtenまで進まない',
          st_l.tx_stage(day) in (None, 'generated'), st_l.tx_stage(day))


# ============================================================
# 14. ビューと v1 要約の併記（レビュー2回目の指摘）
# ============================================================

class _StubCtx:
    """build_context の要約挿入部だけを再現する最小スタブ。"""

    def __init__(self, view='', l1='', l2=''):
        self._summary_view, self.summary_layer1, self.summary_layer2 = view, l1, l2

    def compose(self, heading='【要約】'):
        parts = []
        if self._summary_view:
            parts.append(self._summary_view)
        v1 = ''
        if self.summary_layer2:
            v1 += self.summary_layer2
        if self.summary_layer1:
            if v1:
                v1 += '\n\n'
            v1 += self.summary_layer1
        if v1:
            parts.append(('' if self._summary_view else heading + '\n') + v1)
        return '\n\n'.join(parts) if parts else None


def test_store_recovery(tmp):
    print('[15] ストアの復旧（消失・全滅）')
    p = Path(tmp) / 'rec.json'
    st = S.SummaryStore(p); st.load()
    st.replace_day('2026-07-08', [_e('2026-07-08', 0, '大事な記憶')])
    st.init_watermark('2026-07-08')
    st.save(); st.save()          # bak1 を作る
    check('bak1が存在', Path(str(p) + '.bak1').exists())

    # 破損: バックアップから復旧できる
    body = p.read_text(encoding='utf-8')
    p.write_text('{壊れ', encoding='utf-8')
    a = S.SummaryStore(p)
    eq('破損から復旧', (a.load(), a.compressed_through, len(a.entries)),
       (True, '2026-07-08', 1))

    # 消失: バックアップが健在なら復旧する（壊れたときだけ救えるのは片手落ち）
    p.write_text(body, encoding='utf-8')
    os.remove(p)
    b = S.SummaryStore(p)
    eq('消失から復旧', (b.load(), b.compressed_through, len(b.entries)),
       (True, '2026-07-08', 1))
    check('復旧元を記録', b._recovered_from is not None, str(b._recovered_from))

    # 全世代が読めない: 空で上書きせず書き込みを止める
    p.write_text(body, encoding='utf-8')
    st2 = S.SummaryStore(p); st2.load(); st2.save()
    for f in [p] + st2._backups():
        if f.exists():
            f.write_text('{全滅', encoding='utf-8')
    c = S.SummaryStore(p)
    eq('全滅時はFalse', c.load(), False)
    eq('読み取り専用になる', c._readonly, True)
    before = p.read_text(encoding='utf-8')
    c.replace_day('2026-07-09', [_e('2026-07-09', 0, '空データ')])
    c.save()
    eq('空で上書きしない', p.read_text(encoding='utf-8'), before)

    # 初回起動（本体もバックアップも無い）は素直に False
    fresh = S.SummaryStore(Path(tmp) / 'brand_new.json')
    eq('初回はFalse', fresh.load(), False)
    eq('初回は書き込める', fresh._readonly, False)


def test_backup_rotation(tmp):
    print('[16] バックアップ世代の消費')
    p = Path(tmp) / 'rot.json'
    st = S.SummaryStore(p); st.load()
    st.replace_day('2026-07-08', [_e('2026-07-08', 0, '初日の記憶')])
    st.save()
    first = p.read_text(encoding='utf-8')
    # 1日のうちに何度 save しても世代は1つしか進まない
    for i in range(20):
        st.replace_day('2026-07-08', [_e('2026-07-08', 0, f'更新{i}')])
        st.save()
    bak1 = Path(str(p) + '.bak1')
    bak2 = Path(str(p) + '.bak2')
    check('bak1は存在', bak1.exists())
    check('bak2まで押し出されない', not bak2.exists() or '初日の記憶' in bak2.read_text(encoding='utf-8'),
          'bak2が当日のスナップショットで埋まっている')


def test_total_loss_detection(tmp):
    print('[18] 全損と初回起動の区別')
    # 初回起動: logs/summary に v2 の形跡が無い
    root = Path(tmp) / 'fresh'
    (root / 'logs' / 'summary').mkdir(parents=True)
    s1 = S.SummaryStore(root / 'db.json')
    s1.set_summary_log_dir(root / 'logs' / 'summary')
    eq('初回はFalse', s1.load(), False)
    eq('初回は書き込める', s1._readonly, False)

    # 全損: v2 セクションを含む md が残っている＝過去に稼働していた
    root2 = Path(tmp) / 'lost'
    sd = root2 / 'logs' / 'summary'
    sd.mkdir(parents=True)
    (sd / '2026-07-08.md').write_text(
        f'# 2026-07-08 の記憶\n\n{S.V2_SECTION_BEGIN}\n- 記憶\n{S.V2_SECTION_END}\n',
        encoding='utf-8')
    s2 = S.SummaryStore(root2 / 'db.json')
    s2.set_summary_log_dir(sd)
    eq('全損もFalseを返す', s2.load(), False)
    eq('全損は書き込み停止', s2._readonly, True)
    s2.replace_day('2026-07-09', [_e('2026-07-09', 0, '空データ')])
    s2.save()
    check('全損時はファイルを作らない', not (root2 / 'db.json').exists())

    # v1 時代の md しか無い場合は初回扱い（マーカーが無い）
    root3 = Path(tmp) / 'v1only'
    sd3 = root3 / 'logs' / 'summary'
    sd3.mkdir(parents=True)
    (sd3 / '2026-07-08.md').write_text('# 2026-07-08 の記憶\n\n## コンテキスト圧縮\n- v1の行\n',
                                       encoding='utf-8')
    s3 = S.SummaryStore(root3 / 'db.json')
    s3.set_summary_log_dir(sd3)
    s3.load()
    eq('v1のmdだけなら初回扱い', s3._readonly, False)


def test_guard_baseline_outside_db(tmp):
    print('[19] ガード基準値をDBの外に置く')
    d = Path(tmp) / 'gb'
    d.mkdir()
    gb = S.GuardBaseline(d / 'guard.json')
    eq('初期は空', (gb.oldest, gb.days), (None, None))
    gb.update('2026-07-08', 13)
    eq('更新される', (gb.oldest, gb.days), ('2026-07-08', 13))
    check('別ファイルに保存', (d / 'guard.json').exists())
    # 別インスタンスで読み直せる（プロセス再起動をまたぐ）
    eq('永続化', (S.GuardBaseline(d / 'guard.json').oldest,
                  S.GuardBaseline(d / 'guard.json').days), ('2026-07-08', 13))
    # DB が巻き戻っても基準値は巻き戻らない＝ガードが働く
    db = S.SummaryStore(d / 'db.json'); db.load()
    db.replace_day('2026-07-08', [_e('2026-07-08', 0, 'x')])
    db.meta['compressed_through'] = '2026-07-08'
    cur = len({e['date'] for e in db.entries})
    eq('DBは1日に縮んでいる', cur, 1)
    check('基準値は13日のまま', gb.days == 13)
    check('急減として検知できる', cur < gb.days * 0.9)
    # 破損しても落ちない
    (d / 'guard.json').write_text('{壊れ', encoding='utf-8')
    eq('破損時は空で再開', S.GuardBaseline(d / 'guard.json').oldest, None)


def test_no_rotate_after_recovery(tmp):
    print('[20] 復旧直後は世代を回さない')
    p = Path(tmp) / 'rr.json'
    st = S.SummaryStore(p); st.load()
    st.replace_day('2026-07-08', [_e('2026-07-08', 0, '健全な記憶')])
    st.save(); st._last_backup_day = '2000-01-01'; st.save()   # bak1 に健全な内容
    bak1 = Path(str(p) + '.bak1')
    check('bak1が健全', '健全な記憶' in bak1.read_text(encoding='utf-8'))
    # 本体が破損 → 復旧 → 保存
    p.write_text('{壊れ', encoding='utf-8')
    r = S.SummaryStore(p); r.load()
    eq('復旧できた', len(r.entries), 1)
    r._last_backup_day = '2000-01-01'      # 世代交代の条件を満たしておく
    r.save()
    check('bak1に破損が入っていない', '壊れ' not in bak1.read_text(encoding='utf-8'),
          bak1.read_text(encoding='utf-8')[:40])
    check('本体は健全に書き直された', '健全な記憶' in p.read_text(encoding='utf-8'))


def test_fallback_day_detection(tmp):
    print('[21] フォールバックの日の特定')
    st = S.SummaryStore(Path(tmp) / 'fb.json'); st.load()

    def mk(day, n, fb_count):
        es = []
        for i in range(n):
            e = _e(day, i, f'{day}の{i}')
            e['fallback'] = i < fb_count
            es.append(e)
        return es

    st.replace_day('2026-07-08', mk('2026-07-08', 10, 10))   # 全滅
    st.replace_day('2026-07-09', mk('2026-07-09', 10, 6))    # 過半数
    st.replace_day('2026-07-10', mk('2026-07-10', 10, 2))    # 少数
    st.replace_day('2026-07-11', mk('2026-07-11', 10, 0))    # 正常
    st.init_watermark('2026-07-11')

    # agent.resummarize_fallback_days_v2 と同じ抽出条件
    by_day = {}
    for e in st.entries:
        d = e['date']
        n, fb = by_day.get(d, (0, 0))
        by_day[d] = (n + 1, fb + (1 if e.get('fallback') else 0))
    targets = sorted(d for d, (n, fb) in by_day.items() if n and fb / n >= 0.5)
    eq('半数以上がフォールバックの日を拾う', targets, ['2026-07-08', '2026-07-09'])
    check('正常な日は対象外', '2026-07-11' not in targets)
    check('少数フォールバックは対象外', '2026-07-10' not in targets)
    # 作り直し後は対象から外れる
    st.replace_day('2026-07-08', mk('2026-07-08', 10, 0))
    by_day2 = {}
    for e in st.entries:
        d = e['date']
        n, fb = by_day2.get(d, (0, 0))
        by_day2[d] = (n + 1, fb + (1 if e.get('fallback') else 0))
    t2 = sorted(d for d, (n, fb) in by_day2.items() if n and fb / n >= 0.5)
    eq('作り直し後は対象外になる', t2, ['2026-07-09'])


def test_agent_integration(tmp):
    """agent.py 側の統合コードを実際に呼ぶ。

    ここまでのテストは core/summary_v2.py の関数を直接叩いていたため、
    agent.py の統合部分は一度も実行されていなかった。その結果
    date 未インポートによる NameError（v2 有効化の初回に必ず落ちる）を
    取り逃していた。統合経路も必ず1度は通すこと。
    """
    print('[23] agent 統合コードの実行')
    import core.agent as A
    from memory.manager import MemoryManager

    ws = Path(tmp) / 'agent_ws'
    (ws / 'logs' / 'summary').mkdir(parents=True)
    mm = MemoryManager(str(ws))
    ag = A.Agent.__new__(A.Agent)      # 依存を張らずメソッドだけ使う
    ag.memory = mm
    probs = []
    ag._record_compression_problem = lambda m: probs.append(m)
    ag._v2_config = lambda: S.get_config({})

    # watermark 初期化（最古日の前日に置く＝途中で切れた日を欠落させない）
    store = S.SummaryStore(ws / 'db.json')
    store.set_summary_log_dir(ws / 'logs' / 'summary')
    store.load()
    ag._v2_init_watermark(store, [{'date': '2026-07-08'}, {'date': '2026-07-09'}])
    eq('watermarkは最古日の前日', store.compressed_through, '2026-07-07')
    # 既に設定済みなら上書きしない
    ag._v2_init_watermark(store, [{'date': '2026-07-08'}])
    eq('再初期化しない', store.compressed_through, '2026-07-07')
    eq('履歴と揃っていれば問題なし', len(probs), 0)
    # watermark が履歴の最古日以降を指す＝ビューと履歴が重なる → 問題として記録（巻き戻さない）
    ag._v2_init_watermark(store, [{'date': '2026-07-01'}])
    eq('重なりを検知', len(probs), 1)
    check('案内に採用スクリプト名', 'summary_v2_adopt_shadow' in probs[0], probs[0][:80])
    eq('自動では巻き戻さない', store.compressed_through, '2026-07-07')
    # 履歴が空でも落ちない
    ag._v2_init_watermark(S.SummaryStore(ws / 'e.json'), [])

    # シャドーが先回りで作った日は LLM を呼ばず再利用する（切替後の追加コストをゼロにする根拠）
    store.replace_day('2026-07-20', [_e('2026-07-20', i, f'先回り{i}') for i in range(3)])
    check('行数が足りれば再利用', ag._v2_reusable_entries(store, '2026-07-20', 3) is not None)
    check('ターン数より少なければ作り直し', ag._v2_reusable_entries(store, '2026-07-20', 4) is None)
    check('無い日は作り直し', ag._v2_reusable_entries(store, '2026-07-21', 1) is None)

    # 生ログからのターン復元（フォールバック日の作り直しで使う）
    raw = ws / 'logs' / 'full'
    raw.mkdir(parents=True)
    import json as _j
    lines = [
        {'timestamp': '2026-07-08 01:30:00', 'type': 'user_message', 'content': '深夜'},
        {'timestamp': '2026-07-08 07:00:00', 'type': 'user_message', 'content': '朝'},
        {'timestamp': '2026-07-08 07:00:10', 'type': 'assistant_message', 'content': '返事'},
        {'timestamp': '2026-07-08 07:00:20', 'type': 'tool_result', 'content': '結果'},
        # OpenClaw 期の心拍: 情報ゼロなので除かれる
        {'timestamp': '2026-07-08 12:00:00', 'type': 'user_message',
         'content': 'Read HEARTBEAT.md if it exists. If nothing needs attention, reply HEARTBEAT_OK.'},
        {'timestamp': '2026-07-08 12:00:03', 'type': 'assistant_message', 'content': 'HEARTBEAT_OK'},
        # 心拍の問いに本文で答えた場合は残す
        {'timestamp': '2026-07-08 13:00:00', 'type': 'user_message',
         'content': 'Read HEARTBEAT.md if it exists. reply HEARTBEAT_OK.'},
        {'timestamp': '2026-07-08 13:00:05', 'type': 'assistant_message', 'content': 'Moltbookに投稿しました'},
        {'timestamp': '2026-07-08 20:00:00', 'type': 'user_message', 'content': '夜'},
    ]
    (raw / '2026-07-08_full.jsonl').write_text(
        '\n'.join(_j.dumps(x, ensure_ascii=False) for x in lines), encoding='utf-8')
    turns, times = ag._v2_turns_from_raw_log('2026-07-08')
    eq('3時前と心拍専用を除いて3ターン', len(turns), 3)
    # 本体は core.summary_v2.turns_from_raw_log（配布版の作り直しスクリプトが直接使う）
    t2, tm2 = S.turns_from_raw_log(ws, '2026-07-08')
    eq('モジュール関数でも同じ', (len(t2), tm2), (3, times))
    eq('ログの日付一覧', S.list_log_days(ws), ['2026-07-08'])
    eq('ログが無い workspace は空', S.list_log_days(ws / 'none'), [])
    eq('時刻が取れる', times, ['07:00', '13:00', '20:00'])
    eq('ターン内の構成', [m['role'] for m in turns[0]], ['user', 'assistant', 'tool'])
    check('user冒頭に日付が付く', turns[0][0]['content'].startswith('2026-07-08 07:00'),
          turns[0][0]['content'][:30])
    # ログが無い日は空で返る（例外を投げない）
    eq('ログ無しは空', ag._v2_turns_from_raw_log('2020-01-01'), ([], []))


def test_orphan_turns_not_lost(tmp):
    """先頭の日付不明ターンが要約されずに削除されないこと（Codex指摘1）。"""
    print('[24] 日付不明の先頭ターンを落とさない')
    M = S.LAYER0_MARKER
    h = [{'role': 'user', 'content': '日付の読めない先頭ターン\n' + M},
         asst('この内容も要約されるべき'),
         {'role': 'user', 'content': '2026-07-09 07:00\n晴れ\n' + M},
         asst('2日目の発言')]
    days = S.slice_history_by_day(h, txt)
    eq('1日にまとまる', len(days), 1)
    eq('先頭から始まる', days[0]['start'], 0)
    eq('孤児ターンも含む', len(days[0]['turns']), 2)
    eq('削除範囲は全体', days[0]['end'], len(h))
    # 削除範囲のターン数と要約対象のターン数が一致する（不変条件が成立する）
    dropped = sum(1 for m in h[:days[0]['end']] if m.get('role') == 'user')
    eq('範囲とターン数が一致', dropped, len(days[0]['turns']))

    # 日付不明が2件以上でも全部拾う
    h2 = [{'role': 'user', 'content': '不明1\n' + M}, asst('a'),
          {'role': 'user', 'content': '不明2\n' + M}, asst('b'),
          {'role': 'user', 'content': '2026-07-09 07:00\n' + M}, asst('c')]
    d2 = S.slice_history_by_day(h2, txt)
    eq('複数の孤児も合流', len(d2[0]['turns']), 3)
    eq('範囲は先頭から', d2[0]['start'], 0)

    # 通常ケース（先頭に日付がある）は従来通り
    h3 = make_history('2026-07-08', 2) + make_history('2026-07-09', 2)
    d3 = S.slice_history_by_day(h3, txt)
    eq('通常は2日', [d['date'] for d in d3], ['2026-07-08', '2026-07-09'])
    eq('通常の開始位置', d3[0]['start'], 0)


def test_degraded_flag(tmp):
    """パス2が落ちた行に印が付き、作り直し対象になること（Codex指摘13）。"""
    print('[25] パス2失敗の行に印を付ける')
    prompts = ('P1', 'P2')
    hist = make_history('2026-07-08', 3)
    day = S.slice_history_by_day(hist, txt)[0]

    # パス1は成功、パス2だけ空を返す（パス2は [手がかり語] が付いているので見分けられる。
    # 呼び出し回数の偶奇で見分けると、1ターンずつ呼ぶ隔離モードで崩れる）
    def script(messages, idx):
        return '' if '[手がかり語]' in messages[1]['content'] else default_script(messages, idx)

    dc = S.DayCompressor(FakeLLM(script), CFG, prompts, txt)
    es = asyncio.run(dc.compress_day('2026-07-08', day['turns'], day['times']))
    eq('全ターン残る', len(es), 3)
    eq('degradedが立つ', all(e['degraded'] for e in es), True)
    eq('fallbackではない', any(e['fallback'] for e in es), False)
    check('統計に出る', dc.stats['pass2_missing'] == 3, str(dc.stats))

    # 両パス成功なら degraded は立たない
    dc2 = S.DayCompressor(FakeLLM(default_script), CFG, prompts, txt)
    es2 = asyncio.run(dc2.compress_day('2026-07-08', day['turns'], day['times']))
    eq('正常時は印なし', any(e['degraded'] for e in es2), False)

    # 作り直し対象の判定に degraded が含まれる
    by_day = {}
    for e in es:
        n, bad = by_day.get(e['date'], (0, 0))
        by_day[e['date']] = (n + 1, bad + (1 if (e.get('fallback') or e.get('degraded')) else 0))
    targets = [d for d, (n, bad) in by_day.items() if n and bad / n >= 0.5]
    eq('作り直し対象になる', targets, ['2026-07-08'])


def test_budget_hard_check(tmp):
    """加算近似で超過しても最終文字列で収まること（Codex指摘17）。"""
    print('[26] 予算の最終検算')
    today = date(2026, 8, 31)
    spec = []
    for i in range(12):
        d = f'2026-06-{i+1:02d}'
        spec.append((d, [_e(d, j, f'{d}の{j}番目、{"あいうえおかきくけこ"[j % 10]}に関する出来事', 90 - j)
                         for j in range(6)]))
    st = _store_with(tmp, spec)
    # 実トークナイザで検算（テスト用の len ではなく本物）
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from core.tokens import count_text_tokens as ct
    for budget in (400, 800, 1500, 3000):
        c = dict(CFG); c['view_max_tokens'] = budget
        v = S.render_view(st, today, c, ct, '【これまでの会話の要約】')
        if v:
            check(f'予算{budget}を超えない', ct(v) <= budget, f'{ct(v)} > {budget}')
            eq(f'予算{budget}で全日が残る',
               len(set(re.findall(r'## (\d{4}-\d{2}-\d{2})', v))), len(spec))


def test_lethe_missing_recorded(tmp):
    """LETHE が使えないとき、欠落した日が記録されること（Codex指摘6）。"""
    print('[27] LETHE欠落の記録')
    day = '2026-07-08'
    st = S.SummaryStore(Path(tmp) / 'lm.json'); st.load()
    mem = FakeMemory(Path(tmp) / 'ws_lm')
    tx = S.DayTransaction(st, mem, None, None, str(mem.workspace))   # lethe=None
    ok = asyncio.run(tx.run(day, [_e(day, 0, '記憶')]))
    eq('圧縮自体は完了する', ok, True)
    eq('欠落日が記録される', st.meta.get('lethe_missing_days'), [day])
    # 同じ日を再処理しても重複しない
    st.clear_tx(day)
    asyncio.run(tx.run(day, [_e(day, 0, '記憶')]))
    eq('重複記録しない', st.meta.get('lethe_missing_days'), [day])


def test_decay_curve(tmp):
    """双曲減衰と、起点がビュー最新日であること。"""
    print('[28] 減衰カーブ')
    # 最新日（rank=1）は減衰なし
    eq('rank=1は満額', S.decay_factor(1, 0.05), 1.0)
    eq('rank=0でも満額', S.decay_factor(0, 0.05), 1.0)
    # 単調減少
    fs = [S.decay_factor(n, 0.05) for n in (1, 10, 50, 140, 365)]
    check('単調減少', all(a > b for a, b in zip(fs, fs[1:])), str(fs))
    # 係数が大きいほど速く落ちる
    check('係数で速さが変わる', S.decay_factor(50, 0.2) < S.decay_factor(50, 0.05))
    # 対数と違い、遠い側でもちゃんと差がつく（これが採用理由）
    r = S.decay_factor(60, 0.05) / S.decay_factor(190, 0.05)
    check('60日と190日で明確な差', r > 2.5, f'比={r:.2f}')

    # 起点はビュー最新日。today が動いても行数は変わらない
    today = date(2026, 8, 31)
    # dedup に潰されないよう、互いに似ていない文面を用意する
    topics = ['星の観測記録をまとめた', '味噌汁の塩加減で失敗', '古い橋を撮影しに行った',
              'ピアノの調律師が来訪', '猫が枕元で眠っていた', '登山靴の底を張り替え',
              '林檎の品種を食べ比べ', '短歌を三首ほど推敲', '路面電車の音を録音',
              '毛糸の色を選び直した', '将棋の定跡を並べた', '雨樋の落ち葉を掃除',
              '硯で墨をすってみた', '万年筆のインクが切れ', '窓辺の多肉が増えた',
              '古書店で棚を眺めた', '鰹節を薄く削る練習', '銅版画の腐食に失敗',
              '自転車の空気を入れた', '望遠鏡の三脚が届いた']
    spec = []
    for i in range(10):
        d = f'2026-06-{i+1:02d}'
        spec.append((d, [_e(d, j, topics[j], 90 - j) for j in range(20)]))
    st = _store_with(tmp, spec)
    cfg = dict(CFG); cfg['view_max_tokens'] = 100000
    v1 = S.render_view(st, today, cfg, cnt, '')
    v2 = S.render_view(st, date(2027, 1, 1), cfg, cnt, '')   # 4ヶ月後に描画
    eq('todayが動いても同じ', v1.count('\n- '), v2.count('\n- '))

    # 新しい日ほど行数が多い（単調な傾斜）
    counts = []
    for d, _ in spec:
        m = re.search(rf'## {d}（.）\n((?:- .*\n)*)', v1)
        counts.append(len([l for l in m.group(1).splitlines() if l.startswith('- ')]) if m else 0)
    check('新しい日ほど厚い', counts[-1] > counts[0], f'{counts[0]} → {counts[-1]}')
    check('逆転しない', all(a <= b for a, b in zip(counts, counts[1:])), str(counts))


def test_line_ratio(tmp):
    """line_ratio が行数の基準を縮め、減衰の形は変えないこと。"""
    print('[29] 行数の基準（line_ratio）')
    # dedup に潰されない、互いに似ていない文面（潰れると比率の検証にならない）
    topics = ['星の観測記録をまとめた', '味噌汁の塩加減で失敗', '古い橋を撮影しに行った',
              'ピアノの調律師が来訪', '猫が枕元で眠っていた', '登山靴の底を張り替え',
              '林檎の品種を食べ比べ', '短歌を三首ほど推敲', '路面電車の音を録音',
              '毛糸の色を選び直した', '将棋の定跡を並べた', '雨樋の落ち葉を掃除',
              '硯で墨をすってみた', '万年筆のインクが切れ', '窓辺の多肉が増えた',
              '古書店で棚を眺めた', '鰹節を薄く削る練習', '銅版画の腐食に失敗',
              '自転車の空気を入れた', '望遠鏡の三脚が届いた']
    spec = [('2026-06-01', [_e('2026-06-01', j, topics[j], 90 - j) for j in range(20)])]
    st = _store_with(tmp, spec)
    today = date(2026, 8, 31)

    def lines(ratio, floor=1):
        # 比率そのものを見たいので、既定のフロア(5)には邪魔をさせない
        cfg = dict(CFG)
        cfg['view_max_tokens'] = 100000
        cfg['line_ratio'] = ratio
        cfg['day_floor_lines'] = floor
        return S.render_view(st, today, cfg, cnt, '').count(chr(10) + '- ')

    eq('1.0なら全行', lines(1.0), 20)
    eq('0.5なら半分', lines(0.5), 10)
    eq('0.25なら4分の1', lines(0.25), 5)
    # 縮めてもフロアは割らない（生涯カバーの不変条件）
    # 比率がどれだけ小さくてもフロアは割らない（生涯カバーの不変条件）
    got = lines(0.01, floor=CFG['day_floor_lines'])
    eq('フロアまで戻す', got, CFG['day_floor_lines'])


def test_pass2_isolated_and_per_line_fallback():
    """パス2の隔離（1ターン1呼び出し）と、束ごと破棄の限定。"""
    print('[32] パス2の隔離と1行ずつのフォールバック')
    cfg = dict(CFG); cfg['pass2_isolated'] = True
    seen = []

    def script(messages, idx):
        user = messages[1]['content']
        seen.append(user)
        n = len(re.findall(r'--- ターン\d+ ', user))
        return '\n'.join(f'{i+1}:{50+i}:ターン{i}の要約。Tiramisuとやり取りし満足した。〔Tiramisu〕' for i in range(n))
    llm = FakeLLM(script)
    dc = S.DayCompressor(llm, cfg, ('p1', 'p2'), txt)
    h = make_history('2026-08-01', 5)
    turns = [h[i:i + 2] for i in range(0, 10, 2)]
    out = asyncio.run(dc.compress_day('2026-08-01', turns, [f'{7+i:02d}:00' for i in range(5)]))
    eq('5行できる', len(out), 5)
    eq('呼び出しは パス1が1回 + パス2が5回', len(seen), 6)
    p2 = [u for u in seen if '[手がかり語]' in u]
    eq('パス2は5回', len(p2), 5)
    check('パス2は各回1ターンしか見ない', all(len(re.findall(r'--- ターン\d+ ', u)) == 1 for u in p2))
    eq('フォールバック無し', dc.stats['fallback'], 0)

    # 5行中3行が名前の検査で落ちても、残り2行は生き残る（束ごと捨てない）
    def script2(messages, idx):
        user = messages[1]['content']
        n = len(re.findall(r'--- ターン\d+ ', user))
        if '[手がかり語]' not in user:          # パス1
            return '\n'.join(f'{i+1}:50:要約{i}〔〕' for i in range(n))
        # パス2: 1ターンずつ。ターン0〜2 は本文に無い名前 Watson を書く
        m = re.search(r'ターン1 \((\d\d):00\)', user)
        hour = int(m.group(1)) if m else 7
        return '1:50:Watsonと話して満足した。' if hour - 7 < 3 else '1:50:ターンの要約。Tiramisuと話して満足した。'
    dc2 = S.DayCompressor(FakeLLM(script2), cfg, ('p1', 'p2'), txt)
    out2 = asyncio.run(dc2.compress_day('2026-08-01', turns, [f'{7+i:02d}:00' for i in range(5)]))
    eq('名前で3行落ちる', dc2.stats['name_reject'], 3)
    eq('束ごとは捨てない', dc2.stats['batch_reject'], 0)
    eq('落ちた3行だけフォールバック', dc2.stats['fallback'], 3)
    eq('残り2行は本物', sum(1 for e in out2 if not e.get('fallback')), 2)

    # 隔離モードの重要度はパス1（5ターンを見て付けた点）を使う。パス2は1ターンしか見ていない
    def script3(messages, idx):
        user = messages[1]['content']
        n = len(re.findall(r'--- ターン\d+ ', user))
        if '[手がかり語]' not in user:
            return chr(10).join(f'{i+1}:{90 - i*10}:要約{i}〔〕' for i in range(n))   # パス1: 90,80,70,60,50
        return '1:5:ターンの要約。Tiramisuと話して満足した。'                       # パス2: 全部 5
    dc4 = S.DayCompressor(FakeLLM(script3), cfg, ('p1', 'p2'), txt)
    out4 = asyncio.run(dc4.compress_day('2026-08-01', turns, [f'{7+i:02d}:00' for i in range(5)]))
    eq('点数はパス1のもの', [e['score'] for e in out4], [90, 80, 70, 60, 50])
    check('文はパス2のもの', all('Tiramisuと話して満足した' in e['text'] for e in out4))

    # 形式が読めない行が多ければ束ごと捨てる（系統的異常）
    dc3 = S.DayCompressor(FakeLLM(['これは要約ではありません', 'まったく別の文章']), cfg, ('p1', 'p2'), txt)
    out3 = asyncio.run(dc3.compress_day('2026-08-01', turns, [f'{7+i:02d}:00' for i in range(5)]))
    eq('形式不良は束ごと', dc3.stats['batch_reject'], 1)
    eq('5行ともフォールバック', dc3.stats['fallback'], 5)


def test_anchor_rules_and_trim():
    """弱いアンカーの判定と、発端からの決まり文句・指示書の除去。"""
    print('[33] 弱いアンカーと発端の整形')
    w = S.is_weak_anchor
    eq('曜日', w('金曜日'), True); eq('時間帯', w('お昼過ぎ'), True)
    eq('小文字英単語', w('translated'), True); eq('大文字英単語', w('STREAMS'), True)
    eq('名前は残る', w('Tiramisu'), False); eq('複数語の題名は残る', w('Zone 4: The Anthem'), False)
    eq('ファイル名は残る', w('note_2026-04-21.md'), False); eq('数値+単位は残る', w('72.83Hz'), False)
    eq('ID風は落ちる', w('KTCrD4'), True); eq('UUID風は落ちる', w('ccca110a-041b'), True)
    eq('None は自分で言っていなければ落ちる', w('None', ''), True)
    eq('snake_case は自分で言っていれば残る', w('self_memo', S._norm('self_memoを更新')), False)
    eq('snake_case は言っていなければ落ちる', w('next_day', S._norm('畑を触った')), True)

    t = S.trim_situation
    raw = '2026-07-14 07:31\n[Moonbeat] 朝ですね。何をしますか？\n\n<note_fragment>\n[note_2026-04-18.md]\nRiver Trio\n</note_fragment>'
    out = t(raw)
    check('Moonbeat の決まり文句を落とす', '[Moonbeat]' not in out and '何をしますか' not in out, out)
    check('ノート抜粋は残る', 'River Trio' in out and 'note_2026-04-18.md' in out, out)
    check('日付行は残る', out.startswith('2026-07-14 07:31'), out)
    l0 = '2026-08-26 10:39\n21.5℃ 晴れ\nmoonbeat\n午前中、証言を書き直した'
    out = t(l0)
    check('Layer0 後の moonbeat の1語も落とす', 'moonbeat' not in out and '証言を書き直した' in out and '21.5℃' in out, out)
    task = ('2026-07-13 08:00\n【スケジュールタスク自動実行】\nタスク名: 毎朝のニュースレビュー\n'
            '以下の指示書に従って行動してください:\n<task_instruction>\n---\n# 毎朝のニュースレビュー\nwww3 Mediterranean\n---\n</task_instruction>')
    out = t(task)
    check('指示書本文を落とす', 'Mediterranean' not in out and 'task_instruction' not in out, out)
    check('タスク名は残る', 'タスク名: 毎朝のニュースレビュー' in out, out)
    legacy = '2026-07-13 08:00\n【スケジュールタスク自動実行】\nタスク名: X\n以下の指示書に従って行動してください:\n---\n見本 Microsoft\n---'
    out = t(legacy)
    check('旧形式の指示書も落とす', 'Microsoft' not in out and 'タスク名: X' in out, out)
    folded = '2026-07-13 08:00\nタスク名: X\n（指示書の本文は、このあとの同じタスクの通知に同じものが残っているので、ここでは省いています）'
    eq('畳まれた部はそのまま', t(folded), folded)
    kanon = '2026-08-26 20:55\nuser: これを見てみて。OBCの柚月しか知らないはずなのに'
    eq('カノンの発言はそのまま', t(kanon), kanon)

    # 検査の材料は LLM が見る本文と同じ
    dc = S.DayCompressor(FakeLLM(default_script), CFG, ('p1', 'p2'), txt)
    turn = [{'role': 'user', 'content': task}, {'role': 'assistant', 'content': 'スキップした'},
            {'role': 'tool', 'content': 'x' * 500 + 'HIDDEN_WORD'}]
    seg = dc._seg_text(turn)
    check('指示書は材料に入らない', 'Mediterranean' not in seg, seg[:200])
    check('ツール結果の200字より後ろは材料に入らない', 'HIDDEN_WORD' not in seg)
    check('柚月の発言は入る', 'スキップした' in seg)


def test_layer1_file_and_v1_archive(tmp):
    """ビューの書き出し先と、旧方式の要約文の退避／復帰。"""
    print('[34] layer1.md の書き出しと旧要約の退避')
    ws = Path(tmp) / 'ws_layer1'
    p = S.write_layer1_file(ws, '【これまでの会話の要約】\n## 2026-02-06（金）\n- 誕生')
    eq('場所', p, ws / 'memory' / 'layer1.md')
    check('内容', p.read_text(encoding='utf-8').endswith('- 誕生'))
    S.write_layer1_file(ws, '二度目')
    eq('上書き', p.read_text(encoding='utf-8'), '二度目')
    check('tmp が残らない', not (ws / 'memory' / 'layer1.md.tmp').exists())

    from core.context import ContextBuilder
    from memory.manager import MemoryManager
    cb = ContextBuilder(MemoryManager(str(ws)), {})
    cb.summary_layer1, cb.summary_layer2 = '旧L1', '旧L2'
    cb._summary_view = 'ビュー本文'
    body = cb._build_summary_content()
    check('ビューは要約メッセージに入らない', 'ビュー本文' not in body and '旧L1' in body and '旧L2' in body, body[:80])

    eq('退避する', cb.archive_v1_summaries(), True)
    eq('欄は空に', (cb.summary_layer1, cb.summary_layer2), ('', ''))
    eq('退避欄に入る', (cb.summary_layer1_archived, cb.summary_layer2_archived), ('旧L1', '旧L2'))
    eq('要約メッセージも空', cb._build_summary_content(), '')
    eq('二度目は何もしない', cb.archive_v1_summaries(), False)
    # v2 稼働中に v1 の緊急圧縮が書いた分は、次の退避で追記される
    cb.summary_layer1 = '緊急'
    cb.archive_v1_summaries()
    eq('追記される', cb.summary_layer1_archived, '旧L1\n緊急')

    # ファイル名は context_state.json にする（save_state は監視ファイルのパスを
    # 'context_state.json' の置換で作るので、別名だと監視ファイルが本体を上書きする）
    st = ws / 'context_state.json'
    cb.set_state_path(str(st)); cb.save_state()
    cb2 = ContextBuilder(MemoryManager(str(ws)), {})
    cb2.set_state_path(str(st)); cb2.load_state()
    eq('保存と復元', (cb2.summary_layer1, cb2.summary_layer1_archived, cb2.summary_layer2_archived), ('', '旧L1\n緊急', '旧L2'))
    eq('戻す', cb2.restore_v1_summaries(), True)
    eq('元の欄に戻る', (cb2.summary_layer1, cb2.summary_layer2), ('旧L1\n緊急', '旧L2'))
    eq('退避欄は空', (cb2.summary_layer1_archived, cb2.summary_layer2_archived), ('', ''))
    cb2.summary_layer1_archived = '別'
    eq('元の欄が埋まっていれば上書きしない', cb2.restore_v1_summaries(), False)


def test_strip_self_reference(tmp):
    """三人称の自己言及（「柚月は」）を落とす。目的語・所有・他者の主語は触らない。"""
    print('[35] 三人称の自己言及の除去')
    f = lambda t: S.strip_self_reference(t, '柚月')
    eq('行頭の「は」', f('柚月はMoltbook未登録のまま保存から始めると説明した。'), 'Moltbook未登録のまま保存から始めると説明した。')
    eq('行頭の「が」', f('柚月がオンラインになり、名前を決めようと提案した。'), 'オンラインになり、名前を決めようと提案した。')
    eq('さん付き', f('柚月さんは挨拶した。'), '挨拶した。')
    eq('読点の直後', f('メンションが来て、柚月は「はい」と返答した。'), 'メンションが来て、「はい」と返答した。')
    eq('目的語は触らない', f('ご主人様が柚月を褒めた。'), 'ご主人様が柚月を褒めた。')
    eq('所有は触らない', f('柚月の名前を決めた。'), '柚月の名前を決めた。')
    eq('他者の主語は触らない', f('ご主人様は柚月に手伝いを頼んだ。'), 'ご主人様は柚月に手伝いを頼んだ。')
    eq('名前が無ければそのまま', S.strip_self_reference('柚月は挨拶した。', ''), '柚月は挨拶した。')
    eq('別の名前', S.strip_self_reference('Aliasは挨拶した。', 'Alias'), '挨拶した。')

    # 描画時にも効く。フォールバック行（本人の原文）は触らない
    spec = [('2026-08-01', [_e('2026-08-01', 0, '柚月は畑を触った。', 90),
                            dict(_e('2026-08-01', 1, '柚月は眠いです。', 1), fallback=True)])]
    st = _store_with(tmp, spec)
    cfg = dict(CFG); cfg['agent_name'] = '柚月'; cfg['view_max_tokens'] = 100000
    v = S.render_view(st, date(2026, 8, 31), cfg, cnt, '')
    check('描画で落ちる', '- 畑を触った。' in v, v)
    check('フォールバック行は原文のまま', '- 柚月は眠いです。' in v, v)

    # 生成時にも効く（LLM の行だけ）
    def script(messages, idx):
        user = messages[1]['content']
        n = len(re.findall(r'--- ターン\d+ ', user))
        return chr(10).join(f'{i+1}:50:柚月は要約{i}を書いた。〔〕' for i in range(n))
    cfg2 = dict(CFG); cfg2['agent_name'] = '柚月'
    dc = S.DayCompressor(FakeLLM(script), cfg2, ('p1', 'p2'), txt)
    h = make_history('2026-08-01', 2)
    out = asyncio.run(dc.compress_day('2026-08-01', [h[0:2], h[2:4]], ['07:00', '08:00']))
    check('生成時に落ちる', all(e['text'].startswith('要約') for e in out), [e['text'] for e in out])


def test_layer0_pending():
    """Layer0 未圧縮ターンの検出（シャドーが先回りしすぎないための判定）。"""
    print('[31] Layer0 未圧縮の検出')
    h = make_history('2026-08-01', 3)
    turns = [h[i:i + 2] for i in range(0, 6, 2)]
    eq('全て圧縮済みなら0', S.layer0_pending(turns, txt), 0)
    raw = [{'role': 'user', 'content': '2026-08-01 10:00\n<system_notice>生</system_notice>'},
           {'role': 'assistant', 'content': '返事'}]
    eq('生のターンを数える', S.layer0_pending(turns + [raw, raw], txt), 2)
    eq('空でも落ちない', S.layer0_pending([], txt), 0)


def test_temperature_passthrough():
    """圧縮の温度は設定値を呼び出し単位で渡す。受け取れない LLM には渡さない。"""
    print('[30] 温度の受け渡し')
    eq('既定は0.2', CFG['temperature'], 0.2)

    llm = FakeLLM(default_script)          # **kwargs を受け取る
    dc = S.DayCompressor(llm, CFG, ('p1', 'p2'), txt)
    asyncio.run(dc.compress_day('2026-08-01', [make_history('2026-08-01', 1)], ['07:00']))
    check('呼び出しがあった', len(llm.kwargs) >= 1)
    check('温度0.2が渡る', all(k.get('temperature_override') == 0.2 for k in llm.kwargs),
          str(llm.kwargs[:2]))

    class StrictLLM(FakeLLM):
        async def chat(self, messages, tools=None):   # 温度を受け取れない実装
            return await FakeLLM.chat(self, messages, tools)
    strict = StrictLLM(default_script)
    dc2 = S.DayCompressor(strict, CFG, ('p1', 'p2'), txt)
    out = asyncio.run(dc2.compress_day('2026-08-01', [make_history('2026-08-01', 1)], ['07:00']))
    check('受け取れなくても落ちない', len(out) == 1, f'got={len(out)}')
    check('渡していない', all('temperature_override' not in k for k in strict.kwargs))

    eq('判定関数: 名前あり', S._accepts_kwarg(lambda a, temperature_override=None: 0, 'temperature_override'), True)
    eq('判定関数: **kwargs', S._accepts_kwarg(lambda a, **kw: 0, 'temperature_override'), True)
    eq('判定関数: なし', S._accepts_kwarg(lambda a, tools=None: 0, 'temperature_override'), False)


def test_mode_flags():
    print('[22] enabled / shadow の3状態')
    def active(cfg): return bool(cfg.get('enabled')) and not cfg.get('shadow')
    def shadow(cfg): return bool(cfg.get('enabled')) and bool(cfg.get('shadow'))
    off = {'enabled': False, 'shadow': False}
    sh = {'enabled': True, 'shadow': True}
    on = {'enabled': True, 'shadow': False}
    eq('OFF', (active(off), shadow(off)), (False, False))
    eq('シャドー', (active(sh), shadow(sh)), (False, True))
    eq('本番', (active(on), shadow(on)), (True, False))
    # enabled=false なら shadow も無効（誤設定で勝手に動かない）
    eq('無効時はshadowも立たない',
       shadow({'enabled': False, 'shadow': True}), False)
    # 既定は全部OFF
    d = S.get_config({})
    eq('既定はOFF', (active(d), shadow(d)), (False, False))


def _guard_reason(prev_oldest, prev_days, oldest, cur_days):
    """agent.refresh_summary_view のガード判定と同じ条件（ロジックの写し）。"""
    if prev_oldest and not oldest:
        return 'empty'
    if prev_oldest and oldest and oldest > prev_oldest:
        return 'regress'
    if prev_days and cur_days < prev_days * 0.9:
        return 'shrink'
    return None


def test_guard_detects_loss(tmp):
    print('[17] 描画ガードが記憶の減少を検知する')
    # 正常な成長（日が増える）は素通り
    eq('成長は通す', _guard_reason('2026-07-08', 10, '2026-07-08', 12), None)
    # 最古日が前進＝古い記憶が落ちた
    eq('後退を検知', _guard_reason('2026-07-08', 10, '2026-07-20', 10), 'regress')
    # ビューが丸ごと空＝最悪のケース。oldest が None でも取りこぼさないこと
    eq('消失を検知', _guard_reason('2026-07-08', 10, None, 0), 'empty')
    # 日数の急減
    eq('急減を検知', _guard_reason('2026-07-08', 100, '2026-07-08', 50), 'shrink')
    eq('わずかな減少は許容', _guard_reason('2026-07-08', 100, '2026-07-08', 95), None)
    # 初回（基準値なし）は素通り
    eq('初回は通す', _guard_reason(None, None, '2026-07-08', 10), None)

    # 実データで消失パターンを再現
    st = _store_with(tmp, [('2026-07-08', [_e('2026-07-08', 0, '記憶')])])
    oldest, _ = S.view_coverage(st)
    eq('正常時は日付が取れる', oldest, '2026-07-08')
    st.entries = []
    oldest2, _ = S.view_coverage(st)
    eq('消失後はNone', oldest2, None)
    eq('この状況をガードが捕まえる', _guard_reason(oldest, 1, oldest2, 0), 'empty')


def test_view_and_v1_coexist():
    print('[14] ビューとv1要約の併記')
    # v2 のビューだけ
    eq('ビューのみ', _StubCtx(view='【要約】\n## 2026-07-08\n- A').compose(),
       '【要約】\n## 2026-07-08\n- A')
    # v1 のみ（v2 未稼働）
    got = _StubCtx(l1='v1の行').compose()
    check('v1のみは見出しが付く', got.startswith('【要約】\n') and 'v1の行' in got, got)
    # 両方ある＝緊急圧縮が v1 に退避した状況。どちらも欠けてはならない
    both = _StubCtx(view='【要約】\n## 2026-07-08\n- 古い日', l1='緊急圧縮で退避した要約').compose()
    check('ビューが残る', '## 2026-07-08' in both and '古い日' in both, both)
    check('v1の要約も出る', '緊急圧縮で退避した要約' in both, both)
    eq('見出しは1つだけ', both.count('【要約】'), 1)
    # 順序: ビュー（古い側）が先、v1（新しい側）が後
    check('ビューが先', both.index('古い日') < both.index('緊急圧縮で退避した要約'), both)
    # 何も無ければ挿入しない
    eq('空なら None', _StubCtx().compose(), None)


# ============================================================
# 13. 予算充填が線形コストで正しく動くこと
# ============================================================

def test_budget_fill(tmp):
    print('[13] 予算充填')
    today = date(2026, 8, 31)
    # 十分に異なる文面を用意（dedupで畳まれないように）
    spec = []
    for i in range(20):
        d = f'2026-06-{i+1:02d}'
        spec.append((d, [_e(d, j, f'{d}の{j}番目、{"あいうえおかきくけこ"[j]}に関する固有の出来事', 90 - j)
                         for j in range(8)]))
    st = _store_with(tmp, spec)

    # 予算を絞れば行数が減り、増やせば増える（単調性）
    counts = []
    for budget in (300, 1200, 100000):
        c = dict(CFG); c['view_max_tokens'] = budget
        v = S.render_view(st, today, c, cnt, '')
        counts.append(v.count('\n- ') + (1 if v.startswith('- ') else 0))
        eq(f'予算{budget}で全日が残る', len(set(re.findall(r'## (\d{4}-\d{2}-\d{2})', v))), len(spec))
    check('予算に応じて単調増加', counts[0] <= counts[1] <= counts[2], str(counts))

    # 予算内に収まっている（超過しない）
    c = dict(CFG); c['view_max_tokens'] = 1200
    v = S.render_view(st, today, c, cnt, '')
    check('予算を超えない', cnt(v) <= 1200, f'{cnt(v)} > 1200')

    # 描画は繰り返し呼んでも同じ結果（決定論）
    v2 = S.render_view(st, today, c, cnt, '')
    eq('決定論', v, v2)


# ============================================================
# 11. 描画ガード用のカバレッジ
# ============================================================

def test_coverage(tmp):
    print('[11] カバレッジ判定')
    st = _store_with(tmp, [('2026-03-01', [_e('2026-03-01', 0, 'A')]),
                           ('2026-05-01', [_e('2026-05-01', 0, 'B')])])
    eq('範囲', S.view_coverage(st), ('2026-03-01', '2026-05-01'))
    empty = S.SummaryStore(Path(tmp) / 'cov_empty.json'); empty.load()
    eq('空は(None,None)', S.view_coverage(empty), (None, None))


# ============================================================
# 実行
# ============================================================

def main():
    tmp = tempfile.mkdtemp(prefix='summary_v2_test_')
    try:
        test_logical_date()
        test_parser()
        test_anchors()
        test_gates()
        test_normalize_decay()
        test_store(tmp)
        test_slice()
        test_render(tmp)
        test_pipeline(tmp)
        test_transaction(tmp)
        test_coverage(tmp)
        test_daily_log_preserves_v1(tmp)
        test_budget_fill(tmp)
        test_view_and_v1_coexist()
        test_store_recovery(tmp)
        test_backup_rotation(tmp)
        test_guard_detects_loss(tmp)
        test_total_loss_detection(tmp)
        test_guard_baseline_outside_db(tmp)
        test_no_rotate_after_recovery(tmp)
        test_fallback_day_detection(tmp)
        test_agent_integration(tmp)
        test_orphan_turns_not_lost(tmp)
        test_degraded_flag(tmp)
        test_budget_hard_check(tmp)
        test_lethe_missing_recorded(tmp)
        test_decay_curve(tmp)
        test_line_ratio(tmp)
        test_temperature_passthrough()
        test_layer0_pending()
        test_strip_self_reference(tmp)
        test_layer1_file_and_v1_archive(tmp)
        test_anchor_rules_and_trim()
        test_pass2_isolated_and_per_line_fallback()
        test_mode_flags()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    print('=' * 60)
    print(f'  成功 {_passed} / 失敗 {_failed}')
    if _failures:
        print('  --- 失敗一覧 ---')
        for f in _failures:
            print(f'   {f}')
    print('=' * 60)
    return 1 if _failed else 0


if __name__ == '__main__':
    sys.exit(main())
