# Third-party software

Fukamiruのライセンスは、第三者ソフトウェア・辞書・モデルのライセンスを置き換えません。
このリポジトリはアプリケーションのソースを提供します。実行環境やモデルのファイルを同梱する場合は、それぞれの著作権表示・ライセンス本文・再配布条件も保持してください。

## Pythonの直接依存

| ソフトウェア | 用途 | ライセンス・配布元 |
| --- | --- | --- |
| FastAPI | HTTP API | MIT — <https://github.com/fastapi/fastapi> |
| Uvicorn | ASGIサーバー | BSD-3-Clause — <https://github.com/Kludex/uvicorn> |
| Pydantic | データ検証 | MIT — <https://github.com/pydantic/pydantic> |
| pypdf | PDF処理 | BSD-3-Clause — <https://github.com/py-pdf/pypdf> |
| PyMuPDF / MuPDF | PDF描画・文字抽出 | AGPL / Artifex商用ライセンス — <https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright> |
| cryptography | 暗号化PDF等 | Apache-2.0 OR BSD-3-Clause — <https://github.com/pyca/cryptography> |
| truststore | OS証明書ストア | MIT — <https://github.com/sethmlarson/truststore> |
| FAISS | ベクトル検索 | MIT — <https://github.com/facebookresearch/faiss> |
| NumPy | 数値計算 | BSD-3-Clause。配布物には追加ライセンスあり — <https://numpy.org/> |
| PyTorch | 推論 | BSD系等。配布物の同梱コンポーネントにも個別条件あり — <https://github.com/pytorch/pytorch> |
| Sentence Transformers | 埋め込み | Apache-2.0 — <https://github.com/huggingface/sentence-transformers> |
| Transformers | モデル実行 | Apache-2.0 — <https://github.com/huggingface/transformers> |
| mecab-python3 / MeCab | 日本語解析 | 各配布物のBSD・LGPL・GPL等の条件を参照 — <https://github.com/SamuraiT/mecab-python3> |
| UniDic Lite | 日本語辞書 | UniDicの辞書ライセンスとパッケージの条件を参照 — <https://github.com/polm/unidic-lite> |
| GiNZA / ja_ginza | 日本語解析 | MIT等。利用モデル・辞書の条件も参照 — <https://github.com/megagonlabs/ginza> |
| Mistral Python SDK | OCRサービス接続 | SDKのLICENSEを参照 — <https://github.com/mistralai/client-python> |
| Hugging Face Hub SDK / CLI | 任意のHF Jobs校正・認証 | Apache-2.0 — <https://github.com/huggingface/huggingface_hub/blob/main/LICENSE> |

## 外部ツール・モデル

| ソフトウェア等 | 用途 | ライセンス・配布元 |
| --- | --- | --- |
| SQLite | データ保存・全文検索 | Public domain — <https://www.sqlite.org/copyright.html> |
| Git | 原稿の版管理 | GPLv2 — <https://git-scm.com/> |
| llama.cpp | ローカル推論 | MIT — <https://github.com/ggml-org/llama.cpp> |
| NDLOCR-Lite | 日本語OCR | 国立国会図書館、CC BY 4.0。依存ライブラリとモデルには別条件あり — <https://github.com/ndl-lab/ndlocr-lite> |
| BGE-M3等のモデル | 埋め込み・推論 | 各モデルのモデルカード・ライセンスを参照。重みは同梱しない |
| LLM-jp-4.1 33B thinking GGUF | 日本語校正 | Apache-2.0。固定revision・hashはソースに記載。重みは同梱しない — <https://huggingface.co/llm-jp/llm-jp-4.1-33b-thinking-gguf> |

開発用のpytest・pytest-cov・pytest-xdist・Ruff・httpx・Hatchlingなどにも各配布物のライセンスが適用されます。
UIの独立ブラウザ検証には[Playwright](https://github.com/microsoft/playwright)（Apache-2.0）を使用します。ブラウザ本体は同梱しません。
外部API・検索サービスの利用条件と、取得した資料の権利はソフトウェアのライセンスとは別です。

商用ライセンスの提供対象は原作者が権利を持つ部分です。PyMuPDF / MuPDF等の第三者コンポーネントに必要な別ライセンスを含むものではありません。
