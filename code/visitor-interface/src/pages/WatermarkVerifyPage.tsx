import { useEffect, useRef, useState } from "react";
type Verdict = {
    token: string;
    exists: boolean;
    authenticated?: boolean;
    legacy?: boolean;
    source?: "flagship_generation" | "cli_generation" | "legacy_flagship";
    meeting_id?: number;
    output_type?: string;
    meeting_title?: string;
    city_name?: string;
    prompt_version?: string;
    generated_at?: string;
    note?: string;
    status?: "registered" | "superseded";
    provider?: string;
    model?: string;
    content_sha256?: string;
    account_state?: "active" | "deleted";
    meeting?: {
        public_id: string;
        title: string;
        date: string;
        city: string;
        county: string;
        state: string;
    };
};
type UploadResult = {
    token: string | null;
    bbox?: number[] | null;
    stats?: any;
    verdict?: Verdict;
    error?: string;
};
const BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
const PALETTE: Record<number, string> = {
    0b00: "#1A3A7C",
    0b01: "#EF4444",
    0b10: "#22C55E",
    0b11: "#F5A524",
};
function tokenToBlocks(token: string): number[] {
    const upper = token.toUpperCase();
    if (upper.length !== 8)
        return [];
    const bits: number[] = [];
    for (const ch of upper) {
        const idx = BASE32_ALPHABET.indexOf(ch);
        if (idx === -1)
            return [];
        for (let b = 4; b >= 0; b--)
            bits.push((idx >> b) & 1);
    }
    const blocks: number[] = [];
    for (let i = 0; i < 40; i += 2)
        blocks.push((bits[i] << 1) | bits[i + 1]);
    return blocks;
}
function DecodeAnimation({ token }: {
    token: string | null;
}) {
    const blocks = token ? tokenToBlocks(token) : [];
    const realToken = token && blocks.length === 20;
    const displayToken = realToken ? token : "ZSPANYRZ";
    const displayBlocks = realToken ? blocks : tokenToBlocks(displayToken);
    const [step, setStep] = useState(0);
    useEffect(() => {
        const timers: number[] = [];
        timers.push(window.setTimeout(() => setStep(1), 600));
        timers.push(window.setTimeout(() => setStep(2), 1800));
        timers.push(window.setTimeout(() => setStep(3), 2800));
        timers.push(window.setTimeout(() => setStep(4), 3800));
        return () => timers.forEach(window.clearTimeout);
    }, [displayToken]);
    const blockWidth = 12;
    const gap = 3;
    const height = 28;
    const ribbonWidth = (blockWidth + gap) * 20 - gap;
    const bitsString = displayBlocks
        .map((v) => v.toString(2).padStart(2, "0"))
        .join("");
    return (<div className="bg-[#141416] border border-white/10 rounded-xl p-5 my-6">
      <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-[#6B6B72] mb-3">
        How a ribbon decodes
      </div>

      
      <div className="flex justify-center mb-2">
        <svg width={ribbonWidth} height={height} xmlns="http://www.w3.org/2000/svg">
          {displayBlocks.map((value, i) => (<rect key={i} x={i * (blockWidth + gap)} y={0} width={blockWidth} height={height} fill={PALETTE[value]} style={{
                opacity: step >= 1 ? 1 : 0.85,
                transition: "opacity 250ms ease",
            }}/>))}
        </svg>
      </div>

      
      <div className="flex justify-center font-mono text-[10px] text-gray-400 mb-3" style={{ gap: `${gap}px` }}>
        {displayBlocks.map((value, i) => (<span key={i} style={{
                width: blockWidth,
                opacity: step >= 1 ? 1 : 0,
                transform: step >= 1 ? "translateY(0)" : "translateY(-4px)",
                transition: `opacity 200ms ease ${i * 25}ms, transform 200ms ease ${i * 25}ms`,
                textAlign: "center",
            }}>
            {value.toString(2).padStart(2, "0")}
          </span>))}
      </div>

      
      <div className="font-mono text-[11px] text-gray-300 text-center mb-3 px-2" style={{
            opacity: step >= 2 ? 1 : 0,
            transform: step >= 2 ? "translateY(0)" : "translateY(-4px)",
            transition: "opacity 300ms ease, transform 300ms ease",
            letterSpacing: "0.05em",
            wordBreak: "break-all",
        }}>
        {bitsString}
      </div>

      <div className="text-[10px] text-gray-500 text-center mb-2" style={{
            opacity: step >= 2 ? 1 : 0,
            transition: "opacity 300ms ease 150ms",
        }}>
        40 bits · base32 encode ↓
      </div>

      
      <div className="font-mono text-[20px] tracking-[0.2em] text-white text-center" style={{
            opacity: step >= 3 ? 1 : 0,
            transform: step >= 3 ? "scale(1)" : "scale(0.95)",
            transition: "opacity 350ms ease, transform 350ms ease",
        }}>
        {displayToken}
      </div>

      <div className="text-[11px] text-gray-500 text-center mt-3" style={{
            opacity: step >= 4 ? 1 : 0,
            transition: "opacity 350ms ease",
        }}>
        ↓ looked up in Z-SPAN's public audit log
      </div>
    </div>);
}
export default function WatermarkVerifyPage() {
    const [directToken, setDirectToken] = useState<string | null>(null);
    const [directVerdict, setDirectVerdict] = useState<Verdict | null>(null);
    const [directLoading, setDirectLoading] = useState(false);
    const fileInputRef = useRef<HTMLInputElement>(null);
    const [uploadResult, setUploadResult] = useState<UploadResult | null>(null);
    const [uploading, setUploading] = useState(false);
    const [uploadPreview, setUploadPreview] = useState<string | null>(null);
    useEffect(() => {
        if (typeof window === "undefined")
            return;
        const sp = new URLSearchParams(window.location.search);
        const t = (sp.get("token") || "").toUpperCase();
        if (!t || t.length !== 8)
            return;
        setDirectToken(t);
        setDirectLoading(true);
        fetch(`/api/watermark-lookup/${encodeURIComponent(t)}`)
            .then((r) => r.json())
            .then((d) => setDirectVerdict(d))
            .catch((e) => setDirectVerdict({ token: t, exists: false, note: String(e) }))
            .finally(() => setDirectLoading(false));
    }, []);
    const handleUpload = async (file: File) => {
        setUploading(true);
        setUploadResult(null);
        setUploadPreview(URL.createObjectURL(file));
        try {
            const form = new FormData();
            form.append("image", file);
            const resp = await fetch("/api/decode-ribbon-image", { method: "POST", body: form });
            const data = await resp.json();
            setUploadResult(data);
        }
        catch (err: any) {
            setUploadResult({ token: null, error: String(err) });
        }
        finally {
            setUploading(false);
        }
    };
    const renderVerdictCard = (v: Verdict | undefined | null) => {
        if (!v)
            return null;
        const authenticated = v.exists && v.authenticated === true;
        if (v.legacy) {
            return (<div className="border rounded-lg p-4" style={{
                    borderColor: "rgba(245,165,36,0.5)",
                    background: "rgba(245,165,36,0.08)",
                }}>
          <div className="text-[16px] font-semibold mb-2 text-[#F5A524]">
            Legacy identifier — not authentication
          </div>
          <div className="text-[13px] text-gray-300">
            This is a publicly reproducible legacy identifier — not authentication.
            The screenshot content itself is not authenticated.
          </div>
          {(v.city_name || v.meeting_title) && (<div className="mt-3 text-[12px] text-gray-400">
              {[v.city_name, v.meeting_title].filter(Boolean).join(" · ")}
            </div>)}
        </div>);
        }
        if (authenticated && v.source === "cli_generation") {
            return (<div className="border rounded-lg p-4" style={{
                    borderColor: "rgba(34,197,94,0.4)",
                    background: "rgba(34,197,94,0.06)",
                }}>
          <div className="text-[16px] font-semibold mb-2 text-[var(--success-green)]">
            Registry match · community generation
          </div>
          <div className="text-[13px] text-gray-300 mb-3">
            Created with a signed-in Z-SPAN CLI.
          </div>
          {v.meeting && (<div className="text-[13px] text-gray-300 space-y-1">
              <div className="text-white font-medium">{v.meeting.title}</div>
              <div>
                {[v.meeting.city, v.meeting.county, v.meeting.state]
                        .filter(Boolean)
                        .join(" · ")}
              </div>
              <div>{v.meeting.date}</div>
            </div>)}
          <div className="mt-3 text-[12px] text-gray-300 space-y-1">
            <div>Output: {v.output_type}</div>
            <div>Provider / model: {v.provider} / {v.model}</div>
            <div>Registered: {v.generated_at}</div>
          </div>
          {v.status === "superseded" && (<div className="mt-3 text-[12px] text-[#F5A524]">
              Superseded — a newer registration replaced this generation.
            </div>)}
          {v.account_state === "deleted" && (<div className="mt-3 text-[12px] text-gray-400">
              The registering account has since been deleted; this provenance record remains.
            </div>)}
          <div className="mt-3 text-[11px] text-gray-400">Content SHA-256</div>
          <div className="font-mono text-[11px] text-gray-200 break-all">
            {v.content_sha256}
          </div>
          <div className="mt-1 text-[11px] text-gray-400">{v.note}</div>
        </div>);
        }
        if (authenticated) {
            return (<div className="border rounded-lg p-4" style={{
                    borderColor: "rgba(34,197,94,0.4)",
                    background: "rgba(34,197,94,0.06)",
                }}>
          <div className="text-[16px] font-semibold mb-2 text-[var(--success-green)]">
            Registry match · canonical Z-SPAN record
          </div>
          <div className="text-[13px] text-gray-300">
            This token maps to Z-SPAN&apos;s canonical record. The screenshot content
            itself is not authenticated.
          </div>
          <div className="mt-3 text-[12px] text-gray-400">
            {[v.city_name, v.meeting_title, v.output_type].filter(Boolean).join(" · ")}
          </div>
        </div>);
        }
        return (<div className="border rounded-lg p-4" style={{
                borderColor: "rgba(239,68,68,0.4)",
                background: "rgba(239,68,68,0.06)",
            }}>
        <div className="text-[16px] font-semibold mb-2 text-[var(--alert-red)]">
          Token not registered
        </div>
        <div className="text-[13px] text-gray-300">
          {v.note || "This token isn't in our audit log."}
        </div>
      </div>);
    };
    return (<div className="min-h-screen bg-[#0A0A0A] text-white">
      <div className="max-w-md mx-auto px-5 py-6">
        <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-[#6B6B72] mb-4">
          Z-SPAN · Provenance
        </div>

        
        {directToken && (<div className="mb-5">
            {directLoading ? (<div className="text-[13px] text-gray-400 italic">Looking up…</div>) : (renderVerdictCard(directVerdict))}
          </div>)}

        
        <DecodeAnimation token={directToken}/>

        
        <p className="text-[13px] text-gray-400 leading-relaxed mb-5">
          {directToken
            ? "If you see Z-SPAN content elsewhere online, you can come back here to verify it."
            : "Verify a Z-SPAN-attributed screenshot from anywhere on the internet."}
        </p>

        
        <input ref={fileInputRef} type="file" accept="image/*" className="hidden" onChange={(e) => {
            const f = e.target.files?.[0];
            if (f)
                void handleUpload(f);
        }}/>
        <button onClick={() => fileInputRef.current?.click()} disabled={uploading} className="w-full bg-white text-black font-semibold py-3 rounded-lg text-[14px] tracking-wide disabled:bg-gray-700 disabled:text-gray-500">
          {uploading ? "Decoding…" : "Verify a screenshot"}
        </button>

        <button onClick={() => {
            window.location.search = "?view=scan";
        }} className="w-full mt-2 border border-white/20 text-white font-medium py-3 rounded-lg text-[14px] tracking-wide">
          Scan with camera
        </button>

        {uploadPreview && (<div className="mt-3 rounded-lg overflow-hidden border border-white/10">
            <img src={uploadPreview} alt="Upload preview" className="w-full h-auto"/>
          </div>)}

        {uploadResult && !uploading && (<div className="mt-3">
            {uploadResult.error ? (<div className="border border-[var(--alert-red)]/40 bg-[var(--alert-red)]/5 rounded-lg p-3 text-[12px] text-gray-300">
                <div className="text-[var(--alert-red)] font-semibold mb-1">
                  Decode failed
                </div>
                {uploadResult.error}
              </div>) : uploadResult.token ? (renderVerdictCard(uploadResult.verdict)) : (<div className="border border-[#F5A524]/40 bg-[#F5A524]/5 rounded-lg p-3 text-[12px] text-gray-300">
                <div className="text-[#F5A524] font-semibold mb-1">
                  No ribbon found
                </div>
                Crop closer to just the ribbon and try again.
              </div>)}
          </div>)}
      </div>
    </div>);
}
