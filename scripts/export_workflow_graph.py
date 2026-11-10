"""Export an executable workflow graph without importing optional runtime dependencies."""

from __future__ import annotations

import argparse
import ast
import json
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/fukamiru/workflow/templates.py"
PARAGRAPH_ARGUMENT_MOVE_CATALOG = runpy.run_path(
    str(ROOT / "src/fukamiru/authoring/paragraph_moves.py")
)["PARAGRAPH_ARGUMENT_MOVE_CATALOG"]


class Spec:
    def __init__(self, **values):
        self.resource_lane = "ingestion_cpu"
        self.retry = None
        self.join_from = []
        self.join_generation_source = None
        self.condition = "success"
        self.__dict__.update(values)

    def model_copy(self, *, update):
        return type(self)(**{**vars(self), **update})

    def model_dump(self):
        return vars(self).copy()

    @classmethod
    def model_validate(cls, value):
        return cls(**value)


def export(*, version: str = "31", workflow_name: str | None = None) -> dict:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    namespace = {
        "NodeSpec": Spec,
        "EdgeSpec": Spec,
        "RetryPolicy": Spec,
        "WorkflowDefinition": Spec,
    }
    module = ast.Module(
        body=[item for item in tree.body if isinstance(item, ast.FunctionDef)],
        type_ignores=[],
    )
    exec(compile(module, str(SOURCE), "exec"), namespace)
    workflow = (
        namespace[workflow_name]()
        if workflow_name
        else namespace["authoring_workflow"](version=version)
    )
    nodes = [
        {
            "id": node.id,
            "title": node.title,
            "handler": node.handler,
            "resource_lane": node.resource_lane,
            "max_attempts": getattr(getattr(node, "retry", None), "max_attempts", 1),
            **(
                {"human_review": node.human_review}
                if getattr(node, "human_review", "never") != "never"
                else {}
            ),
            "join_from": getattr(node, "join_from", []),
            "join_generation_source": getattr(node, "join_generation_source", None),
        }
        for node in workflow.nodes
    ]
    edges = [
        {"from": edge.source, "to": edge.target, "condition": edge.condition}
        for edge in workflow.edges
    ]
    node_ids = {node["id"] for node in nodes}
    has_invalid_endpoint = any(
        edge["from"] not in node_ids or edge["to"] not in node_ids for edge in edges
    )
    if len(nodes) != len(node_ids) or has_invalid_endpoint:
        raise ValueError(f"V{version} DAG has duplicate nodes or an unresolved edge endpoint")
    return {
        "name": workflow.name,
        "version": workflow.version,
        "nodes": nodes,
        "edges": edges,
        **({"reader_brief_excerpt_policy": {
            "summary": (
                "固定した論証と資料から本文を起草し、解釈・出典・推論記録を保持します。"
                "起草前の監査で抜粋が原文と一致しない場合、抜粋だけを一度訂正させます。"
                "監査判断・理由・対象ID・指摘件数は変更できず、訂正後も連続した原文との"
                "完全一致を検査します。不一致なら停止します。元の監査と訂正を記録します。"
                "人名の頭文字を文末と誤認せず、原文と同じ文全体を帰属監査へ渡します。"
                "不明判定は引き続き停止します。本文の根拠の扱いを固定資料の支援役割と照合し、"
                "不一致を対象節の修正指示へ返します。資料の能力や検証状態は昇格させません。"
                "保存済みの監査がある抜粋不一致、人名の頭文字直後の文切れ、書誌情報を事実の"
                "根拠にできない同一入力の停止に限り、履歴を保持して原因ごとに一度再開できます。"
                "起草・改稿の厳格な根拠検査を通過した時点で、その本文世代の根拠対応を固定します。"
            ),
            "input": "固定した節契約・資料、原文と文章候補、監査の抜粋・判断",
            "output": "本文候補、原文と一致する監査抜粋、元の判断と訂正履歴",
        }} if "draft_rewrite" in node_ids else {}),
        **({"evidence_identity_policy": {
            "summary": (
                "起草・改稿の厳格な根拠検査後に、その本文世代の主張・文・出典の対応を固定し、"
                "統合や後続の校正での対応変更を拒否します。前世代の基準と検査記録は履歴に残します。"
                "旧版が残した前世代の基準で停止した場合は、直前の完了済み起草結果との完全一致、"
                "固定資料の不変、元の基準との一致、新しい対応の厳格な再検査を確認した場合だけ、"
                "基準とその検査記録を更新して一度再開します。修正前の入力とエラーを保持します。"
                "本文・主張・出典・検証状態は変更せず、校正後の同じエラーはこの復旧の対象外です。"
            ),
            "input": "厳格な根拠検査済みの本文世代、主張・文・出典の対応、固定資料",
            "output": "同一世代の根拠対応の照合結果、前世代の比較基準と復旧の履歴",
        }} if "reconcile_candidate_context" in node_ids else {}),
        **({"candidate_generation_policy": {
            "summary": (
                "固定した節の主張・資料・人間の回答を保って、新しい本文世代を開始します。"
                "前世代の段落と出典の対応、校正・脚注・提出検査の派生結果を引き継ぎません。"
                "元の値は保存済み入力に保持し、その場所とハッシュを履歴へ記録します。"
                "全文起草の再開時にも同じ初期化を行います。旧対応の持ち越しによる脚注停止は、"
                "元の失敗入力を保持し、現本文の厳格な根拠検査と脚注計画を再確認できる場合だけ、"
                "一度本文統合へ戻します。構造・出典・日本語の監査と脚注検査を再実行します。"
            ),
            "input": "固定済み節契約・資料・保護された本文・人間の回答、前世代の派生結果",
            "output": "新しい本文世代、前世代の結果の保存場所とハッシュ、通常の監査経路",
        }} if "start_paragraph_formation" in node_ids else {}),
        **({"proofreading_generation_policy": {
            "summary": (
                "同じRun内でも本文世代ごとに校正文・候補・タイトル情報を分け、"
                "旧候補・選択・適用・ログを保持します。同じ世代の元本文変更は停止します。"
                "旧版の校正チェックポイントで新本文が停止した場合は、旧文と新文の相違、"
                "新世代が未作成であること、移行の適用、過去の限定復旧履歴を確認できる場合だけ、"
                "上流の検査結果と元入力を保持して校正抽出から一度再開します。"
                "候補生成から適用まで、今回の世代だけを参照します。"
            ),
            "input": "本文世代、校正元HTML、保持された前世代の校正記録",
            "output": "世代ごとの校正文・候補・タイトル情報、校正元HTMLのハッシュ",
        }} if "extract_proofreading_sentences" in node_ids else {}),
        **({"empty_citation_projection_policy": {
            "summary": (
                "出典対応がないと確定した段落も投影済みとして記録し、推敲で節の候補資料を"
                "再配分しません。既存の帰属と脚注の厳格な検査は維持します。"
                "旧版で校正後の脚注が余分な対応により停止した場合は、起草の根拠対応が完全一致し、"
                "既存の引用を全て保ち、追加対応が引用なしの停止段落だけであることと、"
                "現本文の厳格な脚注計画を確認できる場合だけ一度校正後統合へ戻します。"
                "本文・資料・回答・校正結果と元の失敗入力は保持します。"
                "資料・場面が未確認という留保は、不確実性の指摘を残して後段の本文修正へ渡し、"
                "残る場合は段落を示した人間レビューとします。内部作業ラベルとは区別し、"
                "未確認を検証済みへ変更しません。この留保だけで停止した旧版は、"
                "根拠対応と脚注計画が有効な場合に、元入力のまま一度再開できます。"
            ),
            "input": "厳格な起草の根拠対応、全段落の引用有無、校正後の本文と保存済み結果",
            "output": "引用なしを含む段落対応、余分な投影の限定復旧と元入力の履歴",
        }} if "render_evidence_footnotes" in node_ids else {}),
        **({"submission_revalidation_policy": {
            "summary": (
                "人間の回答による再生成依頼を本文世代をまたいで保持します。"
                "同じRunの未承認候補を置き換える際は、旧本文・確認内容・ハッシュを履歴へ保存し、"
                "固定済みの新本文を改めて人間レビューへ渡します。承認済み候補は置き換えません。"
                "旧版が再生成依頼の印を初期化して最終保存で停止した場合は、保存入力と履歴の"
                "ハッシュ、同じ資料・回答、送信済み回答に対応する修正依頼と未承認候補を照合し、"
                "印だけを復元して一度最終保存へ戻します。本文と検査結果は変更しません。"
                "レビューでは情報要求の対象範囲を引き継ぎ、送信済み回答と対象・要求文が一致する"
                "重複表示を除きます。保存済みの質問・回答・版と閉じた履歴は保持し、入力済みの"
                "現在の下書きは隠しません。別の対象や新しい要求は人間の確認へ残します。"
            ),
            "input": "固定済み新候補、著者の再生成依頼、未承認の旧候補と回答履歴",
            "output": "旧候補の不変の履歴、新しい提出候補と人間レビュー",
        }} if "finalize_authoring" in node_ids else {}),
        "paragraph_argument_move_catalog": [
            dict(item) for item in PARAGRAPH_ARGUMENT_MOVE_CATALOG
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="32")
    parser.add_argument("--workflow", default="authoring_workflow")
    args = parser.parse_args()
    print(
        json.dumps(
            export(version=args.version, workflow_name=args.workflow), ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
