"""Deterministic research-contract tests for structure-first authoring."""

from fukamiru.authoring.research import (
    build_atomic_evidence_obligations,
    build_atomic_research_query_candidates,
    build_claim_proposition_bindings,
    build_research_lead_query_candidates,
    build_research_query_candidates,
    classify_proposition_verification_scopes,
    preserve_unmatched_memo_source_annotations,
    restore_memo_source_annotations,
    select_economical_research_candidates,
)


def test_research_lead_can_be_searched_without_becoming_a_claim_obligation() -> None:
    propositions = [
        {"id": "p-lead", "text": "別の解釈を示す資料があるか"},
        {"id": "p-claim", "text": "著者の主張"},
    ]
    classifications = [
        {"proposition_id": "p-lead", "argument_role": "research_lead"},
        {"proposition_id": "p-claim", "argument_role": "author_claim"},
    ]
    [candidate] = build_research_lead_query_candidates(propositions, classifications)
    assert candidate["proposition_id"] == "p-lead"
    assert candidate["evidence_requirement"] == "preferred"
    assert candidate["claim_ids"] == []


def test_claim_wrapper_does_not_inherit_atomic_fact_evidence_duty() -> None:
    propositions = [
        {"id": "p-method", "text": "本稿では二つの時期を比較する。"},
        {
            "id": "p-fact",
            "text": "1998年に雑誌Aで作品Bの連載が開始した。",
            "source_span": "1998年に雑誌Aで作品Bの連載が開始した。",
            "provenance": {"source": "memo"},
        },
        {"id": "p-reservation", "text": "この点は今後の検証を要する。"},
    ]
    classifications = [
        {"proposition_id": "p-method", "type": "method"},
        {"proposition_id": "p-fact", "type": "fact"},
        {"proposition_id": "p-reservation", "type": "reservation"},
    ]
    claims = [
        {
            "id": "c-method",
            "level": "subclaim",
            "text": "二つの時期を比較して媒体条件を総括する。",
            "proposition_id": "p-method",
            "provenance": {
                "source": "memo",
                "source_ids": ["p-method", "p-fact", "p-reservation"],
            },
        }
    ]

    scopes = classify_proposition_verification_scopes(propositions, classifications)
    bindings = build_claim_proposition_bindings(
        claims, propositions, classifications, scopes
    )
    obligations = build_atomic_evidence_obligations(propositions, scopes, bindings)
    candidates = build_atomic_research_query_candidates(obligations)

    obligation_by_proposition = {
        item["proposition_id"]: item for item in obligations
    }
    assert obligation_by_proposition["p-method"]["verification_requirement"] == "none"
    assert obligation_by_proposition["p-reservation"]["verification_requirement"] == "none"
    assert obligation_by_proposition["p-fact"]["verification_requirement"] == (
        "external_required"
    )
    [fact_candidate] = candidates
    assert fact_candidate["proposition_id"] == "p-fact"
    assert fact_candidate["evidence_requirement"] == "preferred"
    assert fact_candidate["planned_realization"] == "attribute_or_omit_if_unverified"


def test_author_theory_annotation_takes_priority_over_method_type() -> None:
    [scope] = classify_proposition_verification_scopes(
        [
            {
                "id": "p1",
                "text": "本稿ではこの配置を媒体レギュレーションと定義する。",
                "memo_annotation_kinds": ["added_theory"],
            }
        ],
        [{"proposition_id": "p1", "type": "definition"}],
    )

    assert scope["verification_scope"] == "theoretical_attribution"
    assert scope["verification_requirement"] == "attribution_required"


def test_scope_topic_example_is_not_promoted_to_external_fact() -> None:
    [scope] = classify_proposition_verification_scopes(
        [
            {
                "id": "p1",
                "text": "論じる話題には悪役令嬢の階級性が含まれる。",
                "source_span": "悪役令嬢の階級性",
                "provenance": {"source": "memo"},
            }
        ],
        [{"proposition_id": "p1", "type": "example"}],
    )

    assert scope["verification_scope"] == "method_or_scope"
    assert scope["verification_requirement"] == "none"


