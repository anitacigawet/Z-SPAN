import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import PromptInfoIcon, {
 DecisionEvidenceDisclosure,
} from "./PromptInfoIcon";
import {
 paragraphizeVerbatimWords,
 transitionDecisionEvidenceState,
 type DecisionEvidence,
} from "./decisionEvidence";

const COMPONENT_DIR = dirname(fileURLToPath(import.meta.url));
const broadcastSource = readFileSync(
 resolve(COMPONENT_DIR, "../pages/BroadcastPage.tsx"),
 "utf8",
);
const promptInfoSource = readFileSync(
 resolve(COMPONENT_DIR, "PromptInfoIcon.tsx"),
 "utf8",
);
const disclosureSource = promptInfoSource.slice(
 0,
 promptInfoSource.indexOf("export default function PromptInfoIcon"),
);
const evidenceCss = readFileSync(
 resolve(COMPONENT_DIR, "../index.css"),
 "utf8",
).slice(0, 5500);

const completeEvidence = (
 text = "item introduced",
 timings?: Array<{ word: string; start: number; end: number }>,
): DecisionEvidence => ({
 index: 1,
 verbatim_spans: [{
 text,
 start_seconds: 1,
 end_seconds: 2,
 source: "item_quote_to_action_quote",
 label: "Verbatim transcript excerpt — complete",
 structure: "contiguous",
 omission_marker: "",
 word_timings: timings,
 }],
});

const renderDisclosure = (evidence: DecisionEvidence[]) => renderToStaticMarkup(
 <DecisionEvidenceDisclosure evidence={evidence}>
 {trigger => <p className="decision-test">Approved it. {trigger}</p>}
 </DecisionEvidenceDisclosure>,
);

