_cancelled_review_sessions: set[str] = set()


def mark_review_cancelled(session_id: str) -> None:
    sid = (session_id or "").strip()
    if sid:
        _cancelled_review_sessions.add(sid)


def is_review_cancelled(session_id: str) -> bool:
    return (session_id or "").strip() in _cancelled_review_sessions


def clear_review_cancel(session_id: str) -> None:
    _cancelled_review_sessions.discard((session_id or "").strip())
