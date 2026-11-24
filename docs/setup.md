# セットアップ

Python 3.11以上とGitを用意します。UIの開発・検査にはNode.js / npmも必要です。
原稿の出力とプレビューはHTMLです。追加の組版エンジンは不要です。
プレビューの「保存」から、スタイルを内包するHTMLを保存できます。`cite` は斜体にしません。

## Windows

リポジトリ直下で実行します。

```powershell
PowerShell -ExecutionPolicy Bypass -File .\scripts\windows\install.ps1
PowerShell -ExecutionPolicy Bypass -File .\scripts\windows\start.ps1
```

ブラウザで <http://127.0.0.1:8766> を開きます。停止は次のコマンドです。

```powershell
PowerShell -ExecutionPolicy Bypass -File .\scripts\windows\stop.ps1
```

インストーラは `.venv` と `config/local.toml` を作成し、空のDBを初期化します。既存のローカル設定は上書きしません。

## Python CLI

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
cp config/local.example.toml config/local.toml
fukamiru init
fukamiru start
```

Windowsで手動実行する場合は `.venv\Scripts\Activate.ps1` で仮想環境を有効にします。
設定ファイルは `fukamiru --config /path/to/local.toml start` または `FUKAMIRU_CONFIG` で指定できます。
設定内の相対パスは、設定ファイルの配置先に関係なくリポジトリ直下から解決します。

## 機能別の依存

| 機能 | 設定 |
| --- | --- |
| 翻訳・原稿生成 | Codex CLIを準備し、`codex login status` で認証を確認 |
| 原稿生成の補助Web検索（任意） | RustXNG v0.30.0 の Standard プロファイル。[導入と起動](specification/research-source-handoff.md) |
| 日本語解析 | `python -m pip install -e '.[japanese]'` |
| ローカル推敲 | `llama-server` とGGUFを準備し、`models.*.path` を設定 |
| 意味検索 | `python -m pip install -e '.[rag]'`、互換性のあるPyTorch、準備済みモデルキャッシュ |
| Mistral OCR | `python -m pip install -e '.[ocr]'` と環境変数 `MISTRAL_API_KEY` |
| 日本語OCR | `scripts/windows/install-ndlocr-lite.ps1` でNDLOCR-Lite 2.0.0を導入し、表示されたコマンドを `ocr.ndlocr_command` に設定 |

意味検索を有効にする場合は、`retrieval.semantic_enabled = true` と、使用するデバイスに対応した `embedding_device`、`require_cuda`、`tokenizer_mode` を設定します。
端末で使う埋め込み・ローカル推論モデルは実行前に準備してください。
HF校正用workerはJob起動時に固定モデルとランタイムを取得します。

Windowsで追加依存とCUDA用PyTorchを導入する場合は、`install.ps1 -Full` を使えます。対応GPUと十分な保存容量が必要です。
このオプションにはHF校正用の追加依存・認証は含まれません。

## NDLOCR-Lite 2.0への更新

Python 3.11以上を使用し、リポジトリ直下で次のスクリプトを実行します。
既存の導入先に再実行すると、検証済みの2.0.0へ更新します。

```powershell
PowerShell -ExecutionPolicy Bypass -File .\scripts\windows\install-ndlocr-lite.ps1
```

専用環境へPyPIパッケージを導入し、公式モデル4個の取得・SHA-256確認とCLIのバージョン確認を行います。
表示された実行ファイルのパスを、利用者の`config/local.toml`の`ocr.ndlocr_command`に設定してください。
モデルはOCR実行前に準備します。

複数ページPDFは一括JSONとして取り込み、不要な透明PDFの生成を抑止します。
部分OCRもv2モデルを使用し、更新前の部分OCRキャッシュを再利用しません。
認識結果の旧字体は保持し、既存の言語別振り分けを継続します。

## 現行33B校正をHF Jobsで使う

基本導入を済ませ、リポジトリ直下で追加依存を入れます。

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[hf-jobs]'
.\.venv\Scripts\hf.exe auth login
```

