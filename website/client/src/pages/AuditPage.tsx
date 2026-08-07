/**
 *— /audit stub. Placeholder for the comprehensive verification
 * surface that V1.5-Audit-Page-1 will fully build out. Lives here at
 * V0 so the "Learn more →" link from /scan has somewhere honest to land
 * instead of 404'ing.
 *
 * When V1.5-Audit-Page-1 fires, this file gets replaced with the full
 * cryptographic-receipt + BYOK provenance + camera/upload-shortcut
 * education surface.
 */
export default function AuditPage() {
 return (
 <div className="min-h-screen bg-[#0A0A0A] text-white">
 <div className="max-w-2xl mx-auto px-5 py-10">
 <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-[#6B6B72] mb-4">
 Z-SPAN · Audit
 </div>
 <h1 className="text-[24px] font-semibold leading-tight mb-4">
 How Z-SPAN verifies its own outputs.
 </h1>
 <p className="text-[14px] text-gray-300 leading-relaxed mb-6">
 Every Z-SPAN-generated output — a key decision, a quote, a
 newsletter passage — carries a small colored ribbon. The ribbon
 encodes a cryptographic token derived from the underlying
 meeting + output type. The token is checked against a public
 audit log: present means a real Z-SPAN output with that token
 exists; absent means Z-SPAN has no record of any such output.
 The token identifies the output — it doesn't authenticate the
 exact text sitting next to it.
 </p>
 {/* (2026-07-01) + ADDENDUM (2026-07-14): honest-attestation
 framing. This check is platform attestation — Z-SPAN confirming
 a generation RUN RECORD exists — stated plainly so nobody reads
 it as independent proof of truth OR as byte-integrity of the
 displayed text (no output digest binds content to a run). */}
 <div className="border border-[#F5A524]/25 rounded-lg p-4 mb-6 bg-[#F5A524]/[0.04]">
 <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-[#F5A524]/80 mb-2">
 What a match does — and doesn't — prove
 </div>
 <p className="text-[13px] text-gray-300 leading-relaxed mb-2">
 A match means <span className="text-white font-medium">Z-SPAN's
 pipeline recorded this generation run</span> — what was retrieved,
 with which prompt, and when — which helps tell genuine outputs
 from fabricated screenshots. It does{" "}
 <span className="text-white font-medium">not</span> by itself
 prove the text you're reading is unchanged since generation, and
 it doesn't prove the content is true or complete: you're asking
 our server about our server. For claims about what was actually
 said, the source recording is always linked — and where the city
 serves a direct video file, anyone can re-download it and re-check
 the SHA256 fingerprint without trusting us (recordings hosted on
 platforms that re-encode video, like YouTube, carry a fingerprint
 of Z-SPAN's archived copy instead — a custody record, not an
 independent proof). That direct-file check is the independently
 verifiable one.
 </p>
 <p className="text-[12px] text-gray-500 leading-relaxed">
 Z-SPAN is an experimental civic-data project. AI-generated
 outputs can contain errors; every output links back to the
 verbatim source so you can check for yourself. Cryptographic
 signing of verification responses (so third parties can verify
 without asking us) is committed future work.
 </p>
 </div>
 <div className="border border-white/10 rounded-lg p-5 mb-6 bg-[#141416]">
 <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-[#6B6B72] mb-2">
 The verification chain
 </div>
 <ol className="text-[13px] text-gray-300 space-y-2 list-decimal list-inside">
 <li>
 A ribbon's colored blocks decode to a deterministic 8-character
 token (40 bits, base32).
 </li>
 <li>
 The token is looked up against{" "}
 <code className="text-[12px] bg-white/5 px-1 rounded">
 /api/verify-run/{"{run_id}"}
 </code>
 .
 </li>
 <li>
 A match returns the source meeting, output type, and
 generation provenance. A miss returns honest-empty —
 ⚠️ "Z-SPAN has no record of this" (the strongest claim a
 lookup can make in the negative direction).
 </li>
 </ol>
 </div>
 <h2 className="text-[16px] font-semibold mb-3">
 Two shortcuts for verifying any ribbon you see
 </h2>
 <ul className="text-[13px] text-gray-300 space-y-3 mb-6">
 <li className="border border-white/10 rounded-lg p-3">
 <span className="font-medium text-white">Scan with your camera.</span>{" "}
 Point any phone at a Z-SPAN ribbon (on screen, on paper, on a
 sticker) and the audit chain runs live. Open at{" "}
 <a
 href="/?view=scan"
 className="text-[#F5A524] hover:underline underline-offset-4"
 >
 zspan.org/scan
 </a>
 .
 </li>
 <li className="border border-white/10 rounded-lg p-3">
 <span className="font-medium text-white">Upload a screenshot.</span>{" "}
 Took a screenshot you want to check later? Upload it at{" "}
 <a
 href="/?view=watermark-verify"
 className="text-[#F5A524] hover:underline underline-offset-4"
 >
 the verifier page
 </a>{" "}
 and the same chain runs against the image.
 </li>
 </ul>
 <p className="text-[12px] text-gray-500 leading-relaxed">
 This page is the placeholder front door. The full audit
 surface — full BYOK provenance walkthrough, per-output run_id
 deep-links, the cryptographic-receipt architecture story — is
 on the build queue.
 </p>
 </div>
 </div>
 );
}
