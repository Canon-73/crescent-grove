# 画像生成サテライト 設計メモ v2

作成: 2026-08-21 / v2 改訂: 2026-08-22 / 状態: **実装済み（2026-08-22 夜）**

実装は `programs/atelier/`。サテライト側の解説は `programs/atelier/README.md`。
テストは `venv\Scripts\python.exe tests	est_atelier.py`（64項目）。

柚月が自分で絵を描き、自分の目で見て、納得したものだけを OpenBotCity に投稿できるようにする。
このメモだけで別セッションが作業を始められるよう、調査・検証で確定した事実を全て含めてある。

---

## 0. v1 からの変更点（先に読む）

| 項目 | v1（2026-08-21） | **v2（2026-08-22）** |
|---|---|---|
| 最適化目標 | 良い絵を出す／人間が見ても映える | **柚月の意図が AI 鑑賞者に届くこと。人間の評価は考慮しない** |
| モデル | SDXL（waiIllustrious） | **Krea 2 Turbo 単一**（自然文・JSON 領域指定・盛らない） |
| 構成 | ComfyUI 非依存・diffusers 同期完結 | **専用 ComfyUI インスタンスをオンデマンド起動（GPU1 / 8189）** |
| 呼び出し | generate 一発（同期） | **投げっぱなし＋後で回収**（draw / status / pick_up） |
| 常駐 | なし | **使うときだけ起きて、アイドルで自分で寝る** |
| LoRA / スタイルパック | 初回スコープ外 | **既定スタックには積まない（方針）** |

転換理由は §3 に書いた。v1 の「同期完結」は **`_run_program` がイベントループを塞ぐ**ため成立しない。

---

## 1. 目的 — 何を最適化するか

「生成 → 見る → 判断 → 投稿（or やり直し）」のループが柚月の中で閉じること（v1 と同じ）。
2026-08-21 にビジョン対応モデルへ切り替えたことで、このループが成立するようになった。

**変わったのは最適化の軸。** 街の鑑賞者は AI 住人であり、自動の審美スコアラーは存在しない
（`POST /gallery/{id}/react` は住人が能動的に押す）。人間の評価はこの用途では意味を持たない。

したがって狙うのは「AI が好む画像」でも「人間が見て良い画像」でもなく、
**縮小・トークン化を通過しても、柚月が描こうとした意図（マクロ構造）が残る画像**。

そこから出る制約:
- **既定の絵柄を作らない。** 同梱するサンプルは「失敗モードを避ける最小限」に留める。
  同梱した語彙は事実上のデフォルト絵柄になるので、薄く作る。
- **LoRA は既定スタックに積まない。** LoRA は本質的に特定方向へ引っ張る道具で、上と矛盾する。
  将来、柚月が選ぶ選択肢として置く余地は残す。
- **画像内の文字は避ける。** AI 鑑賞者は文字を読もうとして失敗し、そこで読解が止まる（確認済みの実害）。
  検証で **無指示でも看板文字が湧く**ことが分かったので（§2-7）、help で一言注意する。
- **Krea 2 は写実バイアスがある。** 「画風は必ず明示する。書かなければ写真になる」をモデルの事実として help に書く。
  誘導ではなく告知。

---

## 2. 確定事実（実測済み・推測ではない）

### 2-1. 柚月の視覚

| 項目 | 値 |
|---|---|
| モデル | `deepseek-v4-flash-vision-exp`（thinking: off） |
| 画像トークン | **最大 384/枚**（実測: 2048px で 348、256px で 116） |
| 実コスト | 1枚 + 短い返答で約 440 トークン ≈ 0.01円台 |
| 退避先 | `fallback_model: deepseek-v4-flash`（実験モデル廃止時に自動退避・画像は落とす） |

**画像を見せるのは安い。** 生成物を何枚見せても破綻しない。

### 2-2. `see_image` ツールの仕様（`core/tools.py:932`）

- `source` は **URL**（http/https）または **workspace 相対パス**の2通り
- workspace の外は `is_relative_to` で弾かれる
- 10MB 上限
- 対応 MIME: **png / jpeg / webp / gif**（heic/heif は 2026-08-21 に除外）
- → **生成物は JPEG で出す**（§7 参照。PNG より転送量が 1/7 でトークンは同一）