def test_scope_topic_inventory_with_ga_aru_is_not_promoted_to_external_fact() -> None:
    [scope] = classify_proposition_verification_scopes(
        [
            {
                "id": "p1",
                "text": "論じる話題には青年誌配置の変容がある。",
                "source_span": "青年誌配置の変容",
                "provenance": {"source": "memo"},
            }
        ],
        [{"proposition_id": "p1", "type": "example"}],
    )

    assert scope["verification_scope"] == "method_or_scope"
    assert scope["verification_requirement"] == "none"


def test_raw_log_reuse_statement_is_source_internal() -> None:
    [scope] = classify_proposition_verification_scopes(
        [
            {
                "id": "p1",
                "text": "この文はI-2の結びと同一の生ログ文の再利用である。",
                "source_span": "（I-2の結びと同一の生ログ一文が再利用されている）",
                "provenance": {"source": "memo"},
            }
        ],
        [{"proposition_id": "p1", "type": "fact"}],
    )

    assert scope["verification_scope"] == "source_internal"
    assert scope["verification_requirement"] == "none"


def test_document_heading_observation_is_source_internal() -> None:
    [scope] = classify_proposition_verification_scopes(
        [
            {
                "id": "p1",
                "text": "参照文献一覧は「生ログ不在=整形段階の追加、確認済み」と記されている。",
                "source_span": "## 参照文献（生ログ不在＝整形段階の追加、確認済み）",
                "provenance": {"source": "memo"},
            }
        ],
        [{"proposition_id": "p1", "type": "observation"}],
    )

    assert scope["verification_scope"] == "source_internal"
    assert scope["verification_requirement"] == "none"


def test_editorial_revision_status_is_source_internal() -> None:
    [scope] = classify_proposition_verification_scopes(
        [
            {
                "id": "p1",
                "text": "レギュレーションの加筆パートは、まだ反映しきれていない。",
                "source_span": "レギュレーション加筆パートは反映しきれてない",
                "provenance": {
                    "source": "memo",
                    "verification_status": "not_applicable",
                },
            }
        ],
        [{"proposition_id": "p1", "type": "observation"}],
    )

    assert scope["verification_scope"] == "source_internal"
    assert scope["verification_requirement"] == "none"


def test_not_applicable_provenance_prevents_coarse_fact_promotion() -> None:
    [scope] = classify_proposition_verification_scopes(
        [
            {
                "id": "p1",
                "text": "並の漫画家の大半は技術の習得に苦労している。",
                "provenance": {
                    "source": "memo",
                    "verification_status": "not_applicable",
                },
            }
        ],
        [{"proposition_id": "p1", "type": "fact"}],
    )

    assert scope["verification_scope"] == "analytic_inference"
    assert scope["verification_requirement"] == "preferred"
    assert scope["classification_reason"] == (
        "provenance_marks_verification_not_applicable"
    )


def test_interpretation_type_is_not_overridden_by_comparative_wording() -> None:
    [scope] = classify_proposition_verification_scopes(
        [
            {
                "id": "p1",
                "text": "雑誌、投稿サイト、SNS、AI生成環境では、共有知の見え方が異なる。",
                "provenance": {"source": "memo"},
            }
        ],
        [{"proposition_id": "p1", "type": "interpretation"}],
    )

    assert scope["verification_scope"] == "analytic_inference"
    assert scope["verification_requirement"] == "preferred"


def test_interpretation_with_bibliographic_fact_still_requires_evidence() -> None:
    [scope] = classify_proposition_verification_scopes(
        [
            {
                "id": "p1",
                "text": (
                    "『ホーリーランド』は『ヤングアニマル』掲載も含め、"
                    "バトル・ロワイアル的な時代の指標である。"
                ),
                "provenance": {"source": "memo"},
            }
        ],
        [{"proposition_id": "p1", "type": "interpretation"}],
    )

    assert scope["verification_scope"] == "bibliographic_identity"
    assert scope["verification_requirement"] == "external_required"


