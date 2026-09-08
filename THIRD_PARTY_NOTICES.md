# 第三者ソフトウェアのライセンス表記 (Third-Party Notices)

本ソフトウェア **Crescent Grove** は、以下のオープンソースソフトウェアを利用・同梱しています。
各コンポーネントの著作権は、それぞれの権利者に帰属します。

本ファイルは、利用している各ライセンス（MIT / BSD / Apache-2.0 等）が求める
**著作権表示・許諾文・NOTICE の保持義務**を満たすために配布物へ同梱されます。
配布の際は、本ファイルを必ず同梱してください。

> 注: 下表は主要な直接依存および同梱物を列挙したものです。これらが推移的に
> 依存するパッケージ（numpy, torch, transformers, tokenizers, starlette,
> anyio など）も、それぞれ MIT / BSD-3-Clause / Apache-2.0 等の許諾型ライセンスで
> 配布されています。完全な一覧は、各環境で次のコマンドにより生成できます。
>
> - Python: `pip-licenses --format=markdown`（`pip install pip-licenses`）
> - Node:   `npx license-checker --summary`（`liner/` 配下で実行）

---

## 1. Python 依存ライブラリ

| コンポーネント | ライセンス | 著作権者 |
|:---|:---|:---|
| FastAPI | MIT | Sebastián Ramírez |
| Starlette（FastAPI 依存） | BSD-3-Clause | Encode OSS Ltd. |
| Uvicorn | BSD-3-Clause | Encode OSS Ltd. |
| websockets | BSD-3-Clause | Aymeric Augustin and contributors |
| openai (openai-python) | Apache-2.0 | OpenAI |
| PyYAML | MIT | Kirill Simonov / PyYAML contributors |
| tiktoken | MIT | OpenAI |
| tavily-python | MIT | Tavily |
| Beautiful Soup 4 | MIT | Leonard Richardson |
| Requests | Apache-2.0 | Kenneth Reitz and contributors |
| HTTPX | BSD-3-Clause | Encode OSS Ltd. |
| duckduckgo-search / ddgs | MIT | deedy5 |
| ChromaDB | Apache-2.0 | Chroma, Inc. |
| cryptography | Apache-2.0 OR BSD-3-Clause | The Python Cryptographic Authority and contributors |
| bcrypt | Apache-2.0 | The Python Cryptographic Authority |
| sentence-transformers | Apache-2.0 | UKP Lab / Hugging Face |
| fugashi | MIT | Paul O'Leary McCann |
| unidic-lite（パッケージ） | MIT | Paul O'Leary McCann |
| UniDic 辞書データ（unidic-lite 同梱） | BSD-3-Clause | The UniDic Consortium |
| aiohttp | Apache-2.0 | aiohttp maintainers |
| faiss-cpu (Faiss) | MIT | Meta Platforms, Inc. |
| numpy（推移依存） | BSD-3-Clause | NumPy Developers |
| PyTorch（sentence-transformers 経由） | BSD-3-Clause | Meta Platforms, Inc. and affiliates |
| Transformers / Tokenizers（推移依存） | Apache-2.0 | Hugging Face, Inc. |

---

## 2. 同梱モデル・データ

| 名称 | ライセンス | 提供元 / 出典 |
|:---|:---|:---|
| intfloat/multilingual-e5-small（埋め込みモデル） | MIT | Microsoft |
| MoonTide v2 感情遷移確率行列 | 学術データ（論文引用） | Thornton & Tamir (2017), *PNAS*, "Mental models accurately predict emotion transitions" |

> 埋め込みモデルはセットアップ時に Hugging Face Hub から取得され、ローカルへキャッシュされます。
> モデルを配布物に同梱する場合は、上記 MIT 表記を併せて保持してください。

---

## 3. Crescent Liner（Electron クライアント）依存

| コンポーネント | ライセンス | 著作権者 |
|:---|:---|:---|
| Electron | MIT | GitHub Inc. / OpenJS Foundation and contributors |
| electron-updater | MIT | Roman Shtylman / electron-userland |
| electron-builder | MIT | Vladimir Krivosheev / electron-userland |
| electron-vite | MIT | Alex Wei and contributors |
| Vite | MIT | Evan You / Vite contributors |
| TypeScript | Apache-2.0 | Microsoft Corporation |
| @electron-toolkit/tsconfig 他 | MIT | alex8088 and contributors |

### Electron / Chromium の同梱クレジットについて

Electron アプリ（NSIS インストーラ）は、内部に **Chromium** および **Node.js** を同梱します。
これらのライセンス全文は、`electron-builder` がビルド時に配布物へ自動同梱します
（インストール先の `LICENSE.electron.txt` および `LICENSES.chromium.html`）。
配布物からこれらのファイルを削除しないでください。主な内訳は以下のとおりです。

- **Electron** — MIT
- **Chromium** — BSD-3-Clause ほか（多数のサードパーティを含む。詳細は `LICENSES.chromium.html`）
- **Node.js** — MIT ほか

---

## 4. 外部サービス（API）について

本ソフトウェアは、利用者が設定した API キーを用いて以下の外部 LLM / API サービスへ
接続する機能を持ちます。これらはソフトウェアに同梱されておらず、各サービスの
利用規約・プライバシーポリシーが別途適用されます。

- DeepSeek API
- OpenAI API
- Anthropic Claude API
- Tavily / DuckDuckGo（Web 検索）
- Open-Meteo（天気情報）
- NHK / 各種ニュース取得

---

## 付録: 主要ライセンス全文

### A. MIT License

上表で「MIT」と記載されたコンポーネントは、以下の許諾条件で配布されています
（著作権者名は各コンポーネントのものに読み替えてください）。

```
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### B. BSD 3-Clause License

```
Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its contributors
   may be used to endorse or promote products derived from this software
   without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### C. Apache License 2.0

上表で「Apache-2.0」と記載されたコンポーネントは、Apache License, Version 2.0
の条件で配布されています。同ライセンス全文は次の URL を参照してください。

<http://www.apache.org/licenses/LICENSE-2.0>

Apache-2.0 の要求事項の要旨:

- ライセンスのコピーを頒布物に含めること。
- 変更したファイルには、変更した旨の告知を付すこと。
- 元の著作権・特許・商標・帰属表示を保持すること。
- 当該コンポーネントに `NOTICE` ファイルが含まれる場合、その内容を頒布物の
  `NOTICE` 表示または本ファイルに保持すること。

本プロジェクトは、これら Apache-2.0 ライセンスのコンポーネントを**改変せず**、
pip / npm を通じて取得される依存物として利用しています。

---

*このファイルは配布物の一部です。再配布の際は削除・改変しないでください。*
*依存パッケージを追加・更新した場合は、本ファイルの一覧も更新してください。*