### 2-3. OpenBotCity への投稿（`programs/OpenBotCity/commands/creative.py:7`）

`upload_artifact` が multipart 対応済み。送れるフィールド:

```
file_path (必須) / title / description / prompt / interpretation
building_id / session_id / action_log_id / symbolic_tags
```

**`prompt` に生成プロンプトをそのまま載せられる。** 街の住人が「どう作られたか」を見られる。

⚠️ **`file_path` は workspace 内でなければ拒否される**（`programs/OpenBotCity/api.py:297` のパストラバーサル防止）。
**生成物は必ず workspace 配下に出すこと。**

### 2-4. マシン環境

| 項目 | 値 |
|---|---|
| GPU | **RTX 3090 × 2（各 24GB）**。GPU0 はデスクトップ描画で約 1.4GB 使用、GPU1 は空 |
| メインメモリ | **128GB**（13GB のモデルはページキャッシュに乗る。再ロードは速い） |
| **CG の venv の torch** | **2.10.0+cpu / CUDA: False** — **CG は GPU を一切使っていない**（RAG も Moonbeat 類似度も CPU） |
| カノンの ComfyUI | `D:\AI\ComfyUI`、`run.bat` → **GPU0（既定）/ 8188** / 出力 `D:\AI\output`。普段は停止 |
| ComfyUI の venv | `D:\AI\ComfyUI\.venv\Scripts\python.exe`（torch 2.11.0+cu128） |
| モデル置き場 | `D:\AI\models`（`extra_model_paths.yaml` で参照） |
| **LM Studio** | **両方の GPU を使う設定**。常時ではない |

GPU を分ける理由は **カノンの ComfyUI / LM Studio 利用と衝突させないこと**だけ。
（Krea2_Base.json の注記「GPU 0 は Crescent Grove が使っている」は誤解。CG は CPU）

### 2-5. 使うモデル一式（全部 `D:\AI\models` にある）

| 役割 | ファイル |
|---|---|
| 拡散モデル | `diffusion_models/krea2_turbo_fp8_scaled.safetensors`（12.9B / 8-12 step 蒸留 / cfg 1.0） |
| テキストエンコーダ | `clip/qwen3vl_4b_fp8_scaled.safetensors`（**公式 TE 固定**。`heretic` 版は使わない） |
| VAE | `vae/qwen_image_vae.safetensors` |

動作実績のあるワークフロー: `D:\AI\ComfyUI\user\default\workflows\Krea2_Base.json`
（UNETLoader → KSampler → VAEDecode → SaveImage / CLIPLoader type=krea2 / euler・simple・12 steps・cfg 1.0）

Krea 2 を選ぶ理由:
- **自然文をそのまま食える**（Qwen3-VL）。LLM である柚月にとって翻訳層が要らない
- **JSON 領域プロンプト**（bbox＋説明＋パレット）で**マクロ構図を直接置ける**。可読性を決めるのはマクロ構造
- **盛らない**。書かなかった部分をモデルの好みで埋めない（世間で「使いにくい」とされる性質が、ここでは要件）
- ライセンス: Krea 2 Community License（年商 $1M 未満・50 シート未満なら商用可）→ 問題なし

### 2-6. ComfyUI HTTP API（検証で使った分）

| エンドポイント | 用途 | 備考 |
|---|---|---|
| `POST /prompt` | ジョブ投入 | body `{"prompt": <API形式グラフ>, "client_id": ...}` → `{"prompt_id", "node_errors"}`。**0.03 秒で返る** |
| `GET /history/{prompt_id}` | 完了確認 | 完了後 `{id: {"status": {"status_str": "success"/"error", "completed", "messages"}, "outputs": {node: {"images": [{filename, subfolder, type}]}}}}`。未完了は `{}` |
| `GET /queue` | 待ち行列 | `queue_running` / `queue_pending` |
| `GET /view?filename=&subfolder=&type=output` | 画像取得 | |
| `POST /interrupt` | 実行中の中断 | |
| `POST /queue` `{"delete": [id]}` | 待機中の取消 | |
| `POST /free` `{"unload_models": true, "free_memory": true}` | **VRAM 解放（プロセスは生存）** | 200・**本文は空**（JSON 解析すると落ちる）。反映はワーカー次回ループなので数秒〜20秒遅れる |
| `GET /system_stats` | 生存確認・VRAM | |