def test_qualified_observation_without_fact_signal_stays_analytic() -> None:
    [scope] = classify_proposition_verification_scopes(
        [
            {
                "id": "p1",
                "text": "会社を描くことはドラマになりにくい。",
                "provenance": {"source": "memo"},
            }
        ],
        [{"proposition_id": "p1", "type": "observation"}],
    )

    assert scope["verification_scope"] == "analytic_inference"
    assert scope["verification_requirement"] == "preferred"


def test_temporal_qualified_fact_still_requires_external_evidence() -> None:
    [scope] = classify_proposition_verification_scopes(
        [
            {
                "id": "p1",
                "text": "2000年代前半には、絵柄と読者の性別の対応が見えにくくなった。",
                "provenance": {"source": "memo"},
            }
        ],
        [{"proposition_id": "p1", "type": "fact"}],
    )

    assert scope["verification_scope"] == "external_historical_fact"
    assert scope["verification_requirement"] == "external_required"


def test_bibliographic_fact_still_requires_external_evidence() -> None:
    [scope] = classify_proposition_verification_scopes(
        [
            {
                "id": "p1",
                "text": "『舞妓さんちのまかないさん』は『週刊少年サンデー』に掲載された。",
                "provenance": {
                    "source": "memo",
                    "verification_status": "not_applicable",
                },
            }
        ],
        [{"proposition_id": "p1", "type": "fact"}],
    )

    assert scope["verification_scope"] == "bibliographic_identity"
    assert scope["verification_requirement"] == "external_required"


def test_direct_subclaim_fact_is_a_core_atomic_evidence_obligation() -> None:
    propositions = [
        {
            "id": "p1",
            "text": "2001年に制度Aが導入された。",
            "source_span": "2001年に制度Aが導入された。",
            "provenance": {"source": "memo"},
        }
    ]
    classifications = [{"proposition_id": "p1", "type": "fact"}]
    claims = [
        {
            "id": "c1",
            "level": "subclaim",
            "text": "制度Aの導入時期を確定する。",
            "proposition_id": "p1",
            "provenance": {"source_ids": ["p1"]},
        }
    ]
    scopes = classify_proposition_verification_scopes(propositions, classifications)
    bindings = build_claim_proposition_bindings(
        claims, propositions, classifications, scopes
    )
    obligations = build_atomic_evidence_obligations(propositions, scopes, bindings)
    [candidate] = build_atomic_research_query_candidates(obligations)

    assert obligations[0]["argument_necessity"] == "core"
    assert candidate["evidence_requirement"] == "required"
    assert candidate["requires_research"] is True
    assert candidate["obligation_id"].startswith("evidence:")


def test_leaf_main_claim_is_core_but_root_synthesis_is_not() -> None:
    propositions = [{"id": "p1", "text": "2001年に制度Aが導入された。"}]
    classifications = [{"proposition_id": "p1", "type": "fact"}]
    scopes = classify_proposition_verification_scopes(propositions, classifications)

    leaf = build_claim_proposition_bindings(
        [
            {
                "id": "main",
                "level": "main_claim",
                "proposition_id": "p1",
            }
        ],
        propositions,
        classifications,
        scopes,
    )
    synthesis = build_claim_proposition_bindings(
        [
            {
                "id": "main",
                "level": "main_claim",
                "proposition_id": "p1",
            },
            {
                "id": "sub",
                "level": "subclaim",
                "parent_id": "main",
                "proposition_id": "p1",
            },
        ],
        propositions,
        classifications,
        scopes,
    )

    assert leaf[0]["argument_necessity"] == "core"
    assert synthesis[0]["argument_necessity"] == "supporting"
    assert synthesis[1]["argument_necessity"] == "core"


