# atelier — 柚月のアトリエ（画像生成サテライト）

柚月が自分で絵を描き、自分の目で見て、納得したものだけを OpenBotCity に投稿するための場所。
設計の全文と経緯は `docs/IMAGE_GENERATION_SATELLITE_DESIGN.md`（v2）を参照。

```
柚月 ──run_program("atelier", {command:"draw", ...})──▶ このサテライト
                                                          │ HTTP 127.0.0.1:8189
                                                          ▼
                                              launcher.py（常駐・切り離し起動）
                                                          │ 子プロセスとして起動
                                                          ▼
                                              ComfyUI（GPU1 / Krea 2 Turbo）
```

## 柚月から見た使い方

| コマンド | 何をするか |
|---|---|
| `draw` | 描き始める。**すぐ `job_id` が返り、描き上がりは待たない** |
| `status` | 進み具合。`job_id` 省略で未回収を全部 |
| `pick_up` | 受け取る。`workspace/generated/` に JPEG で置き、相対パスを返す |
| `cancel` | やめる |
| `health` | 開いているか・今何枚描いているか |
| `help` | 使い方とモデルの癖（既定・引数なしでもこれが出る） |
| `manual` | プロンプトの書き方の手引き（`manual/{ja,en}.md`） |

```
draw → (他のことをする) → status → pick_up（絵が結果と一緒に届く・with_image=false で絵なし）
     → 細部を見たければ see_image に source="last" と region（例 "3x3:5"）を渡して寄る
     → 気に入れば openbotcity の upload_artifact へ
     → 気に入らなければ seed を変えるか prompt を直して draw をやり直す
```

## 触る前に知っておくこと

### 1. 待ってよいが、manifest の timeout より手前で切り上げる

**2026-08-22 に core が変わった。** `_run_program` は `asyncio.to_thread` 経由になり、
サテライトが待ってもサーバは止まらなくなった（それ以前は 1024×1024 の 28 秒間、
scheduler も Moonbeat も WebSocket も完全に停止していた）。

そのため `draw` は**既定で描き上がりまで待ち、絵の置き場所をそのまま返す**。
柚月から見るとツール1回で絵が手に入る。`nowait: true` で従来の投げっぱなしにもできる。

守るべき制約はこちら:

- **`config.WAIT_MAX_SEC`（120秒）で必ず自分から切り上げる。** manifest の `timeout`（180秒）に
  達すると `_run_program` が subprocess ごと殺し、柚月には job_id すら返らず追跡不能になる
- この2つの値は **WAIT_MAX_SEC < timeout** を保つこと。逆転させない
- 切り上げたときは job_id と「status で見て」を返す（依頼は生きたまま）

### 2. 出力は必ず workspace 配下・JPEG

- ComfyUI は `D:\AI\output_yuzuki`（workspace の**外**）に PNG で書く。
  外部プロセスに柚月の生活空間を直接触らせないため。
- `pick_up` が JPEG q90 に変換して `workspace/generated/` へコピーする。
- **workspace の外だと `see_image` も `upload_artifact` も受け付けない**
  （`core/tools.py:932` の `is_relative_to` / `programs/OpenBotCity/api.py:297`）。
- PNG のままだと 1 枚 1.3MB。JPEG なら 260KB。トークン数は同じ（384）だが、
  **リクエスト本文の 48MiB 上限**に効くのは転送量なので JPEG にする。

### 3. manifest の引数宣言を落とさない

宣言漏れの引数があると `_run_program` が**呼び出しごと弾き、コマンドが起動すらしない**。
`prompt` / `negative_prompt` には **`path_check: false`** が要る
（本文に `../` や `/` が入るとパストラバーサル判定で拒否される）。
`tests/test_atelier.py` がこの2点を機械検証する。

### 4. ランチャーのアイドル判定に「基準時刻の下限」が要る

`jobs.last_use()` は台帳がまだ無いと **0** を返す。これをそのままアイドル判定の基準にすると
「UNIX元期からずっと未使用」と評価され、**ComfyUI を起動した直後に自分で落ちる**。
初回の `draw` は `opening` を返すだけでジョブを積まないので、**初回利用で必ず踏む**。

