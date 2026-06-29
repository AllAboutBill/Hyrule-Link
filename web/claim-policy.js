"use strict";

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.HyruleLinkClaimPolicy = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  function canClaim(entry, rules, playerId) {
    rules = rules || {};
    const needFound = rules.require_found_to_claim !== false;
    const openScope = rules.open_season_scope || "owned";
    if (!entry) return !needFound && openScope === "any";
    if (entry.owner === playerId || entry.locked) return false;
    const discovered = (entry.discovered || []).includes(playerId);
    const found = discovered || (!!rules.shared_discovery && (entry.discovered || []).length > 0);
    if (needFound) return found;
    return openScope === "any" || entry.owner != null || found;
  }

  return { canClaim };
});