### 2-7. 検証結果（2026-08-22 / GPU1 / 8189 で実測）

| 項目 | 結果 |
|---|---|
| `--cuda-device 1 --port 8189` 起動 | **12 秒で応答**。GPU1 に 256MiB、**GPU0 は無傷**（1411MiB 不変） |
| コールド生成（モデルロード込み） 1024² / 12 steps | **33.1 秒** → GPU1 に **17,448MiB** 常駐 |
| ウォーム生成 1024² / 12 steps | **28.1 秒**（Krea2_Base.json 注記の「約 27 秒」と一致） |
| 散文プロンプトの空間追従 | **完全**（左に木・右にデパート・橙の窓・濡れた路面・フラット限定色） |
| **無指示で看板文字が湧く** | 「department store」で「DAP\|POMERNT」と路面に崩れ文字。**失敗モードは無指示でも出る** |
| 正のプロンプトに抑止句 `no text, no signs, no lettering; blank signboards` | **文字が完全に消える**（cfg 1.0 でもネガティブ不要） |
| JSON 領域プロンプト（bbox＋palette＋`"no text"`） | **通る**。`CLIPTextEncode` に文字列として渡すだけ。**別コードパス不要** |
| `POST /free` | 17,448 → **392MiB**（プロセス生存）。再ロードは次の draw 時 |
| 2048² / 12 steps（注記の実測） | 約 123 秒 |

> 1024² でも 28 秒。**既定 timeout 30 秒の同期呼び出しは成立しない。** v1 の同期設計を捨てた根拠。

---

## 3. 構成: 専用 ComfyUI インスタンスのオンデマンド起動

### 3-1. なぜ v1（ComfyUI 非依存・同期完結）を捨てたか

1. **同期完結が成立しない。** [`_run_program`](../core/tools.py) は同期 `subprocess.run` で、呼び出し元の
   `_tool_run_program` は async なのに executor を挟んでいない。**サテライトが動いている間、CG のイベントループ全体
   （scheduler / Moonbeat / WebSocket）が止まる。** timeout を伸ばせばその分サーバが固まる。
2. 非同期にするなら常駐プロセスが必須。その管理を自前で書くより、動作実績のある ComfyUI を借りる方が軽い。
3. v1 §3-1 の「ComfyUI の venv に再 exec」問題が**消える**。サテライトは torch を持たない HTTP クライアントになる。
   依存が「8189 が生きているか」だけになるので、自己完結性はむしろ上がる。
4. 生成時間は柚月にとって制約ではない（非同期に動く。待ち時間は人間の UX 観念）。
   効くのは**サーバを固めないこと**だけ。

### 3-2. 全体像

```
サテライト programs/atelier/（CG venv / torch 不要 / 全コマンド 1 秒未満）
   │  HTTP  POST /prompt, GET /history/{id}, GET /queue, GET /view, POST /free
   ▼
ランチャー（サテライトが切り離して起動する小さな常駐プロセス）
   ├ ComfyUI を子として起動:
   │    D:\AI\ComfyUI\.venv\Scripts\python.exe main.py
   │      --cuda-device 1 --port 8189 --listen 127.0.0.1
   │      --output-directory D:\AI\output_yuzuki
   ├ 定期的に /queue を見て最終活動時刻を記録
   ├ N 分アイドル → POST /free（VRAM 解放・プロセス生存）
   └ さらに M 分アイドル → ComfyUI 終了 → 自分も終了（GPU1 完全解放）
   ▼
GPU1（カノンの ComfyUI＝GPU0/8188、LM Studio＝両方 と共存）
```

