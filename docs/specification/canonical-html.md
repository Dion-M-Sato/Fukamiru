# Canonical HTML v1

Canonical prose is stored as one XML-compatible HTML fragment with an `article` root.

```html
<article id="DOC-001" lang="ja">
  <section id="SEC-001">
    <h2 id="H-001">第一章</h2>
    <p id="P-001">本文です。</p>
  </section>
</article>
```

## Invariants

- The only root is `article`; it requires stable `id` and `lang`.
- The article contains sections.
- Every section starts with one heading matching section depth.
- Every structural block has a unique stable id.
- `script`, `style`, inline style and event-handler attributes are rejected.
- Internal `href="#..."` references must resolve.
- A foreign-language inline span may carry its own `lang`.
- Imported human blocks are protected. An agent may add unprotected blocks but cannot change or remove protected blocks.

The original imported HTML is preserved separately under the selected project's `imports` directory. Import normalization creates a `human_import` Revision instead of overwriting the source.
