"""
Case-insensitive matching for find_text: substring first (fast, enough
for most queries), then a "light" Levenshtein distance as a fallback if
fuzzy=True, to tolerate occasional OCR reading errors (e.g. "Parametres"
detected instead of "Paramètres").

The distance applies WORD BY WORD (not on the whole detected line):
comparing a short query to a long line via Levenshtein on the entire
line would unfairly penalize any partial match, whereas comparing
against the line's individual words captures the real intent (the user
is looking for a word or short phrase, not a paragraph).
"""


def _normalize(text: str) -> str:
    return text.casefold()


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))

    previous_row = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current_row = [i] + [0] * len(b)
        for j, char_b in enumerate(b, start=1):
            cost = 0 if char_a == char_b else 1
            current_row[j] = min(
                previous_row[j] + 1,        # deletion
                current_row[j - 1] + 1,     # insertion
                previous_row[j - 1] + cost,  # substitution
            )
        previous_row = current_row
    return previous_row[-1]


def matches(query: str, detected_text: str, fuzzy: bool = True) -> bool:
    normalized_query = _normalize(query)
    normalized_text = _normalize(detected_text)

    if normalized_query in normalized_text:
        return True
    if not fuzzy or not normalized_query:
        return False

    # Tolerance proportional to the query's length rather than a fixed
    # threshold: a 1-2 letter word should tolerate no error, a long word
    # can tolerate several.
    max_distance = max(1, len(normalized_query) // 4)
    return any(
        _levenshtein(normalized_query, word) <= max_distance
        for word in normalized_text.split()
    )
