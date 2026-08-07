import { createHash } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
 DEFAULT_BYOK_SETTINGS,
 LOCAL_WORKSPACE_PROVIDER,
 buildUserMessage,
 executeByokQuery,
 type ByokConfig,
} from "./byok";

const SYSTEM_PROMPT = "System π\r\nline";
const USER_MESSAGE =
 "CURRENT QUESTION: What changed — exactly?\r\nNext\n\n" +
 "RETRIEVED CONTEXT — chunks from meeting_id=42:\n---\n" +
 "[chunk_index=7 timecode=00:01 start_seconds=1.2]\n" +
 "Café line\r\n--- embedded delimiter\n\n" +
 "[chunk_index=8 timecode=01:01 start_seconds=61.3]\n" +
 "Second — chunk\n---";
const EXPECTED_HASH =
 "6824d45f403e724b714c9b98cccd11939dd389d177c4f97c15d37abb82ae4191";
const USER_BYTES_HEX =
 "43555252454e54205155455354494f4e3a2057686174206368616e67656420" +
 "e280942065786163746c793f0d0a4e6578740a0a5245545249455645442043" +
 "4f4e5445585420e28094206368756e6b732066726f6d206d656574696e675f" +
 "69643d34323a0a2d2d2d0a5b6368756e6b5f696e6465783d372074696d65" +
 "636f64653d30303a30312073746172745f7365636f6e64733d312e325d0a" +
 "436166c3a9206c696e650d0a2d2d2d20656d6265646465642064656c696d" +
 "697465720a0a5b6368756e6b5f696e6465783d382074696d65636f64653d" +
 "30313a30312073746172745f7365636f6e64733d36312e335d0a5365636f" +
 "6e6420e28094206368756e6b0a2d2d2d";

const chunks = [
 {
 chunk_index: 7,
 vector_id: "vector-7",
 body: "client legacy body",
 start_seconds: 1.25,
 end_seconds: 20,
 speaker_turns: null,
 score: 0.9,
 },
];

function len8(value: Buffer): Buffer {
 const prefix = Buffer.alloc(8);
 prefix.writeBigUInt64BE(BigInt(value.byteLength));
 return prefix;
}

function fixtureHash(): string {
 const domain = Buffer.from("zspan:librarian-synthesis-envelope", "utf8");
 const version = Buffer.from("envelope-v1", "utf8");
 const system = Buffer.from(SYSTEM_PROMPT, "utf8");
 const user = Buffer.from(USER_MESSAGE, "utf8");
 return createHash("sha256")
 .update(
 Buffer.concat([
 domain,
 len8(version),
 version,
 len8(system),
 system,
 len8(user),
 user,
 ]),
 )
 .digest("hex");
}

function ragPayload(envelope: unknown = {
 system_prompt: SYSTEM_PROMPT,
 user_message: USER_MESSAGE,
 envelope_hash: EXPECTED_HASH,
 envelope_version: "envelope-v1",
 expires_at_utc: "2026-07-29T08:30:00Z",
 run_id: "zspan-rag-test-run",
}) {
 return {
 success: true,
 meeting_id: 42,
 query: "What changed?",
 chunks,
 provenance: {
 run_id: "zspan-rag-test-run",
 vector_ids: ["vector-7"],
 prompt_template_hash: "sha256:test",
 prompt_template_version: "test",
 query_hash: "sha256:test",
 timestamp_utc: "2026-07-29T08:20:00Z",
 },
 recommended_system_prompt: "legacy prompt ignored by relay",
 synthesis_envelope: envelope,
 };
}

const openAiConfig: ByokConfig = {
 provider: "openai-gpt-4o-mini",
 key: "sk-test-not-real",
 fingerprint: "sk-t...real",
 validatedAt: "2026-07-29T00:00:00Z",
};

afterEach(() => {
 vi.unstubAllGlobals();
 vi.restoreAllMocks();
});

