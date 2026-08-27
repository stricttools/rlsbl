"""What an aborted apply says about the writes it already performed.

Every command in this group applies its plan item by item, and an item whose
count moved since the preview is a hard abort.  The abort stops the run; it
does NOT undo the items applied before it, and there is no rollback machinery
here on purpose -- the working tree is git's to restore.

So the abort message has to be honest about the half-done state it leaves.
"nothing further has been written" says only that the FAILING item was not
written; without this sentence a reader takes it to mean nothing at all was.
"""


def already_written(applied):
    """The sentence naming what this run wrote before it aborted.

    Args:
        applied: the running list of item keys the apply has written so far,
            or None when the caller is not tracking one (a direct
            single-item call, which cannot know).

    Returns the sentence to splice into an abort message, ending in a space,
    or the empty string when there is nothing to say.
    """
    if applied is None:
        return ""
    if not applied:
        return "Nothing had been written by this run before the failure. "
    return (
        f"Already written by this run, and still changed on disk: "
        f"{', '.join(applied)}. "
    )
