"""Conservative checks between an extracted value and its exact source quote."""
import re
from datetime import datetime, date


def _infer_context_year(text):
    """Infer the most relevant admission year from page text.

    Priority:
    1. Explicit "Fall YYYY" / "Spring YYYY" mentions → use that year
    2. All explicit years in text → pick one where (month, day) hasn't passed yet this year
       If all years are past or no clear month/day, pick the future-most year >= today.year
    3. Otherwise fallback to today's year (or next if month >= Sept, implying next admissions cycle)
    """
    today = date.today()
    current_year = today.year

    # Strategy 1: Look for "Fall YYYY" or "Spring YYYY" patterns
    cycle_match = re.search(r"(?:fall|spring|autumn|summer)\s*(20\d{2})", text, re.I)
    if cycle_match:
        return int(cycle_match.group(1))

    # Strategy 2: Extract all years and pick the best one
    years = set()
    for match in re.finditer(r"\b(20\d{2})\b", text):
        years.add(int(match.group(1)))

    if not years:
        # Fallback: use current year (or next year if we're in fall/winter)
        # assuming most deadline pages are for the next admission cycle
        if today.month >= 9:  # Oct-Dec: plan for next year
            return current_year + 1
        return current_year

    # If we have years, pick one that makes sense for a future deadline
    # Prefer current/future years first
    future_years = [y for y in years if y >= current_year]
    if future_years:
        return min(future_years)  # Pick the nearest future year

    # All years are in the past; this shouldn't happen for deadline pages,
    # but if it does, assume next year
    return current_year + 1


def _extract_dates_from_text(text, context_year=None):
    """Extract candidate dates from text using multiple strategies."""
    today = date.today()

    # Infer the context year automatically if not provided
    if context_year is None:
        context_year = _infer_context_year(text)

    candidates = []

    # Strategy 1: ISO format YYYY-MM-DD
    for match in re.finditer(r"\b(20\d{2})-(0[1-9]|1[0-2])-([0-3]\d)\b", text, re.I):
        try:
            year, month, day = match.groups()
            candidates.append(datetime(int(year), int(month), int(day)).date().isoformat())
        except ValueError:
            pass

    # Strategy 2: Full English format "Month Day, Year" or "Month Day Year"
    for match in re.finditer(r"\b([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s+(20\d{2})\b", text, re.I):
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                candidates.append(datetime.strptime(" ".join(match.groups()), fmt).date().isoformat())
                break
            except ValueError:
                pass

    # Strategy 3: Short English format "Month Day" without year → infer from context
    # Only use this if quote contains deadline-related keywords, to avoid false positives
    if re.search(r"(?:application\s+)?deadline|due\s+date|closes|submission", text, re.I):
        for match in re.finditer(r"\b([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", text, re.I):
            month_name, day = match.groups()
            for fmt in ("%B %d", "%b %d"):
                try:
                    # Parse month and day
                    parsed_date = datetime.strptime(f"{month_name} {day}", fmt)
                    month, day_int = parsed_date.month, parsed_date.day

                    # Build with context year first
                    dt = datetime(context_year, month, day_int).date()

                    # If this date is in the past, try next year
                    if dt < today:
                        dt = datetime(context_year + 1, month, day_int).date()

                    candidates.append(dt.isoformat())
                    break
                except ValueError:
                    pass

    # Strategy 4: Numeric formats M/D/YYYY or MM/DD/YYYY
    for match in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b", text):
        month, day, year = match.groups()
        try:
            dt = datetime(int(year), int(month), int(day))
            candidates.append(dt.date().isoformat())
        except ValueError:
            pass

    # Strategy 5: Chinese format YYYY年M月D日 or YYYY年MM月DD日
    for match in re.finditer(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", text):
        year, month, day = match.groups()
        try:
            dt = datetime(int(year), int(month), int(day))
            candidates.append(dt.date().isoformat())
        except ValueError:
            pass

    return list(dict.fromkeys(candidates))  # deduplicate while preserving order


def supported_value(field, value, quote):
    lower = quote.casefold()
    if field == "gre_policy":
        if "gre" not in lower:
            return False
        if re.search(r"not accepted|do not accept|不接受", lower):
            policy = "not_accepted"
        elif re.search(r"not required|no gre|无需|不要求", lower):
            policy = "not_required"
        elif re.search(r"optional|可选", lower):
            policy = "optional"
        elif re.search(r"required|必须|要求", lower):
            policy = "required"
        else:
            return False
        return value == policy
    if field == "deadline":
        # Check quote contains deadline-related keywords
        if not re.search(r"(?:application\s+)?deadline|due\s+date|closes|submission", lower):
            return False

        candidates = _extract_dates_from_text(quote)
        return value in candidates
    if field == "language":
        if not re.search(r"toefl|ielts|duolingo|english proficiency|english language|托福|雅思", lower):
            return False
        if value == "not_required":
            return bool(re.search(r"not required|no (?:english )?(?:test|proficiency)|不要求|无需", lower))
        if value == "optional":
            return bool(re.search(r"optional|可选", lower))
        if value == "conditional":
            return bool(re.search(r"waiv|exempt|豁免|not required.{0,100}(?:for|if)|unless", lower))
        if value == "required":
            return bool(re.search(r"required|must|need(?:ed)? to|proof of english|demonstrate english|要求|必须", lower))
        return False
    return bool(value and value in quote)
