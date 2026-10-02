CRC32_POLY = 0x04C11DB7


def crc32_mpeg2(data: bytes) -> int:
    """
    CRC-32/MPEG-2 usado por las secciones PSI/SI.

    Polynomial:
        0x04C11DB7

    Initial value:
        0xFFFFFFFF

    No reflection.
    No final XOR.
    """

    crc = 0xFFFFFFFF

    for byte in data:
        crc ^= byte << 24

        for _ in range(8):

            if crc & 0x80000000:
                crc = (
                    (crc << 1) ^ CRC32_POLY
                ) & 0xFFFFFFFF

            else:
                crc = (
                    crc << 1
                ) & 0xFFFFFFFF

    return crc


def append_crc32(data: bytes) -> bytes:
    crc = crc32_mpeg2(data)

    return data + crc.to_bytes(
        4,
        byteorder="big",
    )