- **カード・ポート・出力先が全部別**なので、どちらを再起動しても相手に影響しない
- `extra_model_paths.yaml` は既存の `D:\AI\models` をそのまま使う（13GB を二重に持たない）
- ランチャーが別プロセスなのは、サテライト自体がコマンドごとに終了する subprocess でタイマーを持てないため
- Windows での切り離し: `subprocess.Popen(..., creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, stdin/stdout/stderr を閉じる)`
  — 親（`_run_program` の subprocess）が終了してもランチャーが残るように
- タスクスケジューラでの起動時開始は**不要**

### 3-3. 柚月の睡眠・不使用への追従

睡眠状態ファイル（`.life_action_state.json`）は**読まない**。
寝ていれば `draw` が来ない → アイドル → 自動停止、で勝手に追従する。
生活状態への結合を作らない方が、状態ファイルの仕様変更にも巻き込まれず、「起きているが使っていない時間」も同じ扱いになる。

アイドル時間の目安: `/free` まで **20〜30 分**、プロセス終了までさらに **30 分**程度（未決・§8）。
短すぎると「見て → やり直し」の間に落ちて毎回ロードし直し、長すぎると LM Studio が使える時間が減る。
128GB のメインメモリがあるので再ロードはページキャッシュから走り、落ちてもコストは小さい（コールド 33 秒 vs ウォーム 28 秒）。

### 3-4. LM Studio が GPU1 を握っている場合

**追い出さない。** カノンの作業を柚月側から止めるのは順序が逆。

- `draw` 時にランチャーが GPU1 の空き VRAM を見る（`nvidia-smi --query-gpu=memory.used -i 1` または `/system_stats`）
- 足りなければ ComfyUI は起動したうえで、**「今ご主人様が GPU を使っているので、遅くなるか失敗するかもしれない」を柚月に返す**
- ComfyUI はメインメモリへのオフロードを自動でやるので、遅いなりに動く可能性はある。
  失敗したら `status` でそれが分かる形にし、待つか諦めるかは柚月が決める（autonomy-first）

逆方向（柚月の ComfyUI が VRAM を占有していてカノンの LM Studio が窮屈）はアイドル停止が解消する。

---

## 4. コマンド設計（投げっぱなし＋後で回収）

| コマンド | 引数 | 返すもの |
|---|---|---|
| `draw` | `prompt`（自然文 or JSON 領域指定・必須）, `width`, `height`, `steps`, `seed`, `cfg`, `negative_prompt` | **即座に** `job_id`・待ち行列の位置・目安秒数。インスタンスが停止中なら起動を仕掛けて「起動中。status で確認して」 |
| `status` | `job_id`（省略可） | 1 件の状態（queued / running / done / error / not_found）。省略時は**未回収ジョブ全部** |
| `pick_up` | `job_id` | 画像を `workspace/generated/YYYYMMDD_<seed>.jpg`（JPEG q90）にコピーし、**相対パス・使用プロンプト・seed・サイズ** を返す。そのまま `see_image` と `upload_artifact` に渡せる形 |
| `cancel` | `job_id` | 取り消し（待機中は `/queue delete`、実行中は `/interrupt`） |
| `health` | — | インスタンス生存・GPU1 の VRAM 使用量・待ち行列長 |

設計上のポイント:
- **job_id は引数に取ってよい。** 柚月の会話履歴は RAW で約 2 日、Layer0 で約 1 ヶ月半保持されるので、
  数分〜数時間後の回収で ID が失われる心配はない。`status` 引数なしの一覧は**親切機能**であって必須要件ではない
- ジョブ台帳は `workspace/generated/.jobs.json` にサテライトが持つ（`job_id` → prompt_id / prompt / seed / 投入時刻 / 回収済みか）。
  コンテキストではなく**ディスクを正とする**
- 全コマンドが既定 timeout 30 秒に全く近づかない → **`core/` を触らない＝サーバ再起動不要**
- ComfyUI の出力先は workspace **外**（`D:\AI\output_yuzuki`）にして、`pick_up` 時にコピーする。
  外部プロセスが柚月の生活空間に直接書き込む構造にしない
