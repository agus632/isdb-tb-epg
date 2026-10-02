from app.isdb.text import (
    encode_isdb_text,
    split_isdb_text,
    truncate_text,
)


SHORT_EVENT_DESCRIPTOR = 0x4D
EXTENDED_EVENT_DESCRIPTOR = 0x4E


def descriptor(
    tag: int,
    payload: bytes,
) -> bytes:

    if len(payload) > 255:
        raise ValueError(
            "Descriptor demasiado grande"
        )

    return bytes(
        [
            tag,
            len(payload),
        ]
    ) + payload


def short_event_descriptor(
    language: str,
    title: str,
    text: str = "",
) -> bytes:
    """
    short_event_descriptor 0x4D
    """

    language_bytes = (
        language[:3]
        .lower()
        .encode(
            "ascii",
            errors="replace",
        )
    )

    language_bytes = (
        language_bytes
        + b"   "
    )[:3]

    title = truncate_text(
        title,
        120,
    )

    text = truncate_text(
        text,
        120,
    )

    title_bytes = encode_isdb_text(
        title
    )

    text_bytes = encode_isdb_text(
        text
    )

    # Dejamos margen para que todo el
    # descriptor permanezca <= 255 bytes.

    title_bytes = title_bytes[:120]
    text_bytes = text_bytes[:120]

    payload = (
        language_bytes
        + bytes([len(title_bytes)])
        + title_bytes
        + bytes([len(text_bytes)])
        + text_bytes
    )

    return descriptor(
        SHORT_EVENT_DESCRIPTOR,
        payload,
    )


def extended_event_descriptors(
    language: str,
    description: str,
) -> list[bytes]:
    """
    Genera uno o más extended_event_descriptor.

    Se deja preparado para descripción extendida.
    """

    description = (
        description or ""
    ).strip()

    if not description:
        return []

    language_bytes = (
        language[:3]
        .lower()
        .encode(
            "ascii",
            errors="replace",
        )
    )

    language_bytes = (
        language_bytes
        + b"   "
    )[:3]

    # Payload:
    #
    # descriptor_number / last_descriptor_number  1
    # language                                3
    # length_of_items                         1
    # text_length                             1
    #
    # Máximo de texto:
    # 249 bytes.

    chunks = split_isdb_text(
        description,
        249,
    )

    if not chunks:
        return []

    # descriptor_number es de 4 bits.

    chunks = chunks[:16]

    last_number = len(chunks) - 1

    descriptors = []

    for number, chunk in enumerate(chunks):

        first_byte = (
            ((number & 0x0F) << 4)
            | (last_number & 0x0F)
        )

        payload = (
            bytes([first_byte])
            + language_bytes
            + b"\x00"
            + bytes([len(chunk)])
            + chunk
        )

        descriptors.append(
            descriptor(
                EXTENDED_EVENT_DESCRIPTOR,
                payload,
            )
        )

    return descriptors
