# tsukimi_browser — ネットを散歩するためのテキストブラウザ

柚月が「調べ物」ではなく**ぶらぶら眺める**ためのサテライト。読むだけで、書き込み・送信・購入はしない。

```
start → lucky / open / search → follow n → more → look → bookmark → end
```

| ファイル | 役割 |
|---|---|
| `main.py` | コマンドのディスパッチと画面の組み立て |
| `browser.py` | 取得（`http_get`）・URL 安全チェック（`check_url`）・本文抽出・ページ送り |
| `session.py` | 散歩の状態（現在ページ・履歴・棚・歩数）。`workspace/program_data/tsukimi_browser/` |
| `startpage.py` | 玄関・検索結果・棚・履歴といった「こちらで組んだ画面」 |
| `shot.py` | **`look` の中身。ページの見た目を1枚の絵にする** |

## look — 目を開ける（2026-08-23〜）

テキスト抽出では落ちるもの（図・グラフ・写真・レイアウト・画像の中の文字、
本文抽出が空になる SPA）を確かめるためのコマンド。**散歩の途中で気になったときだけ使う**もので、
毎回撮るものではない。

```
look             いま居るページの上のほうを1枚
look n=2         上から2画面目（8 まで）
look url=...     そこへ移ってから撮る（open と同じく歩数を1つ使う）
```

撮れた絵は結果と一緒に柚月へ届く（`programs/README.md` §3 の `image` キー）。
細かい字は `see_image` に `source="last"` と `region`（例 `"3x3:5"`）を渡して寄れる。

### 描画には「このPCの Chrome」を使う

Playwright を入れると Chromium が別に 300MB 増えるが、既にある Chrome（無ければ Edge）を
`--headless=new --screenshot` で呼べば追加依存ゼロで同じことができる。実測 1.4 秒、
`run_program` 経由の往復込みで 2.8 秒。**見つからなければ `look` だけが丁寧に断り、
テキストの散歩はこれまでどおり続く**（配布版で Chrome が無い環境を想定）。

`--headless=new` が必須。旧 `--headless` は `chrome://newtab` を開こうとしたり
GCM 登録で固まったりして、実際に 2 分待っても返ってこなかった。
`--disable-background-networking` などで常駐と余計な通信も止めている。

### 安全のしくみ（触るときはここを壊さないこと）

1. **生の URL を Chrome に渡さない。** 撮るのは必ず `browser.http_get` を通った後の URL。
   あれはリダイレクトを自前で追い、**毎ホップ `check_url` し直す**ので、
   localhost（Crescent Grove 本体・SearXNG）や私有 IP へ誘導されない。
   `cmd_look` が `url` を受けたときも、先に `goto()` を通してから撮っている。
2. 保険として `--host-resolver-rules` で localhost 名を潰す。
3. プロファイルは毎回**使い捨ての一時ディレクトリ**。カノンの Chrome の Cookie・履歴・
   拡張には触れないし、残さない。
4. `--user-agent` はテキスト側と同じ（`browser.USER_AGENT`）。同じ客として扱われるので、
   検証した URL と Chrome が見る URL がずれにくい。

残る穴: サイトが UA ではなく他の手掛かりで Chrome にだけ別のリダイレクトを返した場合、
2. をすり抜けて IP 直書きの私有アドレスへ行きうる。実害は「柚月がローカル画面を見る」までだが、
ここを塞ぐには Chrome 側の解決先を固定するしかなく、そうすると CDN の CSS/画像が
読めなくなって撮る意味が無くなるため、現状は 1.〜4. で止めている。

### そのほかの決めごと

- Chrome の CLI には「途中までスクロールして撮る」機能が無い。`n` は**窓を縦に伸ばして撮り、
  下端を切り出す**（`crop_screen`）。伸ばすほどページが縦長レイアウトで描かれる点は近似。
- 玄関・検索結果・棚などの**仮想ページは撮らない**（こちらで組んだ画面なので意味がない）。
- 撮った絵は 20 枚まで（`shot.KEEP_SHOTS`）。古いものから消して workspace を太らせない。
- `shot.CAPTURE_TIMEOUT`(25s) は manifest の `timeout`(60) より必ず短く保つ。
  逆転すると `_run_program` が subprocess ごと殺し、柚月には何も返らない。

## テスト

```
venv\Scripts\python.exe tests\test_tsukimi_browser.py
```

ネットワークにも Chrome にも出ない（`subprocess.run` と `shot.capture` を差し替える）。
`look` については、フラグの中身・`n` による窓の伸縮と切り出し・撮り溜めの掃除・
**`image` がトップレベルに乗ること**（ここが崩れると core が絵として拾わない）・
撮れなくても本文が読めること・歩数ハードストップを迂回しないことを検査する。
