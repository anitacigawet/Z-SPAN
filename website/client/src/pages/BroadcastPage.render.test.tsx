import type { ReactNode } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../player/ZspanPlayer", async () => {
 const React = await import("react");
 return {
 default: React.forwardRef(() => <div>Video player</div>),
 };
});

vi.mock("../components/PromptInfoIcon", async () => {
 const React = await import("react");
 return {
 default: () => <span aria-label="Prompt information" />,
 DecisionEvidenceDisclosure: ({
 evidence,
 children,
 }: {
 evidence: Array<{ verbatim_spans?: unknown[] }>;
 children: (trigger: ReactNode) => ReactNode;
 }) => (
 <>
 {children(
 evidence.some(decision => (decision.verbatim_spans?.length ?? 0) > 0)
 ? <button aria-label="Transcript evidence">i</button>
 : null,
 )}
 </>
 ),
 };
});

vi.mock("../components/PublicDataDisclaimerGate", async () => {
 const React = await import("react");
 return {
 PublicDataDisclaimerGate: ({ children }: { children: ReactNode }) => <>{children}</>,
 useDisclaimerAcked: () => true,
 };
});

vi.mock("../components/WatermarkRibbon", () => ({
 WatermarkRibbon: () => null,
}));
vi.mock("../components/SyncedQuote", () => ({
 default: ({ wordTimings }: { wordTimings: Array<{ word?: string }> }) => (
 <span>{wordTimings.map(timing => timing.word).join(" ")}</span>
 ),
}));
vi.mock("../components/CitationPanel", () => ({ default: () => null }));
vi.mock("../components/DefinitionHint", () => ({ default: () => null }));
vi.mock("../components/SignInBenefitsToast", () => ({ SignInBenefitsToast: () => null }));
vi.mock("../components/ByokSetupModal", () => ({
 ByokSetupModal: () => null,
 LibrarianAccessGate: () => null,
}));
vi.mock("../components/ByokQueryPanel", () => ({ ByokQueryPanel: () => null }));
vi.mock("../hooks/useCurrentUser", () => ({
 useCurrentUser: () => ({ isOwner: false, loading: false, user: null }),
}));
vi.mock("../lib/karaokeRender", () => ({
 KaraokeText: ({ text }: { text: string }) => <>{text}</>,
 KaraokeLoadingDots: () => null,
}));
vi.mock("./ChannelsPage", () => ({ V1_PROCESSED_CITIES: new Set<string>() }));
vi.mock("lucide-react", async () => {
 const React = await import("react");
 const Icon = () => React.createElement("span");
 return {
 Building2: Icon,
 Play: Icon,
 Send: Icon,
 BrainCircuit: Icon,
 ArrowLeft: Icon,
 Briefcase: Icon,
 Film: Icon,
 Youtube: Icon,
 Loader2: Icon,
 AlertCircle: Icon,
 Headphones: Icon,
 ChevronDown: Icon,
 Info: Icon,
 Check: Icon,
 Sparkles: Icon,
 Menu: Icon,
 X: Icon,
 Lock: Icon,
 Mail: Icon,
 ShieldCheck: Icon,
 KeyRound: Icon,
 };
});

import BroadcastPage from "./BroadcastPage";

class MemoryStorage {
 private readonly values = new Map<string, string>();

 getItem(key: string) {
 return this.values.get(key) ?? null;
 }

 setItem(key: string, value: string) {
 this.values.set(key, String(value));
 }
}

class MiniNode {
 readonly childNodes: MiniNode[] = [];
 parentNode: MiniNode | null = null;
 ownerDocument: MiniDocument;
 nodeValue: string | null = null;

 constructor(
 readonly nodeType: number,
 ownerDocument: MiniDocument,
 ) {
 this.ownerDocument = ownerDocument;
 }

 appendChild<T extends MiniNode>(child: T): T {
 child.parentNode?.removeChild(child);
 child.parentNode = this;
 this.childNodes.push(child);
 return child;
 }

 insertBefore<T extends MiniNode>(child: T, before: MiniNode | null): T {
 if (before === null) return this.appendChild(child);
 const index = this.childNodes.indexOf(before);
 if (index < 0) throw new Error("Reference node is not a child");
 child.parentNode?.removeChild(child);
 child.parentNode = this;
 this.childNodes.splice(index, 0, child);
 return child;
 }

 removeChild<T extends MiniNode>(child: T): T {
 const index = this.childNodes.indexOf(child);
 if (index < 0) throw new Error("Node is not a child");
 this.childNodes.splice(index, 1);
 child.parentNode = null;
 return child;
 }

 get firstChild(): MiniNode | null {
 return this.childNodes[0] ?? null;
 }

