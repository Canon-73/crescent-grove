# -*- coding: utf-8 -*-
"""過去の日次ログ（workspace/logs/full）から Layer1（会話要約）を作る。

導入前から溜めていたログを、日付付きの要約DB（data/summary_db.json）と
起動時記憶 memory/layer1.md にする道具。**サーバを止めてから**1回走らせる
（稼働中のサーバは要約DBをメモリに持っていて、外から書いた分を次の保存で上書きするため）。

使い方（プロジェクト直下で。配布版は同梱の python.exe でも同じ）:
    python scripts/build_layer1_from_logs.py --dry-run          # 対象の日数・ターン数・見込み費用だけ出す
    python scripts/build_layer1_from_logs.py                    # 全部作る（途中で止めても次回は続きから）
    python scripts/build_layer1_from_logs.py --from 2026-02-06 --to 2026-03-31
    python scripts/build_layer1_from_logs.py --api-key-env CG_DEEPSEEK_SEARCH   # 別のキーで
    python scripts/build_layer1_from_logs.py --data-root <利用者データのフォルダ>  # 配布版

既定の対象は「ログにあって要約DBに無く、会話履歴にもまだ残っていない日」。
会話履歴に残っている日は通常の圧縮が順に処理するので対象外（--include-history で含められる。
その場合は圧縮が到達したとき LLM を呼ばずに再利用される）。
LLM は設定ファイルの本体の設定を使う（--provider / --model / --api-key-env で上書き可）。
要約DBが無ければ新しく作り、境界（compressed_through）は会話履歴の最古日の前日に置く。
既にあれば境界は動かさない（後退はさせない）。
"""
import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# 実測（2026-09-04・DeepSeek flash・208日 9,794ターン）: 1ターンあたり入力 1,211・出力 99 トークン、
# オフピーク換算で 2.16 ドル ≒ 1ターン 0.022 セント、所要 0.26 秒/ターン
COST_PER_TURN_USD = 2.16 / 9794
SEC_PER_TURN = 42 * 60 / 9794


