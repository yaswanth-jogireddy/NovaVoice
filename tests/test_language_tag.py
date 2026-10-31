"""The <lang:xx-IN> reply tag and following the caller between languages (demo_session.py)."""
from types import SimpleNamespace

from demo_session import DemoSession, answered_enquiry


class Stub:
    """Just the state the language helpers touch, without a live call."""
    _take_lang_tag = DemoSession._take_lang_tag
    _follow_script = DemoSession._follow_script

    def __init__(self, language="en", mode="voice"):
        self.language, self.mode, self._reply_lang, self._language_pinned = language, mode, None, False
        self.settings = SimpleNamespace(caller_language_switch=True)

    def _set_language(self, code):
        changed, self.language = code != self.language, code
        return changed


def test_tag_is_stripped_and_sets_the_speech_and_call_language():
    s = Stub("en")
    assert s._take_lang_tag("<lang:hi-IN> आपकी अगली EMI 5 नवंबर को है।") == "आपकी अगली EMI 5 नवंबर को है।"
    assert s._reply_lang == "hi" and s.language == "hi"
    assert s._take_lang_tag("<LANG: en-IN>Your next EMI is due on 5 November.") == "Your next EMI is due on 5 November."
    assert s.language == "en"


def test_untagged_or_unknown_tags_change_nothing():
    s = Stub("hi")
    assert s._take_lang_tag("No tag here.") == "No tag here." and s._reply_lang is None and s.language == "hi"
    assert s._take_lang_tag("<lang:xx-IN> Hello.") == "Hello." and s.language == "hi"


def test_hinglish_call_keeps_its_language():
    s = Stub("hinglish")
    s._take_lang_tag("<lang:en-IN> Okay.")
    assert s.language == "hinglish" and s._reply_lang == "en"


def test_caller_switching_script_is_followed_but_a_name_is_not():
    s = Stub("en")
    assert not s._follow_script("my name is प्रिया शर्मा") and s.language == "en"
    assert s._follow_script("मेरा बैलेंस कितना है") and s.language == "hi"
    assert s._follow_script("what is my balance please") and s.language == "en"


def test_answered_enquiry_ignores_identity_and_otp():
    assert answered_enquiry(['get_balance result: {"accounts": []}'])
    assert answered_enquiry(["policy KB-CHG-01 (Charges): ..."])
    assert not answered_enquiry(["verify_identity result: verified, caller's first name is Priya"])
    assert not answered_enquiry(["check_otp result: code correct, caller stepped up", "get_balance FAILED: down"])


class TagStub(Stub):
    _take_tags = DemoSession._take_tags

    def __init__(self):
        super().__init__("en")
        self._reply_tags, self.offtopic_turns = set(), 0


def test_control_tags_are_stripped_and_counted_once_per_reply():
    s = TagStub()
    assert s._take_tags("<lang:en-IN><OFFTOPIC> I can only help with banking.") == "I can only help with banking."
    assert s._take_tags("<off_topic> What do you need?") == "What do you need?"
    assert s._reply_tags == {"offtopic"} and s.offtopic_turns == 1          # one reply, one count
    s._reply_tags = set()
    assert s._take_tags("<lang:en-IN> <HANGUP> Thank you for calling. Goodbye.") == "Thank you for calling. Goodbye."
    assert s._reply_tags == {"hangup"} and s.offtopic_turns == 1
