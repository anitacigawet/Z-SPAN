import { useEffect } from "react";

// .gov-style "you are leaving this site" confirmation. Renders only
// when shown=true. Click outside, press Esc, or hit Cancel to dismiss;
// hit Continue to open the URL in a new tab.
export default function ExternalLinkModal({
 href,
 shown,
 onConfirm,
 onCancel,
}: {
 href: string;
 shown: boolean;
 onConfirm: () => void;
 onCancel: () => void;
}) {
 useEffect(() => {
 if (!shown) return;
 const onKey = (e: KeyboardEvent) => {
 if (e.key === "Escape") onCancel();
 };
 document.addEventListener("keydown", onKey);
 return () => document.removeEventListener("keydown", onKey);
 }, [shown, onCancel]);

 if (!shown) return null;

 return (
 <div className="leave-site-modal" role="dialog" aria-modal="true" aria-labelledby="leave-site-title">
 <div className="leave-site-backdrop" onClick={onCancel} aria-hidden />
 <div className="leave-site-card">
 <div className="leave-site-eyebrow">External link</div>
 <h3 id="leave-site-title" className="leave-site-title">
 You are leaving Z-SPAN
 </h3>
 <p className="leave-site-body">
 This link takes you to a site outside the Z-SPAN ecosystem. The
 maintainer&rsquo;s portfolio and other personal projects are hosted
 there.
 </p>
 <div className="leave-site-url" aria-label="destination URL">
 {href}
 </div>
 <div className="leave-site-actions">
 <button type="button" className="leave-site-cancel" onClick={onCancel}>
 Cancel
 </button>
 <button
 type="button"
 className="leave-site-continue"
 onClick={onConfirm}
 autoFocus
 >
 Continue &rarr;
 </button>
 </div>
 </div>
 </div>
 );
}
