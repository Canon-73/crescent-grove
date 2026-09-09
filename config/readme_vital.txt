# data/vital.json 設定リファレンス

## トップレベル

| キー | 型 | 説明 |
|---|---|---|
| stamina | int | 現在の体力（0〜100）。3時境界で100にリセット |
| mental | int | 現在のメンタル（0〜100）。日次リセットなし |
| last_updated | string | 最終更新時刻（ISO形式）。自然回復の経過時間計算に使用 |
| daily_start_balance | float/null | DeepSeekTrackerモード時の当日開始残高。日付変更でnullにリセット |
| previous_ratio | float | DeepSeekTrackerモード時の前回使用率。tokenモードでは未使用 |
| pending_tokens | object | 未反映のトークン使用量。update_stamina実行時に消費してリセット |
| _last_prompt_tokens | int | 前回のprompt_tokens。入力トークンの増加分計算に使用 |

## config

| キー | 型 | デフォルト | 説明 |
|---|---|---|---|
| stamina_mode | string | "auto" | スタミナ計算方式。"token"=トークンベース、"auto"/"balance"=DeepSeek残高ベース |
| stamina_input_weight | float | 1.0 | 入力トークンの重み係数 |
| stamina_output_weight | float | 1.0 | 出力トークンの重み係数 |
| stamina_cost_multiplier | float | 0.001 | 全体の消費倍率。小さいほどスタミナが減りにくい |
| daily_budget_usd | float | 0.50 | DeepSeekTrackerモード時の日次予算（ドル） |
| daily_token_limit | int | 500000 | TokenTrackerモード時の日次トークン上限 |

## スタミナ計算式（tokenモード）

stamina_cost = round((input_delta × stamina_input_weight + output × stamina_output_weight) × stamina_cost_multiplier)

- input_delta: 前回からの入力トークン増加分
- output: 出力トークン（毎回全量）

## 自然回復

- 体力: 1時間離席ごとに+20（上限100）
- メンタル: 1時間離席ごとに+5（上限100）

## 日次リセット（3時境界）

- stamina → 100
- pending_tokens → {input: 0, output: 0}
- _last_prompt_tokens → 削除
- daily_start_balance → null
- previous_ratio → 0.0
- mental はリセットしない
