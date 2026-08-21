/** Deterministic contextual stencil for grammar-valid Librarian queries. */

import {
  GATE_MESSAGES,
  GATE_VERSION,
  type GateReasonCode,
  validateLibrarianQuery,
} from "./librarianInputGate";

export const STENCIL_VERSION = "stencil-v2";
export const COMPOSED_GATE_VERSION = `${GATE_VERSION}+${STENCIL_VERSION}`;

export const STENCIL_MESSAGES = {
  ...GATE_MESSAGES,
  not_a_question: "Start with a question word — like What, Why, How, or Did.",
  artifact_pattern:
    "That doesn't look like a question about the meeting — ask about what happened in the record.",
} as const;

type StencilReasonCode = GateReasonCode | "not_a_question" | "artifact_pattern";

export interface LibrarianStencilResult {
  ok: boolean;
  canonicalQuery?: string;
  reasonCode?: StencilReasonCode;
  message?: string;
  matchedRuleId?: string;
  gateVersion: string;
}

const INTERROGATIVE_LEADS = new Set([
  "what",
  "who",
  "whom",
  "whose",
  "when",
  "where",
  "why",
  "how",
  "which",
  "did",
  "does",
  "do",
  "is",
  "are",
  "was",
  "were",
  "can",
  "could",
  "will",
  "would",
  "should",
  "shall",
  "has",
  "have",
  "had",
  "may",
  "might",
  "must",
  "am",
  "isn't",
  "aren't",
  "wasn't",
  "weren't",
  "don't",
  "doesn't",
  "didn't",
  "can't",
  "couldn't",
  "won't",
  "wouldn't",
  "shouldn't",
  "hasn't",
  "haven't",
  "hadn't",
  "mightn't",
  "mustn't",
]);
const ARTIFACT_BIGRAMS = new Set([
  "system prompt",
  "developer prompt",
  "system instructions",
  "developer instructions",
]);
const ARTIFACT_NOUNS = new Set([
  "instructions",
  "instruction",
  "prompt",
  "prompts",
  "rules",
  "guidelines",
]);
const DISCARD_VERBS = new Set([
  "ignore",
  "disregard",
  "override",
  "forget",
  "discard",
  "abandon",
  "reset",
]);
const NOFOLLOW_PHRASES = [
  ["without", "following"],
  ["without", "obeying"],
  ["do", "not", "follow"],
  ["not", "follow"],
];
const EXTRACTION_VERBS = new Set([
  "reveal",
  "repeat",
  "show",
  "expose",
  "disclose",
  "dump",
  "print",
]);
const EXTRACTION_NOUNS = new Set([
  "prompt",
  "prompts",
  "instructions",
  "instruction",
]);
const ROLE_PHRASES = [
  ["act", "as"],
  ["pose", "as"],
  ["behave", "as"],
  ["function", "as"],
  ["operate", "as"],
  ["role", "play"],
  ["roleplay"],
  ["pretend", "you", "are"],
  ["pretend", "to", "be"],
];
const SECOND_PERSON = new Set(["you", "your"]);
const ROLE_ARTIFACT_CONTEXT = new Set([
  "system",
  "override",
  "prompt",
  "instructions",
  "unrestricted",
  "unfiltered",
  "developer",
  "assistant",
]);
const EVASION_SINGLE_TOKENS = new Set([
  "bypass",
  "evade",
  "circumvent",
  "disable",
]);
const EVASION_NOUNS = new Set([
  "gate",
  "filter",
  "restrictions",
  "restriction",
  "rules",
  "safeguards",
]);
const ARTICLES = new Set(["the", "a", "an"]);
const POSSESSIVE_NOUNS = new Set([
  "rules",
  "instructions",
  "prompt",
  "prompts",
  "guidelines",
  "filter",
]);

function canonicalTokens(canonicalQuery: string): string[] {
  const body = canonicalQuery.slice(0, -1).trim();
  return body.split(" ").map(token => token.replace(/,$/, "").toLowerCase());
}

