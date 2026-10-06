from dataclasses import dataclass

from app.isdb.crc import append_crc32


SDT_PID = 0x0011
SDT_ACTUAL = 0x42

MAX_SECTION_LENGTH = 1021

RUNNING_STATUS = {
    "undefined": 0,
    "not-running": 1,
    "starts-soon": 2,
    "pausing": 3,
    "running": 4,
    "off-air": 5,
}


@dataclass
class SDTService:
    service_id: int
    service_name: str
    provider_name: str = ""
    service_type: int = 1
    running_status: str = "running"
    free_ca_mode: bool = False
    eit_present_following: bool = False
    eit_schedule: bool = False


def _encode_text(value: str) -> bytes:
    """
    Encode service/provider text for the service descriptor.

    For the first implementation we require single-byte
    ISO-8859-1 compatible text. This preserves ASCII and the
    usual Latin characters without silently replacing data.

    ARIB/ISDB character encoding can be added separately
    without changing the SDT section builder.
    """
    try:
        return value.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"SDT text cannot be encoded: {value!r}"
        ) from exc


def build_service_descriptor(
    service: SDTService,
) -> bytes:
    """
    DVB/ISDB service_descriptor (descriptor_tag 0x48).
    """
    if not 0 <= service.service_type <= 0xFF:
        raise ValueError(
            "service_type must be between 0 and 255"
        )

    provider = _encode_text(
        service.provider_name
    )
    name = _encode_text(
        service.service_name
    )

    if len(provider) > 255:
        raise ValueError(
            "provider_name is too long"
        )

    if len(name) > 255:
        raise ValueError(
            "service_name is too long"
        )

    payload = (
        bytes([
            service.service_type,
            len(provider),
        ])
        + provider
        + bytes([
            len(name),
        ])
        + name
    )

    if len(payload) > 255:
        raise ValueError(
            "service_descriptor is too large"
        )

    return (
        bytes([
            0x48,
            len(payload),
        ])
        + payload
    )


def build_service(
    service: SDTService,
) -> bytes:
    if not 0 <= service.service_id <= 0xFFFF:
        raise ValueError(
            "service_id must be between 0 and 65535"
        )

    try:
        running_status = RUNNING_STATUS[
            service.running_status
        ]
    except KeyError as exc:
        raise ValueError(
            "Invalid SDT running_status: "
            f"{service.running_status!r}"
        ) from exc

    descriptors = build_service_descriptor(
        service
    )

    descriptors_length = len(descriptors)

    if descriptors_length > 0x0FFF:
        raise ValueError(
            "SDT descriptor loop is too large"
        )

    eit_flags = (
        0xFC
        | (
            (1 if service.eit_schedule else 0)
            << 1
        )
        | (
            1
            if service.eit_present_following
            else 0
        )
    )

    status_and_length = (
        ((running_status & 0x07) << 13)
        | (
            (1 if service.free_ca_mode else 0)
            << 12
        )
        | descriptors_length
    )

    return (
        service.service_id.to_bytes(
            2,
            "big",
        )
        + bytes([
            eit_flags,
        ])
        + status_and_length.to_bytes(
            2,
            "big",
        )
        + descriptors
    )


def build_sdt_section(
    *,
    transport_stream_id: int,
    original_network_id: int,
    version: int,
    section_number: int,
    last_section_number: int,
    services: list[SDTService],
) -> bytes:
    if not 0 <= transport_stream_id <= 0xFFFF:
        raise ValueError(
            "transport_stream_id must be "
            "between 0 and 65535"
        )

    if not 0 <= original_network_id <= 0xFFFF:
        raise ValueError(
            "original_network_id must be "
            "between 0 and 65535"
        )

    if not 0 <= version <= 31:
        raise ValueError(
            "version must be between 0 and 31"
        )

    if not 0 <= section_number <= 255:
        raise ValueError(
            "section_number must be between 0 and 255"
        )

    if not 0 <= last_section_number <= 255:
        raise ValueError(
            "last_section_number must be "
            "between 0 and 255"
        )

    service_data = b"".join(
        build_service(service)
        for service in services
    )

    body = (
        transport_stream_id.to_bytes(
            2,
            "big",
        )
        + bytes([
            0xC1
            | ((version & 0x1F) << 1)
        ])
        + bytes([
            section_number,
            last_section_number,
        ])
        + original_network_id.to_bytes(
            2,
            "big",
        )
        + bytes([
            0xFF,
        ])
        + service_data
    )

    # section_length counts everything following the
    # section_length field through the end of the CRC.
    section_length = (
        len(body)
        + 4
    )

    if section_length > MAX_SECTION_LENGTH:
        raise ValueError(
            "SDT section exceeds maximum "
            f"section_length ({section_length})"
        )

    header = (
        bytes([
            SDT_ACTUAL,
            0xF0
            | (
                (section_length >> 8)
                & 0x0F
            ),
            section_length & 0xFF,
        ])
    )

    return append_crc32(
        header + body
    )


