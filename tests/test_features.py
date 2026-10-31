import pytest

from features import FEATURES, PRESETS, FeatureFlags, resolve


def test_default_is_milestone_1():
    milestone, active = resolve({})
    assert milestone == 1 and active == PRESETS[1]


@pytest.mark.parametrize("m", [1, 2, 3])
def test_presets_grow_monotonically(m):
    _, active = resolve({"MILESTONE": str(m)})
    if m > 1:
        assert resolve({"MILESTONE": str(m - 1)})[1] <= active


def test_level_1_brief_needs_account_tools_but_not_the_desk():
    assert "ACCOUNT_TOOLS" in resolve({"MILESTONE": "1"})[1]
    assert "WARM_HANDOFF" not in resolve({"MILESTONE": "1"})[1]
    assert "WARM_HANDOFF" in resolve({"MILESTONE": "2"})[1]


def test_override_wins_both_ways():
    _, active = resolve({"MILESTONE": "1", "FEATURE_WARM_HANDOFF": "true", "FEATURE_TICKETS": "off"})
    assert "WARM_HANDOFF" in active and "TICKETS" not in active


def test_garbage_values_are_safe():
    milestone, active = resolve({"MILESTONE": "banana", "FEATURE_EVALS": "maybe"})
    assert milestone == 1 and "EVALS" not in active
    assert resolve({"MILESTONE": "9"})[0] == 3


def test_sip_never_on_by_preset():
    assert all("SIP_OUTBOUND" not in p for p in PRESETS.values())


def test_unknown_feature_raises():
    with pytest.raises(KeyError):
        FeatureFlags({}).enabled("NOPE")


def test_live_switch_requires_real_token():
    f = FeatureFlags({"ADMIN_TOKEN": "s3cret-token"})
    with pytest.raises(PermissionError):
        f.set_milestone(3, "wrong")
    assert f.set_milestone(3, "s3cret-token")["milestone"] == 3
    with pytest.raises(PermissionError):   # placeholder token must never unlock
        FeatureFlags({"ADMIN_TOKEN": "change-me-long-random"}).set_milestone(2, "change-me-long-random")


def test_snapshot_lists_every_feature():
    snap = FeatureFlags({"MILESTONE": "2"}).snapshot()
    assert set(snap["features"]) == set(FEATURES) and snap["milestone"] == 2
