/**
 * Stitch-chrome — generative chrome for cited reports.
 *
 * Port of fractal-framework's Stitch pipeline (wizard/server/_core/
 * stitch.ts + siteScaffold.ts) adapted to Z-SPAN:
 *
 * - Stitch NEVER sees report content. It designs chrome around literal
 * {{TOKEN}} placeholders; the real content (already-rendered HTML
 * fragments from the Flask fragments endpoint — same fragments the V0
 * template uses, chips and provenance included) is injected locally
 * after the design is validated. LLM-directives-as-soft-hints physics:
 * the prompt asks for preservation, the string check guarantees it.
 * - Validation is occurrence-COUNT (== 1 per required token), not the
 * source project's presence-only check — a duplicated token would
 * double-inject a section into a civic record.
 * - Aesthetic direction is Z-SPAN's dark broadcast language, not the
 * source project's amber advocacy look: a record, not a pitch.
 *
 * The driver runs inside the Express process (the SDK is npm-only);
 * Flask stays the DB owner — outcomes persist via POST
 * /api/report-runs/:id/stitch-result with the operator cookie forwarded,
 * so the owner gate rides every hop. Live progress is process-local
 * (Express restart loses the progress view, never the persisted result).
 */
import { readFileSync } from "fs";
import { homedir } from "os";
import path from "path";

const PARSER_API_URL = process.env.PARSER_API_URL || "http://127.0.0.1:5001";

// ── Key + model resolution ────────────────────────────────────────────
// Canonical operator-key home is parsers/user_settings.json (gitignored);
// env var wins for lab overrides. Never log the key. Path resolution
// avoids import.meta (tsx may compile CJS where it's undefined): env
// override → cwd-relative (the dev server runs from council_navigator/)
// → home-derived fallback for the operator's standard layout, mirroring
// qdrant_synthesizer's claude-bin resolution idiom.
const USER_SETTINGS_CANDIDATES = [
 process.env.ZSPAN_USER_SETTINGS_PATH,
 path.resolve(process.cwd(), "parsers", "user_settings.json"),
 // user_settings.json path removed from published tree,
].filter((p): p is string => !!p);

export interface StitchAuth {
 apiKey?: string;
 accessToken?: string;
 projectId?: string;
}

/**
 * Resolve Stitch credentials by SHAPE, not by field name alone: Google
 * API keys start with "AIza" (durable; sent as X-Goog-Api-Key); other
 * values (e.g. "AQ."-prefixed) are OAuth access tokens (short-lived;
 * sent as Authorization: Bearer, optionally paired with a quota project
 * via stitch_project_id / GOOGLE_CLOUD_PROJECT). The 2026-07-02 smoke
 * proved the wrong-mode failure: an access token sent as an apiKey gets
 * "Expected OAuth 2 access token" back from Google.
 */
export function resolveStitchAuth(): StitchAuth | null {
 // SHAPE-PRIORITY across ALL sources (2026-07-02 brainstorm-audit F1):
 // a stale access token sitting in user_settings must never shadow a
 // real AIza key dropped in Desktop/stitch.txt — so gather every
 // candidate first, then prefer the first AIza-shaped credential from
 // ANY source; only fall back to a token when no key exists anywhere.
 const candidates: string[] = [];
 let projectId = (process.env.GOOGLE_CLOUD_PROJECT || "").trim();
 if (process.env.STITCH_API_KEY) candidates.push(process.env.STITCH_API_KEY.trim());
 for (const path_ of USER_SETTINGS_CANDIDATES) {
 try {
 const settings = JSON.parse(readFileSync(path_, "utf-8"));
 if (typeof settings.stitch_api_key === "string" && settings.stitch_api_key.trim()) {
 candidates.push(settings.stitch_api_key.trim());
 }
 if (typeof settings.stitch_project_id === "string") {
 projectId = projectId || settings.stitch_project_id.trim();
 }
 } catch {
 // try the next candidate
 }
 }
 try {
 // The Desktop drop-file the key arrived in (2026-07-02): replacing
 // its contents with a fresh credential works with no re-ingestion.
 const desktop = readFileSync(path.join(homedir(), "Desktop", "stitch.txt"), "utf-8").trim();
 if (desktop) candidates.push(desktop);
 } catch {
 /* absent — fine */
 }
 if (candidates.length === 0) return null;
 const credential = candidates.find((c) => c.startsWith("AIza")) ?? candidates[0];
 if (credential.startsWith("AIza")) return { apiKey: credential };
 // Non-AIza shapes are OAuth access tokens; the SDK requires a paired
 // Google Cloud project id for Bearer auth (constructor-enforced) and
 // these tokens expire within ~an hour — surfaced to the operator as a
 // clear error rather than a mysterious 401.
 if (!projectId) {
 throw new Error(
 "The configured Stitch credential is an OAuth access token (not an AIza API key). " +
 "The SDK requires accessToken + projectId together, and such tokens expire quickly. " +
 "Durable fix: put the AIza API key from stitch.withgoogle.com's developer flow in " +
 "Desktop/stitch.txt or user_settings.json stitch_api_key — or add stitch_project_id " +
 "to pair with the token."
 );
 }
 return { accessToken: credential, projectId };
}

