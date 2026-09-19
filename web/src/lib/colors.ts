// Colour rules (spec E + dataviz palette):
// - the reference algorithm is always the neutral grey
// - candidates take categorical slots 1..3 in their selection order (validated light + dark)
// - a comparison therefore holds at most 4 algorithms (reference + 3)
export const MAX_ALGORITHMS = 4;
const SLOTS = ["var(--s1)", "var(--s2)", "var(--s3)"];
export const REFERENCE_COLOR = "var(--ref)";

export function colorMap(keysInOrder: string[], reference: string): Record<string, string> {
  const out: Record<string, string> = {};
  let slot = 0;
  for (const k of keysInOrder) {
    if (k === reference) out[k] = REFERENCE_COLOR;
    else out[k] = SLOTS[Math.min(slot++, SLOTS.length - 1)];
  }
  return out;
}