describe("Librarian synthesis envelope", () => {
 it("matches the Python fixture bytes and domain-separated hash", () => {
 expect(Buffer.from(SYSTEM_PROMPT, "utf8").toString("hex")).toBe(
 "53797374656d20cf800d0a6c696e65",
 );
 expect(Buffer.from(USER_MESSAGE, "utf8").byteLength).toBe(259);
 expect(Buffer.from(USER_MESSAGE, "utf8").toString("hex")).toBe(
 USER_BYTES_HEX,
 );
 expect(fixtureHash()).toBe(EXPECTED_HASH);
 });

 it("forwards server strings verbatim with version and run_id one-shot", async () => {
 const fetchMock = vi
 .fn()
 .mockResolvedValueOnce(
 new Response(JSON.stringify(ragPayload()), { status: 200 }),
 )
 .mockResolvedValueOnce(
 new Response(
 JSON.stringify({
 choices: [{ message: { content: "answer" } }],
 usage: { prompt_tokens: 10, completion_tokens: 2 },
 }),
 { status: 200 },
 ),
 );
 vi.stubGlobal("fetch", fetchMock);

 await executeByokQuery(
 42,
 "What changed?",
 openAiConfig,
 DEFAULT_BYOK_SETTINGS,
 );

 const relayBody = JSON.parse(
 String(fetchMock.mock.calls[1][1]?.body),
 );
 expect(relayBody.system_prompt).toBe(SYSTEM_PROMPT);
 expect(relayBody.user_message).toBe(USER_MESSAGE);
 expect(relayBody.envelope_version).toBe("envelope-v1");
 expect(relayBody.run_id).toBe("zspan-rag-test-run");
 });

 it("forwards the same envelope verbatim on the streaming relay", async () => {
 const encoder = new TextEncoder();
 const stream = new ReadableStream<Uint8Array>({
 start(controller) {
 controller.enqueue(
 encoder.encode(
 'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n' +
 "data: [DONE]\n\n",
 ),
 );
 controller.close();
 },
 });
 const fetchMock = vi
 .fn()
 .mockResolvedValueOnce(
 new Response(JSON.stringify(ragPayload()), { status: 200 }),
 )
 .mockResolvedValueOnce(
 new Response(stream, {
 status: 200,
 headers: { "Content-Type": "text/event-stream" },
 }),
 );
 vi.stubGlobal("fetch", fetchMock);

 const result = await executeByokQuery(
 42,
 "What changed?",
 openAiConfig,
 DEFAULT_BYOK_SETTINGS,
 { onDelta: vi.fn() },
 );

 expect(result.answer).toBe("answer");
 const relayBody = JSON.parse(
 String(fetchMock.mock.calls[1][1]?.body),
 );
 expect(relayBody.system_prompt).toBe(SYSTEM_PROMPT);
 expect(relayBody.user_message).toBe(USER_MESSAGE);
 expect(relayBody.run_id).toBe("zspan-rag-test-run");
 });

 it("stops before relay dispatch when the envelope is malformed", async () => {
 const fetchMock = vi.fn().mockResolvedValueOnce(
 new Response(
 JSON.stringify(
 ragPayload({
 system_prompt: SYSTEM_PROMPT,
 user_message: USER_MESSAGE,
 envelope_hash: "not-a-hash",
 envelope_version: "envelope-v1",
 expires_at_utc: "2026-07-29T08:30:00Z",
 run_id: "zspan-rag-test-run",
 }),
 ),
 { status: 200 },
 ),
 );
 vi.stubGlobal("fetch", fetchMock);

 await expect(
 executeByokQuery(
 42,
 "What changed?",
 openAiConfig,
 DEFAULT_BYOK_SETTINGS,
 ),
 ).rejects.toThrow("malformed synthesis envelope");
 expect(fetchMock).toHaveBeenCalledTimes(1);
 });

 it("retains the legacy builder for Gemini direct and local workspace", async () => {
 const expectedLegacy = buildUserMessage(
 42,
 "What changed?",
 chunks,
 );
 // JS's legacy rounding remains 1.3; the relay never uses this string.
 expect(expectedLegacy).toContain("start_seconds=1.3");

 const geminiFetch = vi
 .fn()
 .mockResolvedValueOnce(
 new Response(JSON.stringify(ragPayload()), { status: 200 }),
 )
 .mockResolvedValueOnce(
 new Response(
 JSON.stringify({
 candidates: [{ content: { parts: [{ text: "gemini" }] } }],
 }),
 { status: 200 },
 ),
 );
 vi.stubGlobal("fetch", geminiFetch);
 await executeByokQuery(
 42,
 "What changed?",
 {
 ...openAiConfig,
 provider: "google-gemini-2.5-flash",
 },
 DEFAULT_BYOK_SETTINGS,
 );
 const geminiBody = JSON.parse(
 String(geminiFetch.mock.calls[1][1]?.body),
 );
 expect(geminiBody.contents[0].parts[0].text).toBe(expectedLegacy);

 const localFetch = vi
 .fn()
 .mockResolvedValueOnce(
 new Response(JSON.stringify(ragPayload()), { status: 200 }),
 )
 .mockResolvedValueOnce(
 new Response(
 JSON.stringify({
 success: true,
 answer: "local",
 provider_id: LOCAL_WORKSPACE_PROVIDER,
 }),
 { status: 200 },
 ),
 );
 vi.stubGlobal("fetch", localFetch);
 await executeByokQuery(
 42,
 "What changed?",
 {
 ...openAiConfig,
 provider: LOCAL_WORKSPACE_PROVIDER,
 key: "",
 },
 DEFAULT_BYOK_SETTINGS,
 );
 const localBody = JSON.parse(
 String(localFetch.mock.calls[1][1]?.body),
 );
 expect(localBody.user_message).toBe(expectedLegacy);
 });
});
