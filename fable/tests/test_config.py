"""Config, budget, and router behavior — the control plane."""

import dataclasses

import pytest

from fable.config import (
    DEFAULT_ROLES,
    TIERS,
    Budget,
    EscalationExhausted,
    FableConfig,
    ModelTier,
    RolePolicy,
    Router,
)


class TestModelTier:
    def test_cost_usd_prices_all_four_usage_fields(self):
        tier = ModelTier("m", 10.0, 20.0, 12.5, 1.0, 100, 100)
        usage = {
            "input_tokens": 1_000_000,
            "output_tokens": 500_000,
            "cache_creation_input_tokens": 200_000,
            "cache_read_input_tokens": 2_000_000,
        }
        # 10 + 10 + 2.5 + 2 = 24.5
        assert tier.cost_usd(usage) == pytest.approx(24.5)

    def test_cost_usd_tolerates_missing_fields(self):
        tier = TIERS["strong"]
        assert tier.cost_usd({}) == 0.0

    def test_shipped_tiers_use_real_model_ids(self):
        assert TIERS["strong"].model_id == "claude-opus-4-8"
        assert TIERS["mid"].model_id == "claude-sonnet-5"
        assert TIERS["cheap"].model_id == "claude-haiku-4-5"
        for tier in TIERS.values():
            # NEVER date-suffixed ids (they 404). haiku's alias has no suffix.
            assert not tier.model_id[-8:].isdigit()

    def test_cache_prices_follow_anthropic_multipliers(self):
        for tier in TIERS.values():
            assert tier.cache_write_per_mtok == pytest.approx(tier.input_per_mtok * 1.25)
            assert tier.cache_read_per_mtok == pytest.approx(tier.input_per_mtok * 0.10)


class TestBudget:
    def test_defaults_are_valid(self):
        Budget()

    @pytest.mark.parametrize("field, value", [
        ("max_usd", 0),
        ("max_usd", -1.0),
        ("max_turns", 0),
        ("max_wall_seconds", -5.0),
        ("warn_pct", 0.0),
        ("warn_pct", 1.5),
    ])
    def test_rejects_non_positive_rails(self, field, value):
        with pytest.raises(ValueError):
            Budget(**{field: value})

    def test_rejects_unordered_pressure_ladder(self):
        with pytest.raises(ValueError, match="ladder"):
            Budget(warn_pct=0.8, edit_pct=0.7, checkpoint_pct=0.9)


class TestFableConfig:
    def test_default_config_is_valid_and_frozen(self):
        config = FableConfig()
        with pytest.raises(dataclasses.FrozenInstanceError):
            config.gate_max_retries = 99

    def test_rejects_role_referencing_unknown_tier(self):
        with pytest.raises(ValueError, match="unknown tier"):
            FableConfig(roles={"executor": RolePolicy("nonexistent", "high")})

    def test_rejects_task_budget_below_beta_minimum(self):
        with pytest.raises(ValueError, match="20_000|20000"):
            FableConfig(task_budget_tokens=19_999)

    def test_rejects_non_positive_tunables(self):
        with pytest.raises(ValueError):
            FableConfig(gate_max_retries=0)

    def test_with_overrides_returns_new_config(self):
        base = FableConfig()
        derived = base.with_overrides(gate_max_retries=5)
        assert derived.gate_max_retries == 5
        assert base.gate_max_retries != 5 or base is not derived
        assert derived is not base

    def test_tier_for_resolves_role_to_tier(self):
        config = FableConfig()
        assert config.tier_for("mechanical").model_id == "claude-haiku-4-5"
        assert config.tier_for("executor").model_id == "claude-opus-4-8"

    def test_default_roles_cover_the_documented_set(self):
        assert set(DEFAULT_ROLES) >= {
            "orchestrator", "executor", "researcher", "judge", "refuter", "mechanical",
        }


class TestRouter:
    def test_policy_unknown_role_raises_with_known_roles_listed(self):
        router = Router(FableConfig())
        with pytest.raises(KeyError, match="executor"):
            router.policy("no_such_role")

    def test_route_step_judgment_pins_strong(self):
        policy = Router(FableConfig()).route_step("judgment")
        assert (policy.tier, policy.effort) == ("strong", "xhigh")

    def test_route_step_shell_verifier_routes_mid(self):
        policy = Router(FableConfig()).route_step("pytest -q")
        assert (policy.tier, policy.effort) == ("mid", "medium")

    def test_escalate_climbs_effort_before_tier(self):
        router = Router(FableConfig())
        policy = RolePolicy("cheap", "low")
        policy = router.escalate(policy)
        assert (policy.tier, policy.effort) == ("cheap", "medium")

    def test_escalate_climbs_tier_after_max_effort(self):
        router = Router(FableConfig())
        policy = router.escalate(RolePolicy("cheap", "max"))
        assert (policy.tier, policy.effort) == ("mid", "max")

    def test_escalate_is_bounded_at_strong_max(self):
        router = Router(FableConfig())
        with pytest.raises(EscalationExhausted):
            router.escalate(RolePolicy("strong", "max"))

    def test_escalate_full_ladder_is_finite(self):
        router = Router(FableConfig())
        policy = RolePolicy("cheap", "low")
        steps = 0
        with pytest.raises(EscalationExhausted):
            while True:
                policy = router.escalate(policy)
                steps += 1
                assert steps < 20, "escalation must be finite"
        # low->medium->high->xhigh->max (4), then cheap->mid, mid->strong (2)
        assert steps == 6