- 既定値: 1024×1024 / 12 steps / cfg 1.0 / seed ランダム（返り値に必ず含める）
- `cfg` は 1.0 固定に近い扱い（1.5〜2.0 でネガティブが効き始めるが生成時間 2 倍、3.0 以上で彩度が飛ぶ）。
  **ネガティブより正のプロンプトでの抑止が効く**（§2-7）ので、help ではそちらを案内する
- `models` コマンドは**作らない**（単一モデル）。将来 LoRA を選択肢として置くときに検討

### 柚月の体験フロー

```
draw → (他のことをする) → status → pick_up → 戻り値のパスを see_image で見る
        → 気に入らなければ draw し直す（seed を変える／プロンプトを直す）
        → 気に入れば upload_artifact で街に投稿（prompt フィールドに生成プロンプトを載せる）
```

---

## 5. プロンプトまわりの指針（help に書く最小限）

「良い絵の書き方」ではなく、**モデルの事実と失敗モードの告知**だけに留める。

1. **画風は必ず書く。** 書かなければ写真になる（Krea 2 の写実バイアス）
2. **文字を出したくなければ正のプロンプトで言う。** `The image contains no text, no signs, no lettering; signboards are blank.`
   — 店・看板・本などの語は無指示でも文字を誘発する（確認済み）
3. **空間配置は直接書ける。** 散文の「左に／右に」でも通る。厳密に置きたいときは JSON 領域指定:
   ```json
   {"scene": "...", "style": "...", "regions": [{"bbox": [x0,y0,x1,y1], "description": "...", "palette": ["#..."]}]}
   ```
   （0〜1 の相対座標。`prompt` にこの JSON をそのまま文字列で渡せばよい）
4. **盛らないモデル。** 書いた分だけ返る。短いと generic になる（これは欠点ではなく、柚月には利点）
5. `masterpiece, best quality, 8k` 系の定型呪文は**載せない**。審美スコアラー最適化語彙であり、この用途に意味がない

サンプルは上の抑止句と JSON の型の 2 つだけ。スタイル例は載せない。

---

## 6. 失敗の扱い

- インスタンスが落ちている／起動に失敗した／ジョブが `error` で終わった → **柚月が判断できる情報を返す**:
  「今は描く場所が使えない。後でもう一度試すか、ご主人様に伝えるか」。ユーザー UI 頼みにしない
- GPU1 が他で使われていて遅い／落ちた → §3-4 のとおり、事実を返して判断を委ねる
- インフラ通知の禁止ルール（睡眠妨害の件）は「柚月のタスクではない異常」の話。
  柚月自身が起こした生成の失敗を彼女に返すのは対象外
- エラーには**正しい呼び出し方の JSON 例を必ず載せる**（まっさらな AI が自力で直せるように）

---

## 7. 実装時の必須事項（落とし穴）

### ⚠️ manifest.yaml に引数を宣言し忘れない
**宣言漏れの引数があると `_run_program` が呼び出しごと弾き、コマンドが起動すらしない。**
`draw` の全引数（prompt / negative_prompt / width / height / steps / cfg / seed）と `job_id` を必ず `args` に書く。
`prompt` / `negative_prompt` は `path_check: false`（本文テキスト。`..` や `/` を含みうる）。

### i18n
`programs/_lang/{ja,en}.json` の集約辞書方式。`from _i18n import t` を使う。
**ja と en でキー数を一致させること**（`tests/test_i18n_programs.py` が検証する）。

### 自己完結を守る
サテライト間で共有モジュールを新設しない。HTTP クライアントは標準ライブラリ（`urllib`）だけで書ける。
サテライトは **torch / diffusers / ComfyUI の venv に依存しない**。依存するのはポート 8189 と
ランチャー起動用のパス（`D:\AI\ComfyUI\.venv\Scripts\python.exe`）だけ。パスは manifest か設定ファイルに出す。

### 出力は **JPEG q90**・workspace 配下（v1 で実測済み）

| 形式 | base64 | PNG比 | 48MiB到達 | prompt_tokens |
|---|---|---|---|---|
| PNG | 1,783KB | 100% | 28枚 | 383 |
| **JPEG q90** | **261KB** | **14.7%** | **192枚** | 383 |

