from datetime import datetime, timedelta, timezone

from app.isdb.crc import append_crc32
from app.isdb.datetime import (
    bcd,
    encode_datetime,
)


# ---------------------------------------------------------
# ISDB-Tb / SBTVD time tables
# ---------------------------------------------------------

TIME_PID = 0x0014

TDT_TABLE_ID = 0x70
TOT_TABLE_ID = 0x73

LOCAL_TIME_OFFSET_DESCRIPTOR = 0x58


# ---------------------------------------------------------
# Argentina fixed profile
# ---------------------------------------------------------

COUNTRY_CODE = b"ARG"

COUNTRY_REGION_ID = 0

ARGENTINA_OFFSET_HOURS = 3
ARGENTINA_OFFSET_MINUTES = 0

# Argentina currently uses UTC-3.
ARGENTINA_OFFSET = timedelta(hours=-3)

ARGENTINA_TZ = timezone(
    ARGENTINA_OFFSET,
    name="UTC-03:00",
)


def argentina_now() -> datetime:
    """
    Current Argentina wall-clock time.

    Returned as naive datetime intentionally because
    encode_datetime() preserves naive wall-clock values.
    """

    return (
        datetime.now(timezone.utc)
        .astimezone(ARGENTINA_TZ)
        .replace(tzinfo=None)
    )


def build_tdt(
    now: datetime | None = None,
) -> bytes:
    """
    Build ISDB-Tb/SBTVD TDT.

    PID      0x0014
    table_id 0x70
    """

    if now is None:
        now = argentina_now()

    time_data = encode_datetime(now)

    section_length = 5

    section = bytearray()

    section.append(TDT_TABLE_ID)

    section.append(
        0x70
        | (
            (section_length >> 8)
            & 0x0F
        )
    )

    section.append(
        section_length
        & 0xFF
    )

    section.extend(time_data)

    return bytes(section)


def build_local_time_offset_descriptor() -> bytes:
    """
    Build local_time_offset_descriptor (0x58)
    for Argentina.

    One 13-byte country/region entry is generated.

    country_code:
        ARG

    country_region_id:
        0

    local_time_offset_polarity:
        1 = negative

    local_time_offset:
        03:00

    No DST transition is currently used in Argentina,
    therefore time_of_change is filled with 0xFF and
    next_time_offset remains 03:00.
    """

    payload = bytearray()

    # ISO 3166-1 alpha-3.
    payload.extend(COUNTRY_CODE)

    # country_region_id: 6 bits
    # reserved:          1 bit = 1
    # polarity:          1 bit = 1 (negative)
    payload.append(
        (
            (COUNTRY_REGION_ID & 0x3F)
            << 2
        )
        | 0x02
        | 0x01
    )

    # local_time_offset HH:MM in BCD.
    payload.extend(
        bytes(
            [
                bcd(
                    ARGENTINA_OFFSET_HOURS
                ),
                bcd(
                    ARGENTINA_OFFSET_MINUTES
                ),
            ]
        )
    )

    # Argentina has no scheduled DST transition.
    #
    # Use a valid future transition date while keeping
    # next_time_offset equal to the current -03:00.
    # Therefore no effective clock change occurs.
    future_change = datetime(
        2037,
        1,
        1,
        0,
        0,
        0,
    )

    payload.extend(
        encode_datetime(future_change)
    )

    # next_time_offset = 03:00
    payload.extend(
        bytes(
            [
                bcd(
                    ARGENTINA_OFFSET_HOURS
                ),
                bcd(
                    ARGENTINA_OFFSET_MINUTES
                ),
            ]
        )
    )

    if len(payload) != 13:
        raise RuntimeError(
            "Invalid local_time_offset descriptor payload"
        )

    return bytes(
        [
            LOCAL_TIME_OFFSET_DESCRIPTOR,
            len(payload),
        ]
    ) + bytes(payload)


def build_tot(
    now: datetime | None = None,
) -> bytes:
    """
    Build ISDB-Tb/SBTVD TOT.

    PID      0x0014
    table_id 0x73
    """

    if now is None:
        now = argentina_now()

    time_data = encode_datetime(now)

    descriptors = (
        build_local_time_offset_descriptor()
    )

    descriptors_loop_length = len(
        descriptors
    )

    # UTC/time reference        5
    # descriptor loop length    2
    # descriptors               N
    # CRC32                     4
    section_length = (
        5
        + 2
        + descriptors_loop_length
        + 4
    )

    section = bytearray()

    section.append(TOT_TABLE_ID)

    section.append(
        0x70
        | (
            (section_length >> 8)
            & 0x0F
        )
    )

    section.append(
        section_length
        & 0xFF
    )

    section.extend(time_data)

    section.append(
        0xF0
        | (
            (
                descriptors_loop_length
                >> 8
            )
            & 0x0F
        )
    )

    section.append(
        descriptors_loop_length
        & 0xFF
    )

    section.extend(descriptors)

    return append_crc32(
        bytes(section)
    )


def build_time_tables(
    now: datetime | None = None,
) -> list[bytes]:
    """
    Generate TDT and TOT using exactly the same
    time reference.
    """

    if now is None:
        now = argentina_now()

    return [
        build_tdt(now),
        build_tot(now),
    ]
