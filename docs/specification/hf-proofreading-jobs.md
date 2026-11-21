# HF Jobsによる日本語33B校正

`hf_proofreading.enabled`は、`llmjp33b_adopt`の既存の節単位校正ノードに
必要時のJob管理を追加します。指示、strict schema、モデル条件、Solの確認、Lunaの表記整形は
[日本語校正の契約](japanese-proofreading.md)と同じです。

## 実行と停止

1. 入力と最終schemaを検査し、SQLiteの保存結果・受領記録を調べる。
2. 未取得の節があるときだけ、時間・費用枠をstate.dbへ予約する。
   現行のH200単価とprivate port料金を取得し、上限超過なら起動を拒否する。
3. 所有ラベルを付けたprivate-port Jobを1件起動する。
   固定33B BF16モデルとllama.cpp b12375 CUDA assetsのrevision、容量、SHA-256を検証する。
   クライアント側でもモデルの実読込、contextとhighのテンプレートを確認する。
4. 各節を1回だけ送信し、receiptをpollする。同じtask ID・同じ要求は同じtaskを返す。
   同じIDで内容が違えば拒否する。native要求は直列で、正しい終了理由を必要とする。
5. 正確な要求、raw終端応答、解析済み結果、受領進捗をSQLiteに保存する。
   raw受領後に中断した場合は保存rawから解析を復旧し、再推論しない。
6. 日本語モデルを使う区間の完了・失敗・取消で、所有Jobを停止して終端状態を確認する。
   後続のSol・LunaやHTML処理では、このGPU Jobを維持しない。

完了済みcacheの指紋には原文、節・stage、指示・payload・schema、decoding、
固定model/runtime/workerとtoken budgetを含め、一時的なJob URLは含めません。
サービス再起動後も同じ完了結果をGPUなしで再利用できます。

## 不明な要求とcleanup

未完了・受領不明の要求は、Job終了後も自動再送しません。
要求と部分応答を保持し、未解決receiptとして報告します。
停止失敗もエラーとして残し、回収を確認するまで新たな割当を止めます。
monitorは停止を再試行し、ownerが失効した場合も自instance/sessionのラベルとreceiptが一致するJobだけを回収します。
別ownerの生きたJobや無関係なJobは停止しません。

HF側のhard timeoutと、remote workerの無処理時終了を併用します。
時間予約には課金丸めと停止用の1分を取り、Run単位で予約額を合算します。
停止済みの予約を推定返金として差し引きません。上限を増やしても受領不明の節を再推論する理由にはしません。
これらはアプリの割当制御であり、HFの請求メーターに代わるものではありません。

## 設定と認証

[セットアップ](../setup.md#現行33b校正をhf-jobsで使う)に依存導入と設定例があります。
HF認証は既存のHFストアまたは`HF_TOKEN`から読みます。
namespaceには利用者自身のアカウントか実行権限のある組織を指定します。
固定モデルは公開モデルなので、HFの資格情報はcontainer環境やmodel payloadへ転送しません。
private gatewayへの要求にはHF認証を使用します。
グローバルなendpointやtoken環境変数を書き換えて並行Runへ流用しません。

起動、health確認、Run作成、日本語校正の無効化ではGPUを割り当てません。
migration 0057はstate.dbのJob・request記録だけを追加します。
knowledge.db、索引、元Runと既存migrationは移動・再構築しません。

公式資料：[一時推論サーバー](https://huggingface.co/docs/hub/jobs-serving)、
[認証・timeout等の設定](https://huggingface.co/docs/hub/jobs-configuration)、
[課金](https://huggingface.co/docs/hub/jobs-pricing)。
