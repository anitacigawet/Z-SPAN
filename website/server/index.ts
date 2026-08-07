import express from "express";
import { createServer } from "http";
import { createServer as createHttpsServer } from "https";
import { readFileSync, existsSync } from "fs";
import { join } from "path";
import { promises as fs } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import {
 type FlaskProxyRequest,
 flaskProxyHeaders,
 originGateAllows,
 pickAuthOriginHost,
 requireEdgeToken,
} from "./originTrust";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const PARSER_API_URL = process.env.PARSER_API_URL || "http://127.0.0.1:5001";

function forwardRateLimitHeaders(upstream: Response, res: any): void {
 const retryAfter = upstream.headers.get("retry-after");
 if (retryAfter) res.setHeader("Retry-After", retryAfter);
}

/**
 * Proxy a request to the Flask API. Supports GET, POST, DELETE.
 * Default timeout is 30s; pass `timeoutMs` for slower endpoints (e.g.,
 * the YouTube Data API matcher).
 */
async function proxyToFlask(
 path: string,
 options: {
 method?: string;
 body?: any;
 timeoutMs?: number;
 // when set, the X-Zspan-Agent-Role header on the incoming
 // request is forwarded to Flask. Pass `req` from the Express handler
 // for routes that agent-employees call. Routes the operator UI alone
 // touches don't need it; the header just won't be present.
 req?: FlaskProxyRequest;
 } = {}
): Promise<any> {
 const controller = new AbortController();
 const timeoutId = setTimeout(
 () => controller.abort(),
 options.timeoutMs ?? 30_000
 );

 const headers: Record<string, string> = {};
 if (options.body) headers["Content-Type"] = "application/json";
 if (options.req) {
 Object.assign(headers, flaskProxyHeaders(options.req));
 // Express lowercases header keys.
 const raw = options.req.headers["x-zspan-agent-role"];
 const role =
 typeof raw === "string"
 ? raw.trim()
 : Array.isArray(raw) && typeof raw[0] === "string"
 ? raw[0].trim()
 : "";
 if (role) headers["X-Zspan-Agent-Role"] = role;

 // Session-31 (2026-07-04) — auth-audit remediation. Forward the
 // incoming session cookie so Flask endpoints with `_require_owner()`
 // gates can identify the caller. Prior state was: proxyToFlask
 // silently dropped cookies, and any owner-check added to a Flask
 // handler behind a proxyToFlask route would reject the real owner
 // too. Now every route that passes `req` (which callers should
 // start doing wherever the Flask endpoint checks auth) gets
 // correct cookie forwarding automatically. Absence of a cookie
 // is fine — endpoints that don't check auth won't notice.
 const rawCookie = options.req.headers["cookie"];
 const cookie =
 typeof rawCookie === "string"
 ? rawCookie
 : Array.isArray(rawCookie) && typeof rawCookie[0] === "string"
 ? rawCookie[0]
 : "";
 if (cookie) headers["Cookie"] = cookie;
 }

 try {
 const response = await fetch(`${PARSER_API_URL}${path}`, {
 method: options.method || "GET",
 headers: Object.keys(headers).length ? headers : undefined,
 body: options.body ? JSON.stringify(options.body) : undefined,
 signal: controller.signal,
 });
 clearTimeout(timeoutId);
 return await response.json();
 } catch (error: any) {
 clearTimeout(timeoutId);
 throw error;
 }
}

