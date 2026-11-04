export function translationSourceRuns(items) {
  const seen = new Set();
  return items
    .map((item) => item.run || item)
    .filter((run) => {
      const source = String(run.input?.source_path || "").trim();
      if (run.workflow_name !== "translation" || !source || seen.has(source)) return false;
      seen.add(source);
      return true;
    });
}

export function approvedOutputReviews(reviews) {
  return reviews.filter((review) =>
    review.object_type === "manuscript_rewrite_proposal"
    || (review.object_type === "authoring_submission" && ["authoring", "manuscript_generator"].includes(review.workflow_name))
    || (review.object_type === "run_artifact" && review.workflow_name === "translation"));
}
