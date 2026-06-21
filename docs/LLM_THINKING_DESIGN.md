# LLM thinking モード ＋ サリア個別LLM 設計

最終更新: 2026-06（フル対応で実装着手）。各社 thinking/reasoning API は変化が速いので、実装/改修時はこの表を最新化すること。

## 各社の現行 thinking/reasoning 仕様（2026年・調査済み）
| プロバイダ | パラメータ | 値 | 落とし穴 |
|---|---|---|---|
| **DeepSeek V4** | `extra_body={"thinking":{"type":"enabled"/"disabled"}}`（既定 enabled）＋ `reasoning_effort` | `high`/`max`（low/medium→high, xhigh→max） | thinking時 temperature/top_p/penalties 無効。disabled時 reasoning_effort 不可。**★ツール使用＋thinking時、assistant の `reasoning_content` を以降の全ターンの API 送信に含めないと 400**（ツール無しなら不要・無視） |
| **OpenAI (GPT-5.5)** | `reasoning_effort` | none/low/medium(既定)/high/xhigh | 非reasoningモデルに送ると error |
| **Claude (4.7/4.8)** | `thinking:{type:"adaptive"}` ＋ `effort` | low/medium/high / `type:"disabled"` | 旧 budget_tokens は新モデルで 400。ネイティブAPI（OpenAI互換でない） |
| **Gemini 3** | `thinking_level`（互換層は `reasoning_effort`→thinking_level 自動マップ） | low/medium/high（互換: low→low, medium→high, high→high） | Flashは無効化可、Proは完全停止不可あり |
| **xAI Grok 4.3** | `reasoning_effort` | none/low(既定)/high | 旧 grok-4 は非対応＝送るとerror |

## 統一抽象 `thinking ∈ {auto, off, low, medium, high}`
- `auto`=何も送らない（モデル既定）。`off`=各社の disable/none。`low/medium/high`=各社 effort にマップ（DeepSeekは high/max のみなので丸め、Grokは none/low/high 中心）。
- 非対応 provider×model には送らない（gpt-4o, 旧grok-4 等）。

## 設定スキーマ
```yaml
llm:
  provider/model/...   # 既存
  thinking: "auto"     # 新設
salia:
  enabled: true
  model: null          # null=メイン追従
  thinking: null       # null=メイン追従
```

## ★最重要: DeepSeek の reasoning_content 戻し（フル対応の核）
本アプリはツール多用。DeepSeek thinking ON では、**ツール呼び出しを含む assistant メッセージの `reasoning_content` を会話履歴に保持し、次以降の API messages に含める**必要がある（欠けると 400）。
- llm.chat の戻り `LLMResponse.reasoning_content` を、agent が conversation_history の assistant メッセージに保持。
- messages 構築時に assistant メッセージへ `reasoning_content` を付与して送る。
- `_sanitize_for_save`（context.py）は reasoning_content を保持（画像のみ除去）。
- 他社（reasoning_content を返さない/不要）では付与しないか無害。

## 実装範囲
- `core/llm.py`: モデル名推測（flash/pro）廃止、`apply_thinking()` 新設、`self.thinking` 適用、reasoning_content 取得。
- `core/context.py` / `core/agent.py`: assistant メッセージに reasoning_content を保持＋API送信に含める（DeepSeek 400 対策）。
- `core/salia.py`: 自前 model＋thinking。
- `server.py`: `LLMConfigReq` に thinking/salia_model/salia_thinking、update/get_llm_config 対応。
- `config.yaml`/`dist_template/config.yaml`: スキーマ。dev は柚月の希望に合わせ `llm.thinking` を明示（旧: flash名でthinking OFF）。
- `web/settings_llm.html`: main の thinking ドロップダウン＋サリア「メインと同じ」セクション。

## dev 影響
- 旧: `deepseek-v4-flash` 名推測で thinking OFF。新: 設定 `llm.thinking`。柚月は thinking を使う場面があるので auto/任意。移行時に dev config の `llm.thinking` を明示すること。

## 出典（2026調査）
- DeepSeek: https://api-docs.deepseek.com/guides/thinking_mode
- Claude: https://platform.claude.com/docs/en/build-with-claude/extended-thinking
- OpenAI: https://developers.openai.com/api/docs/models/gpt-5.5
- Gemini: https://ai.google.dev/gemini-api/docs/thinking
- Grok: https://docs.x.ai/developers/model-capabilities/text/reasoning