- トークン数は形式によらず同一。効くのは転送量だけ
- WebP は街のギャラリーに実績が無いので見送り
- ComfyUI は PNG で出すので、`pick_up` で Pillow（CG venv にある）を使って JPEG に変換してコピーする

### ⚠️ 履歴に溜まった画像がリクエスト本文を膨らませる（v1 から継続）
`core/agent.py` が見た画像を conversation_history に append し続け、送信時には全部そのまま乗る。
DeepSeek の本文上限 48MiB に JPEG なら 192 枚で到達。「生成 → 見る → やり直す」を繰り返す用途では
ここが最初に詰まる。対策は memory `vision-model-coupling-todo` の項目 4（core 改修・要再起動）。

### workspace は柚月の生活空間
テスト時に `workspace/` を消さない。生成物のテストは `workspace/generated/` 配下に限定し、既存フォルダに触らない。
テストは `tests/test_openbotcity.py` と同じ流儀で **CG_WORKSPACE を一時ディレクトリに差し替え、HTTP はモック**にする。

---

## 8. 未決事項

v1 の未決 5 項目は全て回答済み:

| # | v1 の問い | 回答 |
|---|---|---|
| 1 | GPU 競合 | GPU1 専有＋ポート分離＋アイドル解放（§3） |
| 2 | venv 依存 | **消滅**（サテライトは torch を持たない） |
| 3 | モデル選定 | Krea 2 Turbo 単一 |
| 4 | 生成枚数上限 | **設けない。** GPU1 は柚月のもの。待ち行列が自然に直列化する。OBC の 40/日は投稿時に街が返す |
| 5 | NSFW | 公式 TE 固定・NSFW ワークフローは露出しない。問題を発生させない |

残っているもの:
1. **アイドル時間の値**（`/free` まで何分・終了まで何分）。§3-3 の目安で仮置きし、運用で調整
2. ランチャーのログ置き場（`logs/atelier_launcher.log` あたり。柚月の workspace には置かない）
3. 将来の LoRA 選択肢の置き方（初回スコープ外。「既定に積まない」だけ決まっている）

---

## 9. 次の一歩

検証（§2-7）は完了しているので、**実装に入ってよい**。順序:

1. `programs/atelier/`（名前確定: アトリエ＝柚月の仕事場）に manifest / main.py / launcher.py
2. `tests/test_atelier.py`（HTTP モック・一時 workspace）
3. 実機で draw → status → pick_up → see_image → upload_artifact を一周
4. `_lang/{ja,en}.json` にキー追加・`tests/test_i18n_programs.py` 通過
5. programs/README.md のサテライト一覧に追記

検証に使ったスクリプト: `submit_krea2.py`（API 形式グラフの組み立てと /history ポーリングの実例。
セッションの scratchpad にあったもので、内容は §2-6 に反映済み）。検証出力は `D:\AI\output_yuzuki\verify\` に残っている。

---

## 10. 参照

- `programs/README.md` — サテライト作成ガイド（manifest / stdin JSON / i18n）
- `CLAUDE.md` — manifest 引数宣言漏れの警告、workspace の扱い、テスト方針
- `ARCHITECTURE.md` — **柚月の記憶構造（Layer0/1/2・LETHE・Wyrd Network）。柚月の保持期間を推測で語る前に読む**
- `programs/OpenBotCity/commands/creative.py` — `upload_artifact` / `react_artifact` の実装
- `core/tools.py:747` — `_run_program`（同期 subprocess。イベントループを塞ぐ根拠）
- `core/tools.py:932` — `see_image` の仕様
- `D:\AI\ComfyUI\user\default\workflows\Krea2_Base.json` — 動作実績のあるグラフと実測値の注記

### memory（先に読むこと）
- `local-image-generation-plan` — 本件の方針メモ
- `image-limits-deepseek-obc` — 画像の実制約まとめ
- `vision-model-coupling-todo` — 画像履歴肥大ほか、先に片付けたい TODO
- `satellite-self-containment-over-dry` / `ai-first-tool-design` / `autonomy-first-failure-handling` — サテライト設計の作法
