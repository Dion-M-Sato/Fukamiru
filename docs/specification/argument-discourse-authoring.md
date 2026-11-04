# Argument and discourse authoring

This specification defines the proposal authoring workflow: a raw note becomes a reviewed first paper Revision through an explicit, auditable plan and a human-approved proposal. The note is source material. Its headings, section boundaries, and ordering do not constrain the new paper.

## End-to-end flow

1. **Capture the raw note.** Preserve the submitted note as the source record, with a stable identity and content hash.
2. **Decompose and classify.** Split it into semantically minimal atomic propositions, retain source spans, and classify each as fact, observation, interpretation, hypothesis, definition, method, reservation, or example.
3. **Persist structural data.** Store propositions, types, provenance, and source references in the authoring structure database before prose generation.
4. **Research the question and evidence.** Fix exactly one research question and its scope/assumptions. Plan research queries, retrieve relevant material, resolve evidence to source references, and record verification state.
5. **Build the argument structure.** Create a Main Claim → Subclaim → supporting-claim hierarchy; connect Claim, Evidence, Warrant, Qualifier, Counterexample, and Rebuttal nodes with typed relations. Validate support and identify gaps or competing explanations.
6. **Design discourse and section contracts.** Build a discourse tree with relations such as Evidence, Explanation, Elaboration, Contrast, Concession, Cause, Condition, Background, Result, Sequence, and Summary. Each section contract fixes its local question, conclusion, claim/evidence references, warrants, qualifiers, counterarguments, bridges, terminology, and excluded topics. Set a section or document length only if the user specifies one.
7. **Draft sections.** Generate section prose against the locked contracts, record claim/evidence references, and flag any propositions introduced outside the plan.
8. **Create the first candidate canonical HTML.** Assemble the section drafts into a complete candidate. This is the first full manuscript artifact, but it is not a paper Revision.
9. **Prepare and audit the candidate.** Run Japanese proofreading, bibliography/reference generation, footnote generation, rendered-structure repair, citation/reference quality checks, and whole-document audits. Check claim/topic drift, evidence support, missing warrants, qualifier loss, counterargument coverage, discourse coherence, footnote/reference integrity, contradictions, and redundancy. Repairs are limited to affected sections; rebuild and re-audit the candidate after repair.
10. **Persist an authoring pending proposal.** Freeze the prepared candidate with its plan revision, provenance, references, footnotes, audit findings, and unresolved user information. The Japanese proofreader may create candidate/shadow previews but must not silently overwrite the canonical manuscript.
11. **Human approval creates the first Revision.** Present the proposal for a human decision. Only explicit approval creates the paper's initial Revision from the approved candidate. Rejection or requested structural changes create no Revision and do not mutate the frozen proposal.

Manuscript quality components validate the composed candidate after section drafting. The authoring plan derives from the source note and its argument contracts. Preparation and extraction stages for rewriting an existing manuscript belong to the separate rewrite workflow.

## Folder input for manuscript generation

The Workbench can start from a note typed directly or from a folder inside the selected Project. A folder input contains one table-of-contents file named `目次`, `もくじ`, `toc`, or `contents` (with a supported manuscript suffix) and one body file whose stem matches each contents title. The match is deterministic; missing or ambiguous files stop preview and Run creation. Supported body formats are Markdown, plain text, DOCX, and text-bearing PDF. Other supported body files are listed as omitted in the preview.

The preview presents the contents order, resolved filenames, extraction warnings, omitted supported files, and the combined Markdown. Starting a Run saves that Markdown under the Project's `authoring_inputs` directory and passes its text plus the resolved section records to `prepare_note`. The original files are read again at Run creation; the Run then uses the saved input text rather than reading mutable files during execution. The source Revision represents each body file as a separate section and records its Project-relative path. `decompose_memo` processes only the body sections, retaining their titles in each chunk; it does not turn the table of contents or file labels into propositions. After decomposition, the full section content is removed from the forwarded context, while the small title/path manifest remains. The contents hierarchy and order describe the input sources and do not fix the generated paper's argument or section order.

## Structure, evidence, and provenance

The argument graph records why a claim is warranted; the discourse tree records how claims and evidence are presented to a reader. Keep these separate and link them with stable claim and section IDs. RAG and model inference may propose Claim, Evidence, Warrant, Qualifier, Counterexample, and Rebuttal content, but every item records provenance (`memo`, `rag`, `llm_inference`, `user`, or `mixed`), source spans or retrieval references, and verification state. Inferred evidence is marked unverified until checked against a source. Generated content does not verify itself.