def test_atomic_obligation_is_shared_across_claims_with_stable_identity() -> None:
    propositions = [{"id": "p1", "text": "1990年に作品Aが刊行された。"}]
    classifications = [{"proposition_id": "p1", "type": "fact"}]
    claims = [
        {
            "id": "c1",
            "level": "subclaim",
            "text": "第一の主張。",
            "proposition_id": "p1",
        },
        {
            "id": "c2",
            "level": "supporting_claim",
            "text": "第二の主張。",
            "proposition_id": "p1",
        },
    ]
    scopes = classify_proposition_verification_scopes(propositions, classifications)
    bindings = build_claim_proposition_bindings(
        claims, propositions, classifications, scopes
    )
    first = build_atomic_evidence_obligations(propositions, scopes, bindings)
    second = build_atomic_evidence_obligations(propositions, scopes, bindings)

    assert len(first) == 1
    assert first[0]["claim_ids"] == ["c1", "c2"]
    assert first[0]["obligation_id"] == second[0]["obligation_id"]


def test_economical_research_selection_keeps_required_and_bounds_preferred() -> None:
    candidates = [
        {
            "claim_id": "required",
            "evidence_requirement": "required",
            "requires_research": True,
            "claim_level": "supporting",
        },
        *[
            {
                "claim_id": f"preferred-{index}",
                "evidence_requirement": "preferred",
                "requires_research": True,
                "claim_level": "sub" if index == 3 else "supporting",
                "memo_annotation_kinds": ["added_theory"] if index == 4 else [],
            }
            for index in range(5)
        ],
    ]

    selected = select_economical_research_candidates(candidates, preferred_limit=2)

    assert [item["claim_id"] for item in selected if item["requires_research"]] == [
        "required",
        "preferred-3",
        "preferred-4",
    ]
    assert selected[1]["research_selection_reason"] == "qualified_inference_without_search"


def test_memo_source_annotations_are_restored_from_exact_source_lines() -> None:
    note = "\n".join(
        [
            "- 媒体は欲望を配置する。(理: 後から追加した理論)",
            "- この節の骨格は対話相手の発言である。(他/要: 石の発言。著者の主張ではない)",
            "- 注記のない命題。",
        ]
    )
    propositions = [
        {"id": "p1", "text": "媒体は欲望を配置する。", "source_span": "媒体は欲望を配置する。"},
        {
            "id": "p2",
            "text": "この節の骨格は対話相手の発言である。",
            "source_span": "この節の骨格は対話相手の発言である。",
        },
        {"id": "p3", "text": "注記のない命題。", "source_span": "注記のない命題。"},
    ]

    restored, report = restore_memo_source_annotations(note, propositions)

    assert report == {
        "tagged_source_line_count": 2,
        "matched_tagged_source_line_count": 2,
        "annotated_proposition_count": 2,
        "unmatched_tagged_lines": [],
    }
    assert restored[0]["memo_annotation_kinds"] == ["added_theory"]
    assert "(理:" in restored[0]["source_span"]
    assert restored[1]["memo_annotation_kinds"] == ["needs_review", "other_speaker"]
    assert restored[2]["memo_annotations"] == []


def test_unmatched_memo_annotation_is_reported_instead_of_silently_dropped() -> None:
    restored, report = restore_memo_source_annotations(
        "- 取り込まれなかった主張。(要: 検証が必要)",
        [{"id": "p1", "text": "別の主張。", "source_span": "別の主張。"}],
    )

    assert restored[0]["memo_annotation_kinds"] == []
    assert report["matched_tagged_source_line_count"] == 0
    assert report["unmatched_tagged_lines"][0]["label"] == "要"


def test_unmatched_memo_annotation_is_materialized_without_fuzzy_attachment() -> None:
    restored, report = preserve_unmatched_memo_source_annotations(
        "- この装置を媒体レギュレーションと呼ぶ。(理: 著者指定の理論)",
        [{"id": "p1", "text": "別の主張。", "source_span": "別の主張。"}],
    )

    assert report["unmatched_tagged_lines"] == []
    assert report["materialized_tagged_source_line_count"] == 1
    supplemental = restored[-1]
    assert supplemental["id"] == "p-source-annotation-0001"
    assert supplemental["text"] == "この装置を媒体レギュレーションと呼ぶ。"
    assert supplemental["memo_annotation_kinds"] == ["added_theory"]
    assert supplemental["source_span"].endswith("(理: 著者指定の理論)")


