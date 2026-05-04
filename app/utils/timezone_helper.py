"""Best-effort timezone guessing from race name / location keywords."""

_LOCATION_TZ = {
    "sydney": "Australia/Sydney",
    "melbourne": "Australia/Melbourne",
    "brisbane": "Australia/Brisbane",
    "perth": "Australia/Perth",
    "gold coast": "Australia/Brisbane",
    "london": "Europe/London",
    "berlin": "Europe/Berlin",
    "amsterdam": "Europe/Amsterdam",
    "paris": "Europe/Paris",
    "new york": "America/New_York",
    "boston": "America/New_York",
    "chicago": "America/Chicago",
    "los angeles": "America/Los_Angeles",
    "tokyo": "Asia/Tokyo",
    "singapore": "Asia/Singapore",
    "dubai": "Asia/Dubai",
    "hong kong": "Asia/Hong_Kong",
    "toronto": "America/Toronto",
    "auckland": "Pacific/Auckland",
}


def guess_timezone(race_name: str) -> str:
    name_lower = race_name.lower()
    for keyword, tz in _LOCATION_TZ.items():
        if keyword in name_lower:
            return tz
    return "UTC"