function artifactTokens(surfaceTokens: string[]): string[] {
  return surfaceTokens.flatMap(surfaceToken => {
    const withoutTrailingApostrophe = surfaceToken.endsWith("'")
      ? surfaceToken.slice(0, -1)
      : surfaceToken;
    return withoutTrailingApostrophe
      .replace(/[-']/g, " ")
      .split(" ")
      .filter(Boolean);
  });
}

function containsPhrase(tokens: string[], phrase: string[]): boolean {
  for (let index = 0; index + phrase.length <= tokens.length; index += 1) {
    if (phrase.every((token, offset) => tokens[index + offset] === token)) {
      return true;
    }
  }
  return false;
}

function hasAnyPhrase(tokens: string[], phrases: string[][]): boolean {
  return phrases.some(phrase => containsPhrase(tokens, phrase));
}

function intersects(tokens: Set<string>, candidates: Set<string>): boolean {
  return Array.from(candidates).some(token => tokens.has(token));
}

function hasNonattributiveEvasion(tokens: string[]): boolean {
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index];
    const isEvasion =
      EVASION_SINGLE_TOKENS.has(token) ||
      (token === "get" && tokens[index + 1] === "around");
    if (!isEvasion) {
      continue;
    }
    if (index > 0 && ARTICLES.has(tokens[index - 1])) {
      continue;
    }
    return true;
  }
  return false;
}

function reject(
  reasonCode: StencilReasonCode,
  matchedRuleId?: string,
  message?: string
): LibrarianStencilResult {
  return {
    ok: false,
    reasonCode,
    message: message ?? STENCIL_MESSAGES[reasonCode],
    matchedRuleId,
    gateVersion: COMPOSED_GATE_VERSION,
  };
}

function rejectArtifact(matchedRuleId: string): LibrarianStencilResult {
  return reject("artifact_pattern", matchedRuleId);
}

export function evaluateLibrarianQuery(
  raw: string | null | undefined
): LibrarianStencilResult {
  const grammarResult = validateLibrarianQuery(raw);
  if (!grammarResult.ok) {
    return reject(
      grammarResult.reasonCode ?? "not_a_string",
      undefined,
      grammarResult.message
    );
  }

  const canonicalQuery = grammarResult.canonicalQuery!;
  const surfaceTokens = canonicalTokens(canonicalQuery);

  if (!INTERROGATIVE_LEADS.has(surfaceTokens[0])) {
    return reject("not_a_question");
  }

  const tokens = artifactTokens(surfaceTokens);
  const tokenSet = new Set(tokens);

  for (let index = 0; index + 1 < tokens.length; index += 1) {
    if (ARTIFACT_BIGRAMS.has(`${tokens[index]} ${tokens[index + 1]}`)) {
      return rejectArtifact("deny.artifact_bigram.v2");
    }
  }

  if (tokenSet.has("jailbreak") || containsPhrase(tokens, ["jail", "break"])) {
    return rejectArtifact("deny.jailbreak.v2");
  }

  if (
    intersects(tokenSet, DISCARD_VERBS) &&
    intersects(tokenSet, ARTIFACT_NOUNS)
  ) {
    return rejectArtifact("deny.discard.v2");
  }

  if (
    hasAnyPhrase(tokens, NOFOLLOW_PHRASES) &&
    intersects(tokenSet, ARTIFACT_NOUNS)
  ) {
    return rejectArtifact("deny.nofollow.v2");
  }

  if (
    intersects(tokenSet, EXTRACTION_VERBS) &&
    intersects(tokenSet, EXTRACTION_NOUNS)
  ) {
    return rejectArtifact("deny.extraction.v2");
  }

  if (
    hasAnyPhrase(tokens, ROLE_PHRASES) &&
    (intersects(tokenSet, SECOND_PERSON) ||
      intersects(tokenSet, ROLE_ARTIFACT_CONTEXT))
  ) {
    return rejectArtifact("deny.role.v2");
  }

  if (hasNonattributiveEvasion(tokens) && intersects(tokenSet, EVASION_NOUNS)) {
    return rejectArtifact("deny.evasion.v2");
  }

  if (tokenSet.has("your") && intersects(tokenSet, POSSESSIVE_NOUNS)) {
    return rejectArtifact("deny.possessive.v2");
  }

  return {
    ok: true,
    canonicalQuery,
    gateVersion: COMPOSED_GATE_VERSION,
  };
}
