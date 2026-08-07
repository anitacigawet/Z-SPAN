/**
 * VoiceDictationButton — dictate into the search box.
 *
 * Replaces the retired V-Op-2 voice-PRIME button (biometric voice-search,
 * removed + operator direction 2026-07-02: the voice-search
 * capability itself is no longer wanted — keep the nice button, make it
 * voice-to-text). This is plain browser dictation: the Web Speech API
 * turns the user's speech into text inside their own browser session;
 * Z-SPAN records nothing, stores nothing, and no audio touches our
 * servers. Renders for everyone; when the browser doesn't support
 * speech recognition (or the page isn't a secure context) the button
 * stays visible but inert with an explanatory tooltip — the operator's
 * requested placeholder behavior.
 */
import { useEffect, useRef, useState } from "react";
import { Mic } from "lucide-react";

type Props = { onTranscript: (text: string) => void };

function getRecognitionCtor(): any {
 if (typeof window === "undefined") return null;
 const w = window as any;
 return w.SpeechRecognition || w.webkitSpeechRecognition || null;
}

export function VoiceDictationButton({ onTranscript }: Props) {
 const [listening, setListening] = useState(false);
 const recRef = useRef<any>(null);
 const supported = getRecognitionCtor() !== null;

 // Abort any in-flight recognition if the component unmounts mid-listen.
 useEffect(() => () => recRef.current?.abort?.(), []);

 const toggle = () => {
 if (!supported) return;
 if (listening) {
 recRef.current?.stop?.();
 return;
 }
 const Ctor = getRecognitionCtor();
 const rec = new Ctor();
 rec.lang = navigator.language || "en-US";
 rec.interimResults = false;
 rec.maxAlternatives = 1;
 rec.onresult = (e: any) => {
 const text = Array.from(e.results as ArrayLike<any>)
 .map(r => r[0]?.transcript ?? "")
 .join(" ")
 .trim();
 if (text) onTranscript(text);
 };
 rec.onend = () => setListening(false);
 rec.onerror = () => setListening(false);
 recRef.current = rec;
 setListening(true);
 try {
 rec.start();
 } catch {
 setListening(false);
 }
 };

 return (
 <button
 type="button"
 onClick={toggle}
 title={
 supported
 ? listening
 ? "Stop dictation"
 : "Dictate your search"
 : "Voice dictation isn't supported in this browser"
 }
 aria-label="Dictate your search"
 aria-pressed={listening}
 disabled={!supported}
 className="ml-1.5 flex h-7 w-7 flex-none items-center justify-center rounded-full border border-white/15 bg-white/5 transition-colors hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-40"
 >
 <Mic
 className="h-3.5 w-3.5"
 style={{
 color: listening ? "var(--success-green)" : "rgba(255,255,255,0.55)",
 }}
 />
 </button>
 );
}
