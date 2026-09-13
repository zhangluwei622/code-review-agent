def run(view, arguments, emit):
    count = 0
    for hunk in view.hunks():
        for line in hunk["lines"]:
            if arguments["query"] in line["text"]:
                if count == arguments["limit"]:
                    # Explicit bounded search coverage, not a silent empty/full result.
                    emit({"more_matches": True})
                    return
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
                count += 1
