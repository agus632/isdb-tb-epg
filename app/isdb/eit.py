from dataclasses import dataclass
from datetime import datetime

from app.isdb.crc import append_crc32
from app.isdb.datetime import (
    encode_datetime,
    encode_duration,
)
from app.isdb.descriptors import (
    extended_event_descriptors,
    short_event_descriptor,
)


EIT_ACTUAL_PRESENT_FOLLOWING = 0x4E

EIT_ACTUAL_SCHEDULE_FIRST = 0x50
EIT_ACTUAL_SCHEDULE_LAST = 0x5F

MAX_SECTION_LENGTH = 4093


@dataclass
class EITEvent:
    event_id: int
    start_time: datetime
    stop_time: datetime

    title: str

    subtitle: str = ""
    description: str = ""

    running_status: int = 0
    free_ca_mode: bool = False


def build_event(
    event: EITEvent,
    language: str = "spa",
    extended_description: bool = True,
) -> bytes:

    if event.stop_time <= event.start_time:
        raise ValueError(
            "stop_time debe ser posterior a start_time"
        )

    duration_seconds = int(
        (
            event.stop_time
            - event.start_time
        ).total_seconds()
    )

    short_text = (
        event.subtitle or ""
    )

    descriptors = (
        short_event_descriptor(
            language=language,
            title=event.title,
            text=short_text,
        )
    )

    if (
        extended_description
        and event.description
    ):
        descriptors += b"".join(
            extended_event_descriptors(
                language=language,
                description=event.description,
            )
        )

    descriptor_length = len(
        descriptors
    )

    if descriptor_length > 0x0FFF:
        raise ValueError(
            "Descriptor loop demasiado grande"
        )

    flags = (
        ((event.running_status & 0x07) << 13)
        | (
            (1 if event.free_ca_mode else 0)
            << 12
        )
        | descriptor_length
    )

    return (
        event.event_id.to_bytes(
            2,
            "big",
        )
        + encode_datetime(
            event.start_time
        )
        + encode_duration(
            duration_seconds
        )
        + flags.to_bytes(
            2,
            "big",
        )
        + descriptors
    )


def build_eit_section(
    *,
    table_id: int,
    service_id: int,
    transport_stream_id: int,
    original_network_id: int,
    version: int,
    section_number: int,
    last_section_number: int,
    segment_last_section_number: int,
    last_table_id: int,
    events: list[EITEvent],
    language: str = "spa",
    extended_description: bool = True,
) -> bytes:

    event_data = b"".join(
        build_event(
            event,
            language=language,
            extended_description=(
                extended_description
            ),
        )
        for event in events
    )

    body = (
        service_id.to_bytes(
            2,
            "big",
        )
        + bytes(
            [
                0xC1
                | (
                    (version & 0x1F)
                    << 1
                )
            ]
        )
        + bytes(
            [
                section_number & 0xFF,
                last_section_number & 0xFF,
            ]
        )
        + transport_stream_id.to_bytes(
            2,
            "big",
        )
        + original_network_id.to_bytes(
            2,
            "big",
        )
        + bytes(
            [
                segment_last_section_number
                & 0xFF,
                last_table_id & 0xFF,
            ]
        )
        + event_data
    )

    # section_length cuenta desde service_id
    # hasta el final del CRC.

    section_length = (
        len(body)
        + 4
    )

    if section_length > MAX_SECTION_LENGTH:
        raise ValueError(
            "La sección EIT excede 4093 bytes"
        )

    header = bytes(
        [
            table_id,
            0xF0
            | (
                (section_length >> 8)
                & 0x0F
            ),
            section_length & 0xFF,
        ]
    )

    section_without_crc = (
        header
        + body
    )

    return append_crc32(
        section_without_crc
    )


def build_present_following(
    *,
    service_id: int,
    transport_stream_id: int,
    original_network_id: int,
    version: int,
    present: EITEvent | None,
    following: EITEvent | None,
    language: str = "spa",
    extended_description: bool = True,
) -> list[bytes]:

    sections = []

    if present is not None:

        sections.append(
            build_eit_section(
                table_id=0x4E,
                service_id=service_id,
                transport_stream_id=(
                    transport_stream_id
                ),
                original_network_id=(
                    original_network_id
                ),
                version=version,
                section_number=0,
                last_section_number=(
                    1
                    if following is not None
                    else 0
                ),
                segment_last_section_number=(
                    1
                    if following is not None
                    else 0
                ),
                last_table_id=0x4E,
                events=[present],
                language=language,
                extended_description=(
                    extended_description
                ),
            )
        )

    if following is not None:

        sections.append(
            build_eit_section(
                table_id=0x4E,
                service_id=service_id,
                transport_stream_id=(
                    transport_stream_id
                ),
                original_network_id=(
                    original_network_id
                ),
                version=version,
                section_number=1,
                last_section_number=1,
                segment_last_section_number=1,
                last_table_id=0x4E,
                events=[following],
                language=language,
                extended_description=(
                    extended_description
                ),
            )
        )

    return sections

