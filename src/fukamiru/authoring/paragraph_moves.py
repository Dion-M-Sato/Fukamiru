"""Standard argument-move labels for V31 paragraph planning."""

from __future__ import annotations

PARAGRAPH_ARGUMENT_MOVE_CATALOG = (
    {
        "id": "object_context",
        "label": "研究対象と背景",
        "group": "introduction",
        "description": "研究対象を示し、必要な社会的・歴史的背景に位置づける。",
    },
    {
        "id": "prior_research_position",
        "label": "先行研究の到達点",
        "group": "introduction",
        "description": "出典で確認できる先行研究の議論や到達点を整理する。",
    },
    {
        "id": "problematisation",
        "label": "問題化・研究上の問いの必要",
        "group": "introduction",
        "description": "既存の説明や資料から何が問題となるのかを具体的に示す。",
    },
    {
        "id": "research_question",
        "label": "研究の問い",
        "group": "introduction",
        "description": "論文全体が答える研究上の問いを明確に提示する。",
    },
    {
        "id": "provisional_answer",
        "label": "暫定的な答え・中心命題",
        "group": "introduction",
        "description": "問いへの暫定的な答え、または論文の中心命題を先に示す。",
    },
    {
        "id": "scope_and_materials",
        "label": "対象・資料・範囲",
        "group": "introduction",
        "description": "何を対象にし、どの資料と範囲を扱うかを定める。",
    },
    {
        "id": "method_or_analytic_axis",
        "label": "方法・分析軸",
        "group": "introduction",
        "description": "資料をどの方法・観点・分析軸から読むかを示す。",
    },
    {
        "id": "key_concept_or_distinction",
        "label": "中核概念・分析上の区別",
        "group": "introduction",
        "description": "論証に必要な概念や、混同しないための区別を定義する。",
    },
    {
        "id": "contribution_or_implication",
        "label": "学術的貢献・含意",
        "group": "introduction",
        "description": "問いに答えることで研究上どの理解を更新するかを示す。",
    },
    {
        "id": "roadmap",
        "label": "論文構成の予告",
        "group": "introduction",
        "description": "各節が何を明らかにするかを論証の順序に沿って案内する。",
    },
    {
        "id": "local_claim",
        "label": "節内の主張",
        "group": "body",
        "description": "段落または節で立証する個別の主張を提示する。",
    },
    {
        "id": "evidence_or_source_detail",
        "label": "根拠・資料の具体",
        "group": "body",
        "description": "主張を支える資料、記述、データ、出典の具体を示す。",
    },
    {
        "id": "concrete_case_or_example",
        "label": "具体例・事例",
        "group": "body",
        "description": "論点を具体化する事例や例を提示し、その位置づけを明らかにする。",
    },
    {
        "id": "case_analysis_or_close_reading",
        "label": "事例分析・精読",
        "group": "body",
        "description": "事例や資料の細部を読み、論証に関わる意味を説明する。",
    },
    {
        "id": "warrant_or_explanatory_mechanism",
        "label": "推論根拠・説明メカニズム",
        "group": "body",
        "description": "根拠から主張が導かれる理由、関係、作用の仕組みを説明する。",
    },
    {
        "id": "comparison_or_contrast",
        "label": "比較・対照",
        "group": "body",
        "description": "比較対象と比較軸を明確にし、共通点や差異の意味を論じる。",
    },
    {
        "id": "parallel_branch_contribution",
        "label": "複数枝の並置・未解決関係",
        "group": "body",
        "description": (
            "同じ節に含まれる複数の論点枝の寄与を区別する。資料に関係の直接記述がなくても、"
            "前後の議論から筆者の解釈として関係を論じられる場合は本文で示す。"
        ),
    },
    {
        "id": "cross_branch_synthesis",
        "label": "根拠に基づく枝間統合",
        "group": "body",
        "description": (
            "複数の論点枝を結ぶ資料上の関係、または論理を示した筆者の解釈から、"
            "節の結論を導く。外部事実や出典の主張を創作しない。"
        ),
    },
    {
        "id": "alternative_or_counterargument",
        "label": "対抗説明・反論",
        "group": "body",
        "description": "主張を脅かす根拠ある別解釈や反論を提示する。",
    },
    {
        "id": "response_or_rebuttal",
        "label": "応答・再反論",
        "group": "body",
        "description": "対抗説明・反論を検討し、主張を維持・修正する理由を示す。",
    },
    {
        "id": "qualification_or_scope",
        "label": "限定・適用条件",
        "group": "body",
        "description": "主張が成り立つ条件や、説明できる範囲を具体的に限定する。",
    },
    {
        "id": "intermediate_synthesis_or_bridge",
        "label": "小括・次段への接続",
        "group": "body",
        "description": "ここまでの分析から得た結果をまとめ、次の論点へつなぐ。",
    },
    {
        "id": "final_answer_to_research_question",
        "label": "研究の問いへの最終回答",
        "group": "conclusion",
        "description": "確立した本文の成果に基づいて、研究の問いに最終回答する。",
    },
    {
        "id": "synthesis_of_section_outputs",
        "label": "各節の成果の統合",
        "group": "conclusion",
        "description": "各節の結果を言い換えるだけでなく、全体の論証として統合する。",
    },
    {
        "id": "limitation_or_open_question",
        "label": "限界・未解決の問い",
        "group": "conclusion",
        "description": "証拠や範囲に基づく限界と、残る問いを具体的に示す。",
    },
)

PARAGRAPH_ARGUMENT_MOVE_IDS = tuple(item["id"] for item in PARAGRAPH_ARGUMENT_MOVE_CATALOG)
INTRODUCTION_ARGUMENT_MOVE_IDS = tuple(
    item["id"]
    for item in PARAGRAPH_ARGUMENT_MOVE_CATALOG
    if item["group"] == "introduction"
)