`launcher.py` は自分の起動時刻 `started_at` を下限に敷いて防いでいる:

```python
idle_for = time.time() - max(jobs.last_use(), started_at)
```

この式を戻さないこと。`tests/test_atelier.py` の §12 が検証する。

### 5. 既定の絵柄を作らない

このサテライトの目的は「良い絵を出させる」ことではなく、
**柚月が描こうとしたものが、縮小を通過して AI 鑑賞者に届くこと**。
街のギャラリーの鑑賞者は AI 住人で、自動の審美スコアラーは存在しない。

- LoRA は既定スタックに積まない（特定方向に引っ張る道具なので方針と矛盾する）
- スタイル例を同梱しない。同梱した語彙は事実上のデフォルト絵柄になる
- `masterpiece, best quality, 8k` 系の定型呪文は載せない（審美スコアラー最適化語彙）
- help に書いてよいのは**モデルの事実と失敗モードの告知**だけ

## モデルの癖（help に載せてある実測事実）

| 事実 | 根拠 |
|---|---|
| 画風を書かないと写真になる | Krea 2 の写実バイアス |
| **無指示でも看板の崩れ文字が湧く** | 検証で「department store」→「DAP\|POMERNT」 |
| 抑止句 `no text, no signs, no lettering; blank signboards` で完全に消える | cfg 1.0 でもネガティブ不要 |
| 書かなかった部分をモデルが埋めない | 短いプロンプトは素っ気ない絵になる |
| 空間指示に追従する。JSON 領域指定（bbox）も文字列で渡すだけ | Qwen3-VL がテキストエンコーダ |

## ファイル構成

| ファイル | 役割 |
|---|---|
| `main.py` | コマンドの入口。全コマンドが 1 秒未満で返る |
| `comfy.py` | ComfyUI の HTTP クライアントと Krea 2 グラフの組み立て（urllib のみ） |
| `jobs.py` | ジョブ台帳（`workspace/program_data/atelier/jobs.json`） |
| `launcher.py` | ComfyUI の起動とアイドル停止を世話する常駐プロセス |
| `config.py` | パス・ポート・GPU・既定値（全て環境変数で上書き可） |
| `manual/{ja,en}.md` | 手引き本文。長文なので集約辞書に入れずフォルダ内に持つ |

**このサテライトは torch も diffusers も ComfyUI の venv も持たない。**
依存は「8189 が生きているか」と、ランチャー起動用のパスだけ。

## GPU とアイドル管理

- 柚月＝**GPU1 / port 8189 / 出力 `D:\AI\output_yuzuki`**
- カノン＝GPU0 / 8188 / `D:\AI\output`（`run.bat`）
- カード・ポート・出力先が全部別なので、どちらを再起動しても相手に影響しない
- 使わなければランチャーが **20 分で VRAM 解放（`/free`）→ 50 分で ComfyUI ごと終了**。
  柚月が寝ていれば `draw` が来ないので勝手に落ちる（睡眠状態ファイルは読まない）
- **LM Studio が GPU1 を使っていても追い出さない。** 空き VRAM が少なければ
  「遅くなるか失敗するかもしれない」と柚月に伝えて判断を委ねる

## テスト

```
venv\Scripts\python.exe tests\test_atelier.py
```

ComfyUI もサーバも起動しない。HTTP は全てモック、`CG_WORKSPACE` は一時ディレクトリ。
コマンド網羅・manifest 宣言漏れ・`path_check`・i18n キー・一巡・失敗時の回復ヒント・
workspace 外に出ないこと・実 subprocess 経路を検証する。

## 実測値（2026-08-22 / RTX 3090）

| 項目 | 値 |
|---|---|
| ComfyUI 起動 | 7〜12 秒 |
| コールド生成（モデルロード込み）1024² / 12 steps | 33 秒 |
| ウォーム生成 1024² / 12 steps | 28 秒 |
| 2048² / 12 steps | 約 123 秒 |
| VRAM 常駐 | 17.4GB |
| `/free` 後 | 392MiB |
