import {
  Children,
  isValidElement,
  type ReactElement,
  type ReactNode,
} from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockUnfollow = vi.hoisted(() => vi.fn(async () => true));
const mockSetCityTopics = vi.hoisted(() => vi.fn(async () => [] as string[]));
const mockUseFollows = vi.hoisted(() =>
  vi.fn(() => ({
    follows: [
      {
        target_type: "city" as const,
        target_key: "Kingman",
        created_at: "2026-07-30T12:00:00Z",
      },
      {
        target_type: "topic" as const,
        target_key: "transit",
        created_at: "2026-07-29T12:00:00Z",
      },
      {
        target_type: "meeting" as const,
        target_key: "127696",
        created_at: "2026-07-28T12:00:00Z",
      },
    ],
    cityTopics: { Kingman: ["data_centers"] } as Record<string, string[]>,
    loading: false,
    unfollow: mockUnfollow,
    setCityTopics: mockSetCityTopics,
  }))
);

vi.mock("../hooks/useFollows", () => ({
  useFollows: mockUseFollows,
}));

import { FollowingList } from "./FollowingList";

function findElement(
  node: ReactNode,
  predicate: (element: ReactElement) => boolean
): ReactElement | undefined {
  if (!isValidElement(node)) return undefined;
  if (predicate(node)) return node;

  const children = (node.props as { children?: ReactNode }).children;
  for (const child of Children.toArray(children)) {
    const match = findElement(child, predicate);
    if (match) return match;
  }
  return undefined;
}

beforeEach(() => {
  mockUnfollow.mockClear();
  mockSetCityTopics.mockClear();
  mockUseFollows.mockClear();
});

describe("FollowingList", () => {
  it("groups enabled follows, hides legacy topics, and preserves styling", () => {
    const markup = renderToStaticMarkup(<FollowingList onNavigate={vi.fn()} />);

    expect(markup).toContain("Cities · 1");
    expect(markup).toContain("Meetings · 1");
    expect(markup).not.toContain("Topics");
    expect(markup).not.toContain('aria-label="Unfollow transit"');
    expect(markup).toContain('aria-label="Unfollow Kingman"');
    expect(markup).toContain("hover:text-rose-300");
    expect(markup).toContain(
      "When you enable a topic, matching meetings will highlight it in the email."
    );
  });

  it("calls useFollows unfollow from the inline button", () => {
    const tree = FollowingList({ onNavigate: vi.fn() });
    const button = findElement(
      tree,
      element =>
        (element.props as { "aria-label"?: string })["aria-label"] ===
        "Unfollow Kingman"
    );

    expect(button).toBeDefined();
    const { onClick } = button!.props as { onClick: () => void };
    onClick();

    expect(mockUseFollows).toHaveBeenCalledOnce();
    expect(mockUnfollow).toHaveBeenCalledWith("city", "Kingman");
  });

  it("renders per-city topic checkboxes with correct enabled state", () => {
    const tree = FollowingList({ onNavigate: vi.fn() });
    const expectedStates: Record<string, boolean> = {
      "Data Centers": true,
      "Water Rights": false,
      "Diversity & Inclusion": false,
      LGBTQ: false,
      Education: false,
    };

    for (const [label, expected] of Object.entries(expectedStates)) {
      const checkbox = findElement(
        tree,
        element =>
          (element.props as { "aria-label"?: string })["aria-label"] ===
          `${label} tag for Kingman`
      );
      expect(checkbox, `${label} checkbox`).toBeDefined();
      expect((checkbox!.props as { checked: boolean }).checked).toBe(expected);
    }
  });

  it("hydrates canonical city topics for a legacy mixed-case city follow", () => {
    mockUseFollows.mockReturnValueOnce({
      follows: [
        {
          target_type: "city" as const,
          target_key: "kInGmAn",
          created_at: "2026-07-30T12:00:00Z",
        },
      ],
      cityTopics: { Kingman: ["data_centers"] },
      loading: false,
      unfollow: mockUnfollow,
      setCityTopics: mockSetCityTopics,
    });

    const tree = FollowingList({ onNavigate: vi.fn() });
    const checkbox = findElement(
      tree,
      element =>
        (element.props as { "aria-label"?: string })["aria-label"] ===
        "Data Centers tag for kInGmAn"
    );

    expect(checkbox).toBeDefined();
    expect((checkbox!.props as { checked: boolean }).checked).toBe(true);
  });

  it("toggles a per-city topic via setCityTopics", () => {
    const tree = FollowingList({ onNavigate: vi.fn() });
    // Enabling water_rights should union with the already-enabled data_centers.
    const enableBox = findElement(
      tree,
      element =>
        (element.props as { "aria-label"?: string })["aria-label"] ===
        "Water Rights tag for Kingman"
    );
    expect(enableBox).toBeDefined();
    (enableBox!.props as { onChange: () => void }).onChange();
    expect(mockSetCityTopics).toHaveBeenLastCalledWith("Kingman", [
      "data_centers",
      "water_rights",
    ]);

    // Toggling the already-enabled data_centers should drop it.
    const disableBox = findElement(
      tree,
      element =>
        (element.props as { "aria-label"?: string })["aria-label"] ===
        "Data Centers tag for Kingman"
    );
    expect(disableBox).toBeDefined();
    (disableBox!.props as { onChange: () => void }).onChange();
    expect(mockSetCityTopics).toHaveBeenLastCalledWith("Kingman", []);
  });

  it("does not render topic checkboxes under non-city rows", () => {
    const markup = renderToStaticMarkup(<FollowingList onNavigate={vi.fn()} />);
    // The meeting row must not surface topic-tag inputs.
    expect(markup).not.toContain(
      'aria-label="Data Centers tag for 127696"'
    );
  });
});
