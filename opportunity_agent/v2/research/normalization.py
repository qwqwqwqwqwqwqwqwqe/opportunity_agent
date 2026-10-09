"""Conservative checks between an extracted value and its exact source quote."""
import re
from datetime import datetime


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
        candidates = re.findall(r"20\d{2}-\d{2}-\d{2}", quote)
        for match in re.finditer(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(20\d{2})", quote):
            for fmt in ("%B %d %Y", "%b %d %Y"):
                try:
                    candidates.append(datetime.strptime(" ".join(match.groups()), fmt).date().isoformat())
                    break
                except ValueError:
                    pass
        for year, month, day in re.findall(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", quote):
            try:
                candidates.append(datetime(int(year), int(month), int(day)).date().isoformat())
            except ValueError:
                pass
        return value in candidates and bool(re.search(r"deadline|due|截止", lower))
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
