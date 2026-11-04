const ACTIVITIES = new Set([
  "projects", "translation", "authoring", "literature", "rag", "review", "settings",
]);
const PANELS = new Set(["problems", "run", "events"]);

export function readSelectedTabs(search) {
  const params = new URLSearchParams(search);
  const activity = params.get("activity");
  const panel = params.get("panel");
  return {
    activity: ACTIVITIES.has(activity) ? activity : "projects",
    panel: PANELS.has(panel) ? panel : "problems",
  };
}

export function urlForSelectedTabs(href, { activity, panel }) {
  const url = new URL(href);
  if (ACTIVITIES.has(activity) && activity !== "projects") {
    url.searchParams.set("activity", activity);
  } else {
    url.searchParams.delete("activity");
  }
  if (PANELS.has(panel) && panel !== "problems") {
    url.searchParams.set("panel", panel);
  } else {
    url.searchParams.delete("panel");
  }
  return `${url.pathname}${url.search}${url.hash}`;
}