Evidence may connect to a same-claim warrant, which then warrants the claim: `evidence --supports--> warrant --warrants--> claim`. This directed, typed path satisfies the evidence-to-claim handoff; a redundant direct evidence-to-claim edge is not required. The graph validator checks the node types, edge relations, direction, and shared `claim_id`. After one bounded correction, remaining content-level graph findings are recorded with the candidate and carried into the final proposal report. They do not pause the Run for an intermediate human decision; the final proposal remains available for human review.

The hierarchical treatment of discourse purpose, attention, and local coherence is informed by Grosz and Sidner (1986) and Grosz, Joshi, and Weinstein (1995). Typed rhetorical relations follow the organizing idea of Rhetorical Structure Theory (Mann and Thompson, 1988); a complete linguistic parser is not required.

## Immutable plans and controlled repair

Persist every locked plan as an immutable revision with parent revision, source note revision, producer run, schema version, and content hash. A section draft identifies the plan and contract revision it follows. If the research question, claim hierarchy, argument graph, discourse tree, or contracts change, create a new plan revision and re-evaluate dependent sections. Drafting and proofreading must not mutate a plan.

Structural audit repair may regenerate only affected sections while the plan remains valid. Reassemble and audit a new candidate after repair. Japanese proofreading is prose-focused and must preserve argumentative content and source references; detected structural changes return to plan revision and structural audit. Bibliography entries and footnotes are generated from cited source records. Unresolved or unverifiable references remain visible in the pending proposal.

V31 citation recovery uses the locked bibliography records as structured data. When a work title appears exactly once in a paragraph and maps to one confirmed record, restore that record's ID as `<cite data-reference-id="…">`; keep the citation's evidence and claim relations in their separate records. If the title is absent or its binding is ambiguous, regenerate only that paragraph while preserving its required bibliography and argument. Compare the rendered paragraph count with its semantic paragraph mappings before accepting a draft, then repair only the affected section when they differ. If citation markup or HTML validation fails, first rebuild from the generated paragraph records; when editing existing HTML, use its original DOM and footnote anchors as the repair baseline. If bounded repair cannot resolve a problem, retain the candidate, record the affected paragraph and reason as an open final-review item, and continue the Run. Never remove a required citation silently or label an unresolved candidate as a clean pass.

The research question and conclusion answer remain exact in structured metadata. Their reader-facing prose may paraphrase them naturally when it preserves the same object, relation, scope, and conditions; substring equality is not a prose validation rule.

V31 keeps three authoring responsibilities distinct across generation, local repair,
audit, and final review. The argument graph and discourse contracts hold claims,
evidence, warrants, rebuttals, qualifications, and section relations as structured
data. Draft prompts turn those relations into reader-facing claims without narrating
the plan, audit, or input workflow. Enumeration-context records expose the original
items, linked memo propositions, claim/evidence/warrant statements, and known
qualifications to paragraph planning and drafting;
the model explains only relations supported by that data and leaves unknown relations
open. The prose review checks for compressed lists and concealed authorial theory use
by meaning and context, not by deleting academic terms or forcing a repeated first-
person template. A local repair preserves unaffected paragraphs and the locked
evidence obligations while correcting the affected statement. Auditor replacement
content may be paraphrased when its object, scope, and qualification are preserved.

V31 binds each evidence treatment to a specific generated sentence. When one
evidence ID appears with more than one sentence, the draft must provide an explicit
paragraph and sentence index, or an unambiguous support hint; the system does not
choose the first occurrence. At footnote rendering, a citation event must match the
same rendered paragraph as its evidence use. A citation to the same source elsewhere
in the manuscript does not satisfy that binding. Unresolved footnote bindings
remain open review items with their source and paragraph or sentence identifiers;
the candidate continues to the final reference audit and cannot receive a clean
citation result. Deterministic section-contract and sentence-binding errors after
bounded local repair are non-retryable with the same input.
V32 retains the V31 evidence and citation scope. Its generated footnote numbers
precede a following comma or period in the manuscript text, including Japanese
and Western punctuation variants. After `redraft_body_prose`, V32 moves only
editable prose within the same paragraph so generated numbers remain immediately
before punctuation. A paragraph whose rewritten text cannot support that position
retains its pre-redraft text for human review. Persisted V31 Runs keep their original
renderer policy and workflow definition.
When a citation evidence node is shared across claims, V31 selects its required
source IDs through the ratified atomic evidence contract for the section. The
node's optional single `claim_id` field is not used to discard a source that the
atomic contract assigns to the section. The draft obligation carries all relevant
claim IDs when a source supports more than one claim.