def server_is_running(port: int) -> bool:
    try:
        urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=2)
        return True
    except urllib.error.HTTPError:
        return True          # 応答があれば起動している（認証で 401/403 でも）
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description='過去ログから Layer1（会話要約）を作る')
    ap.add_argument('--dry-run', action='store_true', help='対象と見込みだけ出して何も書かない')
    ap.add_argument('--from', dest='date_from', help='開始日 YYYY-MM-DD')
    ap.add_argument('--to', dest='date_to', help='終了日 YYYY-MM-DD')
    ap.add_argument('--max-days', type=int, default=0, help='今回処理する日数の上限（0=無制限）')
    ap.add_argument('--include-history', action='store_true', help='会話履歴にまだ残っている日も対象にする')
    ap.add_argument('--force', action='store_true', help='要約DBに既にある日も作り直す')
    ap.add_argument('--provider', help='LLM プロバイダを上書き（deepseek / ollama など）')
    ap.add_argument('--model', help='モデル名を上書き')
    ap.add_argument('--api-key-env', help='API キーを入れた環境変数名（本体の設定と別のキーを使うとき）')
    ap.add_argument('--no-view', action='store_true', help='最後に memory/layer1.md を描画しない')
    ap.add_argument('--allow-running', action='store_true', help='サーバ稼働中でも実行する（非推奨）')
    ap.add_argument('--data-root', help='利用者データのフォルダ（配布版でサーバに渡しているものと同じ）')
    args = ap.parse_args()

    from core.paths import set_data_root
    set_data_root(args.data_root)           # サーバと同じく、.env や設定を読む前に確定させる
    from core.env_manager import EnvManager
    EnvManager.load_env()
    from core.config_loader import load_config, apply_prompt_placeholders
    from core.paths import resolve_workspace, resolve_path, data_file, config_file
    from core.i18n import init_i18n, t
    from core.tokens import count_text_tokens as ct
    import core.summary_v2 as S

    config = load_config()
    init_i18n(config)
    port = int(config.get('server', {}).get('port', 8080))
    if not args.dry_run and not args.allow_running and server_is_running(port):
        print(f'サーバが {port} 番で応答しています。止めてから実行してください（--dry-run は稼働中でも可）。')
        return 1

    workspace = resolve_workspace(config)
    agent_name = config.get('profile', {}).get('agent', {}).get('name', 'Assistant')
    honorific = config.get('profile', {}).get('user', {}).get('honorific', 'ユーザー')
    cc_path = config_file('compression_config.json')
    cc = json.loads(cc_path.read_text(encoding='utf-8')) if cc_path.exists() else {}
    cfg = S.get_config(cc)
    cfg['agent_name'] = agent_name
    db_path = resolve_path(cfg['db_file'])

    # --- 対象の日を決める ---
    log_days = S.list_log_days(workspace)
    if not log_days:
        print(f'日次ログがありません: {workspace / "logs" / "full"}')
        return 1
    store = S.SummaryStore(db_path)
    store.load()
    have = set(store.days())

    def txt(m):
        c = m.get('content', '')
        return ''.join(p.get('text', '') for p in c if isinstance(p, dict)) if isinstance(c, list) else (c if isinstance(c, str) else str(c))

    oldest_hist = None
    state_path = data_file('context_state.json')
    if state_path.exists():
        try:
            hist = json.loads(state_path.read_text(encoding='utf-8')).get('conversation_history', [])
            days_in_hist = S.slice_history_by_day(hist, txt)
            if days_in_hist:
                oldest_hist = days_in_hist[0]['date']
        except Exception as e:
            print(f'会話履歴を読めませんでした（無視して続行）: {e}')

    targets = []
    for d in log_days:
        if args.date_from and d < args.date_from:
            continue
        if args.date_to and d > args.date_to:
            continue
        if d in have and not args.force:
            continue
        if oldest_hist and d >= oldest_hist and not args.include_history:
            continue
        targets.append(d)
    if args.max_days:
        targets = targets[:args.max_days]

    # ターン数を数える（復元は読むだけ）
    plan = []
    for d in targets:
        turns, times = S.turns_from_raw_log(workspace, d)
        if turns:
            plan.append((d, turns, times))
    n_turns = sum(len(p[1]) for p in plan)
    print(f'ログの日数 {len(log_days)}（{log_days[0]}〜{log_days[-1]}）/ 要約DB {len(have)}日 / '
          f'会話履歴の最古日 {oldest_hist or "なし"}')
    print(f'対象 {len(plan)}日 / {n_turns:,}ターン'
          + (f'（{plan[0][0]}〜{plan[-1][0]}）' if plan else ''))
    print(f'見込み: 所要 {n_turns * SEC_PER_TURN / 60:.0f}分 / 費用 約 {n_turns * COST_PER_TURN_USD:.2f}ドル'
          f'（DeepSeek flash オフピーク換算。ピーク時間帯はこの2倍。ローカルLLMなら0）')
    if args.dry_run or not plan:
        return 0

    # --- LLM ---
    llm_cfg = dict(config.get('llm') or {})
    if args.provider:
        llm_cfg['provider'] = args.provider
    if args.model:
        llm_cfg['model'] = args.model
    if args.api_key_env:
        key = os.environ.get(args.api_key_env, '')
        if not key:
            print(f'環境変数 {args.api_key_env} が空です')
            return 1
        llm_cfg['api_key'] = key
    from core.llm import create_provider
    llm = create_provider(llm_cfg)
    prompts = tuple(
        apply_prompt_placeholders(resolve_path(cfg[k]).read_text(encoding='utf-8'), agent_name, honorific)
        for k in ('pass1_prompt_file', 'pass2_prompt_file'))
    print(f'LLM: {llm_cfg.get("provider")}/{llm_cfg.get("model")}  開始')

    async def run():
        t_all = time.time()
        total_lines = total_fb = 0
        for i, (d, turns, times) in enumerate(plan, 1):
            dc = S.DayCompressor(llm, cfg, prompts, txt)
            t0 = time.time()
            entries = await dc.compress_day(d, turns, times)
            store.replace_day(d, entries)
            store.save()               # 1日ごとに保存（途中で止めても続きから）
            total_lines += len(entries)
            total_fb += dc.stats['fallback']
            print(f'[{i:3d}/{len(plan)}] {d} {len(turns):3d}ターン → {len(entries):3d}行 '
                  f'フォールバック {dc.stats["fallback"]}  {time.time() - t0:.0f}秒', flush=True)
        print(f'完了: {len(plan)}日 / {total_lines:,}行 / フォールバック {total_fb}'
              f'（{total_fb / max(total_lines, 1) * 100:.2f}%）/ {(time.time() - t_all) / 60:.0f}分')

    asyncio.run(run())

    # --- 境界: 無ければ置く。あれば動かさない ---
    if store.compressed_through is None:
        if oldest_hist:
            wm = (date.fromisoformat(oldest_hist) - timedelta(days=1)).isoformat()
        else:
            wm = max(store.days())
        store.init_watermark(wm)
        store.save()
        print(f'境界（compressed_through）を {wm} に置きました')
    else:
        print(f'境界（compressed_through）は {store.compressed_through} のまま')

    if not args.no_view:
        view = S.render_view(store, date.today(), cfg, ct, t('ctx_summary_heading'))
        p = S.write_layer1_file(workspace, view)
        print(f'{p} を描画しました（{ct(view):,} トークン・{view.count(chr(10) + "- "):,}行）')
    print('サーバを起動すれば、boot_memories に memory/layer1.md があれば次のターンから読まれます。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
