export const PANE_NAMES = ["left", "right", "bottom"];

export const PANE_LIMITS = Object.freeze({
  left: Object.freeze({ min: 180, max: 420 }),
  right: Object.freeze({ min: 240, max: 680 }),
  bottom: Object.freeze({ min: 96, max: 420 }),
});

export function normalizePaneLayout(value = {}) {
  const source = value && typeof value === "object" ? value : {};
  const visibleSource = source.visible && typeof source.visible === "object" ? source.visible : {};
  const sizesSource = source.sizes && typeof source.sizes === "object" ? source.sizes : {};
  const visible = {};
  const sizes = {};

  PANE_NAMES.forEach((pane) => {
    visible[pane] = visibleSource[pane] !== false;
    const size = Number(sizesSource[pane]);
    sizes[pane] = Number.isFinite(size) && size > 0 ? Math.round(size) : null;
  });

  return { visible, sizes };
}

export function clampPaneSize(pane, value, dynamicMaximum = Infinity) {
  const limits = PANE_LIMITS[pane];
  if (!limits) throw new Error(`Unknown pane: ${pane}`);
  const requestedMaximum = Number(dynamicMaximum);
  const maximum = Number.isFinite(requestedMaximum)
    ? Math.max(limits.min, Math.min(limits.max, requestedMaximum))
    : limits.max;
  const numericValue = Number(value);
  const resolvedValue = Number.isFinite(numericValue) ? numericValue : limits.min;
  return Math.round(Math.max(limits.min, Math.min(maximum, resolvedValue)));
}

export function paneSizeFromKey(pane, currentSize, key, { shiftKey = false, maximum = Infinity } = {}) {
  const limits = PANE_LIMITS[pane];
  if (!limits) return null;
  if (key === "Home") return limits.min;
  if (key === "End") return clampPaneSize(pane, maximum, maximum);

  const step = shiftKey ? 48 : 16;
  const directions = {
    left: { ArrowLeft: -1, ArrowRight: 1 },
    right: { ArrowLeft: 1, ArrowRight: -1 },
    bottom: { ArrowUp: 1, ArrowDown: -1 },
  };
  const direction = directions[pane]?.[key];
  if (!direction) return null;
  return clampPaneSize(pane, Number(currentSize) + direction * step, maximum);
}
