import {
  ArrowDown,
  BookOpen,
  CheckCircle2,
  ExternalLink,
  Github,
} from "lucide-react";
import { ReactNode, useRef, useState } from "react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";

const CATALOG_HOME = "https://github.com/anitacigawet/national-civics-catalog";

interface CatalogContributionDialogProps {
  trigger: ReactNode;
  placeName?: string;
  contributionUrl?: string;
}

interface CatalogContributionExplainerBodyProps {
  contributionUrl: string;
  onContinue?: () => void;
}

/**
 * The visual story inside the sleeping-cat dialog. Kept separate from the
 * Radix portal so the visitor-facing copy and handoff link can be tested
 * without duplicating the dialog's focus-management machinery.
 */
export function CatalogContributionExplainerBody({
  contributionUrl,
  onContinue,
}: CatalogContributionExplainerBodyProps) {
  const hasListingSpecificGuide = contributionUrl.startsWith(
    "/public-api/catalog/contribute/"
  );
  const hasStateFolder = contributionUrl.includes("/tree/main/data/states/");
  const fallbackActionLabel = hasStateFolder
    ? "Open this state’s catalog folder"
    : "Open the National Civics Catalog";

  return (
    <div className="px-5 pb-6 sm:px-7 sm:pb-7">
      <div className="mt-5 flex flex-col items-center">
        <section className="w-full rounded-xl border border-[var(--line)] bg-[var(--surface)] p-4 sm:p-5">
          <div className="flex gap-3.5">
            <div
              className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full border border-[var(--line-strong)] bg-[var(--surface-2)] text-amber-400"
              aria-hidden="true"
            >
              <Github className="h-5 w-5" />
            </div>
            <div className="min-w-0 text-left">
              <h3 className="text-[15px] font-semibold text-foreground">
                One shared source catalog
              </h3>
              <p className="mt-1 text-[13px] leading-relaxed text-foreground/70">
                Z-SPAN uses the National Civics Catalog, a separate public
                dataset of continuing meeting sources. This shelf is asleep
                because its source has not been added yet.
              </p>
            </div>
          </div>
        </section>

        <ArrowDown
          className="my-2 h-5 w-5 text-foreground/35"
          aria-hidden="true"
        />

        <section className="w-full rounded-xl border border-[var(--line)] bg-[var(--surface)] p-4 sm:p-5">
          <div className="mb-3 flex items-center gap-2 text-left">
            <CheckCircle2
              className="h-5 w-5 text-green-400"
              aria-hidden="true"
            />
            <h3 className="text-[15px] font-semibold text-foreground">
              Acceptance starts the guarantee
            </h3>
          </div>
          <img
            src="/brand/zspan-guarantee-card.svg"
            alt=""
            aria-hidden="true"
            className="h-auto w-full select-none rounded-lg"
            draggable={false}
          />
          <p className="mt-3 text-left text-[13px] leading-relaxed text-foreground/70">
            After the source is reviewed and accepted, Z-SPAN will put its
            parser in place—or post a clear public update explaining what
            blocked it—within three days. As soon as the parser works and
            meetings are available, this shelf can begin filling in.
          </p>
        </section>
      </div>

      <section className="mt-5 border-t border-[var(--line)] pt-5 text-left">
        <div className="flex items-start gap-3">
          <BookOpen
            className="mt-0.5 h-5 w-5 shrink-0 text-amber-400"
            aria-hidden="true"
          />
          <div>
            <h3 className="text-[15px] font-semibold text-foreground">
              Want to help wake it?
            </h3>
            <p className="mt-1 text-[13px] leading-relaxed text-foreground/70">
              {hasListingSpecificGuide
                ? "Take the short guide to the AI assistant you already use. It asks a few ordinary questions, then helps submit the source through GitHub or a simple browser form. You never have to edit JSON by hand."
                : `${fallbackActionLabel} to choose a place and follow its contribution guide. You never have to edit JSON by hand.`}
            </p>
          </div>
        </div>

        <a
          href={contributionUrl}
          target="_blank"
          rel="noopener noreferrer"
          onClick={onContinue}
          aria-label={`${hasListingSpecificGuide ? "Take the contribution guide to your AI" : fallbackActionLabel} (opens in a new tab)`}
          className="mt-5 inline-flex min-h-11 w-full items-center justify-center gap-2 rounded-lg border border-amber-400/35 bg-amber-400/10 px-4 py-2.5 text-sm font-semibold text-amber-300 transition-colors hover:border-amber-400/60 hover:bg-amber-400/15 hover:text-amber-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400 focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--background)]"
        >
          {hasListingSpecificGuide
            ? "Take the guide to your AI"
            : fallbackActionLabel}
          <ExternalLink className="h-4 w-4" aria-hidden="true" />
        </a>
      </section>
    </div>
  );
}

export default function CatalogContributionDialog({
  trigger,
  placeName,
  contributionUrl = CATALOG_HOME,
}: CatalogContributionDialogProps) {
  const [open, setOpen] = useState(false);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const placeLabel = placeName?.trim() || "this shelf";

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent
        className="z-[80] max-h-[calc(100dvh-2rem)] overflow-y-auto overscroll-contain border-[var(--line-strong)] bg-[var(--background)] p-0 text-foreground sm:max-w-md"
        overlayClassName="z-[79] bg-black/75"
        onOpenAutoFocus={event => {
          event.preventDefault();
          titleRef.current?.focus();
        }}
      >
        <DialogHeader className="px-5 pb-0 pt-6 pr-12 text-left sm:px-7 sm:pt-7 sm:pr-12">
          <p className="text-[10px] font-semibold uppercase tracking-[0.22em] text-amber-400/80">
            How this shelf wakes up
          </p>
          <DialogTitle
            ref={titleRef}
            tabIndex={-1}
            className="text-xl leading-tight text-foreground outline-none sm:text-2xl"
          >
            Help wake this shelf
          </DialogTitle>
          <DialogDescription className="text-[13px] leading-relaxed text-foreground/65">
            Here is how {placeLabel} goes from a missing source to a working
            Z-SPAN parser.
          </DialogDescription>
        </DialogHeader>

        <CatalogContributionExplainerBody
          contributionUrl={contributionUrl}
          onContinue={() => setOpen(false)}
        />
      </DialogContent>
    </Dialog>
  );
}
