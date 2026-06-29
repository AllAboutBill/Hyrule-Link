"use strict";

const assert = require("assert");
const { canClaim } = require("../web/claim-policy.js");

const base = { owner: 1, discovered: [1], locked: false };
assert.strictEqual(canClaim(base, { require_found_to_claim: true }, 2), false);
assert.strictEqual(canClaim(base, { require_found_to_claim: true, shared_discovery: true }, 2), true);
assert.strictEqual(canClaim(base, { require_found_to_claim: false, open_season_scope: "owned" }, 2), true);
assert.strictEqual(canClaim(null, { require_found_to_claim: false, open_season_scope: "any" }, 2), true);
assert.strictEqual(canClaim(null, { require_found_to_claim: false, open_season_scope: "owned" }, 2), false);
assert.strictEqual(canClaim({ ...base, locked: true }, { require_found_to_claim: false }, 2), false);
console.log("claim policy tests passed");
