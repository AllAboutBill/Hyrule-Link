"""Canonical custom-rule defaults and named editor presets."""

DEFAULT_RULES = {
    "claiming": True,
    "require_found_to_claim": True,
    "open_season_scope": "owned",
    "steal_cooldown_s": 5.0,
    "cooldown_scope": "item",
    "steal_back_lock_s": 0.0,
    "steal_budget_per_min": 0,
    "hold_limit_s": 0.0,
    "hold_expiry": "next_finder",
    "tenure_lock_s": 0.0,
    "idle_release_s": 0.0,
    "borrow_s": 0.0,
    "borrow_revert": "prev_owner",
    "auto_shuffle_s": 0.0,
    "shuffle_scope": "all",
    "shared_discovery": False,
}

PRESET_OVERRIDES = {
    "normal": {},
    "hot_potato": {"claiming": False, "hold_limit_s": 120, "hold_expiry": "next_finder"},
    "chaos": {"claiming": False, "auto_shuffle_s": 120, "shuffle_scope": "all"},
    "cutthroat": {
        "require_found_to_claim": False, "open_season_scope": "owned",
        "cooldown_scope": "thief", "steal_cooldown_s": 20,
        "steal_budget_per_min": 3, "steal_back_lock_s": 30,
    },
    "lease": {"claiming": True, "hold_limit_s": 300, "hold_expiry": "release"},
    "raid": {"require_found_to_claim": False, "borrow_s": 120,
             "borrow_revert": "prev_owner"},
    "siege": {"claiming": True, "tenure_lock_s": 240},
}