def build_sdt_actual(
    *,
    transport_stream_id: int,
    original_network_id: int,
    version: int,
    services: list[SDTService],
) -> list[bytes]:
    """
    Build SDT Actual sections (table_id 0x42).

    Services are split across sections when necessary.
    """

    encoded_services = [
        (
            service,
            build_service(service),
        )
        for service in services
    ]

    # Fixed bytes counted in section_length:
    #
    # TSID                    2
    # version/current_next    1
    # section_number          1
    # last_section_number     1
    # original_network_id     2
    # reserved_future_use     1
    # CRC                     4
    #
    # Total fixed = 12 bytes.
    max_service_bytes = (
        MAX_SECTION_LENGTH - 12
    )

    groups: list[list[SDTService]] = []
    current: list[SDTService] = []
    current_size = 0

    for service, encoded in encoded_services:
        size = len(encoded)

        if size > max_service_bytes:
            raise ValueError(
                "Single SDT service is too large "
                f"(service_id={service.service_id})"
            )

        if (
            current
            and current_size + size
            > max_service_bytes
        ):
            groups.append(current)
            current = []
            current_size = 0

        current.append(service)
        current_size += size

    if current:
        groups.append(current)

    # An empty SDT is valid and still needs one section.
    if not groups:
        groups = [[]]

    if len(groups) > 256:
        raise ValueError(
            "SDT requires more than 256 sections"
        )

    last_section_number = (
        len(groups) - 1
    )

    return [
        build_sdt_section(
            transport_stream_id=(
                transport_stream_id
            ),
            original_network_id=(
                original_network_id
            ),
            version=version,
            section_number=index,
            last_section_number=(
                last_section_number
            ),
            services=group,
        )
        for index, group in enumerate(groups)
    ]


def generate_mux_sdt_sections(
    db,
    mux_id: int,
) -> list[bytes]:
    """
    Generate SDT Actual sections for one MUX from the
    independent SDT configuration stored in SQLite.

    Returns an empty list when:
      - the MUX does not exist,
      - it has no SDT configuration,
      - or its SDT configuration is disabled.
    """
    # Imported here to keep the binary SDT builder
    # independent from SQLAlchemy/model initialization.
    from app.models import (
        ISDBTransportStream,
        ISDBSDTService,
    )

    mux = db.get(
        ISDBTransportStream,
        mux_id,
    )

    if mux is None:
        return []

    config = mux.sdt_config

    if config is None:
        return []

    if not config.enabled:
        return []

    services_db = (
        db.query(ISDBSDTService)
        .filter(
            ISDBSDTService.sdt_config_id_fk
            == config.id,
            ISDBSDTService.enabled.is_(True),
        )
        .order_by(
            ISDBSDTService.service_id
        )
        .all()
    )

    services = [
        SDTService(
            service_id=service.service_id,
            service_name=service.service_name,
            provider_name=service.provider_name,
            service_type=service.service_type,
            running_status=service.running_status,
            free_ca_mode=service.free_ca_mode,
            eit_present_following=(
                service.eit_present_following
            ),
            eit_schedule=service.eit_schedule,
        )
        for service in services_db
    ]

    return build_sdt_actual(
        transport_stream_id=(
            mux.transport_stream_id
        ),
        original_network_id=(
            mux.network.original_network_id
        ),
        version=config.version,
        services=services,
    )
