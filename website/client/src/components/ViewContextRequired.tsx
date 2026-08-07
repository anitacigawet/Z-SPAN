// ViewContextRequired — rendered when an owner-only view is reached
// without the required context params (e.g., Compiler without a
// meetingId, TruthBook without a cast member). These views are normally
// entered FROM a specific meeting or member, not as standalone pages.
// A hard-typed URL or a TopBar nav-chip click against a fresh account
// (no localStorage history of "last compiler meeting") can land here;
// this surface explains why and points back to Channels rather than
// rendering an empty body that reads as broken/locked. Added 2026-06-21
// after the operator reported the Compiler appearing "still locked" on
// a fresh operator account — auth was working; the conditional
// render at App.tsx required a meetingId param that wasn't there.

import { ArrowLeft, Compass } from "lucide-react";

interface ViewContextRequiredProps {
 onBack: () => void;
 surface: string;
 body: string;
}

export default function ViewContextRequired({
 onBack,
 surface,
 body,
}: ViewContextRequiredProps) {
 return (
 <div className="min-h-screen bg-background text-foreground flex flex-col items-center justify-center px-6 py-20">
 <div className="max-w-md text-center space-y-5">
 <div className="inline-flex items-center justify-center w-12 h-12 rounded-full border border-[var(--line)] bg-[var(--surface)]">
 <Compass className="w-5 h-5 text-foreground/60" />
 </div>
 <div className="space-y-2">
 <h1 className="text-lg font-semibold tracking-tight text-white">
 {surface} — needs a starting point
 </h1>
 <p className="text-sm text-muted-foreground leading-relaxed">
 {body}
 </p>
 </div>
 <button
 onClick={onBack}
 className="inline-flex items-center gap-2 px-4 py-2 rounded-md border border-[var(--line)] bg-[var(--surface)] hover:bg-[var(--surface-3)] text-foreground/80 hover:text-white text-[12px] font-medium uppercase tracking-widest transition-colors"
 >
 <ArrowLeft className="w-3.5 h-3.5" />
 Back to channels
 </button>
 </div>
 </div>
 );
}
