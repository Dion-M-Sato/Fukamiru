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
| 日本語解析 | `python -m pip install -e '.[japanese]'` |
| ローカル推敲 | `llama-server` とGGUFを準備し、`models.*.path` を設定 |
| 意味検索 | `python -m pip install -e '.[rag]'`、互換性のあるPyTorch、準備済みモデルキャッシュ |
| Mistral OCR | `python -m pip install -e '.[ocr]'` と環境変数 `MISTRAL_API_KEY` |
| 日本語OCR | `scripts/windows/install-ndlocr-lite.ps1` を実行し、表示されたコマンドを `ocr.ndlocr_command` に設定 |

意味検索を有効にする場合は、`retrieval.semantic_enabled = true` と、使用するデバイスに対応した `embedding_device`、`require_cuda`、`tokenizer_mode` を設定します。
モデルは処理中に自動ダウンロードしません。モデルとキャッシュは実行前に準備してください。

Windowsで追加依存とCUDA用PyTorchを導入する場合は、`install.ps1 -Full` を使えます。対応GPUと十分な保存容量が必要です。

## 保存と更新

`state.db` と `knowledge.db` は別ファイルです。設定・DB・資料・モデル・ログはGit管理しません。
更新前に処理を終了し、DBの整合性を保つ方法でバックアップします。ソースの更新時も `config/local.toml` は保持してください。
開発・検証用の設定では、日常利用中のDBとは異なる保存先とポートを指定します。