def split_events_into_sections(
    events: list[EITEvent],
    *,
    language: str,
    extended_description: bool,
    max_event_bytes: int = 4078,
) -> list[list[EITEvent]]:
    """
    Divide eventos en secciones sin superar
    section_length = 4093.

    4093
    - 11 bytes cabecera EIT después de section_length
    - 4 bytes CRC
    = 4078 bytes disponibles para eventos.
    """

    if not events:
        return [[]]

    sections = []
    current = []
    current_size = 0

    for event in events:

        encoded = build_event(
            event,
            language=language,
            extended_description=extended_description,
        )

        event_size = len(encoded)

        if event_size > max_event_bytes:
            raise ValueError(
                f"Evento {event.event_id} demasiado grande "
                "para una sección EIT"
            )

        if (
            current
            and current_size + event_size > max_event_bytes
        ):
            sections.append(current)
            current = []
            current_size = 0

        current.append(event)
        current_size += event_size

    if current:
        sections.append(current)

    return sections


def build_schedule(
    *,
    service_id: int,
    transport_stream_id: int,
    original_network_id: int,
    version: int,
    events: list[EITEvent],
    base_midnight: datetime,
    language: str = "spa",
    extended_description: bool = True,
    table_versions: dict[int, int] | None = None,
) -> list[bytes]:
    """
    Construye EIT Schedule Actual TS.

    table_id:
        0x50 .. 0x5F

    Cada table_id:
        4 días

    Cada table_id:
        32 segmentos

    Cada segmento:
        3 horas

    Cada segmento:
        hasta 8 secciones
    """

    if not events:
        return []

    events = sorted(
        events,
        key=lambda event: event.start_time,
    )

    # -------------------------------------------------
    # Clasificar eventos por table_id / segmento.
    # -------------------------------------------------

    buckets = {}

    max_table_index = 0
    max_global_segment = 0

    for event in events:

        delta = (
            event.start_time
            - base_midnight
        )

        total_seconds = int(
            delta.total_seconds()
        )

        if total_seconds < 0:
            continue

        global_segment = (
            total_seconds // (3 * 3600)
        )

        table_index = (
            global_segment // 32
        )

        if table_index > 15:
            continue

        segment_index = (
            global_segment % 32
        )

        key = (
            table_index,
            segment_index,
        )

        buckets.setdefault(
            key,
            [],
        ).append(event)

        max_table_index = max(
            max_table_index,
            table_index,
        )

        max_global_segment = max(
            max_global_segment,
            global_segment,
        )

    if not buckets:
        return []

    last_table_id = (
        0x50 + max_table_index
    )

    # -------------------------------------------------
    # Primero calculamos todas las secciones para
    # conocer last_section_number de cada subtabla.
    # -------------------------------------------------

    table_segments = {}

    for table_index in range(
        max_table_index + 1
    ):

        segments = {}

        # La última tabla puede terminar antes del
        # segmento 31.
        if table_index < max_table_index:
            last_segment = 31
        else:
            last_segment = (
                max_global_segment % 32
            )

        for segment_index in range(
            last_segment + 1
        ):

            segment_events = buckets.get(
                (
                    table_index,
                    segment_index,
                ),
                [],
            )

            sections = split_events_into_sections(
                segment_events,
                language=language,
                extended_description=(
                    extended_description
                ),
            )

            if len(sections) > 8:
                raise ValueError(
                    "Un segmento EIT de 3 horas "
                    "requiere más de 8 secciones. "
                    "Hay demasiada información "
                    "para cumplir la estructura."
                )

            segments[
                segment_index
            ] = sections

        table_segments[
            table_index
        ] = segments

    result = []

    # -------------------------------------------------
    # Generar las secciones definitivas.
    # -------------------------------------------------

    for table_index, segments in (
        table_segments.items()
    ):

        table_id = (
            0x50 + table_index
        )

        last_segment_index = max(
            segments.keys()
        )

        last_segment_sections = (
            segments[last_segment_index]
        )

        last_section_number = (
            last_segment_index * 8
            + len(last_segment_sections)
            - 1
        )

        for (
            segment_index,
            section_groups,
        ) in segments.items():

            first_section_number = (
                segment_index * 8
            )

            segment_last_section_number = (
                first_section_number
                + len(section_groups)
                - 1
            )

            for local_index, section_events in enumerate(
                section_groups
            ):

                section_number = (
                    first_section_number
                    + local_index
                )

                result.append(
                    build_eit_section(
                        table_id=table_id,
                        service_id=service_id,
                        transport_stream_id=(
                            transport_stream_id
                        ),
                        original_network_id=(
                            original_network_id
                        ),
                        version=(
                            table_versions.get(
                                table_id,
                                version,
                            )
                            if table_versions
                            else version
                        ),
                        section_number=(
                            section_number
                        ),
                        last_section_number=(
                            last_section_number
                        ),
                        segment_last_section_number=(
                            segment_last_section_number
                        ),
                        last_table_id=(
                            last_table_id
                        ),
                        events=section_events,
                        language=language,
                        extended_description=(
                            extended_description
                        ),
                    )
                )

    return result