If a V31 mandatory citation lacks a confirmed bibliography entry or its
source-to-claim evidence link, drafting records the source ID, affected section
and claims, any supplied author/work identity, and the author's original
assertion as an unresolved citation obligation. The implicated evidence node
is withheld from verified citation treatment, while the section's supported
argument remains in the candidate. The generator does not invent a reference
ID, quotation, footnote, or source assertion. The obligation and its review
item travel with the candidate to the final proposal; they cannot produce a
clean completion status. This uncertainty does not create an intermediate
`waiting_human` stop at `draft_rewrite`.
If one argument evidence node contains several mandatory citations, deferring
that node records every affected source obligation, including sources with
otherwise complete bibliography entries; none disappears from final review.

For the V31 manuscript generator, three distinct workflow nodes run after `validate_draft_coherence` and before `extract_proofreading_sentences`: `diagnose_japanese_prose` diagnoses vocabulary and syntax without editing; `redraft_japanese_paragraphs` rewrites paragraphs with adjacent context, allowing sentences to be split, joined, and reordered; and `review_japanese_sections` compares each candidate to the original for claims, distinctions, qualifications, and attribution independently of style, then reads the exact candidate as a section and rates vocabulary, collocation, syntax, reference clarity, abstraction, rhythm, and academic register separately. Failed checks route only unresolved sections back to `redraft_japanese_paragraphs`, up to three attempts per section. It may change sentence structure inside a paragraph but cannot change the locked plan, paragraph IDs, section boundaries, evidence bindings, citation anchors, quoted material, numbers, or protected human content. The application applies text-only edits to canonical HTML and rejects candidates that remove source mentions or protected information. It recomputes the paragraph-to-evidence bridge for every candidate; footnote rendering and final citation audits still run downstream. If all attempts fail, the source section remains, and the attempts and a human review item identify the unresolved quality or preservation issue.

This plan-first approach is consistent with Plan-and-Write (Yao et al., 2019), detailed outline control in DOC (Yang et al., 2023), and intermediate planning representations for long-form generation (Liang et al., 2024). Dynamic outlining work (Wang et al., 2025) supports explicit, versioned updates instead of silent plan mutation.

## User decisions and approval boundary

V31 and V32 additionally run `redraft_body_prose` after footnote and bibliography rendering.
This stage expands unexplained enumerations and clarifies the writer's theoretical
acts without changing the argument plan. It edits only existing DOM text slots,
locks all note and bibliography content, independently audits paragraph content
and section flow, and keeps unresolved paragraphs unchanged with their IDs and
reasons. Four final validators read the exact redrafted HTML; changed body text
cannot reuse an earlier semantic audit. Freeze performs a read-only surface check
on that audited candidate. The report is persisted with the proposal and submission
manifest. The full contract is in
[`../japanese-prose-style-policy.md`](../japanese-prose-style-policy.md).

Show missing information, model assumptions, affected claims and sections, candidate choices, and the default used if the user does not answer. Ask for input when an unresolved choice materially changes the paper's purpose, scope, or factual basis; other uncertainty may continue as a labeled assumption. The pending proposal presents the candidate HTML, provenance, generated references and footnotes, audit results, and unresolved questions together. Human approval is the boundary before the first paper Revision. Protected human-authored blocks remain unchanged throughout.

## Conceptual references

- Grosz, Barbara J. and Candace L. Sidner (1986), “Attention, Intentions, and the Structure of Discourse.”
- Grosz, Barbara J., Aravind K. Joshi, and Scott Weinstein (1995), “Centering: A Framework for Modeling the Local Coherence of Discourse.”
- Mann, William C. and Sandra A. Thompson (1988), “Rhetorical Structure Theory: Toward a Functional Theory of Text Organization.”
- Yao, Lili et al. (2019), “Plan-and-Write: Towards Better Automatic Storytelling.”
- Yang, Kevin et al. (2023), “DOC: Improving Long Story Coherence With Detailed Outline Control.”
- Liang, Yi et al. (2024), “Integrating Planning into Single-Turn Long-Form Text Generation.”
- Wang, Qianyue et al. (2025), “Generating Long-form Story Using Dynamic Hierarchical Outlining with Memory-Enhancement.”
