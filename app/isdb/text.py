import unicodedata


ISDB_TEXT_ENCODING = "iso-8859-15"


def normalize_text(
    value: str | None,
) -> str:

    if not value:
        return ""

    value = value.replace(
        "\r\n",
        "\n",
    )

    value = value.replace(
        "\r",
        "\n",
    )

    value = " ".join(
        value.split()
    )

    return value.strip()


def sanitize_isdb_text(
    value: str | None,
) -> str:
    """
    Normaliza texto para ISDB-Tb.

    El perfil latinoamericano ISDB-Tb utiliza
    ISO/IEC 8859-15 para estos textos.

    Los caracteres no representables se
    sustituyen por '?'.
    """

    text = normalize_text(
        value
    )

    text = unicodedata.normalize(
        "NFC",
        text,
    )

    return (
        text
        .encode(
            ISDB_TEXT_ENCODING,
            errors="replace",
        )
        .decode(
            ISDB_TEXT_ENCODING,
        )
    )


def truncate_text(
    value: str,
    max_chars: int,
) -> str:

    value = sanitize_isdb_text(
        value
    )

    if len(value) <= max_chars:
        return value

    if max_chars <= 3:
        return value[:max_chars]

    return (
        value[
            :max_chars - 3
        ].rstrip()
        + "..."
    )


def encode_isdb_text(
    value: str | None,
) -> bytes:
    """
    Codifica texto para ISDB-Tb latinoamericano.

    ISO/IEC 8859-15 directo, sin selector DVB.
    """

    text = sanitize_isdb_text(
        value
    )

    return text.encode(
        ISDB_TEXT_ENCODING,
        errors="replace",
    )


def split_isdb_text(
    value: str | None,
    max_bytes: int,
) -> list[bytes]:
    """
    Divide texto sin cortar caracteres.

    Cada fragmento codificado ocupa como máximo
    max_bytes.

    Se intenta cortar primero por espacio.
    """

    text = sanitize_isdb_text(
        value
    )

    if not text:
        return []

    chunks: list[bytes] = []

    remaining = text

    while remaining:

        encoded = remaining.encode(
            ISDB_TEXT_ENCODING,
            errors="replace",
        )

        if len(encoded) <= max_bytes:
            chunks.append(encoded)
            break

        candidate = remaining[
            :max_bytes
        ]

        split_at = candidate.rfind(
            " "
        )

        if split_at > 0:
            candidate = candidate[
                :split_at
            ]

        candidate = candidate.rstrip()

        if not candidate:
            candidate = remaining[
                :max_bytes
            ]

        chunk = candidate.encode(
            ISDB_TEXT_ENCODING,
            errors="replace",
        )

        chunks.append(
            chunk
        )

        remaining = remaining[
            len(candidate):
        ].lstrip()

    return chunks
