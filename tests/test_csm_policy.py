from csm import policy
from csm.policy import CallState, Decision
from csm.tools import TOOLS


def state(**kw):
    return CallState(call_id="t", **kw)


def test_public_tools_allowed_anonymous():
    assert policy.decide(TOOLS["search_policy"], state()).decision == Decision.ALLOW
    assert policy.decide(TOOLS["create_ticket"], state()).decision == Decision.ALLOW


def test_account_tools_need_verification():
    for name in ("get_balance", "recent_transactions", "loan_emi_info", "request_statement"):
        assert policy.decide(TOOLS[name], state()).decision == Decision.NEED_VERIFICATION


def test_verified_allows_reads_but_risky_needs_step_up():
    s = state()
    policy.record_verification(s, True, "C1001", "Priya")
    assert s.identity == policy.VERIFIED_STATE
    assert policy.decide(TOOLS["get_balance"], s).decision == Decision.ALLOW
    assert policy.decide(TOOLS["block_card"], s).decision == Decision.STEP_UP
    assert policy.decide(TOOLS["raise_dispute"], s).decision == Decision.STEP_UP


def test_otp_steps_up_and_expires():
    s = state()
    policy.record_verification(s, True, "C1002", "Rahul")
    code = policy.issue_otp(s, now=1000)
    assert len(code) == 6 and s.otp_hash and code not in s.otp_hash
    assert not policy.check_otp(s, "000000" if code != "000000" else "111111", now=1001)
    assert policy.check_otp(s, code, now=1002)
    assert s.identity == policy.STEPPED_UP and s.otp_hash is None
    assert policy.decide(TOOLS["block_card"], s, now=1003).decision == Decision.ALLOW
    # a step-up lasts STEP_UP_TTL_S
    assert policy.decide(TOOLS["block_card"], s, now=1002 + policy.STEP_UP_TTL_S + 1).decision == Decision.STEP_UP


def test_expired_otp_rejected():
    s = state()
    policy.record_verification(s, True, "C1002", "Rahul")
    code = policy.issue_otp(s, now=0)
    assert not policy.check_otp(s, code, now=policy.OTP_TTL_S + 1)


def test_three_failed_verifications_lock_and_handoff():
    s = state()
    policy.claim(s, "C1001")
    assert s.identity == policy.CLAIMED
    for _ in range(3):
        policy.record_verification(s, False)
    assert s.identity == policy.LOCKED
    assert policy.decide(TOOLS["get_balance"], s).decision == Decision.HANDOFF
    assert policy.decide(TOOLS["search_policy"], s).decision == Decision.HANDOFF
    # a correct answer after lockout does not unlock
    assert policy.record_verification(s, True, "C1001", "Priya") == policy.LOCKED


def test_otp_send_limit_hands_off():
    s = state()
    policy.record_verification(s, True, "C1002", "Rahul")
    for _ in range(policy.MAX_OTP_SENDS):
        policy.issue_otp(s)
    assert policy.decide(TOOLS["block_card"], s).decision == Decision.HANDOFF


def test_rate_limit():
    s = state()
    for _ in range(policy.MAX_TOOL_CALLS_PER_MIN):
        policy.note_tool_call(s, now=100)
    verdict = policy.decide(TOOLS["search_policy"], s, now=110)
    assert verdict.decision == Decision.DENY and verdict.code == "RATE_LIMIT"
    assert policy.decide(TOOLS["search_policy"], s, now=200).decision == Decision.ALLOW


def test_security_handoff_stops_tools_but_ordinary_handoff_does_not():
    s = state(handed_off=True, handoff_reason="risk_flag")
    assert policy.decide(TOOLS["handoff_to_human"], s).decision == Decision.HANDOFF
    assert policy.decide(TOOLS["search_policy"], s).decision == Decision.DENY
    assert policy.decide(TOOLS["search_policy"], state(handed_off=True, handoff_reason="caller_request")).decision == Decision.ALLOW


def test_se_hits_escalate_on_second():
    s = state()
    assert not policy.register_se_hit(s, "third_party")
    assert policy.register_se_hit(s, "secret_request")
    assert s.risk_flags == ["third_party", "secret_request"]


def test_model_handoff_needs_the_callers_own_words():
    assert policy.model_handoff_now("caller_request", "can I speak to an agent")
    assert not policy.model_handoff_now("caller_request", "Customer.")          # seen live: transferred on one word
    assert not policy.model_handoff_now("complex_issue", "open my account")
    assert policy.answers_offer("yes please") is True and policy.answers_offer("no thanks") is False
    assert policy.answers_offer("what is my balance and also my card status") is None