 get lastChild(): MiniNode | null {
 return this.childNodes[this.childNodes.length - 1] ?? null;
 }

 get textContent(): string {
 if (this.nodeType === 3) return this.nodeValue ?? "";
 return this.childNodes.map(child => child.textContent).join("");
 }

 set textContent(value: string) {
 for (const child of this.childNodes) child.parentNode = null;
 this.childNodes.length = 0;
 if (value) this.appendChild(this.ownerDocument.createTextNode(value));
 }

 addEventListener() {}
 removeEventListener() {}
 dispatchEvent() { return true; }
 getRootNode() { return this.ownerDocument; }
}

class MiniElement extends MiniNode {
 readonly attributes = new Map<string, string>();
 readonly style: Record<string, string> = {};
 readonly namespaceURI: string;
 readonly nodeName: string;
 readonly tagName: string;
 currentTime = 0;
 readyState = 1;

 constructor(tagName: string, ownerDocument: MiniDocument, namespaceURI = "http://www.w3.org/1999/xhtml") {
 super(1, ownerDocument);
 this.nodeName = tagName.toUpperCase();
 this.tagName = this.nodeName;
 this.namespaceURI = namespaceURI;
 }

 setAttribute(name: string, value: unknown) {
 this.attributes.set(name, String(value));
 }

 setAttributeNS(_namespace: string | null, name: string, value: unknown) {
 this.setAttribute(name, value);
 }

 removeAttribute(name: string) {
 this.attributes.delete(name);
 }

 scrollIntoView() {}
 pause() {}
 play() { return Promise.resolve(); }
 focus() {
 this.ownerDocument.activeElement = this;
 }
}

class MiniDocument extends MiniNode {
 readonly nodeName = "#document";
 readonly documentElement: MiniElement;
 readonly body: MiniElement;
 activeElement: MiniElement | null = null;
 defaultView: Record<string, unknown> | null = null;

 constructor() {
 super(9, null as unknown as MiniDocument);
 this.ownerDocument = this;
 this.documentElement = new MiniElement("html", this);
 this.body = new MiniElement("body", this);
 this.documentElement.appendChild(this.body);
 this.appendChild(this.documentElement);
 }

 createElement(tagName: string) {
 return new MiniElement(tagName, this);
 }

 createElementNS(namespaceURI: string, tagName: string) {
 return new MiniElement(tagName, this, namespaceURI);
 }

 createTextNode(value: string) {
 const node = new MiniNode(3, this);
 node.nodeValue = value;
 return node;
 }
}

type Plane = "public" | "operator";

const ccta = JSON.stringify([{
 speaker_name: "City Clerk",
 speaker_role: "Clerk",
 quote_text: "Submit comments by Friday.",
 actionable_hook: "Submit comments",
}]);

function output(content: string | null, error: string | null = null) {
 return {
 content,
 content_url: null,
 prompt_filename: null,
 prompt_version: null,
 generated_at: null,
 error,
 };
}

function broadcastPayload(
 meetingId: number,
 synopsis: string | null = "Shared synopsis",
 synopsisError: string | null = null,
) {
 return {
 success: true,
 meeting_id: meetingId,
 public_id: `m-${meetingId}`,
 meeting_title: "Regular City Council Meeting",
 meeting_date: "2026-07-22",
 city: "Parity City",
 county: "Test County",
 notebook_id: "notebook",
 video_url: "https://www.youtube.com/watch?v=video",
 approved_at: "2026-07-23T00:00:00Z",
 completeness: { complete: true, required_ok: 4, required_total: 4 },
 outputs: {
 episode_tagline: output("Shared episode tagline"),
 synopsis: output(synopsis, synopsisError),
 key_decisions: output("1. Shared key decision prose"),
 community_calls_to_action: output(ccta),
 },
 };
}

function sidecarPayload(path: string, meetingId: number) {
 if (path.endsWith("/decisions") || path.includes("/preview/decisions/")) {
 return {
 prose_output: `1. Sidecar decision ${meetingId}`,
 decisions: [{
 index: 1,
 verbatim_spans: [{
 text: `Evidence ${meetingId}`,
 source: "item_quote_to_action_quote",
 label: "Verbatim transcript excerpt — complete",
 structure: "contiguous",
 }],
 }],
 };
 }
 if (path.endsWith("/quotes") || path.includes("/preview/quotes/")) {
 return {
 quotes: [{
 speaker_name: "Council Member",
 speaker_role: "Member",
 speaker_class: "council_member",
 quote_text: `Discussion quote ${meetingId}`,
 }],
 };
 }
 if (path.endsWith("/routing") || path.includes("/preview/routing/")) {
 return { routing: [{ quote_index: 0, bucket: "decision_bound", decision_index: 1 }] };
 }
 if (path.endsWith("/recusals") || path.includes("/preview/recusals/")) {
 return { recusal_count: 0, recusals: [] };
 }
 return null;
}