def test_memo_origin_is_not_verification_and_queries_use_linked_propositions() -> None:
    claims = [
        {
            "id": "c-main",
            "level": "main_claim",
            "text": "以上より、Aの普及はBの成立を促進した。",
            "provenance": {
                "source": "memo",
                "source_ids": ["p-origin"],
                "verification_status": "verified",
            },
        }
    ]
    propositions = [
        {
            "id": "p-origin",
            "text": "20世紀初頭にAが普及した。",
            "proposition_type": "fact",
            "provenance": "memo",
        }
    ]

    [candidate] = build_research_query_candidates(claims, propositions)

    assert candidate["requires_research"] is True
    assert candidate["claim_origin"] == "memo"
    assert candidate["verification_status"] == "unverified"
    assert candidate["linked_proposition_ids"] == ["p-origin"]
    assert candidate["linked_proposition_origins"] == {"p-origin": "memo"}
    assert candidate["claim_id"] == candidate["finding_id"] == "c-main"
    assert "促進" in candidate["query"]
    assert "20世紀初頭" in candidate["query"]
    assert "以上" not in candidate["query"]
    assert "媒体レギュレーション" not in candidate["query"]


def test_empirical_obligations_cover_claim_levels_and_preserve_descendant_claims() -> None:
    claims = [
        {
            "id": "main",
            "level": "main",
            "text": "制度変更が地域間の差を拡大した。",
            "parent_id": None,
            "provenance": "llm_inference",
        },
        {
            "id": "sub",
            "level": "sub",
            "text": "地域Aでは制度変更後に参加率が増加した。",
            "parent_id": "main",
            "provenance": "rag",
        },
        {
            "id": "support",
            "level": "supporting",
            "text": "2020年の記録は地域Aの参加率を示す。",
            "parent_id": "sub",
            "provenance": {"source": "memo", "verification_status": "verified"},
        },
    ]

    candidates = build_research_query_candidates(claims, [])

    assert [item["claim_id"] for item in candidates] == ["main", "sub", "support"]
    assert [item["requires_research"] for item in candidates] == [False, True, True]
    assert [item["evidence_requirement"] for item in candidates] == [
        "none",
        "required",
        "required",
    ]
    assert candidates[1]["allowed_support_modes"] == ["external_evidence"]
    assert candidates[0]["subclaim_ids"] == ["main", "sub", "support"]
    assert candidates[0]["subclaims"] == [item["text"] for item in claims]
    assert candidates[1]["subclaim_ids"] == ["sub", "support"]
    assert candidates[2]["subclaim_ids"] == ["support"]


def test_pure_scope_method_and_definition_claims_are_not_research_obligations() -> None:
    claims = [
        {
            "id": "scope",
            "level": "main",
            "type": "scope",
            "text": "本稿では対象を近代期の新聞に限定する。",
        },
        {
            "id": "method",
            "level": "sub",
            "type": "method",
            "text": "本研究ではAとBを比較する。",
        },
        {
            "id": "definition",
            "level": "supporting",
            "type": "definition",
            "text": "ここでは公共圏を議論の場として定義する。",
        },
    ]

    candidates = build_research_query_candidates(claims, [])

    assert [item["requires_research"] for item in candidates] == [False, False, False]
    assert [item["evidence_requirement"] for item in candidates] == ["none"] * 3


