# Argument and discourse authoring

The section-once 33B profile follows [the current Japanese proofreading contract](japanese-proofreading.md).
The earlier local-profile drafting and review rules below remain available for persisted workflows.

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
attach to each explicitly referenced book title immediately before its closing
`』`, whether it appears alone, beside other titles, or in a separate clause.
Existing note IDs, numbers and reference bindings survive rendering again,
including relocation of older title-bound numbers outside their book titles.
Other citations retain their clause anchors.
Primary-work title extraction ignores generated footnote labels and semantic
note references, so a number inside `』` cannot become part of a work title or
create an additional bibliography entry. Actual numbers in titles remain intact.
After the final prose edit and all paragraph rollbacks, V32 extracts verification
status and missing-source caveats as verbatim editable-text excerpts. It deletes
only those excerpts and records their original wording with paragraph and section
IDs in human review. Extraction failures stop the node. Substantive scope,
conditions, negation and counterevidence remain in the argument. Locked human
blocks, inline citations, quotations and footnotes remain protected. A paragraph
containing only an editorial note retains its stable empty DOM node.
The reader review receives prose without superscript footnote labels.
After verification-note removal, V32 independently reviews every adjacent body
paragraph pair, including section boundaries and protected paragraphs as context.
It separately judges whether the central concept is carried into the next
paragraph and whether an appropriate discourse relation is apparent at its start.
An additive `も` or `だけでなく` inside a sentence does not alone establish an
adequate paragraph-level addition cue. Planning, drafting and final composition
preserve necessary central-term repetition and select context-appropriate cues.
The reviewer may propose one repair of the next paragraph's opening sentence;
later sentences, protected text, titles, quotations and notes remain fixed.
All boundaries are rechecked against the exact candidate after any repair,
including the other side of an edited paragraph and boundaries across batches.
An independent comparison must preserve claims, scope, conditions, negation,
certainty and attribution. A failed meaning comparison rejects the repair and
retains an addressed human-review item; it never adds a prose caveat or new
argument to bridge a gap. Invalid coverage or fabricated excerpts stop before
checkpointing. The report is bound to the final prose hash and survives submission.
An unapproved V32 manuscript can also branch through
`POST /api/runs/{run_id}/restart-authoring-from-footnotes`. The new Run reuses
the latest successful footnote node's immutable input, including the authored
paragraphs, evidence and human feedback. It discards downstream render, audit,
proposal and artifact results, records source Run/node IDs and content hashes,
and executes the normal tail from footnote rendering to a new pending proposal.
The source Run and its saved reviews remain intact. Active Runs, incomplete
handoffs and papers already confirmed as a Revision cannot use this boundary.
For citations anchored to prose rather than a book-title tag, numbers
precede a following comma or period in the manuscript text, including Japanese
and Western punctuation variants. After `redraft_body_prose`, V32 moves only
editable prose within the same paragraph so those generated numbers remain immediately
before punctuation; title-bound numbers remain inside their closing `』`.
A paragraph whose rewritten text cannot support that position
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

## V32 contextual concept fit

Claim hierarchy, argument graph, defenses and section locking check a concept's
local meaning, applicability and argumentative role before accepting it. The
independent `review_concept_fit_v32` stage distinguishes appropriate concepts,
wording defects, unsupported premises and unresolved applicability. A generated
contract or an occurrence in the memo does not establish applicability. Defined
authorial analysis is permitted without a verbatim source conclusion. Internal
research gaps and rival hypotheses retain their planning role.

Source-verification status is independent of conceptual applicability. An
unverified attribution or an `added_theory` label alone cannot invalidate a
reasoned interpretation. The compact review material retains the original
theory-attribution permissions. Necessary named concepts remain protected even
when their explanation needs wording repair. Removed verification closures are
saved once, with their original text and target claim, in human review items;
they do not become qualifications in the article. Reviews preserve the author's
contrast, negation target and causal direction rather than neutralizing a value
word or metaphor solely because of its register.

Internal evidence cards carry an explicit `evidence_record` role. Their accurate
verification boundaries are valid ledger information, not reader-facing caveats;
the same status sentence in an article remains a wording defect. This role does
not exempt concepts, research methods or object classifications from inspection.
An isolated V32 argument failure limited to wording in saved evidence cards can
be replayed once after correcting the role contract. The replay retains accepted
checkpoints and records the previous failure; it does not accept a failed review
or promote evidence to verified status.
The same explicit replay endpoint also permits a first-repair regression when
the native result, repair edits and recheck agree with the saved failure and no
second repair has been attempted. The second repair must satisfy the full gate.
Rejected reviews are never accepted as checkpoints: their archived response can
only reconstruct a probe for an exactly matching, previously accepted corrected
review. A probe miss cannot invoke a model; the normal fresh review path follows.
Report versions retain earlier failure files instead of overwriting them.
Text units use canonical object-key traversal, so checkpoint serialization cannot
change their order. Matching an older saved recheck requires the same unique unit
IDs and failure contents; its historical object-key order is not a semantic change.

The review also covers adoption reasons. An internal research gap or a question
does not authorize a new research method, corpus or validation obligation.
Editorial omissions do not make a qualitative reading a sampling study, nor do
they justify silently restricting it to the works retained in a draft. Original
research designs and explicit object conditions remain valid scope evidence.

Quoted phrases are exposed as explicit review candidates. Each candidate must
receive its own verdict, so a verdict about a bundled "analysis framework" cannot
silently omit the individual theory names. Candidate extraction does not establish
applicability or require preservation; unsupported concepts can still be rejected.

A failed upstream candidate receives at most two `repair_concept_fit_v32` calls,
each followed by independent reinspection against the candidate and memo spans.
This permits correction of a new premise introduced by the first repair. An
unchanged candidate or a return to a rejected candidate stops immediately. Prose
wording gets one local repair; a premise defect still returns to the plan.
Repair edits address exact text fields; IDs, hierarchy, citation bindings and
other JSON structure cannot change. Only optional term/qualification list items
can be removed. Appropriate necessary specialist terms are protected through
repair; omitted specialist concepts in prose are checked against the source.
Unresolved premises cannot be hidden by a synonym, an unbounded claim or a new
verification caveat. Reports retain candidates, verdicts, repairs and hashes.

Japanese diagnosis and section review additionally check applicability with
original source material. Paragraph and final prose generation can repair wording;
a premise defect requires an upstream claim/contract repair. The frozen final
candidate receives a separate concept audit; its hash and any unresolved findings
are retained for human review. This partial audit does not certify every semantic
or source-verification requirement.

The frozen concept audit uses reader paragraph text with superscript `doc-noteref`
labels excluded. Original title digits, years and prose numbers remain intact.
Citation HTML and title-only note placement before `』` are unchanged.

`POST /api/runs/{run_id}/restart-authoring-from-final-audit` creates a new Run
from the saved completed `audit_final_findings` input. It requires an inactive
V32 source, an unapproved target, valid prose handoff and frozen HTML identical
to the source Run's saved final HTML. It preserves the source history and all
upstream findings, and executes only final audit, judgement and submission.
The old audit and review report remain with the source Run. The new audit creates
its own review report and pending proposal without revising or approving prose.

`POST /api/runs/{run_id}/restart-authoring-from-argument` branches a V32 Run from
its completed post-evidence argument input. It rechecks inherited claims, creates
a new plan, rebuilds the source-preservation branch with a fresh join generation,
and regenerates the argument, contracts and prose using saved evidence. Original
Runs, answers and artifacts are preserved. No approval is submitted automatically.