function installFetch(
 plane: Plane,
 missingSidecarsFor = new Set<number>(),
 spanlessDecisionsFor = new Set<number>(),
 canonicalPublicId?: string,
) {
 vi.stubGlobal("fetch", vi.fn(async (input: string | URL | Request) => {
 const path = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
 const notebookMatch = path.match(/\/api\/notebook\/(\d+)$/);
 if (notebookMatch) return Response.json(broadcastPayload(Number(notebookMatch[1])));
 const publicMatch = path.match(/\/public-api\/broadcasts\/m-(\d+)$/);
 if (publicMatch) {
 const payload = broadcastPayload(Number(publicMatch[1]));
 if (canonicalPublicId) payload.public_id = canonicalPublicId;
 return Response.json(payload);
 }
 const sidecarMeeting = Number(path.match(/(?:m-|\/)(\d+)(?:\/sidecars\/|$)/)?.[1] ?? 0);
 if (
 path.includes("/sidecars/")
 || path.includes("/api/preview/quotes/")
 || path.includes("/api/preview/decisions/")
 || path.includes("/api/preview/routing/")
 || path.includes("/api/preview/recusals/")
 ) {
 if (missingSidecarsFor.has(sidecarMeeting)) {
 return Response.json({ success: false }, { status: 404 });
 }
 if (
 spanlessDecisionsFor.has(sidecarMeeting)
 && (path.endsWith("/decisions") || path.includes("/preview/decisions/"))
 ) {
 return Response.json({
 prose_output: `1. Spanless sidecar decision ${sidecarMeeting}`,
 decisions: [{ index: 1, verbatim_spans: [] }],
 });
 }
 return Response.json(sidecarPayload(path, sidecarMeeting));
 }
 if (path === "/api/system/status") return Response.json({ mode: "flagship" });
 if (path.includes("/publish-status")) {
 return Response.json({ success: true, meeting: { is_published: true, published_at: "2026-07-23" } });
 }
 if (path === "/api/calendar/events") return Response.json({ events: [] });
 if (path.includes("/public-api/cities/")) return Response.json({ events: [] });
 if (path.includes("/api/cast/") || path.includes("/public-api/cast/")) {
 return Response.json({ members: [] });
 }
 throw new Error(`Unexpected ${plane} fetch: ${path}`);
 }));
}

let documentStub: MiniDocument;
let container: MiniElement;
let root: Root | null;

beforeEach(() => {
 documentStub = new MiniDocument();
 container = documentStub.createElement("div");
 documentStub.body.appendChild(container);
 const location = {
 hostname: "operator.zspan.org",
 search: "",
 href: "https://operator.zspan.org/",
 };
 const windowStub = {
 document: documentStub,
 location,
 localStorage: new MemoryStorage(),
 sessionStorage: new MemoryStorage(),
 innerWidth: 1440,
 addEventListener: vi.fn(),
 removeEventListener: vi.fn(),
 setTimeout,
 clearTimeout,
 setInterval,
 clearInterval,
 requestAnimationFrame: (callback: FrameRequestCallback) => setTimeout(callback, 0),
 cancelAnimationFrame: clearTimeout,
 getSelection: () => null,
 HTMLElement: MiniElement,
 HTMLIFrameElement: class {},
 SVGElement: MiniElement,
 };
 documentStub.defaultView = windowStub;
 vi.stubGlobal("window", windowStub);
 vi.stubGlobal("document", documentStub);
 vi.stubGlobal("Node", MiniNode);
 vi.stubGlobal("Element", MiniElement);
 vi.stubGlobal("HTMLElement", MiniElement);
 vi.stubGlobal("SVGElement", MiniElement);
 vi.stubGlobal("navigator", { userAgent: "vitest" });
 vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
 root = createRoot(container as unknown as Element);
});

afterEach(async () => {
 if (root) {
 await act(async () => root?.unmount());
 root = null;
 }
 vi.unstubAllGlobals();
 vi.restoreAllMocks();
});

async function settle() {
 for (let i = 0; i < 6; i += 1) {
 await act(async () => {
 await new Promise<void>(resolve => setTimeout(resolve, 0));
 });
 }
}

async function renderMeeting(
 plane: Plane,
 meetingId: number,
 canonicalPublicId?: string,
) {
 window.location.hostname = plane === "public" ? "zspan.org" : "operator.zspan.org";
 window.location.href = `https://${window.location.hostname}/`;
 installFetch(plane, new Set(), new Set(), canonicalPublicId);
 await act(async () => {
 root?.render(
 <BroadcastPage
 {...(plane === "public" ? { publicId: `m-${meetingId}` } : { meetingId })}
 onBack={vi.fn()}
 />,
 );
 });
 await settle();
 return container.textContent;
}