const STITCH_MODEL = process.env.STITCH_MODEL || "GEMINI_3_FLASH";

// ── Live progress (process-local; poll target) ────────────────────────
export interface StitchEditRecord {
 index: number;
 step: string;
 validation_ok: boolean;
 validation_issues: string[];
 duration_ms: number;
 created_at: number;
}

interface LiveState {
 status: "running" | "complete" | "error";
 progress: string;
 step?: string;
 edits: StitchEditRecord[];
 error?: string;
 startedAt: number;
}

const live = new Map<string, LiveState>();

export function stitchLiveState(reportRunId: string): LiveState | null {
 return live.get(reportRunId) ?? null;
}

export function stitchIsRunning(reportRunId: string): boolean {
 return live.get(reportRunId)?.status === "running";
}

// ── Fragments shape (from Flask) ──────────────────────────────────────
interface Fragments {
 title: string;
 scope_label: string;
 meta_line: string;
 coverage_line: string;
 section_fragments: Record<
 string,
 { heading: string; html: string; status: string }
 >;
 sources_html: string;
 provenance_html: string;
 css: string;
 run_id: string;
 footer_line: string;
}

interface TokenSpec {
 name: string;
 placeholder: string;
 injectionHtml: string;
 promptHint: string;
 required: boolean;
}