describe("Key Decision transcript evidence disclosure", () => {
 it("has only closed/open states and click-driven dynamic ARIA", () => {
 expect(transitionDecisionEvidenceState("closed")).toBe("open");
 expect(transitionDecisionEvidenceState("open")).toBe("closed");
 const markup = renderDisclosure([completeEvidence()]);
 expect(markup).toContain('data-state="closed"');
 expect(markup).toContain('aria-expanded="false"');
 expect(markup).toContain('aria-label="Show verbatim transcript source for this decision"');
 expect(markup).toMatch(/aria-controls="decision-evidence-[^"]+"/);
 expect(disclosureSource).toContain('`${open ? "Hide" : "Show"} verbatim transcript source for this decision`');
 });

 it("keeps the disclosure in flow beneath its owning decision", () => {
 const markup = renderDisclosure([completeEvidence()]);
 expect(markup).toContain('class="decision-evidence-host w-full min-w-0"');
 expect(markup.indexOf('<p class="decision-test">')).toBeLessThan(
 markup.indexOf('class="decision-evidence-disclosure"'),
 );
 expect(markup).toContain('</p><div id="decision-evidence-');
 expect(evidenceCss).not.toContain(".decision-evidence-disclosure {\n position: absolute");
 });

 it("has no dialog, title tooltip, hover timer, outside click, or internal scroller", () => {
 const markup = renderDisclosure([completeEvidence()]);
 expect(markup).not.toContain('role="dialog"');
 expect(markup).not.toContain('title=');
 expect(markup).not.toContain('aria-haspopup');
 expect(disclosureSource).not.toContain("onPointerEnter");
 expect(disclosureSource).not.toContain("setTimeout");
 expect(disclosureSource).not.toContain('addEventListener("pointerdown"');
 expect(evidenceCss).not.toContain("overflow-y: auto");
 expect(evidenceCss).not.toContain("max-height:");
 });

 it("collapses on Escape only through the focused disclosure host", () => {
 expect(disclosureSource).toContain('event.key === "Escape"');
 expect(disclosureSource).toContain("event.currentTarget.contains(document.activeElement)");
 expect(disclosureSource).not.toContain('document.addEventListener("keydown"');
 });

 it("breaks at 1.500 seconds but not 1.499 seconds", () => {
 const noBreak = [
 { word: "one", start: 0, end: 0.5 },
 { word: "two", start: 1.999, end: 2.2 },
 ];
 const breaks = [
 { word: "one", start: 0, end: 0.5 },
 { word: "two", start: 2, end: 2.2 },
 ];
 expect(paragraphizeVerbatimWords(noBreak, 1.5, "one two")).toEqual(["one two"]);
 expect(paragraphizeVerbatimWords(breaks, 1.5, "one two")).toEqual(["one", "two"]);
 });

 it("preserves word order across multiple pauses", () => {
 const words = [
 { word: "keep", start: 0, end: 0.2 },
 { word: "these", start: 1.7, end: 1.9 },
 { word: "words", start: 2, end: 2.2 },
 { word: "ordered", start: 3.7, end: 3.9 },
 ];
 const paragraphs = paragraphizeVerbatimWords(words, 1.5, "keep these words ordered");
 expect(paragraphs).toEqual(["keep", "these words", "ordered"]);
 expect(paragraphs?.join(" ")).toBe("keep these words ordered");
 });

 it("fails closed for missing, invalid, non-monotonic, and mismatched timings", () => {
 expect(paragraphizeVerbatimWords([], 1.5, "one")).toBeNull();
 expect(paragraphizeVerbatimWords(
 [{ word: "one", start: Number.NaN, end: 1 }], 1.5, "one",
 )).toBeNull();
 expect(paragraphizeVerbatimWords([
 { word: "one", start: 2, end: 3 },
 { word: "two", start: 1, end: 4 },
 ], 1.5, "one two")).toBeNull();
 expect(paragraphizeVerbatimWords(
 [{ word: "changed", start: 0, end: 1 }], 1.5, "original",
 )).toBeNull();
 });

 it("renders source tokens exactly without capitalization or punctuation changes", () => {
 const text = "these Words stay odd";
 const markup = renderDisclosure([completeEvidence(text, [
 { word: "these", start: 0, end: 0.2 },
 { word: "Words", start: 0.3, end: 0.5 },
 { word: "stay", start: 2, end: 2.2 },
 { word: "odd", start: 2.3, end: 2.5 },
 ])]);
 expect(markup).toContain(
 '<p class="decision-evidence-paragraph">these Words</p>'
 + '<p class="decision-evidence-paragraph">stay odd</p>',
 );
 expect(markup).not.toContain("These Words");
 expect(markup).not.toContain("odd.");
 expect(markup).not.toContain("“");
 });

 it("uses one semantic blockquote and no collapse control for a short contiguous excerpt", () => {
 const text = Array.from({ length: 30 }, (_, index) => `word${index}`).join(" ");
 const markup = renderDisclosure([completeEvidence(text)]);
 expect(markup.match(/<blockquote/g)).toHaveLength(1);
 expect(markup).not.toContain("decision-evidence-divider");
 expect(markup).not.toContain("Collapse transcript source");
 });

 it("adds a bottom collapse control for long or two-span evidence", () => {
 const longText = Array.from({ length: 181 }, (_, index) => `word${index}`).join(" ");
 expect(renderDisclosure([completeEvidence(longText)])).toContain("Collapse transcript source");
 const twoSpan = completeEvidence("item introduced");
 twoSpan.verbatim_spans?.push({
 text: "motion carried",
 start_seconds: 1322,
 end_seconds: 1323,
 source: "item_quote_to_action_quote",
 label: "Verbatim transcript excerpts — middle omitted",
 structure: "elided",
 omission_marker: "[Transcript omitted: 999 words · 00:22:00.000 elapsed]",
 });
 const markup = renderDisclosure([twoSpan]);
 expect(markup.match(/<blockquote/g)).toHaveLength(2);
 expect(markup).toContain("Collapse transcript source");
 });

 it("derives the exact human divider from span boundaries, never the stored marker", () => {
 const evidence = completeEvidence("item introduced");
 evidence.verbatim_spans?.push({
 text: "motion carried",
 start_seconds: 1322,
 end_seconds: 1323,
 source: "item_quote_to_action_quote",
 label: "Verbatim transcript excerpts — middle omitted",
 structure: "elided",
 omission_marker: "[WRONG MARKER 999 words 123ms]",
 });
 const markup = renderDisclosure([evidence]);
 expect(markup).toContain("Verbatim transcript resumes about 22 minutes later");
 expect(markup).not.toContain("WRONG MARKER");
 expect(markup).not.toContain("999 words");
 expect(markup).not.toContain("123ms");
 expect(markup).not.toContain("font-mono");
 expect(markup).not.toContain("amber");
 });

 it("filters to materialized item-to-action evidence and omits legacy-empty triggers", () => {
 const evidence = completeEvidence();
 evidence.verbatim_spans?.push({
 text: "wrong source",
 source: "legacy",
 label: "Legacy",
 structure: "contiguous",
 });
 expect(renderDisclosure([evidence])).not.toContain("wrong source");
 expect(renderDisclosure([])).toBe('<p class="decision-test">Approved it. </p>');
 });

 it("keeps Synopsis prompt mode and its call unchanged", () => {
 const markup = renderToStaticMarkup(
 <PromptInfoIcon promptName="synopsis" label="Synopsis" />,
 );
 expect(markup).toContain("Show the exact prompt used to generate Synopsis");
 expect(markup).toContain('class="relative inline-flex"');
 expect(markup).not.toContain("decision-evidence");
 expect(broadcastSource).toContain(
 '<PromptInfoIcon promptName="synopsis" label="Synopsis" color="var(--highway-sign-blue)" />',
 );
 });

 it("keeps the Key Decisions prompt icon universal across operator meetings", () => {
 const headerStart = broadcastSource.indexOf("<span>Key Decisions</span>");
 const headerEnd = broadcastSource.indexOf("{data?.meeting_id && (", headerStart);
 const header = broadcastSource.slice(headerStart, headerEnd);

 expect(headerStart).toBeGreaterThan(-1);
 expect(headerEnd).toBeGreaterThan(headerStart);
 expect(header).toContain("{!publicPlane && (");
 expect(header).toContain(
 '<PromptInfoIcon promptName="key_decisions" label="Key Decisions" color="var(--highway-sign-blue)" />',
 );
 expect(header).not.toContain("previewDecisionsPayload");
 expect(header).not.toContain("verbatim_spans");
 });

 it("wraps each primary decision with the renamed disclosure export", () => {
 const primaryStart = broadcastSource.indexOf("{previewDecisionsList.map");
 const fallbackStart = broadcastSource.indexOf(") : keyDecisions.length > 0", primaryStart);
 const primary = broadcastSource.slice(primaryStart, fallbackStart);
 const fallback = broadcastSource.slice(fallbackStart);
 expect(primary).toContain("<DecisionEvidenceDisclosure");
 expect(primary).toContain("decision => decision.index === idx + 1");
 expect(fallback).not.toContain("<DecisionEvidenceDisclosure");
 expect(promptInfoSource).not.toContain("DecisionEvidencePopover");
 });
});
