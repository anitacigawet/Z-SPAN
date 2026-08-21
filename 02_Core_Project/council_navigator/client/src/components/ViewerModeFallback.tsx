// ViewerModeFallback — rendered in place of owner-only views when an
// allowlisted-but-non-owner viewer somehow navigates to one (the surface
// shouldn't be reachable via UI in the first place — the buttons that
// navigate to these views are gated with <OwnerOnly>. This is
// defense-in-depth + a tidy fallback if state ever gets nudged off the
// happy path.)

import { ArrowLeft, Lock } from 'lucide-react';

interface ViewerModeFallbackProps {
  onBack: () => void;
  // Optional: name of the surface that's owner-only, surfaced in the copy.
  surface?: string;
}

export default function ViewerModeFallback({
  onBack,
  surface,
}: ViewerModeFallbackProps) {
  return (
    <div className="min-h-screen bg-background text-foreground flex flex-col items-center justify-center px-6 py-20">
      <div className="max-w-md text-center space-y-5">
        <div className="inline-flex items-center justify-center w-12 h-12 rounded-full border border-[var(--line)] bg-[var(--surface)]">
          <Lock className="w-5 h-5 text-foreground/60" />
        </div>
        <div className="space-y-2">
          <h1 className="text-lg font-semibold tracking-tight text-white">
            Owner-only surface
          </h1>
          <p className="text-sm text-muted-foreground leading-relaxed">
            {surface
              ? `The ${surface} is part of the flagship operator's control panel — it isn't available to allowlisted viewers.`
              : 'This surface is part of the flagship operator’s control panel — it isn’t available to allowlisted viewers.'}
            {' '}You can browse published broadcasts from the channel guide.
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