def test_empirical_detail_overrides_a_method_label_and_proposition_classification() -> None:
    claims = [
        {
            "id": "method-result",
            "level": "supporting_claim",
            "type": "method",
            "text": "本研究では1998年に参加者80名を対象に調査を実施した。",
            "proposition_id": "p-method",
        }
    ]
    propositions = [
        {"id": "p-method", "text": "調査を実施した。", "proposition_type": "method"}
    ]

    [candidate] = build_research_query_candidates(claims, propositions)

    assert candidate["requires_research"] is True
    assert candidate["evidence_requirement"] == "required"
    assert candidate["claim_level"] == "supporting"
    assert candidate["scope_key"] == "claim:method-result"
    assert "1998年" in candidate["query"]


def test_linked_proposition_classification_can_create_obligation() -> None:
    claims = [
        {
            "id": "c1",
            "level": "subclaim",
            "text": "出来事の背景を説明する。",
            "proposition_id": "p1",
            "type": "definition",
        }
    ]
    propositions = [{"id": "p1", "text": "出来事は1905年に発生した。"}]
    classifications = [{"proposition_id": "p1", "type": "fact"}]

    [candidate] = build_research_query_candidates(claims, propositions, classifications)

    assert candidate["requires_research"] is True
    assert candidate["linked_proposition_ids"] == ["p1"]


def test_qualified_synthesis_over_mixed_links_is_preferred_not_categorical() -> None:
    claims = [
        {
            "id": "mixed",
            "level": "sub",
            "text": "制度変更が地域参加を変化させたと考えられる。",
            "proposition_ids": ["fact", "interpretation"],
        }
    ]
    propositions = [
        {"id": "fact", "text": "2020年に参加率が増加した。"},
        {"id": "interpretation", "text": "制度変更が増加の理由と考えられる。"},
    ]
    classifications = [
        {"proposition_id": "fact", "type": "fact"},
        {"proposition_id": "interpretation", "type": "interpretation"},
    ]

    [candidate] = build_research_query_candidates(claims, propositions, classifications)

    assert candidate["evidence_requirement"] == "preferred"
    assert candidate["requires_research"] is True
    assert candidate["allowed_support_modes"] == [
        "external_evidence",
        "qualified_memo_inference",
        "qualified_llm_inference",
    ]


def test_hypothesis_is_preferred_and_can_proceed_only_as_qualified_inference() -> None:
    claims = [
        {
            "id": "hypothesis",
            "level": "sub",
            "type": "hypothesis",
            "text": "地域活動の経験が参加意欲を高める可能性がある。",
        }
    ]

    [candidate] = build_research_query_candidates(claims, [])

    assert candidate["evidence_requirement"] == "preferred"
    assert candidate["requires_research"] is True
    assert candidate["allowed_support_modes"] == [
        "external_evidence",
        "qualified_memo_inference",
        "qualified_llm_inference",
    ]


def test_meta_caveat_is_not_a_research_claim() -> None:
    claims = [
        {
            "id": "caveat",
            "level": "supporting",
            "text": "この点は後続段階で検証する必要がある。",
        }
    ]

    [candidate] = build_research_query_candidates(claims, [])

    assert candidate["evidence_requirement"] == "none"
    assert candidate["requires_research"] is False
    assert candidate["allowed_support_modes"] == ["no_evidence_required"]


def test_added_theory_annotation_constrains_citation_without_factifying_claim() -> None:
    claims = [
        {
            "id": "theory-claim",
            "level": "supporting",
            "text": "この配置は場の理論として捉えられる。",
            "proposition_id": "p-theory",
        }
    ]
    propositions = [
        {
            "id": "p-theory",
            "text": "この配置は場の理論として捉えられる。",
            "memo_annotations": [
                {
                    "label": "理",
                    "kinds": ["added_theory"],
                    "detail": "整形段階で追加",
                    "raw": "(理: 整形段階で追加)",
                }
            ],
        }
    ]

    [candidate] = build_research_query_candidates(claims, propositions)

    assert candidate["evidence_requirement"] == "preferred"
    assert candidate["allowed_support_modes"] == [
        "external_evidence",
        "qualified_memo_inference",
        "qualified_llm_inference",
    ]
    assert candidate["memo_annotation_kinds"] == ["added_theory"]
    assert candidate["memo_annotations"][0]["label"] == "理"
