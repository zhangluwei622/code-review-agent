def run(view, arguments, emit):
    hunk = view.hunk(arguments["hunk_id"])
    for line in hunk["lines"]:
        # Zero represents the absent side on an added/deleted line.
        emit(
            {
                "hunk_id": hunk["hunk_id"],
                "kind": line["kind"],
                "old_lineno": line["old_lineno"] or 0,
                "new_lineno": line["new_lineno"] or 0,
                "text": line["text"],
                "redacted": line["redacted"],
            }
        )
