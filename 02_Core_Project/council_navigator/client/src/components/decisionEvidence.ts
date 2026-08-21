export type DecisionEvidenceSpan = {
  text?: string;
  start_seconds?: number;
  end_seconds?: number;
  source?: string;
  label?: string;
  structure?: "contiguous" | "elided" | string;
  omission_marker?: string;
  word_timings?: DecisionEvidenceWordTiming[];
};

export type DecisionEvidenceWordTiming = {
  word?: string;
  start?: number;
  end?: number;
};

export type DecisionEvidence = {
  index: number;
  verbatim_spans?: DecisionEvidenceSpan[];
};

export type DecisionEvidenceState = "closed" | "open";

export function transitionDecisionEvidenceState(
  state: DecisionEvidenceState,
): DecisionEvidenceState {
  return state === "closed" ? "open" : "closed";
}

export function paragraphizeVerbatimWords(
  words: DecisionEvidenceWordTiming[],
  pauseSeconds = 1.5,
  storedSpanText?: string,
): string[] | null {
  if (!Array.isArray(words) || words.length === 0 || !Number.isFinite(pauseSeconds)) {
    return null;
  }

  const paragraphs: string[][] = [[]];
  const tokens: string[] = [];
  let previousStart = -Infinity;
  let previousEnd = -Infinity;

  for (const timing of words) {
    const { word, start, end } = timing;
    if (
      typeof word !== "string"
      || typeof start !== "number"
      || typeof end !== "number"
      || !Number.isFinite(start)
      || !Number.isFinite(end)
      || start < 0
      || end < start
      || start < previousStart
      || end < previousEnd
    ) {
      return null;
    }
    if (tokens.length > 0 && start - previousEnd >= pauseSeconds) {
      paragraphs.push([]);
    }
    paragraphs[paragraphs.length - 1].push(word);
    tokens.push(word);
    previousStart = start;
    previousEnd = end;
  }

  const joined = tokens.join(" ");
  if (storedSpanText !== undefined && joined !== storedSpanText) {
    return null;
  }
  const result = paragraphs.map((paragraph) => paragraph.join(" "));
  return result.join(" ") === joined ? result : null;
}

export const EXCERPT_SOURCE = "item_quote_to_action_quote";