describe("BroadcastPage presentation contract", () => {
 it("renders the signed-out chip picker inline in the existing Librarian body", async () => {
 const publicText = await renderMeeting(
 "public",
 10,
 "m_AAAAAAAAAAAAAAAAAAAAAA",
 );
 // The signed-out visitor sees the standardized chip labels
 // (bucket=regular for meetingType "City Council") rendered as clickable
 // suggestions in the existing Librarian chat body. No invented
 // subtitle/empty-state chrome — the "Questions worth asking" heading is
 // the operator-approved wrapper the chips live inside.
 expect(publicText).toContain("Questions worth asking");
 expect(publicText).toContain(
 "What did the council vote on, and how did each member vote?",
 );
 // Subtitle stays the BYOK-style copy — no subtitle drift into the
 // signed-out surface.
 expect(publicText).toContain(
 "Z-SPAN provides cited transcript chunks, your LLM provider handles the synthesis.",
 );
 });

 it("renders every substantive public section on the operator plane too", async () => {
 const operatorText = await renderMeeting("operator", 1);
 expect(operatorText).toContain("Shared episode tagline");
 expect(operatorText).toContain("Shared synopsis");
 expect(operatorText).toContain("Sidecar decision 1");
 expect(operatorText).toContain("Submit comments");

 await act(async () => root?.unmount());
 root = createRoot(container as unknown as Element);
 const publicText = await renderMeeting("public", 1);
 for (const substantiveText of [
 "Shared episode tagline",
 "Shared synopsis",
 "Sidecar decision 1",
 "Submit comments",
 ]) {
 expect(publicText).toContain(substantiveText);
 expect(operatorText).toContain(substantiveText);
 }
 });

 it("renders a plain flagship synopsis on the operator plane", async () => {
 expect(await renderMeeting("operator", 2)).toContain("Shared synopsis");
 });

 it("does not render the Synopsis section for empty or whitespace content", async () => {
 installFetch("operator");
 const fetchMock = vi.mocked(fetch);
 fetchMock.mockImplementation(async (input: string | URL | Request) => {
 const path = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
 if (path === "/api/notebook/3") {
 return Response.json(
 broadcastPayload(3, " \n\t ", "Qdrant collection unavailable"),
 );
 }
 if (path.includes("/api/preview/")) return Response.json(sidecarPayload(path, 3));
 if (path === "/api/system/status") return Response.json({ mode: "flagship" });
 if (path.includes("/publish-status")) {
 return Response.json({ success: true, meeting: { is_published: true } });
 }
 if (path === "/api/calendar/events") return Response.json({ events: [] });
 if (path.includes("/api/cast/")) return Response.json({ members: [] });
 throw new Error(`Unexpected fetch: ${path}`);
 });
 await act(async () => {
 root?.render(<BroadcastPage meetingId={3} onBack={vi.fn()} />);
 });
 await settle();
 expect(container.textContent).not.toContain("Synopsis");
 });

 it("clears Era-C decisions and quotes when the next meeting has 404 sidecars", async () => {
 installFetch("operator", new Set([5]));
 await act(async () => {
 root?.render(<BroadcastPage meetingId={4} onBack={vi.fn()} />);
 });
 await settle();
 expect(container.textContent).toContain("Sidecar decision 4");
 expect(container.textContent).toContain("Discussion quote 4");

 await act(async () => {
 root?.render(<BroadcastPage meetingId={5} onBack={vi.fn()} />);
 });
 await settle();
 expect(container.textContent).not.toContain("Sidecar decision 4");
 expect(container.textContent).not.toContain("Discussion quote 4");
 expect(container.textContent).toContain("Shared key decision prose");
 });

 it("shows the legacy-evidence note only when decision spans are unavailable", async () => {
 installFetch("operator", new Set([8]), new Set([7]));
 await act(async () => {
 root?.render(<BroadcastPage meetingId={6} onBack={vi.fn()} />);
 });
 await settle();
 expect(container.textContent).not.toContain("Transcript evidence isn't available");

 await act(async () => {
 root?.render(<BroadcastPage meetingId={7} onBack={vi.fn()} />);
 });
 await settle();
 expect(container.textContent).toContain(
 "Transcript evidence isn't available for this meeting's generation",
 );

 await act(async () => {
 root?.render(<BroadcastPage meetingId={8} onBack={vi.fn()} />);
 });
 await settle();
 expect(container.textContent).toContain(
 "Transcript evidence isn't available for this meeting's generation",
 );
 });
});
