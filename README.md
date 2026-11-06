# ふかみる / Fukamiru

資料の取り込み、検索、翻訳、原稿生成、人間によるレビューを、ローカルのブラウザで行う研究ワークベンチです。

## 主な機能

- PDF・EPUB・HTML・テキストの取り込み、言語別OCR、出典と書誌情報の管理
- SQLite全文検索、BGE-M3とFAISSを使うローカルRAG
- 出典の調査・引用照合を伴う翻訳と原稿生成、日本語の推敲
- 原稿の差分、保護した本文、人間による確認・承認、処理の再開
- 共通デザインのHTML原稿プレビュー、脚注リンク、処理フローの表示

## 動作環境

| 項目 | 必要な環境 |
| --- | --- |
| 基本実行 | Python 3.11以上、Git、Webブラウザ |
| Windows | PowerShellからインストール・起動・停止 |
| macOS・Linux | Python CLIで起動。一部の外部ツール連携は環境ごとの確認が必要 |
| UIの開発・検査 | Node.js / npm |
| 翻訳・原稿生成 | 利用可能なCodex CLIと認証済みのアカウント |
| ローカル推敲 | llama.cppの `llama-server` と対応するGGUFモデル |
| 意味検索 | 準備済みBGE-M3キャッシュ、FAISS、PyTorch。GPU利用時は対応するCUDA環境 |
| 保存先 | SQLite・GitにはNTFS等の対応ファイルシステムを使用 |

個人設定はGit対象外の `config/local.toml` に保存します。DB・資料・モデル・認証情報をソースと一緒に配布しないでください。
導入と機能別の設定は [セットアップ](docs/setup.md) を参照してください。

## API・外部サービス連携

| 連携先 | 用途 |
| --- | --- |
| OpenAI Codex CLI | 翻訳、原稿生成、根拠検査、日本語の推敲 |
| Mistral OCR | 対応言語の資料の文字認識 |
| OpenAlex / Crossref | 学術文献・DOI・書誌情報の検索 |
| 国立国会図書館サーチ | 日本語文献、書誌・典拠情報の検索 |
| Semantic Scholar / Open Library | 学術文献と書籍の候補検索 |
| RustXNG v0.30.0（任意） | Standard プロファイルによる原稿生成の補助Web検索 |
| 公開Webページ・検索結果 | 書誌候補の確認、公開本文の取得 |

外部推論・OCR・検索を有効にすると、対象の文章・画像・検索語を各サービスへ送信します。接続先の認証、利用料金、利用条件は各自で確認してください。

## 利用しているオープンソース

| 構成 | 主なソフトウェア |
| --- | --- |
| API・データ検証 | [FastAPI](https://fastapi.tiangolo.com/)、[Uvicorn](https://www.uvicorn.org/)、[Pydantic](https://docs.pydantic.dev/) |
| PDF・文書処理 | [pypdf](https://github.com/py-pdf/pypdf)、[PyMuPDF / MuPDF](https://pymupdf.readthedocs.io/) |
| 検索・機械学習 | [SQLite](https://www.sqlite.org/)、[FAISS](https://github.com/facebookresearch/faiss)、[NumPy](https://numpy.org/)、[PyTorch](https://pytorch.org/)、[Sentence Transformers](https://www.sbert.net/)、[Transformers](https://github.com/huggingface/transformers) |
| 日本語・OCR | [MeCab](https://taku910.github.io/mecab/)、[UniDic](https://clrd.ninjal.ac.jp/unidic/)、[GiNZA](https://github.com/megagonlabs/ginza)、[NDLOCR-Lite](https://github.com/ndl-lab/ndlocr-lite) |
| ローカル推論 | [llama.cpp](https://github.com/ggml-org/llama.cpp) |

各ソフトウェア・辞書・モデルには個別のライセンスが適用されます。PyMuPDF / MuPDFはAGPLまたはArtifex商用ライセンスです。
利用範囲とライセンスは [第三者ソフトウェアの表記](THIRD_PARTY_NOTICES.md) を参照してください。

## License

本ソフトウェアは、GNU General Public License v3.0（GPLv3）に基づいて公開されています。

GPLv3の条件に従う限り、商用・非商用を問わず、自由に利用・改変・再配布できます。ただし、著作権表示およびライセンス情報を保持し、原作者を偽って再配布することは禁止します。

GPLv3の条件によらない商用利用（ソースコードを非公開とする製品への組込みなど）については、別途商用ライセンスを提供しています。

商用ライセンスをご希望の場合は、原作者までお問い合わせください。

Copyright © 2026 [Masanao Sato]

ライセンス本文は [LICENSE](LICENSE) を参照してください。別途商用ライセンスが適用される場合も、第三者ソフトウェアのライセンス条件は別に適用されます。
