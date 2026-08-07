import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import {
 GATE_VERSION,
 QUERY_CHAR_CAP,
 validateLibrarianQuery,
} from "./librarianInputGate";

interface FixtureCase {
 name: string;
 raw: string;
 expect_ok: boolean;
 reason_code: string | null;
 canonical: string | null;
}

interface Fixture {
 fixture_version: string;
 gate_version: string;
 base_fixture: string;
 superseded_base_cases: string[];
 cases: FixtureCase[];
}

const fixturePath = path.resolve(
 __dirname,
 "../../../parsers/input_security/librarian_gate_cases.v2.json"
);
const fixture = JSON.parse(readFileSync(fixturePath, "utf8")) as Fixture;
const baseFixturePath = path.resolve(
 path.dirname(fixturePath),
 fixture.base_fixture
);
const baseFixture = JSON.parse(
 readFileSync(baseFixturePath, "utf8")
) as Fixture;

describe("Librarian input gate", () => {
 it("uses the expected gate and fixture versions", () => {
 expect(GATE_VERSION).toBe("grammar-v2");
 expect(fixture.gate_version).toBe(GATE_VERSION);
 expect(fixture.fixture_version).toBe("gate-cases-v2");
 });

 for (const fixtureCase of fixture.cases) {
 it(fixtureCase.name, () => {
 const result = validateLibrarianQuery(fixtureCase.raw);
 expect(result.ok).toBe(fixtureCase.expect_ok);
 expect(result.reasonCode ?? null).toBe(fixtureCase.reason_code);
 expect(result.canonicalQuery ?? null).toBe(fixtureCase.canonical);
 });
 }

 for (const fixtureCase of baseFixture.cases) {
 if (fixture.superseded_base_cases.includes(fixtureCase.name)) {
 continue;
 }
 it(`v1 regression: ${fixtureCase.name}`, () => {
 const result = validateLibrarianQuery(fixtureCase.raw);
 expect(result.ok).toBe(fixtureCase.expect_ok);
 expect(result.reasonCode ?? null).toBe(fixtureCase.reason_code);
 expect(result.canonicalQuery ?? null).toBe(fixtureCase.canonical);
 });
 }

 it("rejects null and undefined before other checks", () => {
 expect(validateLibrarianQuery(null).reasonCode).toBe("not_a_string");
 expect(validateLibrarianQuery(undefined).reasonCode).toBe("not_a_string");
 });

 it("enforces the raw two hundred character boundary", () => {
 const atCap = `What ${"a".repeat(QUERY_CHAR_CAP - 6)}?`;
 const overCap = `What ${"a".repeat(QUERY_CHAR_CAP - 5)}?`;

 expect(Array.from(atCap)).toHaveLength(QUERY_CHAR_CAP);
 expect(validateLibrarianQuery(atCap).ok).toBe(true);
 expect(Array.from(overCap)).toHaveLength(QUERY_CHAR_CAP + 1);
 expect(validateLibrarianQuery(overCap).reasonCode).toBe("too_long");
 });
});
