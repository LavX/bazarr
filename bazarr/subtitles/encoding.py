"""Decode subtitle documents for reading or translation."""


def decode_subtitle_bytes(raw: bytes) -> tuple[str, str]:
    """Decode subtitle bytes and return their original encoding when it is known."""
    # BOM detection
    if raw.startswith(b'\xef\xbb\xbf'):
        return raw[3:].decode('utf-8'), 'utf-8-sig'
    if raw.startswith(b'\xff\xfe'):
        return raw[2:].decode('utf-16-le'), 'utf-16-le'
    if raw.startswith(b'\xfe\xff'):
        return raw[2:].decode('utf-16-be'), 'utf-16-be'

    # Try UTF-8
    try:
        return raw.decode('utf-8'), 'utf-8'
    except UnicodeDecodeError:
        pass

    # charset_normalizer
    try:
        import charset_normalizer
        result = charset_normalizer.from_bytes(raw).best()
        if result is not None:
            return str(result), result.encoding
    except Exception:
        pass

    # Fallback
    return raw.decode('cp1252', errors='replace'), 'cp1252'
