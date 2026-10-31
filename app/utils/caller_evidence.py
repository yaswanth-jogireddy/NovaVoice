"""Source-preserving context for any workflow; no field extraction or demo script."""


def caller_evidence(history):
    """Retain original caller statements and the question each answers.

    No model-generated summary can overwrite this record. Interpretation stays
    with the dialogue model, including corrections with multiple related values.
    """
    entries = []
    previous_reply = ""
    for message in history:
        if message["role"] == "assistant":
            previous_reply = message["content"]
        elif message["role"] == "user":
            entries.append({"turn": len(entries) + 1,
                            "preceding_assistant_reply": previous_reply,
                            "caller_statement": message["content"]})
            previous_reply = ""
    return entries

