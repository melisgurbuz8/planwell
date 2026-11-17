"""Calendar event -> activity key from the config activity registry (settings.registry), or None.

Accepts PlanWell's normalised event dicts ({id, title, start, end, location, ...}) and also raw
Google Calendar event resources ({summary, start: {dateTime|date}, attendees, status}).
Matching is case-insensitive and word-ish: a keyword must start at a word boundary and may be
followed by a common suffix ("walk" matches "Walk", "walks", "walking" but not "sidewalk").
Declined, cancelled and all-day events are ignored.
"""
import re

_SUFFIX = r"(?:s|es|ing|ed|er|ers)?"
_cache = {}


def _pattern(keyword):
    raw = keyword.strip()
    if raw.startswith("^") and len(raw) > 1:
        # "Starts with" rule, case-sensitive: "^BA" matches "BA780", "BA 712 Lecture", "BA-Seminar",
        # but not "Barre" or "Bake sale" (a lowercase letter right after the prefix means a different word).
        if raw in _cache:
            return _cache[raw]
        pat = re.compile(r"^\s*" + re.escape(raw[1:]) + r"(?![a-z])")
        _cache[raw] = pat
        return pat
    kw = raw.lower()
    if not kw:
        return None
    if kw in _cache:
        return _cache[kw]
    body = r"\s+".join(re.escape(p) for p in kw.split())
    alts = [body + _SUFFIX]
    if kw.endswith("e") and " " not in kw:  # hike -> hiking
        alts.append(re.escape(kw[:-1]) + r"ing")
    pat = re.compile(r"(?<![a-z0-9])(?:%s)(?![a-z0-9])" % "|".join(alts), re.IGNORECASE)
    _cache[kw] = pat
    return pat


def _title(event):
    return event.get("title") or event.get("summary") or ""


def is_all_day(event):
    if event.get("all_day"):
        return True
    start = event.get("start")
    if isinstance(start, dict):
        return "dateTime" not in start and "date" in start
    if isinstance(start, str):
        return len(start.strip()) == 10  # 'YYYY-MM-DD'
    return False


def is_declined(event):
    if event.get("declined"):
        return True
    if str(event.get("status", "")).lower() == "cancelled":
        return True
    if str(event.get("self_response", event.get("response_status", ""))).lower() == "declined":
        return True
    for a in event.get("attendees") or []:
        if isinstance(a, dict) and a.get("self") and a.get("responseStatus") == "declined":
            return True
    return False


def match(event, config):
    """(activity_key, keyword, position) for the best keyword match in the title, or None.

    Best = earliest position, then longest keyword, then exercise before daily kinds.
    """
    from planwell.settings import registry
    if not isinstance(event, dict) or is_all_day(event) or is_declined(event):
        return None
    title = _title(event)
    if not title:
        return None
    best = None
    for order, (activity, a) in enumerate(registry(config).items()):
        for w in a.get("keywords") or []:
            pat = _pattern(str(w))
            if pat is None:
                continue
            m = pat.search(title)
            if not m:
                continue
            rank = (m.start(), -(m.end() - m.start()), 0 if a.get("kind") == "exercise" else 1, order)
            if best is None or rank < best[0]:
                best = (rank, activity, w)
    return (best[1], best[2], best[0][0]) if best else None


def classify(event, config):
    """Return the registry activity key ("walking", "pilates", "padel", "class", ...) or None."""
    m = match(event, config)
    return m[0] if m else None


_EXERTION = {"still": 1, "steady": 3, "bursty": 4}


def rules_label(event, config):
    """Rules-only event label in the planwell.labeler shape (fallback when labeler is unavailable)."""
    from planwell.settings import registry
    act = classify(event, config)
    if act:
        a = registry(config)[act]
        t = a.get("traits") or {}
        ex = _EXERTION.get(t.get("pattern"), 2)
        if a.get("kind") == "daily":
            ex = min(ex, 2)
        return {"category": act, "posture": t.get("posture", "standing"), "pattern": t.get("pattern", "steady"),
                "setting": t.get("setting", "either"), "exertion": ex, "source": "rules"}
    return {"category": "other", "posture": "seated", "pattern": "still", "setting": "either",
            "exertion": 1, "source": "rules"}


def label(event, config):
    """planwell.labeler.label_event if available (calendar text only), else rules_label."""
    try:
        from planwell import labeler
        res = labeler.label_event(event, config)
        if isinstance(res, dict) and res.get("category"):
            return res
    except Exception:
        pass
    return rules_label(event, config)