HFでJobsを実行・管理できる権限と課金設定を用意してください。
実行先のアカウントまたは組織に正のクレジット残高が必要です。
利用条件は[HF Jobsの課金](https://huggingface.co/docs/hub/jobs-pricing)を参照してください。
認証はHFの認証ストア、または環境変数`HF_TOKEN`から読みます。
トークンをTOML、ソース、コマンド引数へ書く必要はありません。
翻訳・原稿生成・意味変更の確認・表記整形には、別に認証済みCodex CLIも必要です。
`codex login status`で確認し、`gpt-5.6-sol`と`gpt-6-luna`を利用できるCLI・アカウントを使います。

既存の`config/local.toml`にある同名sectionを編集します。sectionを重複追加しないでください。

```toml
[translation]
japanese_proofreading_profile = "llmjp33b_adopt"

[hf_proofreading]
enabled = true
namespace = "your-hf-account"
max_minutes = 60
max_cost_usd = 5.01
max_hourly_usd = 5.01
startup_seconds = 600
```

`namespace`は自分のHFアカウントまたは実行権限のある組織です。
上記のUSD値はアプリが起動前に検査する費用枠の例で、固定の実料金ではありません。
現在のH200単価とprivate port料金を実行前に取得し、時間・費用枠に収まらなければ起動を拒否します。
Jobのhard timeoutには終了用の余裕を取り、モデル取得・起動時間も処理時間に含みます。
アプリの予約額とHFの請求メーターは別です。
[HFの認証・権限](https://huggingface.co/docs/hub/jobs-configuration)と
[料金・課金](https://huggingface.co/docs/hub/jobs-pricing)も確認してください。

サービスを再起動し、UIのhealth表示で校正backendの設定状態を確認します。
起動、health確認、Run作成だけではGPUを割り当てません。
日本語校正を有効にした原稿が対象ノードへ到達し、保存済みの応答がないときに初めて起動します。
校正を無効にした原稿ではこのJobを起動しません。

この方式はLLM-jp-4.1 33B thinking BF16・highを使い、8Bや量子化モデルへ自動代替しません。
初期設定の`local_v32`は別方式なので、HFを有効にする場合はprofileも必ず変更してください。
[校正の契約](specification/japanese-proofreading.md)と
[HFの停止・再開・失敗時の扱い](specification/hf-proofreading-jobs.md)を参照してください。

## 同じ33Bを専用サーバーで使う

`llmjp33b_adopt`を選び、`hf_proofreading.enabled = false`として、
`models.llm_jp_proofreading`の`endpoint`または`path`・`server_command`を設定します。
endpointにはllama.cppサーバーのルートURLを使います。
OpenAI互換の生成APIだけでなく、`/props`、`/apply-template`、`/tokenize`も必要です。
33B BF16の実際の読込ファイル、1要求65,536以上のcontext、highのテンプレートを検証してから送信します。
BF16モデルとcontextを収容できる環境が必要です。HF用の固定ランタイム情報は
`src/fukamiru/local_llm/hf_protocol.py`を参照してください。

## 意味検索とモデルキャッシュ

設定例は全文検索だけを有効にしています。意味検索を使う場合は`.[rag]`を追加し、
`config/defaults.toml`にあるBGE-M3の固定revisionをあらかじめキャッシュへ準備します。
`retrieval.embedding_cache_dir`が空なら`paths.runtime_root/cache/huggingface`を使用します。
Runの途中で不足する埋め込みモデルを自動ダウンロードする構成ではありません。

Windowsの`-Full`はPyTorch 2.13.0 / CUDA 12.6と対応NVIDIA GPUを確認します。
別のPyTorch・GPU構成は個別導入し、モデルの読込・索引作成・検索を確認してください。
HF校正だけを使う端末に、このCUDA構成を要求するものではありません。

## 保存と更新

`state.db` と `knowledge.db` は別ファイルです。設定・DB・資料・モデル・ログはGit管理しません。
更新前に処理を終了し、DBの整合性を保つ方法でバックアップします。ソースの更新時も `config/local.toml` は保持してください。
開発・検証用の設定では、日常利用中のDBとは異なる保存先とポートを指定します。

公開旧版からの更新では、既存のstate migration 0055までを変更せず、
0056（検索レポート）・0057（HF Jobと受領記録）を追加します。
停止後の整合したDBコピーを保管し、新しいソースで`fukamiru init`を実行してから起動します。
DBだけでなくProjectのGit履歴や資料・成果物も必要に応じてバックアップしてください。
復旧時は対応する旧ソースとDBバックアップを組にして戻します。
schema更新後に旧ソースへ切り替えるだけの復旧は行わないでください。

## 開発・公開検査

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest
npm --prefix ui run check
.\.venv\Scripts\python.exe scripts/check_publication.py
git diff --check
```

生成した図は`scripts/generate_manuscript_graph.py --check`で確認します。
33B方式の図は`--llmjp33b-adopt --check`、別の33B profileは`--llmjp33b-high --check`です。
公開検査はGitの追跡ファイルと無視されていない新規ファイルを対象にします。
配布ZIPも`--archive`で検査してください。

レビュー・検索のブラウザ回帰は、開発用依存を追加して実行します。

```powershell
npm ci --prefix ui
npm --prefix ui run check:browser
```

この検証はheadless Edgeを使用し、架空データのHTTPサーバーだけを起動します。
通常利用やPythonの基本導入にPlaywrightは不要です。
検証結果とスクリーンショットはGit対象外の`output/verification-*`へ保存します。
