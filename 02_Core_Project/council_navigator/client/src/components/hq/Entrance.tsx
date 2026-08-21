import type { Rect } from "./hqHelpers";

// The glowing lobby doors — the primary call to action. Clicking enters the
// channel browser (the app's "home" view).
export default function Entrance({
  rect,
  onEnter,
}: {
  rect: Rect;
  onEnter: () => void;
}) {
  return (
    <div
      className="ov entrance"
      style={{
        top: `${rect.top}%`,
        left: `${rect.left}%`,
        width: `${rect.width}%`,
        height: `${rect.height}%`,
      }}
      onClick={onEnter}
      role="button"
      tabIndex={0}
      aria-label="Enter Z-SPAN — open the channel browser"
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") onEnter();
      }}
    >
      <span className="entrance-label">Enter Z-SPAN</span>
    </div>
  );
}
