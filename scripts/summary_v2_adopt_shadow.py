# -*- coding: utf-8 -*-
"""シャドー運転の summary_db を本番の summary_db として採用する（切替時に1回だけ使う）。

やること:
  1. data/summary_db_shadow.json を data/summary_db.json へコピー（既存の本番DBは退避）
  2. watermark を「会話履歴の最古日の前日」に揃える
     （シャドーは履歴に残っている日を先回りで要約しているため、watermark をそのまま
       使うとその日々がビューと履歴の両方に載る＝二重計上になる）
  3. 先回りぶんのエントリは残す。切替後の圧縮が同じ日に到達したとき
     agent.compress_days_v2 が LLM を呼ばずに再利用する

使い方（サーバを止めてから、または shadow:true のまま実行 → 設定を shadow:false に → 再起動）:
    venv\\Scripts\\python.exe scripts\\summary_v2_adopt_shadow.py
"""
import json
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.paths import resolve_path, data_file
import core.summary_v2 as S


def txt(m):
    c = m.get('content', '')
    if isinstance(c, list):
        return ''.join(p.get('text', '') for p in c if isinstance(p, dict))
    return c if isinstance(c, str) else str(c)


def main():
    cc = json.loads((ROOT / 'config/compression_config.json').read_text(encoding='utf-8'))
    cfg = S.get_config(cc.get('summary_v2', {}))
    prod = resolve_path(cfg['db_file'])
    shadow = prod.parent / 'summary_db_shadow.json'
    if not shadow.exists():
        print(f'シャドーDBがありません: {shadow}')
        return 1

    hist = json.loads(data_file('context_state.json').read_text(encoding='utf-8'))['conversation_history']
    days = S.slice_history_by_day(hist, txt)
    if not days:
        print('会話履歴に論理日がありません。中止します')
        return 1
    oldest = days[0]['date']
    new_wm = (date.fromisoformat(oldest) - timedelta(days=1)).isoformat()

    d = json.loads(shadow.read_text(encoding='utf-8'))
    old_wm = d.get('meta', {}).get('compressed_through')
    ahead = sorted({e['date'] for e in d['entries'] if e['date'] > new_wm})

    if prod.exists():
        bak = prod.with_name(prod.name + '.before_adopt')
        shutil.copyfile(prod, bak)
        print(f'既存の本番DBを退避: {bak.name}')

    d.setdefault('meta', {})['compressed_through'] = new_wm
    d['meta']['day_tx'] = {}
    tmp = prod.with_suffix('.tmp')
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(prod)

    chk = S.SummaryStore(prod)
    chk.load()
    print(f'採用しました: {prod.name}')
    print(f'  エントリ {len(chk.entries):,} 件 / 日数 {len(chk.days())}')
    print(f'  watermark {old_wm} → {new_wm}（会話履歴の最古日 {oldest} の前日）')
    print(f'  先回りぶん {len(ahead)} 日（{ahead[0] if ahead else "-"}〜{ahead[-1] if ahead else "-"}）は'
          f'ビューに出さず保持。切替後の圧縮で LLM を呼ばずに再利用されます')
    print('次: compression_config.json の summary_v2.shadow を false にしてサーバを再起動')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
