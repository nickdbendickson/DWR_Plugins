import { test } from "node:test";
import assert from "node:assert/strict";
import { splitTokenFragment } from "./crest-token.ts";

test("token is read from the fragment and removed from it", () => {
  assert.deepEqual(splitTokenFragment("#crest-token=abc_DEF-123"), { token: "abc_DEF-123", rest: "" });
  assert.deepEqual(splitTokenFragment("#a=1&crest-token=xyz&b=2"), { token: "xyz", rest: "#a=1&b=2" });
});

test("no token in the fragment leaves it as it was", () => {
  assert.deepEqual(splitTokenFragment(""), { token: "", rest: "" });
  assert.deepEqual(splitTokenFragment("#view=map"), { token: "", rest: "#view=map" });
  assert.deepEqual(splitTokenFragment("#not-crest-token=1"), { token: "", rest: "#not-crest-token=1" });
});
