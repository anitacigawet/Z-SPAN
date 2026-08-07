import {
 Children,
 isValidElement,
 type ReactElement,
 type ReactNode,
} from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CurrentUser } from "../hooks/useCurrentUser";

const mocks = vi.hoisted(() => ({
 currentUser: null as CurrentUser | null,
 hidePlaceholders: true,
}));

vi.mock("../hooks/useCurrentUser", () => ({
 useCurrentUser: () => ({
 user: mocks.currentUser,
 loading: false,
 }),
}));

vi.mock("../components/FollowingList", () => ({
 FollowingList: () => "Following list",
}));

vi.mock("../hooks/useHidePlaceholders", async importOriginal => {
 const actual =
 await importOriginal<typeof import("../hooks/useHidePlaceholders")>();
 return {
 ...actual,
 useHidePlaceholders: () =>
 [mocks.hidePlaceholders, actual.setHidePlaceholders] as const,
 };
});

import SettingsPage, { CitizenSettings } from "./SettingsPage";

class MemoryStorage {
 private readonly values = new Map<string, string>();

 getItem(key: string): string | null {
 return this.values.get(key) ?? null;
 }

 setItem(key: string, value: string): void {
 this.values.set(key, value);
 }
}

function user(isOwner: boolean): CurrentUser {
 return {
 user_id: 42,
 email: "reader@example.com",
 display_name: "Civic Reader",
 avatar_url: null,
 role: "light",
 is_owner: isOwner,
 is_operator_search_principal: isOwner,
 librarian_access: "none",
 follows: [],
 city_topics: {},
 };
}

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
 mocks.currentUser = user(false);
 mocks.hidePlaceholders = true;
});

afterEach(() => {
 vi.unstubAllGlobals();
});

describe("SettingsPage", () => {
 it("renders citizen preferences, follows, and account details", () => {
 const markup = renderToStaticMarkup(<SettingsPage onNavigate={vi.fn()} />);

 expect(markup).toContain("Preferences");
 expect(markup).toContain("Following list");
 expect(markup).toContain("Account");
 expect(markup).toContain("Civic Reader");
 expect(markup).toContain("reader@example.com");
 expect(markup).toContain('action="/api/auth/logout"');
 expect(markup).not.toContain("Operator settings");
 });

 it("renders the owner configuration after the citizen sections", () => {
 mocks.currentUser = user(true);

 const markup = renderToStaticMarkup(<SettingsPage onNavigate={vi.fn()} />);

 const account = markup.indexOf("Account");
 const operator = markup.indexOf("Operator settings");
 expect(account).toBeGreaterThan(-1);
 expect(operator).toBeGreaterThan(account);
 expect(markup).toContain("Loading settings");
 });

 it("persists a toggle change through the placeholder preference setter", () => {
 const localStorage = new MemoryStorage();
 vi.stubGlobal("window", { localStorage });

 const tree = CitizenSettings({
 user: user(false),
 onNavigate: vi.fn(),
 });
 const checkbox = findElement(
 tree,
 element => (element.props as { type?: string }).type === "checkbox"
 );

 expect(checkbox).toBeDefined();
 const { onChange } = checkbox!.props as {
 onChange: (event: { target: { checked: boolean } }) => void;
 };
 onChange({ target: { checked: false } });

 expect(localStorage.getItem("zspan.hide_placeholders")).toBe("0");
 });
});