async function startServer() {
 const app = express();
 // 2026-07-04: if dev-cert.pem + dev-key.pem exist in the council_navigator
 // root (generated via `mkcert -key-file dev-key.pem -cert-file dev-cert.pem
 // localhost 127.0.0.1 <LAN-IP>`), boot HTTPS instead of HTTP. Lets a
 // second device (Surface Pro, phone) on the same LAN hit
 // https://<mac-lan-ip>:3000 with a secure-context origin — needed for
 // camera-access features like hand-tracking. Falls back to HTTP if the
 // files aren't present.
 const certDir = process.cwd();
 const keyPath = join(certDir, "dev-key.pem");
 const certPath = join(certDir, "dev-cert.pem");
 const httpsEnabled = existsSync(keyPath) && existsSync(certPath);
 const server = httpsEnabled
 ? createHttpsServer(
 { key: readFileSync(keyPath), cert: readFileSync(certPath) },
 app,
 )
 : createServer(app);
 if (httpsEnabled) {
 console.log("[dev-https] serving HTTPS via dev-cert.pem / dev-key.pem");
 }

 // Enable JSON parsing FIRST. Limit raised to 10mb so the V1.5 flagship
 // sync endpoint (transcript_words can push payloads past the 100kb default
 // — m101091's first V1.5 push was 1.3 MB) doesn't 413 at the global
 // middleware before reaching the per-route `express.json({ limit: "10mb" })`
 // that was already set on /api/sync/meeting/:id. The global fires first
 // when Content-Type is application/json, so it has to be lenient enough
 // to let the per-route limits do their job. Surfaced 2026-05-28 by the
 // first m101091 V1.5 push attempt against the cloud receiver.
 app.use(express.json({ limit: "10mb" }));

 // Origin-Token-1 (2026-07-01, Fable-5 audit A1) — edge-token gate.
 // The Cloudflare Access perimeter only guards zspan.org; the raw
 // Railway origin (findable via CT logs) answers directly and bypasses
 // Access. This middleware requires an X-Zspan-Edge-Token header that
 // matches process.env.ZSPAN_EDGE_TOKEN on every incoming request.
 //
 // /media (public clip files, unauthenticated by posture) + the pure
 // Express liveness endpoint bypass — neither has a private data surface.
 //
 // Railway probes /healthz without an edge token. Keep this
 // list in sync with railway.toml; data-bearing API routes must not bypass
 // the origin gate.
 // RR-8 / SEC-PERIMETER-7: the edge token is mandatory unless a developer
 // explicitly opts into the startup-only local escape hatch. Production
 // ignores that escape hatch and still fails closed.
 const edgeToken = requireEdgeToken(process.env.ZSPAN_EDGE_TOKEN);
 app.use((req, res, next) => {
 const presented = req.header("x-zspan-edge-token");
 if (originGateAllows(req.path, presented, edgeToken)) return next();
 res.status(403).json({ error: "origin gate: edge token missing or invalid" });
 });

 // Pure process liveness: no Flask proxy and no SQLite read.
 app.get("/healthz", (_req, res) => {
 res.json({ status: "ok" });
 });

 // ============================================
 // Z-SPAN: Serve NotebookLM Studio media files
 // The bridge worker downloads audio/video/infographic artifacts to this
 // directory; the frontend embeds them via /media/<meeting_id>/<file>.
 // Override location via ZSPAN_MEDIA_ROOT (must match the bridge config).
 // ============================================
 const mediaRoot = process.env.ZSPAN_MEDIA_ROOT
 || path.resolve(__dirname, "..", "media");
 app.use("/media", express.static(mediaRoot, {
 fallthrough: false,
 maxAge: "1h",
 setHeaders: (res) => {
 // Permissive CORS on /media — clip files are public meeting
 // recordings (no auth, no PII). Enables cross-origin tooling
 // workflows: e.g., Chrome MCP automation fetching clips into
 // gemini.google.com for the verification step.
 // Matches the public-content posture (the cloud flagship's
 // /media is similarly unauthenticated, fronted by Cloudflare).
 res.setHeader("Access-Control-Allow-Origin", "*");
 },
 }));
 // 404 handler for /media — without this, fallthrough:false produces a generic
 // Express 500 on missing files; we want a clean 404 JSON so the frontend
 // <audio>/<video>/<img> tags fail loudly instead of silently mounting the
 // SPA index.html as binary content (the original "audio summary won't
 // respond" failure mode on m101091 2026-06-18: a missing media file fell
 // through to the SPA which served HTML with HTTP 200; the player got 2463
 // bytes of HTML and silently broke). Surfaced by [speed audit 2026-06-19].
 app.use("/media", (err: any, _req: any, res: any, next: any) => {
 if (!err) return next();
 if (res.headersSent) return next(err);
 if (err.status === 404 || err.statusCode === 404) {
 // Do NOT echo err.path — it discloses the absolute server filesystem
 // path of the missing media file to any (public) caller. The generic
 // 404 still lets the frontend <audio>/<video>/<img> tags fail loudly.
 return res.status(404).json({ error: "media file not found" });
 }
 // Any other static-file error (EACCES, etc.): generic 500 JSON. Do NOT
 // forward to Express's default error handler — with NODE_ENV unset or
 // "development" it echoes the error stack, which includes the absolute
 // media path (the same disclosure the 404 branch above closes).
 console.error("[Static] /media error:", err?.message);
 return res.status(500).json({ error: "media error" });
 });
 console.log(`[Static] Serving /media from ${mediaRoot}`);

 // ============================================
 // V1-Catalog-1 (2026-06-12) — DB-driven catalog + year-pager passthroughs
 // ============================================
 // public /v1 catalog — anonymous, status-preserving parity with Flask.
 // This path deliberately does not forward cookies.
 app.get("/v1/catalog/jurisdictions", (req, res) =>
 proxyJsonAnonymous(req, res, "/v1/catalog/jurisdictions"),
 );

 app.get("/v1/catalog/meetings", (req, res) => {
 const qs = new URLSearchParams();
 for (const name of ["state", "county", "city", "year", "cursor"]) {
 const value = req.query[name];
 if (typeof value === "string") qs.set(name, value);
 }
 const suffix = qs.toString();
 return proxyJsonAnonymous(
 req,
 res,
 `/v1/catalog/meetings${suffix ? `?${suffix}` : ""}`,
 );
 });

 app.get("/v1/catalog/meetings/:publicId", (req, res) =>
 proxyJsonAnonymous(
 req,
 res,
 `/v1/catalog/meetings/${encodeURIComponent(req.params.publicId)}`,
 ),
 );

 // the public Pages surface terminates at this Express
 // server before reaching Flask. These handlers extract only the query keys
 // allowed for their route by functions/publicSurfaceContract.ts.
 function withPublicQuery(
 flaskPath: string,
 values: Record<string, unknown>,
 ): string {
 const query = new URLSearchParams();
 for (const [name, value] of Object.entries(values)) {
 if (typeof value === "string") query.set(name, value);
 }
 const suffix = query.toString();
 return `${flaskPath}${suffix ? `?${suffix}` : ""}`;
 }

 app.get("/public-api/channels/tree", (req, res) =>
 proxyJsonAnonymous(req, res, "/public-api/channels/tree"),
 );
 app.get("/public-api/cities/:city/years", (req, res) =>
 proxyJsonAnonymous(
 req,
 res,
 `/public-api/cities/${encodeURIComponent(req.params.city)}/years`,
 ),
 );
 app.get("/public-api/cities/:city/meetings", (req, res) =>
 proxyJsonAnonymous(
 req,
 res,
 withPublicQuery(
 `/public-api/cities/${encodeURIComponent(req.params.city)}/meetings`,
 { year: req.query.year },
 ),
 ),
 );
 app.get("/public-api/calendar/county/:county/meetings", (req, res) =>
 proxyJsonAnonymous(
 req,
 res,
 withPublicQuery(
 `/public-api/calendar/county/${encodeURIComponent(req.params.county)}/meetings`,
 { state: req.query.state },
 ),
 ),
 );
 app.get("/public-api/calendar/search", (req, res) =>
 proxyJsonAnonymous(
 req,
 res,
 withPublicQuery("/public-api/calendar/search", {
 q: req.query.q,
 county: req.query.county,
 state: req.query.state,
 date_from: req.query.date_from,
 date_to: req.query.date_to,
 limit: req.query.limit,
 offset: req.query.offset,
 }),
 ),
 );
 app.get("/public-api/calendar/stats", (req, res) =>
 proxyJsonAnonymous(req, res, "/public-api/calendar/stats"),
 );
 app.get("/public-api/health", (req, res) =>
 proxyJsonAnonymous(req, res, "/public-api/health"),
 );
 app.get("/public-api/broadcasts/:public_id", (req, res) =>
 proxyJsonAnonymous(
 req,
 res,
 `/public-api/broadcasts/${encodeURIComponent(req.params.public_id)}`,
 ),
 );
 app.get("/public-api/broadcasts/:public_id/sim-queries", (req, res) => {
 if (Object.keys(req.query).length > 0) {
 return res.status(404).json({ success: false, error: "not found" });
 }
 return proxyJsonAnonymous(
 req,
 res,
 `/public-api/broadcasts/${encodeURIComponent(req.params.public_id)}/sim-queries`,
 );
 });
 app.get(
 "/public-api/broadcasts/:public_id/sidecars/:type",
 (req, res) =>
 proxyJsonAnonymous(
 req,
 res,
 `/public-api/broadcasts/${encodeURIComponent(req.params.public_id)}/sidecars/${encodeURIComponent(req.params.type)}`,
 ),
 );
 app.get("/public-api/broadcasts/:public_id/citation", (req, res) =>
 proxyJsonAnonymous(
 req,
 res,
 `/public-api/broadcasts/${encodeURIComponent(req.params.public_id)}/citation`,
 ),
 );
 app.get("/public-api/cast/:city", (req, res) =>
 proxyJsonAnonymous(
 req,
 res,
 `/public-api/cast/${encodeURIComponent(req.params.city)}`,
 ),
 );
 app.get("/public-api/cast/:city/:seat_id", (req, res) =>
 proxyJsonAnonymous(
 req,
 res,
 `/public-api/cast/${encodeURIComponent(req.params.city)}/${encodeURIComponent(req.params.seat_id)}`,
 ),
 );
 app.get("/public-api/ledger/:city", (req, res) =>
 proxyJsonAnonymous(
 req,
 res,
 withPublicQuery(
 `/public-api/ledger/${encodeURIComponent(req.params.city)}`,
 {
 status: req.query.status,
 aged: req.query.aged,
 limit: req.query.limit,
 },
 ),
 ),
 );
 app.get("/public-api/guide", (req, res) =>
 proxyJsonAnonymous(req, res, "/public-api/guide"),
 );
 app.get("/public-api/coverage", (req, res) =>
 proxyJsonAnonymous(req, res, "/public-api/coverage"),
 );
 app.get("/public-api/corrections", (req, res) =>
 proxyJsonAnonymous(req, res, "/public-api/corrections"),
 );
 app.get("/public-api/travelers", (req, res) =>
 proxyJsonAnonymous(req, res, "/public-api/travelers"),
 );
 app.get("/public-api/youtube/embed-check", (req, res) =>
 proxyJsonAnonymous(
 req,
 res,
 withPublicQuery("/public-api/youtube/embed-check", {
 video_id: req.query.video_id,
 }),
 ),
 );

 app.get("/api/channels/tree", async (_req, res) => {
 try {
 const data = await proxyToFlask("/api/channels/tree");
 res.json(data);
 } catch (error: any) {
 console.error("Channels tree error:", error.message);
 res.status(500).json({ ok: false, states: [], error: error.message });
 }
 });

 // — universal city coordinate lookup via Census
 // Places Gazetteer. Returns {ok, lat, lng, source} or 404. Used by
 // the frontend's serverCoords lazy-fetch path for cities not in
 // parser_index.json (e.g., the demo Chicago fixture).
 app.get("/api/gazetteer/lookup", async (req, res) => {
 try {
 const qs = new URLSearchParams();
 if (req.query.city) qs.set("city", String(req.query.city));
 if (req.query.state) qs.set("state", String(req.query.state));
 const data = await proxyToFlask(
 `/api/gazetteer/lookup?${qs.toString()}`,
 );
 res.json(data);
 } catch (error: any) {
 // proxyToFlask throws on non-2xx including 404. Surface 404 as
 // a clean ok:false rather than 500 so the client can distinguish
 // "city unknown" from "service down."
 if (/\b404\b/.test(error.message)) {
 res.status(404).json({ ok: false, error: "not_found" });
 return;
 }
 console.error("Gazetteer lookup error:", error.message);
 res.status(500).json({ ok: false, error: error.message });
 }
 });

 app.get("/api/cities/:city/years", async (req, res) => {
 try {
 const qs = new URLSearchParams();
 if (req.query.include_drafts)
 qs.set("include_drafts", String(req.query.include_drafts));
 const url = `/api/cities/${encodeURIComponent(req.params.city)}/years${
 qs.toString() ? `?${qs.toString()}` : ""
 }`;
 // { req } forwards the session cookie (§ 5b) — include_drafts is
 // owner-gated Flask-side as of the F-6.1 sweep.
 const data = await proxyToFlask(url, { req });
 res.json(data);
 } catch (error: any) {
 console.error("City years error:", error.message);
 res.status(500).json({ ok: false, years: [], error: error.message });
 }
 });

 app.get("/api/cities/:city/meetings", async (req, res) => {
 try {
 const qs = new URLSearchParams();
 if (req.query.year) qs.set("year", String(req.query.year));
 if (req.query.include_drafts)
 qs.set("include_drafts", String(req.query.include_drafts));
 // Public catalog mode (option A, 2026-07-10) — raw-fact rows for
 // ALL cached meetings; allowlisted Flask-side, safe to forward.
 if (req.query.catalog) qs.set("catalog", String(req.query.catalog));
 const url = `/api/cities/${encodeURIComponent(req.params.city)}/meetings${
 qs.toString() ? `?${qs.toString()}` : ""
 }`;
 // { req } forwards the session cookie (§ 5b) — include_drafts is
 // owner-gated Flask-side as of the F-6.1 sweep.
 const data = await proxyToFlask(url, { req });
 res.json(data);
 } catch (error: any) {
 console.error("City meetings error:", error.message);
 res
 .status(500)
 .json({ success: false, events: [], error: error.message });
 }
 });

 // ============================================
 // NEW: Database-backed search endpoint
 // ============================================
 app.get("/api/calendar/search", async (req, res) => {
 try {
 const params = new URLSearchParams();
 if (req.query.q) params.set("q", String(req.query.q));
 if (req.query.county) params.set("county", String(req.query.county));
 if (req.query.state) params.set("state", String(req.query.state));
 if (req.query.date_from)
 params.set("date_from", String(req.query.date_from));
 if (req.query.date_to) params.set("date_to", String(req.query.date_to));
 if (req.query.limit) params.set("limit", String(req.query.limit));
 if (req.query.offset) params.set("offset", String(req.query.offset));

 const data = await proxyToFlask(`/api/search?${params.toString()}`);
 res.json(data);
 } catch (error: any) {
 console.error("Search error:", error.message);
 res
 .status(500)
 .json({
 success: false,
 error: "Search failed",
 results: [],
 total: 0,
 });
 }
 });

 // ============================================
 // NEW: Database stats endpoint
 // ============================================
 app.get("/api/calendar/stats", async (req, res) => {
 try {
 const data = await proxyToFlask("/api/stats");
 res.json(data);
 } catch (error: any) {
 console.error("Stats error:", error.message);
 res.status(500).json({ success: false, error: "Stats failed" });
 }
 });

 // V1-Odometer-1 — public travelers counter for the persistent footer.
 // Proxy parity per [[express-flask-route-parity-discipline]]: every
 // Flask /api/* the SPA calls needs an Express handler so cloud + local
 // dev behave identically.
 app.get("/api/travelers", async (_req, res) => {
 try {
 const data = await proxyToFlask("/api/travelers");
 res.json(data);
 } catch (error: any) {
 console.error("Travelers count error:", error.message);
 res.status(500).json({ success: false, error: "Travelers count failed" });
 }
 });

 // ============================================
 // Z-SPAN: NotebookLM bridge endpoints
 // ============================================
 app.post("/api/notebook/register", express.json(), async (req, res) => {
 try {
 const data = await proxyToFlask("/api/notebook/register", {
 method: "POST",
 body: req.body,
 });
 res.json(data);
 } catch (error: any) {
 console.error("Notebook register error:", error.message);
 res.status(500).json({ success: false, error: "Notebook register failed" });
 }
 });

 app.get("/api/notebook/:meetingId", (req, res) =>
 // proxyJsonAuth forwards the session cookie (§ 5b) AND preserves Flask's
 // status verbatim. The Flask side owner-gates unpublished meetings (F-6.1)
 // with a 401, so a non-owner must RECEIVE that 401 — the prior
 // proxyToFlask + res.json(data) flattened it to a 200 with an error body
 // (2026-07-15 visitor-QA: a res.ok check would misread the block as
 // success — the succeeded-empty-vs-failed-silent class).
 proxyJsonAuth(req, res, `/api/notebook/${req.params.meetingId}`),
 );

 // Keep the literal route before :meetingId so Express never treats
 // "summary" as an id. Preserve the caller's query string for Flask's
 // bounded meeting_ids parser; proxyJsonAuth forwards the owner cookie.
 app.get("/api/episode-audit/summary", (req, res) => {
 const query = req.url.includes("?")
 ? req.url.slice(req.url.indexOf("?"))
 : "";
 return proxyJsonAuth(req, res, `/api/episode-audit/summary${query}`);
 });

 app.get("/api/episode-audit/:meetingId", (req, res) =>
 proxyJsonAuth(
 req,
 res,
 `/api/episode-audit/${req.params.meetingId}`,
 ),
 );

 app.post(
 "/api/episode-audit/:meetingId/apply-fix",
 express.json({ limit: "1mb" }),
 (req, res) =>
 proxyJsonAuth(
 req,
 res,
 `/api/episode-audit/${req.params.meetingId}/apply-fix`,
 ),
 );

 app.post(
 "/api/episode-audit/:meetingId/disposition",
 express.json({ limit: "1mb" }),
 (req, res) =>
 proxyJsonAuth(
 req,
 res,
 `/api/episode-audit/${req.params.meetingId}/disposition`,
 ),
 );

 // Per-generation operator controls. Cookie forwarding is load-bearing:
 // Flask owner-gates both mutations and preserves the status code so an
 // anonymous click remains an honest 401 rather than a flattened response.
 app.post(
 "/api/notebook/:meetingId/outputs/:outputType/void",
 express.json({ limit: "1mb" }),
 (req, res) =>
 proxyJsonAuth(
 req,
 res,
 `/api/notebook/${req.params.meetingId}/outputs/${encodeURIComponent(req.params.outputType)}/void`,
 ),
 );

 app.post(
 "/api/notebook/:meetingId/outputs/:outputType/restore",
 express.json({ limit: "1mb" }),
 (req, res) =>
 proxyJsonAuth(
 req,
 res,
 `/api/notebook/${req.params.meetingId}/outputs/${encodeURIComponent(req.params.outputType)}/restore`,
 ),
 );

 // Quotes Unification Refactor Chunk 6 — broadcast hero quotes for a
 // meeting in the unified shape. Reads from the `quotes` table; falls
 // back to parsing the legacy council_quotes JSON blob for meetings
 // that haven't been re-extracted under the unified prompt yet.
 // Optional query param: include_all=true → return non-hero rows too.
 app.get("/api/quotes/meeting/:meetingId", async (req, res) => {
 try {
 const qs = req.query.include_all ? "?include_all=true" : "";
 // { req } forwards the session cookie (§ 5b) — include_all is
 // owner-gated Flask-side as of the F-6.1 sweep.
 const data = await proxyToFlask(
 `/api/quotes/meeting/${req.params.meetingId}${qs}`,
 { req },
 );
 res.json(data);
 } catch (error: any) {
 console.error("Unified quotes fetch error:", error.message);
 res.status(500).json({ success: false, error: "Quotes fetch failed" });
 }
 });

 // Preview sidecar endpoints — the decision-Discussion karaoke +
 // decision-bound quotes / routing / recusals JSON that BroadcastPage
 // renders under each generation. Draft-visibility gated at the Flask
 // layer (RR-8 Tier C, 2026-07-12): these moved into Flask's
 // /api/preview/<type>/<id> route so the publish/owner gate sits next to
 // is_meeting_publicly_visible (its single source of truth), matching the
 // sibling /api/notebook + /api/quotes content routes. Express is now a
 // thin proxy — proxyJsonAuth forwards the session cookie (so Flask can
 // identify the owner) and preserves Flask's status (so a gated draft's
 // honest-404 reaches the client as 404, not a masked 200). Prior state:
 // Express read the sidecars straight off disk with no publish check, so
 // an anonymous caller could ID-guess any draft's decision content.
 // (Flask reads the same _preview_root() the sync receiver writes; see
 // for the structural-spec-plus-selection-discipline pattern.)
 const proxyPreview = (req: any, res: any, outputType: string) => {
 const meetingId = req.params.meetingId;
 if (!/^\d+$/.test(meetingId)) {
 return res.status(400).json({ success: false, error: "Invalid meeting_id" });
 }
 return proxyJsonAuth(req, res, `/api/preview/${outputType}/${meetingId}`);
 };
 app.get("/api/preview/quotes/:meetingId", (req, res) => proxyPreview(req, res, "quotes"));
 app.get("/api/preview/decisions/:meetingId", (req, res) => proxyPreview(req, res, "decisions"));
 app.get("/api/preview/routing/:meetingId", (req, res) => proxyPreview(req, res, "routing"));
 app.get("/api/preview/recusals/:meetingId", (req, res) => proxyPreview(req, res, "recusals"));

 // DEV-ONLY: Chat passthrough — typing here is functionally equivalent to
 // typing in NotebookLM's web UI for that notebook. Long timeout (90s)
 // because NotebookLM responses can be slow.
 app.post("/api/notebook/:meetingId/chat", express.json(), async (req, res) => {
 try {
 // Local extended-timeout fetch — proxyToFlask uses 30s which is too tight.
 const controller = new AbortController();
 const timeoutId = setTimeout(() => controller.abort(), 90_000);
 try {
 const response = await fetch(
 `${PARSER_API_URL}/api/notebook/${req.params.meetingId}/chat`,
 {
 method: "POST",
 headers: { "Content-Type": "application/json" },
 body: JSON.stringify(req.body || {}),
 signal: controller.signal,
 }
 );
 clearTimeout(timeoutId);
 const data = await response.json();
 res.status(response.status).json(data);
 } finally {
 clearTimeout(timeoutId);
 }
 } catch (error: any) {
 console.error("Notebook chat error:", error.message);
 res.status(504).json({ success: false, error: "Chat request failed or timed out" });
 }
 });

 // V1-RAG-3 per-member retrieval (no synthesis) — the TruthBook V3-preview
 // surface. Pure Qdrant retrieval filtered by member
 // alias mention, NO claude -p call, so timeout can be tighter than the
 // synthesis workflows. Still 60s budget to absorb Surface Pro warm-up + the
 // per-meeting query fan-out cost (one HTTP round-trip per indexed meeting).
 app.post(
 "/api/member-rag/:cityName/:seatId",
 express.json(),
 async (req, res) => {
 try {
 const controller = new AbortController();
 const timeoutId = setTimeout(() => controller.abort(), 60_000);
 try {
 const memberHeaders = flaskProxyHeaders(req, {
 "Content-Type": "application/json",
 });
 const memberCookie = req.headers.cookie;
 if (typeof memberCookie === "string" && memberCookie.length) {
 memberHeaders["Cookie"] = memberCookie;
 }
 const memberAuthorization = req.headers.authorization;
 if (
 typeof memberAuthorization === "string" &&
 memberAuthorization.length
 ) {
 memberHeaders["Authorization"] = memberAuthorization;
 }
 const response = await fetch(
 `${PARSER_API_URL}/api/member-rag/${encodeURIComponent(
 req.params.cityName
 )}/${encodeURIComponent(req.params.seatId)}`,
 {
 method: "POST",
 headers: memberHeaders,
 body: JSON.stringify(req.body || {}),
 signal: controller.signal,
 }
 );
 clearTimeout(timeoutId);
 const data = await response.json();
 res.status(response.status).json(data);
 } finally {
 clearTimeout(timeoutId);
 }
 } catch (error: any) {
 console.error("Member RAG retrieval error:", error.message);
 res
 .status(504)
 .json({ success: false, error: "Member retrieval failed or timed out" });
 }
 }
 );

 // ============================================
 // V1.5 BYOK — Bring Your Own Key proxy handlers
 //
 // + architecture spec: the browser calls these paths and
 // Express forwards verbatim to Flask. proxyToFlask() can't be used because
 // it discards the upstream status code, and BYOK error paths (400 from
 // validate-key, upstream provider errors from /relay) need that signal
 // for the client to render correctly.
 //
 // Key-custody discipline: req.body bytes pass through Express memory only
 // in transit; nothing here logs or persists the body. Matches the bytes-blind
 // discipline documented at parsers/api_server.py:7724+ for the Flask side.
 //
 // Discovered missing 2026-06-24 during the operator-side BYOK smoke test —
 // Flask shipped these endpoints in V1.5-BYOK-Shell-1 + Verify-1 + Query-1 +
 // Relay-1, but the Express proxy layer was never wired through, so every
 // browser call 404'd silently. ~ExpressProxy chunk closes the gap.
 // ============================================

 app.post("/api/byok/validate-key", express.json(), async (req, res) => {
 try {
 const controller = new AbortController();
 const timeoutId = setTimeout(() => controller.abort(), 15_000);
 try {
 const response = await fetch(`${PARSER_API_URL}/api/byok/validate-key`, {
 method: "POST",
 headers: flaskProxyHeaders(req, { "Content-Type": "application/json" }),
 body: JSON.stringify(req.body || {}),
 signal: controller.signal,
 });
 clearTimeout(timeoutId);
 forwardRateLimitHeaders(response, res);
 const data = await response.json();
 res.status(response.status).json(data);
 } finally {
 clearTimeout(timeoutId);
 }
 } catch (error: any) {
 console.error("BYOK validate-key error:", error.message);
 res
 .status(504)
 .json({ valid: false, error: "Validation request failed or timed out" });
 }
 });

 // 120s timeout — fires the user's LLM (OpenAI/Anthropic) through the Flask
 // relay. Slowest realistic synthesis under load is the upper bound.
 app.post("/api/byok/relay", express.json({ limit: "1mb" }), async (req, res) => {
 try {
 const controller = new AbortController();
 const timeoutId = setTimeout(() => controller.abort(), 120_000);
 try {
 // (2026-07-01): forward the session cookie for the Flask
 // owner-or-public-flag gate (open-ended BYOK is V2-locked for
 // the public until thequery-safety research lands).
 const relayHeaders = flaskProxyHeaders(req, {
 "Content-Type": "application/json",
 });
 const relayCookie = req.headers.cookie;
 if (typeof relayCookie === "string" && relayCookie.length) {
 relayHeaders["Cookie"] = relayCookie;
 }
 const response = await fetch(`${PARSER_API_URL}/api/byok/relay`, {
 method: "POST",
 headers: relayHeaders,
 body: JSON.stringify(req.body || {}),
 signal: controller.signal,
 });
 clearTimeout(timeoutId);
 forwardRateLimitHeaders(response, res);
 const data = await response.json();
 res.status(response.status).json(data);
 } finally {
 clearTimeout(timeoutId);
 }
 } catch (error: any) {
 console.error("BYOK relay error:", error.message);
 res
 .status(504)
 .json({ error: { message: "Relay request failed or timed out" } });
 }
 });

 // V1.5-BYOK-Stream-1 (2026-07-04) — SSE pass-through for the streaming
 // relay. Same auth shape as /api/byok/relay (cookie forwarded to Flask
 // for the owner-or-public-flag gate) but pipes the Flask SSE
 // response body straight to the browser instead of buffering + JSON-
 // parsing. Only OpenAI + Anthropic route through here; Gemini is
 // browser-direct.
 //
 // 240s timeout: streaming answers can legitimately take longer than the
 // one-shot relay's 120s (long answers + slow provider queues); the
 // AbortController stays wired so the browser Cancel button still kills
 // the upstream Flask stream.
 app.post("/api/byok/relay-stream", express.json({ limit: "1mb" }), async (req, res) => {
 const controller = new AbortController();
 const timeoutId = setTimeout(() => controller.abort(), 240_000);
 // Wire the response-side close (browser cancels / navigates away
 // mid-stream) to the upstream abort so the Flask stream + provider
 // request don't linger. Use res.on("close") not req.on("close") —
 // req.close fires when the REQUEST completes (which includes the
 // moment express.json finishes parsing the body), which pre-aborts
 // the upstream fetch under Node 22. res.close only fires when the
 // response's underlying connection actually closes, which is the
 // semantic we want.
 const abortIfStillActive = () => {
 if (!res.writableEnded) controller.abort();
 };
 res.on("close", abortIfStillActive);

 try {
 const relayHeaders = flaskProxyHeaders(req, {
 "Content-Type": "application/json",
 });
 const relayCookie = req.headers.cookie;
 if (typeof relayCookie === "string" && relayCookie.length) {
 relayHeaders["Cookie"] = relayCookie;
 }
 const response = await fetch(`${PARSER_API_URL}/api/byok/relay-stream`, {
 method: "POST",
 headers: relayHeaders,
 body: JSON.stringify(req.body || {}),
 signal: controller.signal,
 });

 // Non-2xx: Flask returns JSON (auth denied, bad request, etc). Pass
 // through as JSON so the client can render the error uniformly.
 if (!response.ok) {
 forwardRateLimitHeaders(response, res);
 const errorBody = await response.text();
 res.status(response.status);
 const ctype = response.headers.get("content-type") || "application/json";
 res.setHeader("Content-Type", ctype);
 res.send(errorBody);
 return;
 }

 res.status(200);
 res.setHeader("Content-Type", "text/event-stream");
 res.setHeader("Cache-Control", "no-cache");
 res.setHeader("Connection", "keep-alive");
 res.setHeader("X-Accel-Buffering", "no");
 // Flush headers immediately so the browser opens the SSE reader
 // before the first delta lands.
 if (typeof (res as any).flushHeaders === "function") {
 (res as any).flushHeaders();
 }

 if (!response.body) {
 res.end();
 return;
 }
 // node-fetch/undici stream: iterate chunks (Uint8Array) and forward.
 try {
 for await (const chunk of response.body as any) {
 if (!res.write(chunk)) {
 // apply back-pressure so we don't OOM on slow clients
 await new Promise<void>((resolve) => res.once("drain", () => resolve()));
 }
 }
 } catch (streamErr: any) {
 // Upstream aborted or errored mid-stream — flush an error event
 // the client can distinguish from provider deltas.
 const msg = streamErr?.name === "AbortError" ? "cancelled" : `stream error: ${streamErr?.message || streamErr}`;
 try {
 res.write(`event: relay_error\ndata: ${JSON.stringify({ error: { message: msg, type: "stream_error" } })}\n\n`);
 res.write("data: [DONE]\n\n");
 } catch {
 /* connection may already be closed */
 }
 }
 res.end();
 } catch (error: any) {
 // Failed to reach Flask (timeout, ECONNREFUSED). If we've already
 // sent headers we can only end; if not, emit JSON.
 console.error("BYOK relay-stream error:", error?.name, error?.message, "cause:", error?.cause?.code, error?.cause?.message);
 if (!res.headersSent) {
 res.status(504).json({ error: { message: "Relay-stream request failed or timed out" } });
 } else {
 try {
 res.write(`event: relay_error\ndata: ${JSON.stringify({ error: { message: "Relay-stream request failed or timed out", type: "network_error" } })}\n\n`);
 res.write("data: [DONE]\n\n");
 } catch {
 /* connection may already be closed */
 }
 res.end();
 }
 } finally {
 clearTimeout(timeoutId);
 }
 });

 app.get("/api/byok/providers", async (_req, res) => {
 try {
 const controller = new AbortController();
 const timeoutId = setTimeout(() => controller.abort(), 15_000);
 try {
 const response = await fetch(`${PARSER_API_URL}/api/byok/providers`, {
 signal: controller.signal,
 });
 clearTimeout(timeoutId);
 const data = await response.json();
 res.status(response.status).json(data);
 } finally {
 clearTimeout(timeoutId);
 }
 } catch (error: any) {
 console.error("BYOK providers error:", error.message);
 res.status(504).json({ error: "Providers request failed or timed out" });
 }
 });

 app.get("/api/verify-run/:runId", async (req, res) => {
 try {
 const controller = new AbortController();
 const timeoutId = setTimeout(() => controller.abort(), 15_000);
 try {
 const response = await fetch(
 `${PARSER_API_URL}/api/verify-run/${encodeURIComponent(req.params.runId)}`,
 { headers: flaskProxyHeaders(req), signal: controller.signal }
 );
 clearTimeout(timeoutId);
 forwardRateLimitHeaders(response, res);
 const data = await response.json();
 res.status(response.status).json(data);
 } finally {
 clearTimeout(timeoutId);
 }
 } catch (error: any) {
 console.error("Verify-run error:", error.message);
 res
 .status(504)
 .json({ exists: false, error: "Verify request failed or timed out" });
 }
 });

 // RR-5 — public coverage status; the sealed registry's visible
 // map). Intentionally public; no auth involved either side.
 app.get("/api/coverage", async (_req, res) => {
 try {
 const data = await proxyToFlask("/api/coverage");
 res.json(data);
 } catch (error: any) {
 console.error("Coverage list error:", error.message);
 res.status(500).json({ success: false, error: "Coverage fetch failed" });
 }
 });

 // RR-4 — public corrections logB-4). The read is intentionally
 // public (the "visibly, not silently" corrections promise); { req }
 // forwards the session cookie so an owner session additionally receives
 // detail_internal. Intake is email — there is no public write path.
 app.get("/api/corrections", async (req, res) => {
 try {
 const data = await proxyToFlask("/api/corrections", { req });
 res.json(data);
 } catch (error: any) {
 console.error("Corrections list error:", error.message);
 res.status(500).json({ success: false, error: "Corrections fetch failed" });
 }
 });

 // Owner-only mutations (gated Flask-side from birth per the
 // lesson). Direct fetch rather than proxyToFlask so the 401/403 status
 // codes survive to the caller — the gate matrix probes assert on them.
 const proxyCorrectionsMutation = (flaskPath: (req: any) => string) =>
 async (req: any, res: any) => {
 try {
 const controller = new AbortController();
 const timeoutId = setTimeout(() => controller.abort(), 15_000);
 try {
 const cookie = typeof req.headers["cookie"] === "string" ? req.headers["cookie"] : "";
 const response = await fetch(`${PARSER_API_URL}${flaskPath(req)}`, {
 method: "POST",
 headers: {
 "Content-Type": "application/json",
 ...(cookie ? { Cookie: cookie } : {}),
 },
 body: JSON.stringify(req.body ?? {}),
 signal: controller.signal,
 });
 const data = await response.json();
 res.status(response.status).json(data);
 } finally {
 clearTimeout(timeoutId);
 }
 } catch (error: any) {
 console.error("Corrections mutation error:", error.message);
 res.status(500).json({ success: false, error: "Corrections update failed" });
 }
 };

 app.post("/api/corrections", express.json(), proxyCorrectionsMutation(() => "/api/corrections"));
 app.post(
 "/api/corrections/:id/update",
 express.json(),
 proxyCorrectionsMutation((req) => `/api/corrections/${encodeURIComponent(req.params.id)}/update`)
 );

 //Phase 2 — decode a watermark ribbon out of an uploaded image
 // (off-site screenshot verification). Multipart upload; ~1-3s decode.
 app.post("/api/decode-ribbon-image", async (req, res) => {
 try {
 const controller = new AbortController();
 const timeoutId = setTimeout(() => controller.abort(), 20_000);
 try {
 // Stream the raw request body through to Flask — preserves the
 // multipart boundary + binary payload exactly as the client sent.
 const response = await fetch(`${PARSER_API_URL}/api/decode-ribbon-image`, {
 method: "POST",
 headers: req.headers["content-type"]
 ? { "Content-Type": req.headers["content-type"] as string }
 : undefined,
 body: req as any,
 duplex: "half",
 signal: controller.signal,
 } as any);
 clearTimeout(timeoutId);
 const data = await response.json();
 res.status(response.status).json(data);
 } finally {
 clearTimeout(timeoutId);
 }
 } catch (error: any) {
 console.error("Decode-ribbon-image error:", error.message);
 res.status(504).json({ token: null, error: "Upload failed or timed out" });
 }
 });

 //Phase 1.5 — watermark token → notebook_outputs row lookup.
 // Per [[express-flask-route-parity-discipline]] every Flask /api/*
 // gets an Express handler. Pure passthrough with a 15s timeout — the
 // Flask endpoint is a linear scan over notebook_outputs and lands
 // sub-second on current dataset size.
 app.get("/api/watermark-lookup/:token", async (req, res) => {
 try {
 const controller = new AbortController();
 const timeoutId = setTimeout(() => controller.abort(), 15_000);
 try {
 const response = await fetch(
 `${PARSER_API_URL}/api/watermark-lookup/${encodeURIComponent(req.params.token)}`,
 { signal: controller.signal }
 );
 clearTimeout(timeoutId);
 const data = await response.json();
 res.status(response.status).json(data);
 } finally {
 clearTimeout(timeoutId);
 }
 } catch (error: any) {
 console.error("Watermark-lookup error:", error.message);
 res
 .status(504)
 .json({ exists: false, error: "Lookup request failed or timed out" });
 }
 });

 // 60s — Qdrant retrieval is ~100-500ms but Surface Pro warm-up + LAN
 // round-trip wants headroom. Pure retrieval (no synthesis on flagship side).
 app.post("/api/rag-search/:meetingId", express.json(), async (req, res) => {
 try {
 const controller = new AbortController();
 const timeoutId = setTimeout(() => controller.abort(), 60_000);
 try {
 // (2026-07-01): forward the session cookie so Flask's
 // owner-or-public-flag gate can distinguish the operator from
 // public traffic. Same pattern as the owner-only proxies below.
 const headers = flaskProxyHeaders(req, {
 "Content-Type": "application/json",
 });
 const cookie = req.headers.cookie;
 if (typeof cookie === "string" && cookie.length) {
 headers["Cookie"] = cookie;
 }
 const response = await fetch(
 `${PARSER_API_URL}/api/rag-search/${encodeURIComponent(req.params.meetingId)}`,
 {
 method: "POST",
 headers,
 body: JSON.stringify(req.body || {}),
 signal: controller.signal,
 }
 );
 clearTimeout(timeoutId);
 forwardRateLimitHeaders(response, res);
 const data = await response.json();
 res.status(response.status).json(data);
 } finally {
 clearTimeout(timeoutId);
 }
 } catch (error: any) {
 console.error("RAG search error:", error.message);
 res
 .status(504)
 .json({ success: false, error: "RAG search failed or timed out" });
 }
 });

 // V1.5-OperatorSearch-1 Phase 1 — natural-language scope extraction.
 // Owner-only; the Flask handler does the owner-email cookie check, so
 // this proxy MUST forward the session cookie. 120s timeout because the
 // Sonnet intent-parse subprocess (~5-15s) plus the meetings-table JOIN
 // both happen Flask-side.
 app.post("/api/operator-search/interpret", express.json(), async (req, res) => {
 try {
 const controller = new AbortController();
 const timeoutId = setTimeout(() => controller.abort(), 120_000);
 const headers: Record<string, string> = {
 "Content-Type": "application/json",
 };
 const cookie = req.headers.cookie;
 if (typeof cookie === "string" && cookie.length) {
 headers["Cookie"] = cookie;
 }
 try {
 const response = await fetch(
 `${PARSER_API_URL}/api/operator-search/interpret`,
 {
 method: "POST",
 headers,
 body: JSON.stringify(req.body || {}),
 signal: controller.signal,
 }
 );
 clearTimeout(timeoutId);
 const data = await response.json();
 res.status(response.status).json(data);
 } finally {
 clearTimeout(timeoutId);
 }
 } catch (error: any) {
 console.error("Operator-search interpret error:", error.message);
 res.status(504).json({
 success: false,
 error: "Operator-search interpret failed or timed out",
 });
 }
 });

 // V1.5-OperatorSearch-1 Phase 3 — fan-out + cross-meeting synthesis.
 // Sonnet synthesis with the cross-meeting prompt + up to 50 chunks is
 // wall-clock-dominated by the synthesis subprocess; 300s Flask-side
 // timeout + 360s here so Express doesn't trip first. Cookie forwarded
 // for the same owner-only gate as /interpret.
 app.post("/api/operator-search/execute", express.json({ limit: "1mb" }), async (req, res) => {
 try {
 const controller = new AbortController();
 const timeoutId = setTimeout(() => controller.abort(), 360_000);
 const headers: Record<string, string> = {
 "Content-Type": "application/json",
 };
 const cookie = req.headers.cookie;
 if (typeof cookie === "string" && cookie.length) {
 headers["Cookie"] = cookie;
 }
 try {
 const response = await fetch(
 `${PARSER_API_URL}/api/operator-search/execute`,
 {
 method: "POST",
 headers,
 body: JSON.stringify(req.body || {}),
 signal: controller.signal,
 }
 );
 clearTimeout(timeoutId);
 const data = await response.json();
 res.status(response.status).json(data);
 } finally {
 clearTimeout(timeoutId);
 }
 } catch (error: any) {
 console.error("Operator-search execute error:", error.message);
 res.status(504).json({
 success: false,
 error: "Operator-search execute failed or timed out",
 });
 }
 });

 //Report-V0-1 — cited-report generator. Same owner-only gate as
 // operator-search (cookie forwarded so Flask's principal check fires).
 // Create returns immediately (the pipeline runs in a Flask-side daemon
 // thread); the modal polls the GET; the artifact route passes raw HTML
 // through (preview iframe src + download link both point here).
 app.post("/api/report-runs", express.json(), async (req, res) => {
 try {
 const controller = new AbortController();
 const timeoutId = setTimeout(() => controller.abort(), 60_000);
 const headers: Record<string, string> = {
 "Content-Type": "application/json",
 };
 const cookie = req.headers.cookie;
 if (typeof cookie === "string" && cookie.length) {
 headers["Cookie"] = cookie;
 }
 try {
 const response = await fetch(`${PARSER_API_URL}/api/report-runs`, {
 method: "POST",
 headers,
 body: JSON.stringify(req.body || {}),
 signal: controller.signal,
 });
 clearTimeout(timeoutId);
 const data = await response.json();
 res.status(response.status).json(data);
 } finally {
 clearTimeout(timeoutId);
 }
 } catch (error: any) {
 console.error("Report-run create error:", error.message);
 res.status(504).json({
 success: false,
 error: "Report-run create failed or timed out",
 });
 }
 });

 app.get("/api/report-runs/:id", async (req, res) => {
 try {
 const controller = new AbortController();
 const timeoutId = setTimeout(() => controller.abort(), 30_000);
 const headers: Record<string, string> = {};
 const cookie = req.headers.cookie;
 if (typeof cookie === "string" && cookie.length) {
 headers["Cookie"] = cookie;
 }
 try {
 const response = await fetch(
 `${PARSER_API_URL}/api/report-runs/${encodeURIComponent(req.params.id)}`,
 { headers, signal: controller.signal }
 );
 clearTimeout(timeoutId);
 const data = await response.json();
 res.status(response.status).json(data);
 } finally {
 clearTimeout(timeoutId);
 }
 } catch (error: any) {
 console.error("Report-run poll error:", error.message);
 res.status(504).json({
 success: false,
 error: "Report-run poll failed or timed out",
 });
 }
 });

 app.get("/api/report-runs/:id/artifact", async (req, res) => {
 try {
 const controller = new AbortController();
 const timeoutId = setTimeout(() => controller.abort(), 60_000);
 const headers: Record<string, string> = {};
 const cookie = req.headers.cookie;
 if (typeof cookie === "string" && cookie.length) {
 headers["Cookie"] = cookie;
 }
 try {
 const qs = new URLSearchParams();
 if (req.query.download) qs.set("download", "1");
 if (typeof req.query.variant === "string") qs.set("variant", req.query.variant);
 const qsStr = qs.toString() ? `?${qs.toString()}` : "";
 const response = await fetch(
 `${PARSER_API_URL}/api/report-runs/${encodeURIComponent(req.params.id)}/artifact${qsStr}`,
 { headers, signal: controller.signal }
 );
 clearTimeout(timeoutId);
 // Raw passthrough — the artifact is text/html (or a JSON error
 // envelope on 4xx); forward both content-type + disposition.
 const contentType = response.headers.get("content-type");
 const disposition = response.headers.get("content-disposition");
 if (contentType) res.setHeader("Content-Type", contentType);
 if (disposition) res.setHeader("Content-Disposition", disposition);
 res.setHeader(
 "Content-Security-Policy",
 "default-src 'none'; style-src 'unsafe-inline'"
 );
 res.status(response.status).send(await response.text());
 } finally {
 clearTimeout(timeoutId);
 }
 } catch (error: any) {
 console.error("Report-run artifact error:", error.message);
 res.status(504).json({
 success: false,
 error: "Report-run artifact fetch failed or timed out",
 });
 }
 });

 // Report-Stitch-1V0.5) — fire the generative-chrome driver.
 // The driver lives in this process (the SDK is npm-only); auth rides
 // its Flask fragments fetch with the forwarded cookie, so a non-owner
 // request dies at the first hop. Fire-and-forget; poll stitch-status.
 app.post("/api/report-runs/:id/stitch", express.json(), async (req, res) => {
 const cookie = req.headers.cookie;
 if (typeof cookie !== "string" || !cookie.length) {
 res.status(403).json({ success: false, error: "sign-in required" });
 return;
 }
 const { runStitchChrome, stitchIsRunning } = await import("./stitchChrome");
 if (stitchIsRunning(req.params.id)) {
 res.status(409).json({
 success: false,
 error: "a Stitch chrome run is already in flight for this report",
 });
 return;
 }
 void runStitchChrome(req.params.id, cookie);
 res.json({ success: true, started: true });
 });

 app.get("/api/report-runs/:id/stitch-status", async (req, res) => {
 const { stitchLiveState } = await import("./stitchChrome");
 const state = stitchLiveState(req.params.id);
 if (state) {
 res.json({
 success: true,
 source: "live",
 status: state.status,
 progress: state.progress,
 step: state.step,
 passes: state.edits.length,
 error: state.error,
 });
 return;
 }
 // No live entry (finished long ago / Express restarted) — fall back
 // to the persisted row via Flask so the answer is never a shrug.
 try {
 const headers: Record<string, string> = {};
 const cookie = req.headers.cookie;
 if (typeof cookie === "string" && cookie.length) headers["Cookie"] = cookie;
 const response = await fetch(
 `${PARSER_API_URL}/api/report-runs/${encodeURIComponent(req.params.id)}`,
 { headers }
 );
 const data: any = await response.json();
 if (!response.ok || !data.success) {
 res.status(response.status).json(data);
 return;
 }
 res.json({
 success: true,
 source: "persisted",
 status: data.run?.stitch_status ?? null,
 progress: data.run?.stitch_progress ?? null,
 error: data.run?.stitch_error ?? null,
 has_stitch_artifact: !!data.run?.has_stitch_artifact,
 });
 } catch (error: any) {
 res.status(504).json({ success: false, error: "stitch-status lookup failed" });
 }
 });

 // ============================================
 // Z-SPAN: Work order queue endpoints
 // The worker daemon (notebooklm_bridge/worker.py) picks up "pending"
 // work orders and processes them at a defrag pace. These endpoints
 // let the UI list the queue, trigger a scan, and manage individual orders.
 // ============================================
 app.get("/api/work-orders", (req, res) => {
 const qs = new URLSearchParams(req.query as Record<string, string>).toString();
 return proxyJsonAuth(
 req,
 res,
 `/api/work-orders${qs ? `?${qs}` : ""}`,
 );
 });

 // aggregated status (auth + work_orders + processing) — one
 // round-trip for the operator terminal's always-on banner.
 app.get("/api/system/status", async (req, res) => {
 try {
 // §5b — forward the session cookie so Flask recognizes the owner and
 // returns the full per-meeting processing detail; anon (no cookie)
 // gets the identity-stripped public shape.
 const data = await proxyToFlask("/api/system/status", { req });
 res.json(data);
 } catch (error: any) {
 // Flask unreachable → surface explicitly so the banner shows red.
 res.status(200).json({
 success: false,
 flask_up: false,
 error: error?.message || "Flask unreachable",
 });
 }
 });

 // follow-up: cross-session heartbeat. The StatusBanner posts to
 // this every 5s alongside the status GET. The endpoint upserts the
 // caller's row in active_sessions, prunes stale rows (>30s since
 // last heartbeat), and returns the count of OTHER currently-active
 // sessions so the UI can warn about collisions.
 app.post("/api/system/heartbeat", async (req, res) => {
 try {
 const data = await proxyToFlask("/api/system/heartbeat", {
 method: "POST",
 body: req.body,
 });
 res.json(data);
 } catch (error: any) {
 // Heartbeat failure is non-fatal — the banner just won't warn
 // about other sessions for this tick.
 res.status(200).json({
 success: false,
 error: error?.message || "heartbeat failed",
 other_active: 0,
 sessions: [],
 });
 }
 });

 app.get("/api/work-orders/stats", async (_req, res) => {
 try {
 const data = await proxyToFlask("/api/work-orders/stats");
 res.json(data);
 } catch (error: any) {
 console.error("Work orders stats error:", error.message);
 res.status(500).json({ success: false, error: "Work orders stats failed" });
 }
 });

 app.get("/api/work-orders/:id", async (req, res) => {
 try {
 const data = await proxyToFlask(`/api/work-orders/${req.params.id}`);
 res.json(data);
 } catch (error: any) {
 console.error("Work order get error:", error.message);
 res.status(500).json({ success: false, error: "Work order fetch failed" });
 }
 });

 app.post("/api/work-orders/scan", express.json({ limit: "1mb" }), (req, res) =>
 proxyJsonAuth(req, res, "/api/work-orders/scan"),
 );

 // register-notebook proxy retired with its Flask route (RR-8 fix-list,
 //— vestigial post-, zero callers, was ungated).

 app.post("/api/work-orders/:id/retry", express.json(), async (req, res) => {
 try {
 const data = await proxyToFlask(
 `/api/work-orders/${req.params.id}/retry`,
 { method: "POST", body: req.body || {}, req }
 );
 res.json(data);
 } catch (error: any) {
 console.error("Work order retry error:", error.message);
 res.status(500).json({ success: false, error: "Retry failed" });
 }
 });

 // ─────────────────────────────────────────────────────────────
 // flagship sync routes
 // ─────────────────────────────────────────────────────────────
 //
 // Sender side — runs only on the local Flask (cloud Flask has no
 // operator UI to trigger it). 5-minute timeout because the push
 // streams up to ~50MB of media files in sequence; default 30s is
 // too tight.
 // RR-8 SEC-AUTH-1: owner-ONLY on the Flask side (_require_owner; a shared
 // fleet token must not authorize a production push). Must go through
 // proxyJsonAuth so the owner session cookie forwards — proxyToFlask dropped
 // it here (no `req`), which would 401 the real owner and then MASK the 401
 // as a 200. 5-min timeout for the ~50MB media push.
 app.post(
 "/api/work-orders/:id/push-to-flagship",
 express.json(),
 (req, res) =>
 proxyJsonAuth(
 req,
 res,
 `/api/work-orders/${req.params.id}/push-to-flagship`,
 { timeoutMs: 300_000 },
 ),
 );

 app.get("/api/work-orders/:id/flagship-sync-status", async (req, res) => {
 try {
 const data = await proxyToFlask(
 `/api/work-orders/${req.params.id}/flagship-sync-status`,
 { req }
 );
 res.json(data);
 } catch (error: any) {
 console.error("Flagship-sync-status error:", error.message);
 res
 .status(500)
 .json({ success: false, error: "Sync status fetch failed" });
 }
 });

 // Receiver side — only meaningfully called on cloud Flask (the
 // sender's local Flask also exposes these endpoints, but no remote
 // posts to them in V1). JSON metadata + binary media file uploads.
 // proxyToFlask is JSON-only; the binary route needs a hand-rolled
 // proxy that forwards the raw body + X-Sync-Token header.
 app.post(
 "/api/sync/meeting/:id",
 express.json({ limit: "10mb" }),
 async (req, res) => {
 try {
 // Hand-rolled proxy so we can forward X-Sync-Token to Flask.
 const upstream = await fetch(
 `${PARSER_API_URL}/api/sync/meeting/${req.params.id}`,
 {
 method: "POST",
 headers: {
 "Content-Type": "application/json",
 "X-Sync-Token": String(req.headers["x-sync-token"] || ""),
 },
 body: JSON.stringify(req.body || {}),
 }
 );
 const body = await upstream.text();
 res
 .status(upstream.status)
 .type(upstream.headers.get("content-type") || "application/json")
 .send(body);
 } catch (error: any) {
 console.error("Sync meeting payload error:", error.message);
 res
 .status(502)
 .json({ success: false, error: `Ingest failed: ${error.message}` });
 }
 }
 );

 app.post(
 "/api/sync/meeting/:id/media/:filename",
 // No JSON middleware — body is raw bytes. Bound the body size at the
 // Express layer; Flask is the actual writer and applies the
 // filename allowlist before persisting.
 express.raw({
 type: () => true,
 limit: "100mb",
 }),
 async (req, res) => {
 try {
 const upstream = await fetch(
 `${PARSER_API_URL}/api/sync/meeting/${req.params.id}/media/${encodeURIComponent(req.params.filename)}`,
 {
 method: "POST",
 headers: {
 "Content-Type":
 String(req.headers["content-type"]) ||
 "application/octet-stream",
 "X-Sync-Token": String(req.headers["x-sync-token"] || ""),
 },
 body: req.body as Buffer,
 }
 );
 const body = await upstream.text();
 res
 .status(upstream.status)
 .type(upstream.headers.get("content-type") || "application/json")
 .send(body);
 } catch (error: any) {
 console.error("Sync meeting media error:", error.message);
 res
 .status(502)
 .json({ success: false, error: `Media ingest failed: ${error.message}` });
 }
 }
 );

 // : confirm a medium/needs_review match — copies meetings.video_url
 // onto the WO and flips state to pending.
 app.post("/api/work-orders/:id/confirm-match", express.json(), async (req, res) => {
 try {
 const data = await proxyToFlask(
 `/api/work-orders/${req.params.id}/confirm-match`,
 { method: "POST", body: req.body || {}, req },
 );
 res.json(data);
 } catch (error: any) {
 console.error("Work order confirm-match error:", error.message);
 res.status(500).json({ success: false, error: "Confirm-match failed" , req });
 }
 });

 // Session-31 (2026-07-04) — auth-audit remediation. Approve /
 // publish / unpublish are the human-review-gate mechanisms
 // (CLAUDE.md Guarantee #1). Prior state used `proxyToFlask` which
 // does NOT forward the Cookie header, so the Flask owner-check I
 // just added would silently reject the real owner. Swapped to
 // `proxyJsonAuth` which forwards Cookie + preserves Flask's status
 // code (401 unauthenticated, 403 non-owner) verbatim.
 app.post("/api/work-orders/:id/approve", express.json({ limit: "1mb" }), (req, res) =>
 proxyJsonAuth(req, res, `/api/work-orders/${req.params.id}/approve`),
 );

 app.post("/api/meetings/:id/publish", express.json({ limit: "1mb" }), (req, res) =>
 proxyJsonAuth(req, res, `/api/meetings/${req.params.id}/publish`),
 );

 app.post("/api/meetings/:id/unpublish", express.json({ limit: "1mb" }), (req, res) =>
 proxyJsonAuth(req, res, `/api/meetings/${req.params.id}/unpublish`),
 );

 // llm-health is owner-gated at Flask (RR-8) but had no Express handler, so
 // it fell through to the SPA catch-all. proxyJsonAuth forwards the owner
 // cookie + preserves Flask's status (parity discipline).
 app.get("/api/llm-health", (req, res) =>
 proxyJsonAuth(req, res, "/api/llm-health"),
 );

 app.get("/api/meetings/:id/publish-status", async (req, res) => {
 try {
 // { req } forwards the owner cookie so the draft-content gate lets the
 // owner through (RR-8 §5b — publish-status is owner-gated for drafts).
 const data = await proxyToFlask(
 `/api/meetings/${req.params.id}/publish-status`,
 { req }
 );
 res.json(data);
 } catch (error: any) {
 console.error("Publish-status fetch error:", error.message);
 res.status(500).json({ success: false, error: "Publish-status fetch failed" });
 }
 });

 // Citation log — structured provenance tree for the (i) panel on
 // BroadcastPage. ?audience=public (default, anonymized) or
 // ?audience=operator (raw operator names).
 app.get("/api/citation/:id", async (req, res) => {
 const qs = new URLSearchParams(req.query as Record<string, string>).toString();
 return proxyJsonAuth(
 req,
 res,
 `/api/citation/${encodeURIComponent(req.params.id)}${qs ? `?${qs}` : ""}`,
 );
 });

 // REMOVED (2026-06-25). Manual video-URL paste struck from
 // the project — autonomous ingestion via haiku_match_videos.py is the
 // canonical floor. Proxy retained as HTTP 410 passthrough so any stale
 // caller surfaces loudly rather than silently failing.
 app.post("/api/work-orders/:id/set-video-url", express.json(), async (_req, res) => {
 res.status(410).json({
 success: false,
 error: "Endpoint removed (2026-06-25). Use parsers/scripts/haiku_match_videos.py.",
 autonomous_path: "parsers/scripts/haiku_match_videos.py --city <name> --apply",
 });
 });

 // ============================================
 // Z-SPAN: Step-through processing
 // Use these for the "process one" buttons. The fully-automatic worker
 // daemon is the future state — for now, every run is human-triggered.
 // ============================================
 app.post("/api/work-orders/process-next", express.json({ limit: "1mb" }), (req, res) =>
 proxyJsonAuth(req, res, "/api/work-orders/process-next"),
 );

 app.post("/api/work-orders/:id/process", express.json({ limit: "1mb" }), (req, res) =>
 proxyJsonAuth(req, res, `/api/work-orders/${req.params.id}/process`),
 );

 // Tail the worker subprocess log for a specific WO (incremental polling
 // via ?since=<byte offset>). Lets the operator terminal display worker
 // output live in its activity column.
 app.get("/api/work-orders/:id/log", async (req, res) => {
 try {
 const since = req.query.since ? `?since=${encodeURIComponent(String(req.query.since))}` : "";
 const data = await proxyToFlask(`/api/work-orders/${req.params.id}/log${since}`);
 res.json(data);
 } catch (error: any) {
 console.error("Worker log tail error:", error.message);
 res.status(500).json({ success: false, error: "Worker log tail failed" });
 }
 });

 // ============================================
 // Z-SPAN: Per-city YouTube channel registry
 // ============================================
 app.get("/api/cities/:cityName/youtube-channel", async (req, res) => {
 try {
 const data = await proxyToFlask(
 `/api/cities/${encodeURIComponent(req.params.cityName)}/youtube-channel`,
 { req },
 );
 res.json(data);
 } catch (error: any) {
 console.error("City youtube get error:", error.message);
 res.status(500).json({ success: false, error: "Lookup failed" });
 }
 });

 app.post("/api/cities/:cityName/youtube-channel", express.json(), async (req, res) => {
 try {
 // RR-8 / SEC-PROXY-1: forward req so the owner session cookie reaches
 // Flask's _require_owner() gate (else the real owner is 401'd).
 const data = await proxyToFlask(
 `/api/cities/${encodeURIComponent(req.params.cityName)}/youtube-channel`,
 { method: "POST", body: req.body || {}, req }
 );
 res.json(data);
 } catch (error: any) {
 console.error("City youtube set error:", error.message);
 // RR-8 / SEC-PROXY-1: never serialize the req object into the response.
 res.status(500).json({ success: false, error: "Save failed" });
 }
 });

 app.get("/api/operator/pattern-health", async (req, res) => {
 try {
 const qs = new URLSearchParams(req.query as Record<string, string>).toString();
 const data = await proxyToFlask(
 `/api/operator/pattern-health${qs ? `?${qs}` : ""}`
 );
 res.json(data);
 } catch (error: any) {
 console.error("Pattern-health lookup error:", error.message);
 res.status(500).json({ ok: false, error: "Lookup failed" });
 }
 });

 app.get("/api/cities/:cityName/meeting-patterns", async (req, res) => {
 try {
 // H-6: proxy through to Flask, preserve query params (days_ahead,
 // upcoming_per_pattern). Flask reads city_intelligence/<slug>.json +
 // calls pattern_projection.get_upcoming_meetings_from_patterns.
 const qs = new URLSearchParams(req.query as Record<string, string>).toString();
 const data = await proxyToFlask(
 `/api/cities/${encodeURIComponent(req.params.cityName)}/meeting-patterns${qs ? `?${qs}` : ""}`
 );
 res.json(data);
 } catch (error: any) {
 console.error("City meeting-patterns lookup error:", error.message);
 res.status(500).json({ ok: false, error: "Lookup failed" });
 }
 });

 // ============================================
 // Google OAuth — light-account sign-in
 // (ACCOUNT_SYSTEM_SPEC chunk 2)
 //
 // These routes are passthroughs, NOT JSON proxies — Flask returns
 // 302 redirects + Set-Cookie headers that the browser needs to
 // observe verbatim. proxyToFlask() consumes the body as JSON, which
 // strips both. proxyAuthToFlask() forwards method + path + cookies
 // + body and copies the raw upstream status / headers / body back.
 //
 // We also stamp X-Forwarded-Proto + X-Forwarded-Host explicitly so
 // Flask's compute_redirect_uri() resolves the browser-facing URL
 // (localhost:3000) rather than Flask's loopback (127.0.0.1:5001) —
 // the Google OAuth client registers exact redirect URIs, no
 // wildcards.
 // ============================================
 async function proxyAuthToFlask(req: any, res: any, flaskPath: string) {
 const url = `${PARSER_API_URL}${flaskPath}`;
 // Session-104 (post-PR-#205 diagnostic): Railway ingress rewrites
 // X-Forwarded-Host between CF Pages Function and Express to the
 // internal Railway hostname. The Pages Function synthesizes a
 // companion non-standard pair (X-ZSPAN-Origin-Host/Proto) that
 // Railway leaves untouched — pickAuthOriginHost() reads that pair
 // FIRST (with a hostname allowlist so an injected value can't steer
 // OAuth to an unregistered host), then falls back to X-Forwarded-Host,
 // then req.headers.host, then the caller-supplied default.
 const { host: fwdHost, proto: fwdProto } = pickAuthOriginHost(
 req,
 `localhost:${process.env.PORT || 3000}`,
 (req.protocol as string) || "http",
 );

 const headers: Record<string, string> = {
 "X-Forwarded-Host": fwdHost,
 "X-Forwarded-Proto": fwdProto,
 };
 const cookie = req.headers.cookie;
 if (typeof cookie === "string" && cookie.length) {
 headers["Cookie"] = cookie;
 }
 const contentType = req.headers["content-type"];
 if (typeof contentType === "string") {
 headers["Content-Type"] = contentType;
 }

 let body: any = undefined;
 if (req.method !== "GET" && req.method !== "HEAD") {
 // Express has already parsed JSON bodies upstream — re-serialize
 // the one we have. For the OAuth flow the only non-GET route is
 // POST /api/auth/logout which has no body, so this is mostly a
 // no-op safety net.
 if (req.body && Object.keys(req.body).length) {
 body = JSON.stringify(req.body);
 }
 }

 try {
 const upstream = await fetch(url, {
 method: req.method,
 headers,
 body,
 redirect: "manual",
 });

 // Copy Set-Cookie headers verbatim (there may be multiple per
 // call — state cookie + clear-state on callback). Newer Node
 // fetch exposes them via Headers.getSetCookie(); fall back to
 // the .raw() shape some implementations expose.
 const upstreamSetCookies =
 typeof (upstream.headers as any).getSetCookie === "function"
 ? (upstream.headers as any).getSetCookie()
 : [];
 if (upstreamSetCookies.length) {
 res.setHeader("Set-Cookie", upstreamSetCookies);
 } else {
 const single = upstream.headers.get("set-cookie");
 if (single) res.setHeader("Set-Cookie", single);
 }

 const location = upstream.headers.get("location");
 if (location) res.setHeader("Location", location);

 const ct = upstream.headers.get("content-type");
 if (ct) res.setHeader("Content-Type", ct);

 res.status(upstream.status);
 const bodyBuf = Buffer.from(await upstream.arrayBuffer());
 res.send(bodyBuf);
 } catch (err: any) {
 console.error(`[auth proxy] ${flaskPath} error:`, err.message);
 res.status(502).json({
 success: false,
 error: "auth proxy failed",
 detail: err.message,
 });
 }
 }

 // 2026-06-26 — close the localhost-vs-127 OAuth trap permanently.
 // Background: Google OAuth client only has http://localhost:3000 as
 // its registered dev redirect URI (per google_oauth.py:489's
 // hardcoded fallback). If the user starts sign-in from
 // http://127.0.0.1:3000, the state cookie scopes to the 127 host
 // bucket, Google redirects back to localhost, and the cookie isn't
 // sent → 400 missing_code_or_state. This redirect forces every
 // sign-in start through localhost so the OAuth dance always lands
 // on the same host bucket. Per the always-loaded memory
 // [[localhost-vs-127-cookie-bucket-isolation]] — browser cookies
 // scope per exact host string. Z-SPAN's developer-Claude has hit
 // this trap twice in alone.
 app.get("/api/auth/google/login", (req, res) => {
 const host = (req.headers.host || "").toLowerCase();
 if (host.startsWith("127.0.0.1")) {
 const qs = req.url.includes("?") ? req.url.slice(req.url.indexOf("?")) : "";
 const target = `http://localhost:${host.split(":")[1] || "3000"}/api/auth/google/login${qs}`;
 res.redirect(302, target);
 return;
 }
 return proxyAuthToFlask(
 req,
 res,
 `/api/auth/google/login${req.url.includes("?") ? req.url.slice(req.url.indexOf("?")) : ""}`,
 );
 });

 app.get("/api/auth/google/callback", (req, res) =>
 proxyAuthToFlask(
 req,
 res,
 `/api/auth/google/callback${req.url.includes("?") ? req.url.slice(req.url.indexOf("?")) : ""}`,
 ),
 );

 app.get("/api/auth/me", (req, res) =>
 proxyAuthToFlask(req, res, "/api/auth/me"),
 );

 app.post("/api/auth/logout", express.json(), (req, res) =>
 proxyAuthToFlask(req, res, "/api/auth/logout"),
 );

 // Notification unsubscribe (catch — Haiku visitor sweep
 // 2026-07-31 found /api/unsubscribe 404ing at Express). PR #202 shipped
 // the Flask endpoint (api_server.py:6270 + 6293) and the CF Pages
 // Function public admission (functions/api/[[catchall]].ts
 // PUBLIC_UNSUBSCRIBE_ROUTES), but Express in between had no bridge —
 // Cannot GET /api/unsubscribe was reaching browsers. Both methods
 // return HTML (unsubscribe confirmation page); proxyAuthToFlask is the
 // right passthrough shape — verbatim status + Set-Cookie + Content-Type,
 // no JSON assumption.
 app.get("/api/unsubscribe", (req, res) =>
 proxyAuthToFlask(
 req,
 res,
 `/api/unsubscribe${req.url.includes("?") ? req.url.slice(req.url.indexOf("?")) : ""}`,
 ),
 );

 app.post(
 "/api/unsubscribe",
 express.urlencoded({ extended: false }),
 (req, res) =>
 proxyAuthToFlask(
 req,
 res,
 `/api/unsubscribe${req.url.includes("?") ? req.url.slice(req.url.indexOf("?")) : ""}`,
 ),
 );

 // Librarian request-access layer (2026-07-27, PR #173's Express half —
 // the Flask endpoints + edge admission landed without these
 // registrations, so the routes dead-ended here; Express registers /api
 // routes explicitly, no generic passthrough). proxyAuthToFlask carries
 // the session cookie (§ 5b): request-access needs the user's cookie,
 // the queue + decide routes need the owner's for _require_owner.
 app.post("/api/librarian/request-access", express.json(), (req, res) =>
 proxyAuthToFlask(req, res, "/api/librarian/request-access"),
 );

 app.get("/api/librarian/access-requests", (req, res) =>
 proxyAuthToFlask(req, res, "/api/librarian/access-requests"),
 );

 app.post(
 "/api/librarian/access-requests/:userId/decide",
 express.json(),
 (req, res) => {
 const userId = Number.parseInt(String(req.params.userId), 10);
 if (!Number.isInteger(userId) || userId <= 0) {
 res.status(400).json({ success: false, error: "invalid user id" });
 return;
 }
 return proxyAuthToFlask(
 req,
 res,
 `/api/librarian/access-requests/${userId}/decide`,
 );
 },
 );

 app.get("/api/auth/cli/start", (req, res) => {
 const host = (req.headers.host || "").toLowerCase();
 if (host.startsWith("127.0.0.1")) {
 const qs = req.url.includes("?") ? req.url.slice(req.url.indexOf("?")) : "";
 const target = `http://localhost:${host.split(":")[1] || "3000"}/api/auth/cli/start${qs}`;
 res.redirect(302, target);
 return;
 }
 return proxyAuthToFlask(
 req,
 res,
 `/api/auth/cli/start${req.url.includes("?") ? req.url.slice(req.url.indexOf("?")) : ""}`,
 );
 });

 app.get("/api/auth/cli/finish", (req, res) =>
 proxyAuthToFlask(req, res, "/api/auth/cli/finish"),
 );
 app.post("/api/auth/cli/finish", (req, res) =>
 proxyAuthToFlask(req, res, "/api/auth/cli/finish"),
 );
 app.get("/api/auth/cli/cancel", (req, res) =>
 proxyAuthToFlask(req, res, "/api/auth/cli/cancel"),
 );

 async function proxyJsonBearer(
 req: any,
 res: any,
 flaskPath: string,
 ): Promise<void> {
 const controller = new AbortController();
 const timeoutId = setTimeout(() => controller.abort(), 30_000);
 const headers = flaskProxyHeaders(req);
 const authorization = req.headers.authorization;
 if (typeof authorization === "string" && authorization.length) {
 headers["Authorization"] = authorization;
 }
 let body: string | undefined;
 if (req.method !== "GET" && req.method !== "HEAD") {
 headers["Content-Type"] = "application/json";
 body = JSON.stringify(req.body || {});
 }
 try {
 const upstream = await fetch(`${PARSER_API_URL}${flaskPath}`, {
 method: req.method,
 headers,
 body,
 signal: controller.signal,
 });
 clearTimeout(timeoutId);
 for (const header of ["content-type", "cache-control", "retry-after"]) {
 const value = upstream.headers.get(header);
 if (value) res.setHeader(header, value);
 }
 res.status(upstream.status);
 res.send(await upstream.text());
 } catch (err: any) {
 clearTimeout(timeoutId);
 console.error(`[bearer-json proxy] ${flaskPath} error:`, err.message);
 res.status(502).json({ error: "proxy failed", detail: err.message });
 }
 }

 app.post("/api/auth/cli/exchange", (req, res) =>
 proxyJsonBearer(req, res, "/api/auth/cli/exchange"),
 );
 app.post("/api/auth/cli/revoke", (req, res) =>
 proxyJsonBearer(req, res, "/api/auth/cli/revoke"),
 );
 app.get("/api/auth/cli/me", (req, res) =>
 proxyJsonBearer(req, res, "/api/auth/cli/me"),
 );
 app.post("/api/generations/register", (req, res) =>
 proxyJsonBearer(req, res, "/api/generations/register"),
 );

 async function proxyJsonAnonymous(
 req: any,
 res: any,
 flaskPath: string,
 opts: { timeoutMs?: number } = {},
 ): Promise<void> {
 const controller = new AbortController();
 const timeoutId = setTimeout(
 () => controller.abort(),
 opts.timeoutMs ?? 30_000,
 );
 try {
 const upstream = await fetch(`${PARSER_API_URL}${flaskPath}`, {
 method: req.method,
 headers: flaskProxyHeaders(req),
 signal: controller.signal,
 });
 clearTimeout(timeoutId);
 for (const header of ["content-type", "cache-control", "retry-after"]) {
 const value = upstream.headers.get(header);
 if (value) res.setHeader(header, value);
 }
 res.status(upstream.status);
 res.send(await upstream.text());
 } catch (err: any) {
 clearTimeout(timeoutId);
 console.error(`[anonymous-json proxy] ${flaskPath} error:`, err.message);
 res.status(502).json({
 error: "proxy failed",
 detail: err.message,
 });
 }
 }

 // ============================================
 // Follow/subscribe — light-account Consumer 2
 // (ACCOUNT_SYSTEM_SPEC chunk 3)
 //
 // GET /api/follows → list signed-in user's follows
 // POST /api/follows body{} → add a follow
 // DELETE /api/follows body{} → remove a follow
 //
 // The standard proxyToFlask consumes the body as JSON + discards
 // Flask's status code (re-wrapping with 500 on error). For these
 // routes Flask's status code is load-bearing (401 unauthenticated
 // gates the UI's sign-in prompt; 400 surfaces target-type errors),
 // so we use a small purpose-built helper that forwards Flask's
 // status + body verbatim AND forwards the incoming Cookie header so
 // Flask can read the session JWT.
 // ============================================
 async function proxyJsonAuth(
 req: any,
 res: any,
 flaskPath: string,
 opts: {
 timeoutMs?: number;
 method?: string;
 headers?: Record<string, string>;
 } = {},
 ): Promise<void> {
 const url = `${PARSER_API_URL}${flaskPath}`;
 const headers = flaskProxyHeaders(req, opts.headers);
 const { host: fwdHost, proto: fwdProto } = pickAuthOriginHost(
 req,
 `localhost:${process.env.PORT || 3000}`,
 (req.protocol as string) || "http",
 );
 headers["X-Forwarded-Host"] = fwdHost;
 headers["X-Forwarded-Proto"] = fwdProto;
 const cookie = req.headers.cookie;
 if (typeof cookie === "string" && cookie.length) {
 headers["Cookie"] = cookie;
 }
 const method = opts.method ?? req.method;
 let body: any = undefined;
 if (method !== "GET" && method !== "HEAD") {
 headers["Content-Type"] = "application/json";
 body = req.body ? JSON.stringify(req.body) : "{}";
 }
 // Optional per-call timeout. The RR-8 gated routes that migrated off
 // proxyToFlask carried explicit timeouts (push 5min, ingest 90s); when no
 // timeout is passed behavior stays unbounded, as before, for the existing
 // owner-gated callers (follows / approve / publish / …).
 const controller = new AbortController();
 const timeoutId =
 opts.timeoutMs != null
 ? setTimeout(() => controller.abort(), opts.timeoutMs)
 : null;
 try {
 const upstream = await fetch(url, {
 method,
 headers,
 body,
 signal: controller.signal,
 });
 if (timeoutId) clearTimeout(timeoutId);
 // Forward Set-Cookie headers — POST /api/creators/promote mints a
 // new session JWT with the elevated `creator` role; without this
 // forward the browser would keep its stale light-account cookie.
 const upstreamSetCookies =
 typeof (upstream.headers as any).getSetCookie === "function"
 ? (upstream.headers as any).getSetCookie()
 : [];
 if (upstreamSetCookies.length) {
 res.setHeader("Set-Cookie", upstreamSetCookies);
 } else {
 const single = upstream.headers.get("set-cookie");
 if (single) res.setHeader("Set-Cookie", single);
 }
 const ct = upstream.headers.get("content-type");
 if (ct) res.setHeader("Content-Type", ct);
 forwardRateLimitHeaders(upstream, res);
 res.status(upstream.status);
 const text = await upstream.text();
 res.send(text);
 } catch (err: any) {
 if (timeoutId) clearTimeout(timeoutId);
 console.error(`[auth-json proxy] ${flaskPath} error:`, err.message);
 res.status(502).json({
 success: false,
 error: "proxy failed",
 detail: err.message,
 });
 }
 }

 app.get("/api/follows", (req, res) =>
 proxyJsonAuth(req, res, "/api/follows"),
 );
 app.post("/api/follows", express.json(), (req, res) =>
 proxyJsonAuth(req, res, "/api/follows"),
 );
 app.delete("/api/follows", express.json(), (req, res) =>
 proxyJsonAuth(req, res, "/api/follows"),
 );

 // ============================================
 // Creator Network — ACCOUNT_SYSTEM_SPEC chunks 7 + 8 endpoints
 // Both routes need cookie-auth passthrough + Flask status code
 // verbatim (the promotion endpoint sets a NEW session cookie when
 // it succeeds; proxyJsonAuth forwards Set-Cookie correctly).
 // ============================================
 app.get("/api/creators/me/status", (req, res) =>
 proxyJsonAuth(req, res, "/api/creators/me/status"),
 );
 app.post("/api/creators/promote", express.json(), (req, res) =>
 proxyJsonAuth(req, res, "/api/creators/promote"),
 );

 // V1-UI-3 — login-gated suggestion-query submissions.
 app.post("/api/suggestions", express.json(), (req, res) =>
 proxyJsonAuth(req, res, "/api/suggestions"),
 );

 // ============================================
 // Operator review queue — consumes operator_review_needed=1
 // rows from suggestions + creator_agreements. Cookie auth is
 // load-bearing here (Flask additionally enforces 401 if
 // unauthenticated); status passthrough + body verbatim.
 // ============================================
 app.get("/api/operator/review-queue", (req, res) =>
 proxyJsonAuth(req, res, "/api/operator/review-queue"),
 );
 app.post(
 "/api/operator/review-queue/:queueType/:rowId/resolve",
 express.json(),
 (req, res) => {
 const queueType = encodeURIComponent(req.params.queueType);
 const rowId = encodeURIComponent(req.params.rowId);
 proxyJsonAuth(
 req,
 res,
 `/api/operator/review-queue/${queueType}/${rowId}/resolve`,
 );
 },
 );

 // V1-Repo-1 — / repository deposit gate. Approve / reject /
 // withdraw on a repository_assets row. Cf-Access is the perimeter
 // gate; Flask additionally enforces 401. Body carries { reason } for
 // reject + withdraw; approve ignores it.
 app.post(
 "/api/operator/repository-queue/:assetId/:action",
 express.json(),
 (req, res) => {
 const assetId = encodeURIComponent(req.params.assetId);
 const action = encodeURIComponent(req.params.action);
 proxyJsonAuth(
 req,
 res,
 `/api/operator/repository-queue/${assetId}/${action}`,
 );
 },
 );

 // hardening findings — ingest + history list. The ingest payload
 // can be sizable (up to 200 findings); express.json() defaults to 100 KB
 // which is comfortably above the schema's per-field caps.
 app.post("/api/operator/hardening-runs/ingest", express.json({ limit: "2mb" }), (req, res) =>
 proxyJsonAuth(req, res, "/api/operator/hardening-runs/ingest"),
 );
 app.get("/api/operator/hardening-runs", (req, res) =>
 proxyJsonAuth(req, res, "/api/operator/hardening-runs"),
 );

 // ============================================
 // Z-SPAN: NotebookLM auth health
 // ============================================
 app.get("/api/auth/notebooklm/status", async (req, res) => {
 try {
 const force = req.query.force === "true" ? "?force=true" : "";
 const data = await proxyToFlask(`/api/auth/notebooklm/status${force}`);
 res.json(data);
 } catch (error: any) {
 console.error("Auth status error:", error.message);
 res.status(500).json({ success: false, error: "Status check failed" });
 }
 });

 app.post("/api/auth/notebooklm/relogin", express.json(), async (_req, res) => {
 try {
 const data = await proxyToFlask("/api/auth/notebooklm/relogin", {
 method: "POST", body: {},
 });
 res.json(data);
 } catch (error: any) {
 console.error("Auth relogin error:", error.message);
 res.status(500).json({ success: false, error: "Relogin trigger failed" });
 }
 });

 // Confirm step — feed ENTER to the in-flight `notebooklm login`
 // subprocess once the user has actually finished the Google sign-in
 // in their browser. (Without this, cookies are never saved.)
 app.post("/api/auth/notebooklm/relogin/confirm", express.json(), async (req, res) => {
 try {
 const data = await proxyToFlask("/api/auth/notebooklm/relogin/confirm", {
 method: "POST", body: req.body || {},
 });
 res.json(data);
 } catch (error: any) {
 console.error("Auth relogin confirm error:", error.message);
 res.status(500).json({ success: false, error: "Relogin confirm failed" });
 }
 });

 app.get("/api/auth/notebooklm/relogin/status", async (_req, res) => {
 try {
 const data = await proxyToFlask("/api/auth/notebooklm/relogin/status");
 res.json(data);
 } catch (error: any) {
 console.error("Auth relogin status error:", error.message);
 res.status(500).json({ success: false, error: "Relogin status failed" });
 }
 });

 // ============================================
 // NEW: County meetings endpoint (cached)
 // ============================================
 app.get("/api/calendar/county/:countyName/meetings", async (req, res) => {
 try {
 const data = await proxyToFlask(
 `/api/county/${encodeURIComponent(req.params.countyName)}/meetings`
 );
 res.json(data);
 } catch (error: any) {
 console.error("County meetings error:", error.message);
 res.status(500).json({ success: false, error: "County meetings failed" });
 }
 });

 // ============================================
 // NEW: Council members endpoint
 // ============================================
 app.get("/api/calendar/council/:cityName", async (req, res) => {
 try {
 const data = await proxyToFlask(
 `/api/council/${encodeURIComponent(req.params.cityName)}`
 );
 res.json(data);
 } catch (error: any) {
 console.error("Council error:", error.message);
 res.status(500).json({ success: false, error: "Council lookup failed" });
 }
 });

 // ============================================
 // NEW: All cities endpoint
 // ============================================
 app.get("/api/calendar/cities", async (req, res) => {
 try {
 const data = await proxyToFlask("/api/cities");
 res.json(data);
 } catch (error: any) {
 console.error("Cities error:", error.message);
 res.status(500).json({ success: false, error: "Cities lookup failed" });
 }
 });

 // (Parser scraper daemon retired — the /api/calendar/pipeline/*
 // proxies to Flask's /api/pipeline/* daemon-control endpoints are gone
 // along with the daemon itself. Per-city on-demand scrape survives via
 // /api/calendar/events → Flask /scrape/<city> per PR #46's password gate.)

 // ============================================
 // Owner-only live scrape / draft-cache endpoint. Flask's /scrape route
 // enforces _require_owner(); proxyJsonAuth forwards the session cookie and
 // preserves its 401/403 before any cache read or scrape can run.
 // ============================================
 app.post("/api/calendar/events", (req, res) => {
 const { cityName, refresh, includeDrafts } = req.body;
 if (!cityName || typeof cityName !== "string") {
 return res.status(400).json({
 success: false,
 error: "City name is required",
 events: [],
 });
 }

 const query = new URLSearchParams();
 if (refresh === true || refresh === "true") query.set("refresh", "true");
 if (includeDrafts === true || includeDrafts === "true") {
 query.set("include_drafts", "true");
 }
 const scrapePassword = req.get("X-Scrape-Password");
 const headers = scrapePassword
 ? { "X-Scrape-Password": scrapePassword }
 : undefined;
 const suffix = query.toString();
 return proxyJsonAuth(
 req,
 res,
 `/scrape/${encodeURIComponent(cityName)}${suffix ? `?${suffix}` : ""}`,
 { method: "GET", headers, timeoutMs: 300_000 },
 );
 });

 // ============================================
 // Settings endpoints (LLM provider configuration + API keys)
 // Session-31 (2026-07-04) — auth-audit remediation. All three
 // methods swapped to proxyJsonAuth to forward Cookie for the Flask
 // owner-check. GET leaks last-4 of keys, POST overwrites full keys,
 // DELETE wipes all settings — every method is owner-only.
 // ============================================
 app.get("/api/settings", (req, res) =>
 proxyJsonAuth(req, res, "/api/settings"),
 );

 app.post("/api/settings", express.json({ limit: "1mb" }), (req, res) =>
 proxyJsonAuth(req, res, "/api/settings"),
 );

 app.delete("/api/settings", (req, res) =>
 proxyJsonAuth(req, res, "/api/settings"),
 );

 // ============================================
 // Orchestrator autonomy gate — the dev/operator control surface
 // for the digital-twin orchestrator's graduated autonomy. GET reads the
 // gate; POST flips one capability's on-its-own toggle or sets its audit
 // note. The orchestrator (when built) reads this to know its envelope;
 // forward the agent-role so that read self-attributes in Flask's log.
 // ============================================
 app.get("/api/orchestrator/autonomy", async (req, res) => {
 try {
 const data = await proxyToFlask("/api/orchestrator/autonomy", { req });
 res.json(data);
 } catch (error: any) {
 console.error("Orchestrator-autonomy GET error:", error.message);
 res.status(500).json({ ok: false, error: "Could not load autonomy gate" });
 }
 });

 // Session-31 (2026-07-04) — auth-audit remediation. POST controls
 // spend-triggering capability toggles. Owner-only via proxyJsonAuth
 // cookie forwarding.
 app.post("/api/orchestrator/autonomy", express.json({ limit: "1mb" }), (req, res) =>
 proxyJsonAuth(req, res, "/api/orchestrator/autonomy"),
 );

 // ============================================
 //ingestion governor — the low-hum machine's metering board.
 // GET reads a city's rate/progress (read-only; the orchestrator's rung-1
 // surface — it never advances the queue). POST sets the videos/day
 // calibration dial. Forward the agent-role so reads self-attribute in Flask.
 // ============================================
 app.get("/api/ingestion/governor", async (req, res) => {
 try {
 const city = typeof req.query.city === "string" ? req.query.city : "";
 const qs = city ? `?city=${encodeURIComponent(city)}` : "";
 const data = await proxyToFlask(`/api/ingestion/governor${qs}`, { req });
 res.json(data);
 } catch (error: any) {
 console.error("Ingestion-governor GET error:", error.message);
 res.status(500).json({ ok: false, error: "Could not load ingestion governor" });
 }
 });

 // Session-31 (2026-07-04) — auth-audit remediation. POST writes the
 // $ budget ceiling + compute-per-video cost dial. Owner-only via
 // proxyJsonAuth cookie forwarding.
 app.post("/api/ingestion/calibration", express.json({ limit: "1mb" }), (req, res) =>
 proxyJsonAuth(req, res, "/api/ingestion/calibration"),
 );

 // ============================================
 //the Guide — present-tense directory of live civic broadcasts.
 // GET reads the live_streams cache (read-only; guide_detector.py populates
 // it via calendar-gated YouTube live detection). Forward the agent-role so
 // reads self-attribute in Flask.
 // ============================================
 app.get("/api/guide", async (req, res) => {
 try {
 const data = await proxyToFlask("/api/guide", { req });
 res.json(data);
 } catch (error: any) {
 console.error("Guide GET error:", error.message);
 res.status(500).json({ ok: false, live: [], count: 0, error: "Could not load the Guide" });
 }
 });

 // ============================================
 // HQ skybox traffic events (post-Logstalgia pivot 2026-05-29).
 // The GET is a long-lived Server-Sent Events stream — bypasses
 // proxyToFlask's JSON consumer + 30s timeout entirely and pipes Flask's
 // response body straight through. The POST inject is a standard JSON
 // proxy for the owner-gated mock-traffic panel (visible only when
 // isOwner; Flask itself doesn't gate, edge gates in prod).
 // ============================================
 app.get("/api/hq/traffic-events", async (req, res) => {
 res.setHeader("Content-Type", "text/event-stream");
 res.setHeader("Cache-Control", "no-cache");
 res.setHeader("Connection", "keep-alive");
 res.setHeader("X-Accel-Buffering", "no");
 res.flushHeaders();

 const controller = new AbortController();
 const onClose = () => controller.abort();
 req.on("close", onClose);

 try {
 const upstream = await fetch(`${PARSER_API_URL}/api/hq/traffic-events`, {
 signal: controller.signal,
 });
 if (!upstream.ok || !upstream.body) {
 res.end();
 return;
 }
 const reader = upstream.body.getReader();
 while (true) {
 const { done, value } = await reader.read();
 if (done) break;
 res.write(value);
 }
 res.end();
 } catch (err: any) {
 if (err.name !== "AbortError") {
 console.error("[SSE proxy] /api/hq/traffic-events error:", err.message);
 }
 try { res.end(); } catch { /* already closed */ }
 } finally {
 req.off("close", onClose);
 }
 });

 app.post("/api/hq/traffic-events/inject", (req, res) =>
 proxyJsonAuth(req, res, "/api/hq/traffic-events/inject"),
 );

 // Parser test-results persistence. RR-8 posture: the save was an ungated
 // Express-local file write — any anonymous caller could overwrite it. Both
 // now proxy to Flask, where the write is gated by `_require_owner()`;
 // proxyJsonAuth forwards the owner session cookie so the gate can see it.
 app.get("/api/parser-health", async (req, res) => {
 try {
 const data = await proxyToFlask("/api/parser-health", { req });
 res.json(data);
 } catch (error: any) {
 console.error("Parser-health fetch error:", error.message);
 res.status(502).json({ parsers: [], error: "Parser health unavailable" });
 }
 });
 app.post("/api/parser-results/save", (req, res) =>
 proxyJsonAuth(req, res, "/api/parser-results/save"),
 );
 app.get("/api/parser-results/load", (req, res) =>
 proxyJsonAuth(req, res, "/api/parser-results/load"),
 );

 // ── Cast page V1 () — passthrough to Flask ──────────────────
 // Flask serves the roster + per-member detail from city_intelligence
 // JSONs + council_members/member_attendance/member_quotes tables.
 // Express just proxies; no business logic here.
 app.get("/api/cast/:cityName", async (req, res) => {
 try {
 const data = await proxyToFlask(`/api/cast/${encodeURIComponent(req.params.cityName)}`);
 res.json(data);
 } catch (error: any) {
 console.error("Cast roster fetch error:", error.message);
 res.status(500).json({ success: false, error: "Cast roster fetch failed" });
 }
 });

 app.get("/api/cast/:cityName/:seatId", async (req, res) => {
 try {
 // §5b — forward the cookie so Flask recognizes the owner and returns the
 // full record; anon gets the identity-stripped DTO.
 const data = await proxyToFlask(
 `/api/cast/${encodeURIComponent(req.params.cityName)}/${encodeURIComponent(req.params.seatId)}`,
 { req }
 );
 res.json(data);
 } catch (error: any) {
 console.error("Cast member fetch error:", error.message);
 res.status(500).json({ success: false, error: "Cast member fetch failed" });
 }
 });

 // Tracked Claims Ledger — per-city list view.
 app.get("/api/ledger/:cityName", async (req, res) => {
 try {
 const qs = new URLSearchParams(
 req.query as Record<string, string>
 ).toString();
 // §5b — forward the cookie so the owner keeps the status-updater
 // attribution; anon gets it stripped.
 const data = await proxyToFlask(
 `/api/ledger/${encodeURIComponent(req.params.cityName)}${qs ? `?${qs}` : ""}`,
 { req }
 );
 res.json(data);
 } catch (error: any) {
 console.error("Ledger fetch error:", error.message);
 res.status(500).json({ success: false, error: "Ledger fetch failed" });
 }
 });

 // Truth Book Lite Layer 1) — per-member research surface.
 // Flask assembles per-topic quote swimlanes + the tracked-claims
 // accountability layer from existing tables; Express just proxies.
 app.get("/api/truth-book/:cityName/:seatId", async (req, res) => {
 try {
 // §5b — forward the cookie; the endpoint is owner-only (_require_owner),
 // so without this the owner would 401.
 const data = await proxyToFlask(
 `/api/truth-book/${encodeURIComponent(req.params.cityName)}/${encodeURIComponent(req.params.seatId)}`,
 { req }
 );
 res.json(data);
 } catch (error: any) {
 console.error("Truth book fetch error:", error.message);
 res.status(500).json({ success: false, error: "Truth book fetch failed" });
 }
 });

 // Conversational Compiler V0Track A) — Hex-Rays UX consumer.
 // Returns one meeting's tracked_claims for rendering as typed IR
 // pseudo-code. V0 reads existing data only; Track B parser is separate.
 app.get("/api/compiler/:meetingId", async (req, res) => {
 try {
 const data = await proxyToFlask(
 `/api/compiler/${encodeURIComponent(req.params.meetingId)}`
 );
 res.json(data);
 } catch (error: any) {
 console.error("Compiler fetch error:", error.message);
 res.status(500).json({ success: false, error: "Compiler fetch failed" });
 }
 });

 // Compiler transcript endpoint (build seq item 0 reframed, Decision
 // #7a): returns the meeting's persisted Whisper word array sourced
 // from notebook_outputs.transcript_words. Drives Surface A's left
 // full-transcript pane (SPEC build seq item 4 — pending).
 app.get("/api/compiler/:meetingId/transcript", async (req, res) => {
 try {
 const data = await proxyToFlask(
 `/api/compiler/${encodeURIComponent(req.params.meetingId)}/transcript`
 );
 res.json(data);
 } catch (error: any) {
 console.error("Compiler transcript fetch error:", error.message);
 res.status(500).json({ success: false, error: "Compiler transcript fetch failed" });
 }
 });

 // — operator-triggered status flip on one tracked claim row.
 app.post("/api/tracked-claims/:claimId/status", express.json({ limit: "1mb" }), (req, res) =>
 proxyJsonAuth(req, res, `/api/tracked-claims/${req.params.claimId}/status`),
 );

 // V4 — operator-terminal CLI wrappers: build review queue +
 // ingest Gemini responses. Synchronous proxies; the build path can
 // take several minutes on a cold source-mp4 cache so we widen the
 // proxy timeout for it.
 app.post("/api/work-orders/:id/build-review-queue", express.json({ limit: "1mb" }), (req, res) =>
 proxyJsonAuth(req, res, `/api/work-orders/${req.params.id}/build-review-queue`),
 );

 // RR-8 SEC-AUTH: owner-OR-agent-token on the Flask side. proxyJsonAuth
 // forwards the owner cookie AND preserves Flask's 401/403/503 (proxyToFlask
 // masked them as a 200 with the error body). The headless fleet hits Flask
 // directly with its bearer, so it never traverses this Express route; the
 // agent_role attribution rides in the request body, which is forwarded.
 app.post("/api/work-orders/:id/ingest-responses", express.json(), (req, res) =>
 proxyJsonAuth(
 req,
 res,
 `/api/work-orders/${encodeURIComponent(req.params.id)}/ingest-responses`,
 { timeoutMs: 90_000 },
 ),
 );

 app.get("/api/operator/badges", async (_req, res) => {
 try {
 const data = await proxyToFlask("/api/operator/badges");
 res.json(data);
 } catch (error: any) {
 console.error("Operator-badges error:", error.message);
 res.status(500).json({ success: false, error: "Badges fetch failed" });
 }
 });

 ///— HQ lobby status payload. Aggregates badges + governor +
 // watcher state + escalations + work-order queue into the HQData shape the
 // HQPage renders. Strings are server-rendered from a curated template
 // table (safe-status redline — no free agent prose reaches the public
 // surface). Forward the agent-role so reads self-attribute in Flask.
 app.get("/api/hq/status", async (req, res) => {
 try {
 const data = await proxyToFlask("/api/hq/status", { req });
 res.json(data);
 } catch (error: any) {
 console.error("HQ-status error:", error.message);
 res.status(500).json({ error: "HQ status fetch failed" });
 }
 });

 //— agent escalation surface. Lists unacknowledged escalations
 // posted by employee-agents (via parsers/slack_notifier) and lets the
 // operator acknowledge them. Powers EscalationsInboxPage.
 app.get("/api/operator/pending-escalations", async (_req, res) => {
 try {
 const data = await proxyToFlask("/api/operator/pending-escalations");
 res.json(data);
 } catch (error: any) {
 console.error("Pending-escalations list error:", error.message);
 res.status(500).json({ success: false, error: "List failed" });
 }
 });

 app.post(
 "/api/operator/pending-escalations/:id/acknowledge",
 express.json(),
 async (req, res) => {
 try {
 const data = await proxyToFlask(
 `/api/operator/pending-escalations/${encodeURIComponent(req.params.id)}/acknowledge`,
 { method: "POST", body: req.body || {} }
 );
 res.json(data);
 } catch (error: any) {
 console.error("Pending-escalations acknowledge error:", error.message);
 res.status(500).json({ success: false, error: "Acknowledge failed" });
 }
 }
 );

 // V4 — source-cache cleanup. The bulky per-meeting source.mp4
 // accumulates at ~45 MB/meeting; this lets the operator clear them
 // selectively without dropping to a shell.
 app.post("/api/work-orders/:id/clear-source-cache", express.json(), async (req, res) => {
 try {
 // RR-8 fix-list — { req } forwards the owner session cookie;
 // the Flask side is now owner-gated (§ 5b two-file fix, same commit).
 const data = await proxyToFlask(
 `/api/work-orders/${encodeURIComponent(req.params.id)}/clear-source-cache`,
 { method: "POST", body: req.body || {}, req }
 );
 res.json(data);
 } catch (error: any) {
 console.error("Clear-source-cache error:", error.message);
 res.status(500).json({ success: false, error: "Clear-cache failed" });
 }
 });

 app.get("/api/operator/source-cache-size", async (_req, res) => {
 try {
 const data = await proxyToFlask("/api/operator/source-cache-size");
 res.json(data);
 } catch (error: any) {
 console.error("Source-cache-size error:", error.message);
 res.status(500).json({ success: false, error: "Cache-size fetch failed" });
 }
 });

 // V1-Batch-3 — V1 launch progress dashboard. Read-only; cheap.
 // Per-city in-window meeting counts + WO state breakdown + URL-gap board.
 app.get("/api/v1-launch/progress", async (req, res) => {
 try {
 const qs = new URLSearchParams(req.query as Record<string, string>).toString();
 const data = await proxyToFlask(
 `/api/v1-launch/progress${qs ? `?${qs}` : ""}`,
 { req }
 );
 res.json(data);
 } catch (error: any) {
 console.error("V1 launch progress error:", error.message);
 res.status(500).json({ success: false, error: "V1 launch progress fetch failed" });
 }
 });

 // — Vocabulary Inbox: list pending promotions + promote/reject one.
 // Forwards X-Zspan-Agent-Role per route (via `req`) so when the Vocabulary
 // Curator agent pilot comes online, its identity propagates through Express
 // to Flask's audit log.
 app.get("/api/vocabulary-inbox", async (req, res) => {
 try {
 const qs = new URLSearchParams(req.query as Record<string, string>).toString();
 const data = await proxyToFlask(
 `/api/vocabulary-inbox${qs ? `?${qs}` : ""}`,
 { req }
 );
 res.json(data);
 } catch (error: any) {
 console.error("Vocabulary-inbox list error:", error.message);
 res.status(500).json({ success: false, error: "List failed" });
 }
 });

 app.post("/api/vocabulary-inbox/promote", express.json({ limit: "1mb" }), (req, res) =>
 proxyJsonAuth(req, res, "/api/vocabulary-inbox/promote"),
 );

 app.post("/api/vocabulary-inbox/reject", express.json({ limit: "1mb" }), (req, res) =>
 proxyJsonAuth(req, res, "/api/vocabulary-inbox/reject"),
 );

 // — agent counter-proposal. RR-8 SEC-AUTH: owner-OR-agent-token on
 // Flask; proxyJsonAuth forwards the owner cookie + preserves the auth status
 // (the agent_role attribution rides in the forwarded request body).
 app.post(
 "/api/vocabulary-inbox/:id/agent-propose",
 express.json(),
 (req, res) =>
 proxyJsonAuth(
 req,
 res,
 `/api/vocabulary-inbox/${encodeURIComponent(req.params.id)}/agent-propose`,
 { timeoutMs: 30_000 },
 ),
 );

 // Read-only city dictionary inspection. RR-8 SEC-SEAL-1 + SEC-AUTH-2: the
 // city_intelligence corpus is sealed (owner-OR-agent-token on Flask).
 // proxyJsonAuth forwards the owner cookie and preserves the 401 (proxyToFlask
 // masked it as a 200 carrying the error body). The Vocabulary Curator agent
 // reads it by hitting Flask directly with its bearer, not via this route.
 app.get("/api/city/:slug", (req, res) =>
 proxyJsonAuth(
 req,
 res,
 `/api/city/${encodeURIComponent(req.params.slug)}`,
 { timeoutMs: 30_000 },
 ),
 );

 // V4 — disputed-quotes resolution: list + resolve. Forwards the
 //agent-role header (req) so server-side log lines distinguish
 // agent-driven actions from operator-driven ones.
 app.get("/api/disputed-quotes", async (req, res) => {
 try {
 const qs = new URLSearchParams(req.query as Record<string, string>).toString();
 const data = await proxyToFlask(
 `/api/disputed-quotes${qs ? `?${qs}` : ""}`,
 { req }
 );
 res.json(data);
 } catch (error: any) {
 console.error("Disputed-quotes list error:", error.message);
 res.status(500).json({ success: false, error: "List failed" });
 }
 });

 app.post("/api/disputed-quotes/:quoteId/resolve", express.json({ limit: "1mb" }), (req, res) =>
 proxyJsonAuth(req, res, `/api/disputed-quotes/${req.params.quoteId}/resolve`),
 );

 // extension — Disputed Quotes Reviewer counter-proposal. Mirrors the
 // vocabulary-inbox agent-propose proxy. RR-8 SEC-AUTH: owner-OR-agent-token
 // on Flask; proxyJsonAuth forwards the owner cookie + preserves the auth
 // status (the agent_role attribution rides in the forwarded request body).
 app.post(
 "/api/disputed-quotes/:quoteId/agent-propose",
 express.json(),
 (req, res) =>
 proxyJsonAuth(
 req,
 res,
 `/api/disputed-quotes/${encodeURIComponent(req.params.quoteId)}/agent-propose`,
 { timeoutMs: 30_000 },
 ),
 );

 // V-Op-2 (Tier 2 operator-only) — voice-prime cross-meeting search.
 // The Flask blueprint behind these endpoints lives in the gitignored
 // parsers/operator_only/ directory; on a public clone the blueprint
 // isn't registered and Flask responds 404. Endpoints are owner-gated
 // server-side regardless.
 //
 // MUST forward the session cookie (the generic proxyToFlask strips
 // cookies and the gate at the Flask end uses _current_user_from_cookie).
 // Same shape as V1.5-OperatorSearch-1 / interpret + execute. Failure
 // mode if you forget the cookie: Flask sees no user, gate rejects,
 // 403 with operator_only-principal-only — looks identical to a real
 // gate failure but it's just the proxy losing the auth.
 app.get("/api/operator_only/voice-library", async (req, res) => {
 try {
 const headers: Record<string, string> = {};
 const cookie = req.headers.cookie;
 if (typeof cookie === "string" && cookie.length) {
 headers["Cookie"] = cookie;
 }
 const response = await fetch(
 `${PARSER_API_URL}/api/operator_only/voice-library`,
 { method: "GET", headers },
 );
 const data = await response.json();
 res.status(response.status).json(data);
 } catch (error: any) {
 console.error("voice-library proxy error:", error.message);
 res.status(500).json({ success: false, error: "voice-library fetch failed" });
 }
 });

 app.post("/api/operator_only/voice-search", express.json(), async (req, res) => {
 try {
 const controller = new AbortController();
 const timeoutId = setTimeout(() => controller.abort(), 360_000);
 const headers: Record<string, string> = {
 "Content-Type": "application/json",
 };
 const cookie = req.headers.cookie;
 if (typeof cookie === "string" && cookie.length) {
 headers["Cookie"] = cookie;
 }
 try {
 const response = await fetch(
 `${PARSER_API_URL}/api/operator_only/voice-search`,
 {
 method: "POST",
 headers,
 body: JSON.stringify(req.body || {}),
 signal: controller.signal,
 },
 );
 clearTimeout(timeoutId);
 const data = await response.json();
 res.status(response.status).json(data);
 } finally {
 clearTimeout(timeoutId);
 }
 } catch (error: any) {
 console.error("voice-search proxy error:", error.message);
 res.status(504).json({ success: false, error: "voice-search failed or timed out" });
 }
 });

 // Phase 2 D-Build-B — Speaker Roster Review queue (SpeakerRosterReviewPage).
 // Flask shipped these endpoints 2026-06-24 but the Express proxy
 // handlers were missed at the time, so every fetch fell through to
 // Vite's index.html catch-all and JSON.parse threw "Unexpected token
 // '<', "<!doctype "...". Same shape as the V1.5-BYOK-Shell-1 / Express
 // proxy gap caught 2026-06-25; per [[express-flask-route-parity-discipline]]
 // every new Flask /api/* needs an Express handler in the same commit.
 app.get("/api/speaker-roster/pending-review", async (req, res) => {
 try {
 const qs = new URLSearchParams(req.query as Record<string, string>).toString();
 const data = await proxyToFlask(
 `/api/speaker-roster/pending-review${qs ? `?${qs}` : ""}`,
 { req },
 );
 res.json(data);
 } catch (error: any) {
 console.error("Speaker-roster pending-review error:", error.message);
 res.status(500).json({ success: false, error: "List failed" });
 }
 });

 app.get("/api/speaker-roster/meeting/:meetingId", async (req, res) => {
 try {
 const data = await proxyToFlask(
 `/api/speaker-roster/meeting/${encodeURIComponent(req.params.meetingId)}`,
 { req },
 );
 res.json(data);
 } catch (error: any) {
 console.error("Speaker-roster meeting fetch error:", error.message);
 res.status(500).json({ success: false, error: "Meeting fetch failed" });
 }
 });

 app.post("/api/speaker-roster/:rowId/confirm", express.json({ limit: "1mb" }), (req, res) =>
 proxyJsonAuth(req, res, `/api/speaker-roster/${req.params.rowId}/confirm`),
 );

 app.post("/api/speaker-roster/:rowId/override", express.json({ limit: "1mb" }), (req, res) =>
 proxyJsonAuth(req, res, `/api/speaker-roster/${req.params.rowId}/override`),
 );

 app.post("/api/speaker-roster/:rowId/anonymous", express.json({ limit: "1mb" }), (req, res) =>
 proxyJsonAuth(req, res, `/api/speaker-roster/${req.params.rowId}/anonymous`),
 );

 // speaker-roster-page-redesign-V0 — per-cluster turn-excerpt samples for
 // the operator-identify-by-listening flow. Flask ships the substantive
 // logic (cluster_roster_mapper.top_n_turn_excerpts walking Qdrant
 // chunks); Express just proxies + carries the optional ?n=<N> query
 // param. Same parity-discipline pattern as the 5 sibling handlers above.
 app.get("/api/speaker-roster/:rowId/cluster-samples", async (req, res) => {
 try {
 const qs = new URLSearchParams(req.query as Record<string, string>).toString();
 const data = await proxyToFlask(
 `/api/speaker-roster/${encodeURIComponent(req.params.rowId)}/cluster-samples${qs ? `?${qs}` : ""}`,
 { req },
 );
 res.json(data);
 } catch (error: any) {
 console.error("Speaker-roster cluster-samples error:", error.message);
 res.status(500).json({ success: false, error: "Cluster-samples fetch failed" });
 }
 });

 // — operator-side affordance: open the meeting's local
 // review_queue folder in OS file explorer. Single-user dev only;
 // retired when OAuth'd volunteer review comes online.
 app.post("/api/local-fs/open-review-queue", express.json({ limit: "1mb" }), (req, res) =>
 proxyJsonAuth(req, res, "/api/local-fs/open-review-queue"),
 );

 // /api/quotes/clean proxy removed with its Flask route (RR-8 fix-list,
 //— orphaned spend surface, zero callers). Library path unchanged.

 // video matcher — runs the YouTube channel-to-video matcher for a
 // city and auto-applies high-confidence matches to work_orders. Uses a
 // longer timeout because the matcher hits the YouTube Data API + iterates
 // every recent meeting.
 app.post("/api/work-orders/match-videos/:cityName", express.json(), async (req, res) => {
 try {
 const data = await proxyToFlask(
 `/api/work-orders/match-videos/${encodeURIComponent(req.params.cityName)}`,
 { method: "POST", body: req.body || {}, timeoutMs: 90_000, req },
 );
 res.json(data);
 } catch (error: any) {
 console.error("Match-videos error:", error.message);
 res.status(500).json({ success: false, error: "Match-videos failed" });
 }
 });

 // Prompt-provenance endpoint — surfaces the exact prompt body used to
 // generate each text output (key_decisions / synopsis / quotes /
 // newsletter / etc) so the broadcast-page ⓘ info-icons can show it.
 app.get("/api/prompts/:name", async (req, res) => {
 return proxyJsonAuth(
 req,
 res,
 `/api/prompts/${encodeURIComponent(req.params.name)}`,
 { timeoutMs: 10_000 },
 );
 });

 //cascade-break: commits an operator-approved subset of matches
 // returned by /match-videos preview mode (apply:false). Zero LLM spend.
 app.post("/api/work-orders/promote-matches", express.json(), async (req, res) => {
 try {
 const data = await proxyToFlask("/api/work-orders/promote-matches", {
 method: "POST", body: req.body || {}, timeoutMs: 30_000,
 });
 res.json(data);
 } catch (error: any) {
 console.error("Promote-matches error:", error.message);
 res.status(500).json({ success: false, error: "Promote-matches failed" });
 }
 });

 app.get("/api/cities/with-channels", async (_req, res) => {
 try {
 const data = await proxyToFlask("/api/cities/with-channels");
 res.json(data);
 } catch (error: any) {
 console.error("Cities-with-channels error:", error.message);
 res.status(500).json({ success: false, error: "Cities fetch failed" });
 }
 });

 // ── Notebook GC — see parsers/api_server.py for safety doc ──
 app.get("/api/notebooks", async (_req, res) => {
 try {
 res.json(await proxyToFlask("/api/notebooks"));
 } catch (error: any) {
 console.error("Notebooks list error:", error.message);
 res.status(500).json({ success: false, error: "Notebooks list failed" });
 }
 });

 app.get("/api/notebooks/protected", async (_req, res) => {
 try {
 res.json(await proxyToFlask("/api/notebooks/protected"));
 } catch (error: any) {
 console.error("Notebooks protected list error:", error.message);
 res.status(500).json({ success: false, error: "Protected list failed" });
 }
 });

 app.get("/api/notebooks/audit", async (req, res) => {
 try {
 const qs = new URLSearchParams(req.query as any).toString();
 res.json(
 await proxyToFlask(`/api/notebooks/audit${qs ? `?${qs}` : ""}`)
 );
 } catch (error: any) {
 console.error("Notebooks audit error:", error.message);
 res.status(500).json({ success: false, error: "Audit fetch failed" });
 }
 });

 app.post("/api/notebooks/:notebookId/protect", express.json(), async (req, res) => {
 try {
 res.json(
 await proxyToFlask(
 `/api/notebooks/${encodeURIComponent(req.params.notebookId)}/protect`,
 { method: "POST", body: req.body || {} }
 )
 );
 } catch (error: any) {
 console.error("Notebook protect error:", error.message);
 res.status(500).json({ success: false, error: "Protect failed" });
 }
 });

 app.post("/api/notebooks/:notebookId/unprotect", express.json(), async (req, res) => {
 try {
 res.json(
 await proxyToFlask(
 `/api/notebooks/${encodeURIComponent(req.params.notebookId)}/unprotect`,
 { method: "POST", body: req.body || {} }
 )
 );
 } catch (error: any) {
 console.error("Notebook unprotect error:", error.message);
 res.status(500).json({ success: false, error: "Unprotect failed" });
 }
 });

 // Delete uses the longer timeout because it calls NotebookLM's API
 // (slow + rate-limited). Dry-run is fast (no API call) but still uses
 // the same path for simplicity.
 app.post("/api/notebooks/:notebookId/delete", express.json(), async (req, res) => {
 try {
 res.json(
 await proxyToFlask(
 `/api/notebooks/${encodeURIComponent(req.params.notebookId)}/delete`,
 { method: "POST", body: req.body || {}, timeoutMs: 60_000 }
 )
 );
 } catch (error: any) {
 console.error("Notebook delete error:", error.message);
 res.status(500).json({ success: false, error: "Delete failed" });
 }
 });

 // In development, integrate with Vite (AFTER API routes)
 if (process.env.NODE_ENV === "development") {
 const { createServer: createViteServer } = await import("vite");
 const vite = await createViteServer({
 server: { middlewareMode: true },
 appType: "spa",
 });
 app.use(vite.middlewares);
 }

 // In production, serve static files
 if (process.env.NODE_ENV === "production") {
 const staticPath = path.resolve(__dirname, "public");
 app.use(express.static(staticPath));

 app.get("*", (_req, res) => {
 res.sendFile(path.join(staticPath, "index.html"));
 });
 }

 const port = process.env.PORT || 3000;

 server.listen(port, () => {
 console.log(`Server running on http://localhost:${port}/`);
 });
}

startServer().catch((error) => {
 console.error(error);
 process.exitCode = 1;
});