function escapeHtml(s: string): string {
 return s
 .replace(/&/g, "&amp;")
 .replace(/</g, "&lt;")
 .replace(/>/g, "&gt;")
 .replace(/"/g, "&quot;")
 .replace(/'/g, "&#039;");
}

const SECTION_ORDER = [
 "synopsis",
 "findings",
 "jurisdictions",
 "quotes",
 "decisions",
];

function buildTokens(frags: Fragments): TokenSpec[] {
 const tokens: TokenSpec[] = [
 {
 name: "REPORT_TITLE",
 placeholder: "{{REPORT_TITLE}}",
 injectionHtml: escapeHtml(frags.title),
 promptHint: `The report's title — the operator's question, e.g. "${frags.title.slice(0, 70)}"`,
 required: true,
 },
 {
 name: "META_LINE",
 placeholder: "{{META_LINE}}",
 injectionHtml: escapeHtml(frags.meta_line),
 promptHint: "One small metadata line: scope + generation timestamp.",
 required: true,
 },
 {
 name: "COVERAGE_LINE",
 placeholder: "{{COVERAGE_LINE}}",
 injectionHtml: frags.coverage_line,
 promptHint:
 "A short honest coverage statement (meetings searched / contributed). Render inside a subtle panel.",
 required: true,
 },
 ];
 for (const key of SECTION_ORDER) {
 const f = frags.section_fragments[key];
 if (!f) continue;
 tokens.push({
 name: `SECTION_${key.toUpperCase()}`,
 placeholder: `{{SECTION_${key.toUpperCase()}}}`,
 injectionHtml: f.html,
 promptHint: `Body content for the "${f.heading}" section — multi-paragraph HTML with inline citation chips. Design the section frame + heading treatment around it.`,
 required: true,
 });
 }
 tokens.push(
 {
 name: "SOURCES_BLOCK",
 placeholder: "{{SOURCES_BLOCK}}",
 injectionHtml: frags.sources_html,
 promptHint:
 "Pre-rendered source cards (one per meeting, with timecode links). Design the section frame around them.",
 required: true,
 },
 {
 name: "PROVENANCE_BLOCK",
 placeholder: "{{PROVENANCE_BLOCK}}",
 injectionHtml: frags.provenance_html,
 promptHint:
 "Pre-rendered methodology & provenance panel (run id, verify pointer, disclaimers). Keep it visually quiet but present.",
 required: true,
 },
 {
 name: "FOOTER_LINE",
 placeholder: "{{FOOTER_LINE}}",
 injectionHtml: escapeHtml(frags.footer_line),
 promptHint: "A small monospace footer line.",
 required: true,
 }
 );
 return tokens;
}

// ── Prompts ───────────────────────────────────────────────────────────
const AESTHETIC_DIRECTION = `Aesthetic direction (Z-SPAN civic-record language — a record, not a pitch):
- Dark broadcast palette: near-black warm canvas (#0A0A0A family, NOT pure black), lifted card surfaces (#141416 / #18181A), hairline borders (white at 5-10% opacity).
- Accent discipline: deep civic blue (#1A3A7C) and info blue (#3B82F6) as the ONLY accent family. No amber, no orange, no red except where content itself carries it. Restraint is the brand.
- Typography: clean humanist sans for body; monospace ONLY for small provenance/metadata details. Generous line-height. Reading column max ~860px.
- Section headers: small uppercase letterspaced eyebrows over content, hairline underline. Editorial, calm, institutional — think premium broadsheet meets terminal, NOT startup landing page, NOT advocacy campaign.
- No hero imagery, no stock photos, no illustrations, no icons beyond typographic marks. This is a civic record document.
- Single self-contained HTML page, desktop-first, mobile-responsive, print-sane.`;

const TOKEN_RULES = `TOKEN PRESERVATION (critical, non-negotiable):
- Every {{TOKEN}} listed MUST appear in the returned HTML EXACTLY ONCE, character-for-character, double curly braces included.
- Tokens are content slots the system fills after you design. Do NOT write copy where a token goes. Do NOT paraphrase, duplicate, split, or remove any token.
- Do NOT add <img>, <picture>, <video>, or <iframe> elements anywhere — the injected content carries everything the page needs.`;

function buildInitialPrompt(tokens: TokenSpec[], frags: Fragments): string {
 const sectionList = tokens
 .map((t) => `- ${t.placeholder} → ${t.promptHint}`)
 .join("\n");
 return `Design a single-page civic-record report titled "${frags.title}" for the Z-SPAN civic network (scope: ${frags.scope_label}).

Page structure, in order: masthead (Z-SPAN wordmark text + "CIVIC RECORD REPORT" eyebrow, {{REPORT_TITLE}} as the headline, {{META_LINE}} beneath), a coverage panel ({{COVERAGE_LINE}}), then the report sections in this order — Executive synopsis ({{SECTION_SYNOPSIS}}), Findings ({{SECTION_FINDINGS}}), By jurisdiction ({{SECTION_JURISDICTIONS}}), Key quotes ({{SECTION_QUOTES}}), Decisions & votes ({{SECTION_DECISIONS}}) — then Sources ({{SOURCES_BLOCK}}), Methodology & provenance ({{PROVENANCE_BLOCK}}), and a footer ({{FOOTER_LINE}}).

All tokens:
${sectionList}

${AESTHETIC_DIRECTION}

${TOKEN_RULES}

Return one complete self-contained HTML page with every token placed exactly once in its proper section.`;
}

const REFINE_STEPS: Array<{ step: string; prompt: string }> = [
 {
 step: "masthead",
 prompt:
 "Refine the masthead and coverage panel: give the title a confident editorial treatment, make the metadata line and coverage panel feel like a premium broadsheet's dateline. Keep the rest of the page unchanged.",
 },
 {
 step: "sections",
 prompt:
 "Refine the five report sections: distinct but consistent section frames, comfortable reading rhythm, quiet visual hierarchy between headings, body, and any lists or blockquotes inside the token content. Keep the rest unchanged.",
 },
 {
 step: "sources_provenance",
 prompt:
 "Refine the Sources and Methodology & provenance sections: card-like source entries, a visually quiet but trustworthy provenance panel, and a restrained monospace footer. Keep the rest unchanged.",
 },
];

// ── Validation (occurrence-count — the fix over the source project) ──
function validateHtml(
 html: string,
 tokens: TokenSpec[]
): { ok: boolean; issues: string[] } {
 const issues: string[] = [];
 for (const t of tokens) {
 const count = html.split(t.placeholder).length - 1;
 if (t.required && count === 0) issues.push(`${t.name}: missing`);
 else if (count > 1) issues.push(`${t.name}: appears ${count}× (must be exactly once)`);
 }
 return { ok: issues.length === 0, issues };
}

// ── Injection + post-passes ───────────────────────────────────────────

// DOMPurify is present only as an unresolvable transitive package in this
// pnpm workspace, and the Node process has no DOM implementation for it to
// operate on. Until the server owns that dependency directly, fail closed by
// reconstructing a deliberately small HTML vocabulary instead of trying to
// subtract dangerous markup with regexes. Every emitted tag and attribute is
// created by this code; unrecognized attributes can never pass through.
const ALLOWED_STITCH_TAGS = new Set([
 "html",
 "head",
 "title",
 "style",
 "body",
 "header",
 "main",
 "footer",
 "nav",
 "section",
 "article",
 "aside",
 "div",
 "span",
 "p",
 "h1",
 "h2",
 "h3",
 "h4",
 "h5",
 "h6",
 "a",
 "strong",
 "em",
 "b",
 "i",
 "u",
 "s",
 "small",
 "sub",
 "sup",
 "mark",
 "br",
 "hr",
 "ul",
 "ol",
 "li",
 "dl",
 "dt",
 "dd",
 "blockquote",
 "pre",
 "code",
 "kbd",
 "samp",
 "var",
 "table",
 "caption",
 "colgroup",
 "col",
 "thead",
 "tbody",
 "tfoot",
 "tr",
 "th",
 "td",
 "time",
 "details",
 "summary",
 "meta",
]);

const DROP_WITH_CONTENT_TAGS = new Set([
 "script",
 "iframe",
 "object",
 "form",
 "template",
 "noscript",
 "svg",
 "math",
 "picture",
 "video",
 "audio",
 "canvas",
]);

const VOID_STITCH_TAGS = new Set(["br", "hr", "col", "meta"]);
const GLOBAL_STITCH_ATTRIBUTES = new Set([
 "class",
 "id",
 "style",
 "title",
 "lang",
 "dir",
 "role",
]);
const TAG_STITCH_ATTRIBUTES: Readonly<Record<string, ReadonlySet<string>>> = {
 time: new Set(["datetime"]),
 ol: new Set(["start", "reversed", "type"]),
 li: new Set(["value"]),
 th: new Set(["colspan", "rowspan", "scope"]),
 td: new Set(["colspan", "rowspan"]),
 col: new Set(["span"]),
 details: new Set(["open"]),
};

interface ParsedHtmlAttribute {
 name: string;
 value: string;
 hasValue: boolean;
}

function escapeAttribute(value: string): string {
 return value
 .replace(/&/g, "&amp;")
 .replace(/</g, "&lt;")
 .replace(/>/g, "&gt;")
 .replace(/"/g, "&quot;")
 .replace(/'/g, "&#39;");
}

function findTagEnd(html: string, start: number): number {
 let quote = "";
 for (let i = start + 1; i < html.length; i += 1) {
 const char = html[i];
 if (quote) {
 if (char === quote) quote = "";
 } else if (char === '"' || char === "'") {
 quote = char;
 } else if (char === ">") {
 return i;
 }
 }
 return -1;
}

function parseAttributes(source: string): ParsedHtmlAttribute[] {
 const attributes: ParsedHtmlAttribute[] = [];
 let cursor = 0;
 while (cursor < source.length) {
 while (cursor < source.length && /[\s/]/.test(source[cursor])) cursor += 1;
 if (cursor >= source.length) break;

 const nameStart = cursor;
 while (cursor < source.length && !/[\s=/>]/.test(source[cursor])) cursor += 1;
 if (cursor === nameStart) {
 cursor += 1;
 continue;
 }
 const name = source.slice(nameStart, cursor).toLowerCase();
 while (cursor < source.length && /\s/.test(source[cursor])) cursor += 1;

 let hasValue = false;
 let value = "";
 if (source[cursor] === "=") {
 hasValue = true;
 cursor += 1;
 while (cursor < source.length && /\s/.test(source[cursor])) cursor += 1;
 const quote = source[cursor];
 if (quote === '"' || quote === "'") {
 cursor += 1;
 const valueStart = cursor;
 while (cursor < source.length && source[cursor] !== quote) cursor += 1;
 value = source.slice(valueStart, cursor);
 if (cursor < source.length) cursor += 1;
 } else {
 const valueStart = cursor;
 while (cursor < source.length && !/[\s>]/.test(source[cursor])) cursor += 1;
 value = source.slice(valueStart, cursor).replace(/\/$/, "");
 }
 }
 attributes.push({ name, value, hasValue });
 }
 return attributes;
}

function decodeUrlEntities(value: string): string {
 let decoded = value;
 // Two passes catch a conservatively encoded separator while never turning
 // the result back into markup: the accepted value is escaped on emission.
 for (let pass = 0; pass < 2; pass += 1) {
 decoded = decoded.replace(
 /&(?:#(\d{1,7})|#x([\da-f]{1,6})|colon|tab|newline|amp);?/gi,
 (match, decimal: string | undefined, hexadecimal: string | undefined) => {
 if (decimal) {
 const point = Number.parseInt(decimal, 10);
 return point <= 0x10ffff ? String.fromCodePoint(point) : "";
 }
 if (hexadecimal) {
 const point = Number.parseInt(hexadecimal, 16);
 return point <= 0x10ffff ? String.fromCodePoint(point) : "";
 }
 const named = match.replace(/[&;]/g, "").toLowerCase();
 if (named === "colon") return ":";
 if (named === "tab") return "\t";
 if (named === "newline") return "\n";
 if (named === "amp") return "&";
 return "";
 }
 );
 }
 return decoded;
}

function safeAnchorHref(value: string): string {
 const decoded = decodeUrlEntities(value).trim();
 if (!decoded || decoded.includes("\\")) return "";
 const compact = decoded
 .replace(/[\u0000-\u0020\u007f-\u009f]/g, "")
 .toLowerCase();
 if (compact.startsWith("https://") || compact.startsWith("http://")) {
 return decoded;
 }
 if (decoded.startsWith("#") || decoded.startsWith("?")) return decoded;
 if (decoded.startsWith("/") && !decoded.startsWith("//")) return decoded;
 return "";
}

function safeInlineStyle(value: string): string {
 const normalized = decodeUrlEntities(value)
 .replace(/[\u0000-\u0020\u007f-\u009f]/g, "")
 .toLowerCase();
 if (
 normalized.includes("url(") ||
 normalized.includes("expression(") ||
 normalized.includes("@import") ||
 normalized.includes("-moz-binding")
 ) {
 return "";
 }
 return value;
}

function sanitizeTagAttributes(
 tagName: string,
 attributes: ParsedHtmlAttribute[]
): string {
 if (tagName === "a") {
 const hrefAttribute = attributes.find((attribute) => attribute.name === "href");
 const href = hrefAttribute ? safeAnchorHref(hrefAttribute.value) : "";
 return `${href ? ` href="${escapeAttribute(href)}"` : ""} rel="noopener noreferrer"`;
 }

 if (tagName === "meta") {
 const charset = attributes.find((attribute) => attribute.name === "charset");
 return charset && /^utf-?8$/i.test(charset.value.trim())
 ? ' charset="utf-8"'
 : "";
 }

 const emitted = new Set<string>();
 let output = "";
 for (const attribute of attributes) {
 const { name } = attribute;
 const isAria = name.startsWith("aria-") && /^aria-[a-z0-9-]+$/.test(name);
 const isData = name.startsWith("data-") && /^data-[a-z0-9-]+$/.test(name);
 const allowedForTag = TAG_STITCH_ATTRIBUTES[tagName]?.has(name) ?? false;
 if (
 emitted.has(name) ||
 (!GLOBAL_STITCH_ATTRIBUTES.has(name) && !allowedForTag && !isAria && !isData)
 ) {
 continue;
 }

 let value = attribute.value;
 if (name === "style") {
 value = safeInlineStyle(value);
 if (!value) continue;
 }
 emitted.add(name);
 if (!attribute.hasValue && (name === "open" || name === "reversed")) {
 output += ` ${name}`;
 } else if (attribute.hasValue) {
 output += ` ${name}="${escapeAttribute(value)}"`;
 }
 }
 return output;
}

/**
 * Reconstruct generated HTML from an explicit formatting-only allowlist.
 * Active containers and their contents are discarded; all attributes are
 * rebuilt from parsed values, and anchors are limited to navigation-safe
 * schemes with a fixed rel value.
 */
export function sanitizeStitchHtml(html: string): string {
 let output = "";
 let cursor = 0;
 let suppressedTag = "";
 let suppressedDepth = 0;

 while (cursor < html.length) {
 const tagStart = html.indexOf("<", cursor);
 if (tagStart === -1) {
 if (!suppressedTag) output += html.slice(cursor);
 break;
 }
 if (!suppressedTag) output += html.slice(cursor, tagStart);

 const tagEnd = findTagEnd(html, tagStart);
 if (tagEnd === -1) {
 if (!suppressedTag) output += "&lt;" + html.slice(tagStart + 1);
 break;
 }

 const inner = html.slice(tagStart + 1, tagEnd).trim();
 cursor = tagEnd + 1;
 if (!inner) continue;

 if (/^!doctype\s+html\s*$/i.test(inner)) {
 if (!suppressedTag) output += "<!doctype html>";
 continue;
 }
 if (inner.startsWith("!") || inner.startsWith("?")) continue;

 const closing = inner.startsWith("/");
 const tagSource = closing ? inner.slice(1).trimStart() : inner;
 const nameMatch = /^([a-z][a-z0-9-]*)/i.exec(tagSource);
 if (!nameMatch) continue;
 const tagName = nameMatch[1].toLowerCase();

 if (suppressedTag) {
 if (tagName === suppressedTag) {
 suppressedDepth += closing ? -1 : 1;
 if (suppressedDepth <= 0) {
 suppressedTag = "";
 suppressedDepth = 0;
 }
 }
 continue;
 }

 if (DROP_WITH_CONTENT_TAGS.has(tagName)) {
 if (!closing) {
 suppressedTag = tagName;
 suppressedDepth = 1;
 }
 continue;
 }
 if (!ALLOWED_STITCH_TAGS.has(tagName)) continue;

 if (closing) {
 if (!VOID_STITCH_TAGS.has(tagName)) output += `</${tagName}>`;
 continue;
 }

 const attributeSource = tagSource.slice(nameMatch[0].length);
 const attributes = parseAttributes(attributeSource);
 const sanitizedAttributes = sanitizeTagAttributes(tagName, attributes);
 // A meta element is useful only for an inert charset declaration. Empty
 // meta tags (including every http-equiv attempt) are discarded.
 if (tagName === "meta" && !sanitizedAttributes) continue;
 output += `<${tagName}${sanitizedAttributes}>`;
 }

 return output;
}

function injectHtml(html: string, tokens: TokenSpec[], css: string): string {
 let out = html;
 for (const t of tokens) {
 out = out.split(t.placeholder).join(t.injectionHtml);
 }
 // Inject the shared report CSS so chips/source-cards/provenance style
 // identically to the V0 artifact regardless of the generated chrome.
 const styleBlock = `<style data-injected="zspan-report-fragments">\n${css}\n</style>`;
 out = out.includes("</head>")
 ? out.replace("</head>", `${styleBlock}\n</head>`)
 : `${styleBlock}\n${out}`;
 return sanitizeStitchHtml(out);
}

// ── SDK plumbing (mirrors fractal: fresh client per pass-group, state in
// durable project/screen ids; download-URL fetch for HTML) ──────────
async function loadSdk() {
 const mod: any = await import("@google/stitch-sdk");
 return { StitchToolClient: mod.StitchToolClient, Stitch: mod.Stitch };
}

async function fetchScreenHtml(screen: any): Promise<string> {
 const url: string = await screen.getHtml();
 const res = await fetch(url);
 if (!res.ok) {
 throw new Error(`Stitch download URL returned ${res.status}`);
 }
 return await res.text();
}

const MAX_RETRIES = 2;
const RATE_LIMIT_BACKOFF_MS = [0, 5_000, 15_000, 45_000];
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function runPassWithRetry(
 fire: (prompt: string) => Promise<any>,
 basePrompt: string,
 tokens: TokenSpec[],
 step: string,
 state: LiveState
): Promise<{ screen: any; html: string }> {
 let lastErr: Error | null = null;
 let rateLimited = 0;
 for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
 const started = Date.now();
 const prompt =
 attempt === 0
 ? basePrompt
 : `${basePrompt}\n\nIMPORTANT (retry ${attempt}): your last response violated token rules (${lastErr?.message?.slice(0, 200)}). Every listed {{TOKEN}} must appear EXACTLY ONCE, character-for-character. Do not duplicate, paraphrase, or drop any token.`;
 try {
 const screen = await fire(prompt);
 const html = await fetchScreenHtml(screen);
 const validation = validateHtml(html, tokens);
 state.edits.push({
 index: state.edits.length,
 step,
 validation_ok: validation.ok,
 validation_issues: validation.issues,
 duration_ms: Date.now() - started,
 created_at: Date.now(),
 });
 if (validation.ok) return { screen, html };
 lastErr = new Error(validation.issues.slice(0, 4).join("; "));
 } catch (e: any) {
 lastErr = e;
 if (e?.code === "AUTH_FAILED" || e?.code === "PERMISSION_DENIED") break;
 if (e?.code === "RATE_LIMITED") {
 rateLimited += 1;
 const wait =
 RATE_LIMIT_BACKOFF_MS[
 Math.min(rateLimited, RATE_LIMIT_BACKOFF_MS.length - 1)
 ];
 state.progress = `Rate-limited by Stitch; waiting ${wait / 1000}s...`;
 if (attempt < MAX_RETRIES) await sleep(wait);
 }
 }
 }
 throw lastErr ?? new Error("Stitch pass failed after retries");
}

// ── The pass core (exported for headless smoke — same code path the
// driver runs; only the Flask fetch/persist hops differ) ────────────
export async function generateChromeFromFragments(
 frags: Fragments,
 state: LiveState,
 onProjectId?: (projectId: string) => Promise<void>
): Promise<{ injected: string; projectId: string }> {
 const auth = resolveStitchAuth();
 if (!auth) {
 throw new Error(
 "Stitch credential not configured (user_settings.json stitch_api_key or STITCH_API_KEY)"
 );
 }
 const tokens = buildTokens(frags);

 state.progress = "Creating Stitch project...";
 const { StitchToolClient, Stitch } = await loadSdk();
 const client = new StitchToolClient(auth);
 const sdk = new Stitch(client);
 try {
 const projectResult: any = await client.callTool("create_project", {
 title: `Z-SPAN report — ${frags.run_id}`,
 });
 const rawName: string | undefined =
 projectResult?.name ?? projectResult?.project?.name;
 const projectId: string | undefined =
 projectResult?.projectId ??
 projectResult?.project_id ??
 (rawName ? rawName.replace(/^projects\//, "") : undefined) ??
 projectResult?.id;
 if (!projectId) {
 throw new Error(
 `Stitch create_project returned no project id: ${JSON.stringify(projectResult).slice(0, 200)}`
 );
 }
 if (onProjectId) await onProjectId(projectId);
 const project = sdk.project(projectId);

 // Scaffold pass.
 state.progress = "Designing structural scaffold...";
 state.step = "scaffold";
 let { screen, html } = await runPassWithRetry(
 (p) => project.generate(p, "DESKTOP"),
 buildInitialPrompt(tokens, frags),
 tokens,
 "scaffold",
 state
 );

 // Refinement passes (quota-light: 3 targeted edits).
 for (const refine of REFINE_STEPS) {
 state.progress = `Refining: ${refine.step}...`;
 state.step = refine.step;
 try {
 const result = await runPassWithRetry(
 (p) => screen.edit(p, "DESKTOP", STITCH_MODEL),
 `${refine.prompt}\n\n${TOKEN_RULES}\n\nTokens that must each still appear exactly once: ${tokens.map((t) => t.placeholder).join(", ")}.`,
 tokens,
 refine.step,
 state
 );
 screen = result.screen;
 html = result.html;
 } catch (e: any) {
 // One flaky refinement shouldn't kill the run — keep the last
 // valid screen and continue (fractal's skip semantics).
 console.warn(`[stitch] refinement "${refine.step}" skipped: ${e.message}`);
 }
 }

 // Local injection.
 state.progress = "Injecting report content...";
 state.step = "inject";
 const finalValidation = validateHtml(html, tokens);
 if (!finalValidation.ok) {
 throw new Error(
 `pre-injection validation failed: ${finalValidation.issues.join("; ")}`
 );
 }
 return { injected: injectHtml(html, tokens, frags.css), projectId };
 } finally {
 try {
 await client.close();
 } catch {
 /* ignore */
 }
 }
}

export function newLiveStateForSmoke(): LiveState {
 return { status: "running", progress: "", edits: [], startedAt: Date.now() };
}

// ── Persist outcome via Flask (cookie-gated) ──────────────────────────
async function postResult(
 reportRunId: string,
 cookie: string,
 body: Record<string, unknown>
): Promise<void> {
 const res = await fetch(
 `${PARSER_API_URL}/api/report-runs/${encodeURIComponent(reportRunId)}/stitch-result`,
 {
 method: "POST",
 headers: { "Content-Type": "application/json", Cookie: cookie },
 body: JSON.stringify(body),
 }
 );
 if (!res.ok) {
 throw new Error(`stitch-result persist failed: ${res.status}`);
 }
}

// ── The driver ────────────────────────────────────────────────────────
export async function runStitchChrome(
 reportRunId: string,
 cookie: string
): Promise<void> {
 const state: LiveState = {
 status: "running",
 progress: "Loading report fragments...",
 edits: [],
 startedAt: Date.now(),
 };
 live.set(reportRunId, state);

 const failAndPersist = async (message: string) => {
 state.status = "error";
 state.error = message;
 state.progress = "Failed";
 try {
 await postResult(reportRunId, cookie, {
 stitch_status: "error",
 stitch_progress: "Failed",
 stitch_error: message.slice(0, 1500),
 stitch_edits: state.edits,
 });
 } catch (e: any) {
 console.error(`[stitch ${reportRunId}] persist-of-error failed:`, e.message);
 }
 };

 try {
 if (!resolveStitchAuth()) {
 throw new Error(
 "Stitch credential not configured (user_settings.json stitch_api_key or STITCH_API_KEY)"
 );
 }

 // 1. Fragments (auth rides this call — a 403 aborts everything).
 const fragRes = await fetch(
 `${PARSER_API_URL}/api/report-runs/${encodeURIComponent(reportRunId)}/fragments`,
 { headers: { Cookie: cookie } }
 );
 const fragJson: any = await fragRes.json();
 if (!fragRes.ok || !fragJson.success) {
 throw new Error(fragJson?.error || `fragments fetch failed (${fragRes.status})`);
 }
 const frags: Fragments = fragJson.fragments;

 await postResult(reportRunId, cookie, {
 stitch_status: "running",
 stitch_progress: "Generating chrome...",
 });

 const { injected, projectId } = await generateChromeFromFragments(
 frags,
 state,
 async (pid) => {
 await postResult(reportRunId, cookie, { stitch_project_id: pid });
 }
 );

 await postResult(reportRunId, cookie, {
 stitch_status: "complete",
 stitch_progress: "Stitch chrome complete.",
 stitch_artifact_html: injected,
 stitch_edits: state.edits,
 });
 state.status = "complete";
 state.progress = "Stitch chrome complete.";
 console.log(
 `[stitch ${reportRunId}] complete — project ${projectId}, ${state.edits.length} passes, ${injected.length} bytes`
 );
 } catch (e: any) {
 console.error(`[stitch ${reportRunId}] failed:`, e?.message || e);
 await failAndPersist(e?.message || String(e));
 } finally {
 // Keep the live entry for late polls, then drop it.
 setTimeout(() => live.delete(reportRunId), 10 * 60_000);
 }
}
