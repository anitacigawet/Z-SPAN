/** Deterministic grammar gate for Librarian queries. */

export const GATE_VERSION = "grammar-v2";
export const QUERY_CHAR_CAP = 200;

export const GATE_MESSAGES = {
  not_a_string: "Write one question in the box.",
  empty: "Write a question before sending.",
  too_long: "Keep it under 200 characters — one focused question.",
  control_characters:
    "Use spaces between words instead of tabs, line breaks, or other control characters.",
  non_ascii:
    "Use plain English letters only — write names without accents and spell numbers out as words.",
  digits: "Spell numbers out as words — write 'sixteen million', not '16M'.",
  no_terminal_question_mark: "End your question with one question mark.",
  multiple_question_marks: "Use one question mark, at the end.",
  multiple_sentences:
    "Ask one focused question without periods, exclamation marks, semicolons, or colons.",
  no_words: "Write at least one word before the question mark.",
  bad_word:
    "Use words made from letters, apostrophes, or hyphens; a comma sits right after a word — like 'approved, and' — not {token}.",
} as const;

export type GateReasonCode = keyof typeof GATE_MESSAGES;

export interface LibrarianGateResult {
  ok: boolean;
  canonicalQuery?: string;
  reasonCode?: GateReasonCode;
  message?: string;
}

const WORD_RE = /^[A-Za-z]+(?:['-][A-Za-z]+)*'?,?$/;

function reject(
  reasonCode: GateReasonCode,
  token?: string
): LibrarianGateResult {
  const message =
    token === undefined
      ? GATE_MESSAGES[reasonCode]
      : GATE_MESSAGES[reasonCode].replace("{token}", JSON.stringify(token));
  return { ok: false, reasonCode, message };
}

export function validateLibrarianQuery(
  raw: string | null | undefined
): LibrarianGateResult {
  if (typeof raw !== "string") {
    return reject("not_a_string");
  }

  if (Array.from(raw).length > QUERY_CHAR_CAP) {
    return reject("too_long");
  }

  if (
    Array.from(raw).some(char => {
      const codePoint = char.codePointAt(0)!;
      return codePoint < 0x20 || codePoint === 0x7f;
    })
  ) {
    return reject("control_characters");
  }

  if (Array.from(raw).some(char => char.codePointAt(0)! > 0x7e)) {
    return reject("non_ascii");
  }

  if (/[0-9]/.test(raw)) {
    return reject("digits");
  }

  const canonical = raw.trim().replace(/ +/g, " ");
  if (Array.from(canonical).length > QUERY_CHAR_CAP) {
    throw new Error("Canonical Librarian query exceeded its raw length");
  }
  if (!canonical) {
    return reject("empty");
  }

  if (!canonical.endsWith("?")) {
    return reject("no_terminal_question_mark");
  }

  if (canonical.split("?").length - 1 !== 1) {
    return reject("multiple_question_marks");
  }

  if (/[.!;:]/.test(canonical)) {
    return reject("multiple_sentences");
  }

  const body = canonical.slice(0, -1).trim();
  if (!body) {
    return reject("no_words");
  }

  const tokens = body.split(" ");
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index];
    if (!WORD_RE.test(token)) {
      return reject("bad_word", token);
    }
    if (index === tokens.length - 1 && token.endsWith(",")) {
      return reject("bad_word", token);
    }
  }

  return { ok: true, canonicalQuery: canonical };
}
