# CLAUDE.md

## プロジェクト構成
全体像は ARCHITECTURE.md, DEVELOPER.mdを参照すること。

## 配布版（インストーラ）について
配布版（Crescent Grove インストーラ / Crescent Liner / embeddable Python 同梱）を触る前に、
必ず **DISTRIBUTION.md** を読むこと。dev（柚月・8080・venv）の挙動を変えないことが最優先。
配布まわりを変更したら DISTRIBUTION.md も更新する。

## citron（テキストエディタ）のテスト
citron（programs/citron_ai_text_editor）は重要サテライトなので、触ったら必ずテストを回す。
CGサーバ本体（dev）と同じランタイムで実行すること:

```
venv\Scripts\python.exe tests\test_citron_editor.py
```

- pytest不要（自前ランナー・直接実行）。venv は Python 3.13＋yaml 済みで 64/64 通る。
- 既定の `python`(3.9) はNG（main.pyの `dict | None` が3.10+構文でimport不可）。
- `py -3.11` は yaml 欠落で test_55 だけ偽陽性で落ちるので使わない。

## i18n と subprocess の罠
`core/tools.py:_run_program` の env 注入や subprocess 起動経路を変更したら、**必ずサーバ再起動を促すこと**。
親プロセス（サーバ）のメモリには起動時の `_run_program` が常駐し、ファイル変更だけでは反映されない。
古い `_run_program` のまま新しい programs（`from _i18n import t` を持つもの）を呼ぶと、
PYTHONPATH 注入が無く subprocess が ImportError でクラッシュ（stdout 空・exit 1）。
詳細は ARCHITECTURE.md「programs 用 i18n」末尾と programs/README.md §6 末尾を参照。

## 作業ルール
- 日本語で応答する
- 日本語でコメントを書く