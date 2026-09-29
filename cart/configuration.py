BLANK_CONFIGURATION_SIGNATURE = ""


def build_configuration_signature(option_ids) -> str:
    """Return a canonical, order-independent signature for selected option IDs."""
    if option_ids is None:
        raise ValueError("Option IDs must be an iterable of positive integers")

    parsed = []
    seen = set()
    for value in option_ids:
        option_id = _positive_option_id(value)
        if option_id in seen:
            raise ValueError("Duplicate option IDs are not allowed")
        seen.add(option_id)
        parsed.append(option_id)

    if not parsed:
        return BLANK_CONFIGURATION_SIGNATURE
    return ",".join(str(option_id) for option_id in sorted(parsed))


def _positive_option_id(value) -> int:
    if isinstance(value, bool) or value is None or not isinstance(value, int):
        raise ValueError("Option IDs must be positive integers")
    if value <= 0:
        raise ValueError("Option IDs must be positive integers")
    return value
