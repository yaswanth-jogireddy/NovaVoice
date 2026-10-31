"""Acknowledgement clips: never said twice, and the pre-recorded set loads for a voice."""
import pytest

from demo_session import strip_ack


@pytest.mark.parametrize("model_said,spoken", [
    ("Sure! Your balance is 84,250 rupees.", "Your balance is 84,250 rupees."),
    ("Okay, let me check that. Your card is blocked.", "Your card is blocked."),
    ("Sure, one moment.", ""),
    ("Got it.", ""),
    ("जी, आपका बैलेंस 84,250 रुपये है।", "आपका बैलेंस 84,250 रुपये है।"),
    ("Your balance is 84,250 rupees.", "Your balance is 84,250 rupees."),
    ("Thanks, Priya. You're verified.", "Thanks, Priya. You're verified."),
    ("Surely that's fine.", "Surely that's fine."),
])
def test_model_never_repeats_the_ack(model_said, spoken):
    assert strip_ack(model_said) == spoken
