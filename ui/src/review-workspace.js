// A small per-review store: in-flight saves never replace newer local input.
export class ReviewDrafts {
  entries = new Map();

  load(packet) {
    if (!this.entries.has(packet.review_id)) {
      this.entries.set(packet.review_id, { packet, version: 0, dirty: false, saving: null });
    }
    return this.entries.get(packet.review_id);
  }

  edit(id, update) {
    const entry = this.entries.get(id);
    update(entry.packet);
    entry.version += 1;
    entry.dirty = true;
  }

  async save(id, send, submit = false) {
    const entry = this.entries.get(id);
    const previous = entry.saving;
    const work = async () => {
      if (previous) await previous;
      do {
        const version = entry.version;
        const packet = entry.packet;
        const result = await send({ revision: packet.revision,
          answers: structuredClone(packet.answers), overall_comment: packet.overall_comment,
          submit });
        packet.revision = result.revision;
        packet.submitted_revision = result.submitted_revision;
        packet.submitted_at = result.submitted_at;
        entry.dirty = entry.version !== version;
      } while (entry.dirty);
      return entry.packet;
    };
    const pending = work();
    entry.saving = pending;
    try { return await pending; } finally { if (entry.saving === pending) entry.saving = null; }
  }
}

export function reviewPreviewDocument(html) {
  const parsed = new DOMParser().parseFromString(html, "text/html");
  const allowed = new Set("ARTICLE SECTION HEADER FOOTER ASIDE H1 H2 H3 H4 H5 H6 P DIV SPAN BLOCKQUOTE OL UL LI DL DT DD TABLE THEAD TBODY TR TH TD A CITE Q SUP SUB EM STRONG B I BR HR CODE PRE".split(" "));
  const clean = (source) => {
    if (source.nodeType === Node.TEXT_NODE) return document.createTextNode(source.textContent);
    if (!allowed.has(source.tagName)) return document.createTextNode("");
    const target = document.createElement(source.tagName.toLowerCase());
    if (source.id) target.id = source.id;
    if (source.tagName === "A" && /^#[\w:.-]+$/.test(source.getAttribute("href") || "")) {
      target.setAttribute("href", source.getAttribute("href"));
    }
    for (const child of source.childNodes) target.append(clean(child));
    return target;
  };
  const content = document.createElement("div");
  for (const child of parsed.body.childNodes) content.append(clean(child));
  return `<!doctype html><html lang="ja"><head><meta charset="utf-8">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
    <style>html{color-scheme:dark}*{box-sizing:border-box}body{margin:0;padding:24px;background:#172713;color:#D5DED3;font:16px/1.9 'Segoe UI Variable','Noto Sans JP','Yu Gothic UI',sans-serif;overflow-wrap:anywhere}article{max-width:70ch;margin:auto}h1,h2,h3{line-height:1.5;text-wrap:balance}h1{font-size:1.6rem}h2{font-size:1.3rem;margin-top:2em}h3{font-size:1.1rem}p{margin:1em 0}a{color:#9ED590}blockquote{border-left:2px solid #A3AAA1;margin:1em 0;padding-left:1em}table{border-collapse:collapse;width:100%}th,td{border:1px solid #3E433D;padding:8px}pre{white-space:pre-wrap}</style></head><body>${content.innerHTML}</body></html>`;
}
