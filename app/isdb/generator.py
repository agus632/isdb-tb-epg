import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import (
    EPGChannel,
    EPGProgramme,
    ISDBEventID,
    ISDBEPGGroup,
    ISDBEPGGroupMember,
    ISDBService,
    ISDBTransportStream,
)

from app.isdb.eit import (
    EITEvent,
    build_present_following,
    build_schedule,
)

from app.isdb.packetizer import (
    packetize_sections,
)


OUTPUT_DIR = Path(
    "/opt/isdb-epg/data/generated"
)


def ensure_eit_version_table(
    db: Session,
) -> None:
    """
    Persistent EIT version state.

    One version is maintained for each EIT subtable:
        mux_id + service_id + table_id

    version_number is 5 bits: 0..31.
    """

    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS isdb_eit_version_state (
                mux_id INTEGER NOT NULL,
                service_id INTEGER NOT NULL,
                table_id INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                version INTEGER NOT NULL,
                PRIMARY KEY (
                    mux_id,
                    service_id,
                    table_id
                )
            )
            """
        )
    )


def eit_event_fingerprint(
    event: EITEvent,
) -> str:
    """
    Stable representation of EIT event content.
    """

    return "|".join(
        [
            str(event.event_id),
            event.start_time.isoformat(),
            event.stop_time.isoformat(),
            event.title or "",
            event.subtitle or "",
            event.description or "",
            str(event.running_status),
            "1" if event.free_ca_mode else "0",
        ]
    )


def eit_content_hash(
    *,
    service_id: int,
    transport_stream_id: int,
    original_network_id: int,
    table_id: int,
    language: str,
    extended_description: bool,
    events: list[EITEvent],
) -> str:

    parts = [
        str(service_id),
        str(transport_stream_id),
        str(original_network_id),
        str(table_id),
        language,
        "1" if extended_description else "0",
    ]

    parts.extend(
        eit_event_fingerprint(event)
        for event in events
    )

    raw = "\n".join(parts)

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


def get_eit_version(
    db: Session,
    *,
    mux_id: int,
    service_id: int,
    table_id: int,
    content_hash: str,
    initial_version: int = 0,
) -> int:
    """
    Return the persistent version_number for one EIT subtable.

    Same content:
        keep version.

    Changed content:
        increment version modulo 32.
    """

    ensure_eit_version_table(db)

    row = db.execute(
        text(
            """
            SELECT content_hash, version
            FROM isdb_eit_version_state
            WHERE mux_id = :mux_id
              AND service_id = :service_id
              AND table_id = :table_id
            """
        ),
        {
            "mux_id": mux_id,
            "service_id": service_id,
            "table_id": table_id,
        },
    ).mappings().first()

    if row is None:

        version = int(initial_version) & 0x1F

        db.execute(
            text(
                """
                INSERT INTO isdb_eit_version_state (
                    mux_id,
                    service_id,
                    table_id,
                    content_hash,
                    version
                )
                VALUES (
                    :mux_id,
                    :service_id,
                    :table_id,
                    :content_hash,
                    :version
                )
                """
            ),
            {
                "mux_id": mux_id,
                "service_id": service_id,
                "table_id": table_id,
                "content_hash": content_hash,
                "version": version,
            },
        )

        db.commit()

        return version

    version = int(row["version"]) & 0x1F

    if row["content_hash"] == content_hash:
        return version

    version = (version + 1) & 0x1F

    db.execute(
        text(
            """
            UPDATE isdb_eit_version_state
            SET content_hash = :content_hash,
                version = :version
            WHERE mux_id = :mux_id
              AND service_id = :service_id
              AND table_id = :table_id
            """
        ),
        {
            "content_hash": content_hash,
            "version": version,
            "mux_id": mux_id,
            "service_id": service_id,
            "table_id": table_id,
        },
    )

    db.commit()

    return version



def generate_target_pf_pair(
    db: Session,
    target_mux: ISDBTransportStream,
):
    """
    Generate both P/F representations for one target MUX.

    The EPG lookup and programme-to-event conversion are
    performed only once per service.

    Returns:
        {
            "actual": [...],  # table_id 0x4E
            "other":  [...],  # table_id 0x4F
        }
    """

    network = target_mux.network

    if network is None:
        raise ValueError(
            f"MUX {target_mux.id} has no network"
        )

    timezone_name = (
        network.timezone
        or "America/Argentina/Buenos_Aires"
    )

    local_tz = ZoneInfo(
        timezone_name
    )

    now_local = datetime.now(
        local_tz
    )

    now_utc = (
        now_local
        .astimezone(ZoneInfo("UTC"))
        .replace(tzinfo=None)
    )

    language = (
        target_mux.eit_language
        or "spa"
    )

    initial_version = int(
        target_mux.eit_version
        or 0
    ) & 0x1F

    extended_description = bool(
        target_mux.eit_extended_description
    )

    services = (
        db.query(ISDBService)
        .filter(
            ISDBService.transport_stream_id_fk
            == target_mux.id,
            ISDBService.enabled.is_(True),
        )
        .order_by(
            ISDBService.service_id
        )
        .all()
    )

    actual_sections = []
    other_sections = []

    for service in services:

        if (
            service.epg_source_id is None
            or service.epg_channel_id is None
        ):
            continue

        channel = (
            db.query(EPGChannel)
            .filter(
                EPGChannel.id
                == service.epg_channel_id
            )
            .first()
        )

        if channel is None:
            continue

        if not service.eit_present_following:
            continue

        present_programme, following_programme = (
            get_present_following_programmes(
                db,
                service,
                now_utc,
            )
        )

        present_event = None
        following_event = None

        if present_programme is not None:

            present_event = programme_to_event(
                db=db,
                service=service,
                programme=present_programme,
                running_status=int(
                    target_mux.eit_running_status
                    or 4
                ),
            )

            present_event = event_utc_to_local(
                present_event,
                local_tz,
            )

        if following_programme is not None:

            following_event = programme_to_event(
                db=db,
                service=service,
                programme=following_programme,
                running_status=1,
            )

            following_event = event_utc_to_local(
                following_event,
                local_tz,
            )

        if (
            present_event is None
            and following_event is None
        ):
            continue

        pf_events = [
            event
            for event in (
                present_event,
                following_event,
            )
            if event is not None
        ]

        # -------------------------------------
        # Actual TS - table_id 0x4E
        # -------------------------------------

        actual_hash = eit_content_hash(
            service_id=service.service_id,
            transport_stream_id=(
                target_mux.transport_stream_id
            ),
            original_network_id=(
                network.original_network_id
            ),
            table_id=0x4E,
            language=language,
            extended_description=(
                extended_description
            ),
            events=pf_events,
        )

        actual_version = get_eit_version(
            db,
            mux_id=target_mux.id,
            service_id=service.service_id,
            table_id=0x4E,
            content_hash=actual_hash,
            initial_version=initial_version,
        )

        actual_sections.extend(
            build_present_following(
                service_id=service.service_id,
                transport_stream_id=(
                    target_mux.transport_stream_id
                ),
                original_network_id=(
                    network.original_network_id
                ),
                present=present_event,
                following=following_event,
                version=actual_version,
                language=language,
                extended_description=(
                    extended_description
                ),
                table_id=0x4E,
            )
        )

        # -------------------------------------
        # Other TS - table_id 0x4F
        # -------------------------------------

        other_hash = eit_content_hash(
            service_id=service.service_id,
            transport_stream_id=(
                target_mux.transport_stream_id
            ),
            original_network_id=(
                network.original_network_id
            ),
            table_id=0x4F,
            language=language,
            extended_description=(
                extended_description
            ),
            events=pf_events,
        )

        other_version = get_eit_version(
            db,
            mux_id=target_mux.id,
            service_id=service.service_id,
            table_id=0x4F,
            content_hash=other_hash,
            initial_version=initial_version,
        )

        other_sections.extend(
            build_present_following(
                service_id=service.service_id,
                transport_stream_id=(
                    target_mux.transport_stream_id
                ),
                original_network_id=(
                    network.original_network_id
                ),
                present=present_event,
                following=following_event,
                version=other_version,
                language=language,
                extended_description=(
                    extended_description
                ),
                table_id=0x4F,
            )
        )

    return {
        "actual": actual_sections,
        "other": other_sections,
    }


def generate_group_pf_sections(
    db: Session,
    mux_id: int,
):
    """
    Generate P/F caches for every output MUX in the enabled
    EPG/EIT Group containing mux_id.

    Each target MUX is queried and converted only once.

    Returns:
        {
            output_mux_id: [sections...],
            ...
        }
    """

    current_mux = (
        db.query(ISDBTransportStream)
        .filter(
            ISDBTransportStream.id == mux_id
        )
        .first()
    )

    if current_mux is None:
        raise ValueError(
            f"MUX {mux_id} not found"
        )

    other_muxes = get_eit_other_muxes(
        db,
        current_mux.id,
    )

    targets = [
        current_mux,
        *other_muxes,
    ]

    # Generate Actual + Other representation exactly once
    # for every target transport stream.
    generated = {}

    for target_mux in targets:
        generated[target_mux.id] = (
            generate_target_pf_pair(
                db,
                target_mux,
            )
        )

    # Compose the cache required by each output MUX.
    #
    # For the output's own target use Actual (0x4E).
    # For every remote target use Other (0x4F).
    result = {}

    for output_mux in targets:

        sections = []

        for target_mux in targets:

            pair = generated[
                target_mux.id
            ]

            if target_mux.id == output_mux.id:
                sections.extend(
                    pair["actual"]
                )
            else:
                sections.extend(
                    pair["other"]
                )

        result[output_mux.id] = sections

    return result



def schedule_events_by_table(
    events: list[EITEvent],
    base_midnight: datetime,
    table_id_base: int = 0x50,
) -> dict[int, list[EITEvent]]:
    """
    Group Schedule events by their EIT table_id.

    Actual TS:
        0x50 = days 0..3
        ...
        0x5F = days 60..63

    Other TS:
        0x60 = days 0..3
        ...
        0x6F = days 60..63
    """

    if table_id_base not in (0x50, 0x60):
        raise ValueError(
            "table_id_base debe ser 0x50 o 0x60"
        )

    result: dict[int, list[EITEvent]] = {}

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

        if not 0 <= table_index <= 15:
            continue

        table_id = (
            table_id_base + table_index
        )

        result.setdefault(
            table_id,
            [],
        ).append(event)

    return result


def event_key(
    service: ISDBService,
    programme: EPGProgramme,
) -> str:

    raw = "|".join(
        [
            str(service.id),
            str(programme.source_id),
            programme.channel_xmltv_id,
            programme.start_time.isoformat(),
            programme.title or "",
        ]
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


def get_event_id(
    db: Session,
    service: ISDBService,
    programme: EPGProgramme,
) -> int:

    key = event_key(
        service,
        programme,
    )

    existing = (
        db.query(ISDBEventID)
        .filter(
            ISDBEventID.service_id_fk
            == service.id,
            ISDBEventID.event_key
            == key,
        )
        .first()
    )

    if existing:
        return existing.event_id

    used_ids = {
        row[0]
        for row in (
            db.query(ISDBEventID.event_id)
            .filter(
                ISDBEventID.service_id_fk
                == service.id
            )
            .all()
        )
    }

    # Event ID 0 se evita deliberadamente.
    # 0xFFFF se deja fuera también.

    for candidate in range(
        1,
        0xFFFF,
    ):
        if candidate not in used_ids:

            mapping = ISDBEventID(
                service_id_fk=service.id,
                event_key=key,
                event_id=candidate,
            )

            db.add(mapping)
            db.flush()

            return candidate

    raise RuntimeError(
        "No quedan Event ID disponibles "
        f"para servicio {service.service_name}"
    )


def programme_to_event(
    db: Session,
    service: ISDBService,
    programme: EPGProgramme,
    *,
    running_status: int,
) -> EITEvent:

    stop_time = programme.stop_time

    if stop_time is None:
        stop_time = (
            programme.start_time
            + timedelta(hours=1)
        )

    return EITEvent(
        event_id=get_event_id(
            db,
            service,
            programme,
        ),
        start_time=programme.start_time,
        stop_time=stop_time,
        title=programme.title or "Sin título",
        subtitle=programme.subtitle or "",
        description=programme.description or "",
        running_status=running_status,
        free_ca_mode=False,
    )


def event_utc_to_local(
    event: EITEvent | None,
    local_tz: ZoneInfo,
) -> EITEvent | None:

    if event is None:
        return None

    start_aware = (
        event.start_time
        .replace(tzinfo=ZoneInfo("UTC"))
    )

    stop_aware = (
        event.stop_time
        .replace(tzinfo=ZoneInfo("UTC"))
    )

    event.start_time = (
        start_aware
        .astimezone(local_tz)
        .replace(tzinfo=None)
    )

    event.stop_time = (
        stop_aware
        .astimezone(local_tz)
        .replace(tzinfo=None)
    )

    return event


def get_present_following_programmes(
    db: Session,
    service: ISDBService,
    now_utc: datetime,
):

    if not service.epg_channel_id:
        return None, None

    channel = service.epg_channel

    if not channel:
        return None, None

    present = (
        db.query(EPGProgramme)
        .filter(
            EPGProgramme.source_id
            == channel.source_id,

            EPGProgramme.channel_xmltv_id
            == channel.xmltv_id,

            EPGProgramme.start_time
            <= now_utc,

            EPGProgramme.stop_time
            > now_utc,
        )
        .order_by(
            EPGProgramme.start_time.desc()
        )
        .first()
    )

    if present:

        following = (
            db.query(EPGProgramme)
            .filter(
                EPGProgramme.source_id
                == channel.source_id,

                EPGProgramme.channel_xmltv_id
                == channel.xmltv_id,

                EPGProgramme.start_time
                >= present.stop_time,
            )
            .order_by(
                EPGProgramme.start_time
            )
            .first()
        )

    else:

        following = (
            db.query(EPGProgramme)
            .filter(
                EPGProgramme.source_id
                == channel.source_id,

                EPGProgramme.channel_xmltv_id
                == channel.xmltv_id,

                EPGProgramme.start_time
                > now_utc,
            )
            .order_by(
                EPGProgramme.start_time
            )
            .first()
        )

    return present, following


def get_schedule_programmes(
    db: Session,
    service: ISDBService,
    start_utc: datetime,
    stop_utc: datetime,
):

    if not service.epg_channel_id:
        return []

    channel = service.epg_channel

    if not channel:
        return []

    return (
        db.query(EPGProgramme)
        .filter(
            EPGProgramme.source_id
            == channel.source_id,

            EPGProgramme.channel_xmltv_id
            == channel.xmltv_id,

            EPGProgramme.start_time
            >= start_utc,

            EPGProgramme.start_time
            < stop_utc,
        )
        .order_by(
            EPGProgramme.start_time
        )
        .all()
    )


def generate_mux_eit(
    db: Session,
    mux_id: int,
):
    mux = db.get(
        ISDBTransportStream,
        mux_id,
    )

    if not mux:
        raise ValueError(
            "Transport Stream inexistente"
        )

    network = mux.network

    timezone_name = (
        network.timezone
        or "America/Argentina/Buenos_Aires"
    )

    local_tz = ZoneInfo(
        timezone_name
    )

    now_local = datetime.now(
        local_tz
    )

    # Nuestra DB almacena UTC naive.

    now_utc = (
        now_local
        .astimezone(ZoneInfo("UTC"))
        .replace(tzinfo=None)
    )

    # ABNT Schedule:
    # t0 = medianoche local.
    #
    # Pero EPGProgramme está almacenado en UTC.
    # Convertimos esa medianoche a UTC para
    # consultar la base.

    base_local = now_local.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    base_utc = (
        base_local
        .astimezone(ZoneInfo("UTC"))
        .replace(tzinfo=None)
    )

    schedule_days = max(
        1,
        min(
            mux.eit_schedule_days or 7,
            64,
        ),
    )

    schedule_stop_utc = (
        base_utc
        + timedelta(
            days=schedule_days
        )
    )

    all_sections = []

    stats = {
        "services": 0,
        "pf_sections": 0,
        "schedule_sections": 0,
        "schedule_events": 0,
    }

    services = (
        db.query(ISDBService)
        .filter(
            ISDBService.transport_stream_id_fk
            == mux.id,

            ISDBService.enabled == True,
        )
        .order_by(
            ISDBService.service_id
        )
        .all()
    )

    for service in services:

        if not service.epg_channel_id:
            continue

        stats["services"] += 1

        # ==========================================
        # PRESENT / FOLLOWING
        # ==========================================

        if service.eit_present_following:

            present_programme, following_programme = (
                get_present_following_programmes(
                    db,
                    service,
                    now_utc,
                )
            )

            present_event = None
            following_event = None

            if present_programme:

                present_event = programme_to_event(
                    db,
                    service,
                    present_programme,
                    running_status=(
                        mux.eit_running_status
                        if mux.eit_running_status is not None
                        else 4
                    ),
                )

                present_event = event_utc_to_local(
                    present_event,
                    local_tz,
                )

            if following_programme:

                following_event = programme_to_event(
                    db,
                    service,
                    following_programme,
                    running_status=1,
                )

                following_event = event_utc_to_local(
                    following_event,
                    local_tz,
                )

            pf_sections = build_present_following(
                service_id=service.service_id,
                transport_stream_id=(
                    mux.transport_stream_id
                ),
                original_network_id=(
                    network.original_network_id
                ),
                version=mux.eit_version or 0,
                present=present_event,
                following=following_event,
                language=(
                    mux.eit_language or "spa"
                ),
                extended_description=(
                    bool(
                        mux.eit_extended_description
                    )
                ),
            )

            all_sections.extend(
                pf_sections
            )

            stats["pf_sections"] += len(
                pf_sections
            )

        # ==========================================
        # SCHEDULE
        # ==========================================

        if service.eit_schedule:

            programmes = (
                get_schedule_programmes(
                    db,
                    service,
                    base_utc,
                    schedule_stop_utc,
                )
            )

            schedule_events = []

            for programme in programmes:

                event = programme_to_event(
                    db,
                    service,
                    programme,
                    running_status=0,
                )

                event = event_utc_to_local(
                    event,
                    local_tz,
                )

                schedule_events.append(
                    event
                )

            base_local_naive = (
                base_local.replace(
                    tzinfo=None
                )
            )

            schedule_sections = (
                build_schedule(
                    service_id=service.service_id,
                    transport_stream_id=(
                        mux.transport_stream_id
                    ),
                    original_network_id=(
                        network.original_network_id
                    ),
                    version=(
                        mux.eit_version or 0
                    ),
                    events=schedule_events,
                    base_midnight=(
                        base_local_naive
                    ),
                    language=(
                        mux.eit_language or "spa"
                    ),
                    extended_description=(
                        bool(
                            mux.eit_extended_description
                        )
                    ),
                )
            )

            all_sections.extend(
                schedule_sections
            )

            stats[
                "schedule_sections"
            ] += len(schedule_sections)

            stats[
                "schedule_events"
            ] += len(schedule_events)

    db.commit()

    if not all_sections:
        raise ValueError(
            "No se generaron secciones EIT. "
            "Verifique servicios y programación EPG."
        )

    ts_data, next_cc = packetize_sections(
        all_sections,
        pid=mux.eit_pid or 0x0012,
        continuity_counter=0,
    )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path = (
        OUTPUT_DIR
        / f"eit_mux_{mux.id}.ts"
    )

    output_path.write_bytes(
        ts_data
    )

    stats["sections"] = len(
        all_sections
    )

    stats["packets"] = (
        len(ts_data) // 188
    )

    stats["bytes"] = len(
        ts_data
    )

    stats["pid"] = (
        mux.eit_pid or 0x0012
    )

    stats["output"] = str(
        output_path
    )

    stats["next_cc"] = next_cc

    return stats
def get_eit_other_muxes(
    db: Session,
    mux_id: int,
) -> list[ISDBTransportStream]:
    """
    Return the other transport streams that share the
    enabled EPG/EIT Group with mux_id.

    The current MUX itself is never returned.

    A MUX without an enabled EPG/EIT Group has no
    EIT Other transport streams.
    """

    membership = (
        db.query(ISDBEPGGroupMember)
        .join(
            ISDBEPGGroup,
            ISDBEPGGroup.id
            == ISDBEPGGroupMember.group_id,
        )
        .filter(
            ISDBEPGGroupMember.transport_stream_id_fk
            == mux_id,
            ISDBEPGGroup.enabled.is_(True),
        )
        .first()
    )

    if membership is None:
        return []

    return (
        db.query(ISDBTransportStream)
        .join(
            ISDBEPGGroupMember,
            ISDBEPGGroupMember.transport_stream_id_fk
            == ISDBTransportStream.id,
        )
        .filter(
            ISDBEPGGroupMember.group_id
            == membership.group_id,
            ISDBTransportStream.id != mux_id,
            ISDBTransportStream.enabled.is_(True),
        )
        .order_by(
            ISDBTransportStream.transport_stream_id
        )
        .all()
    )


def generate_other_mux_sections(
    db: Session,
    target_mux: ISDBTransportStream,
    generate: str = "all",
):
    """
    Generate EIT Other TS sections for target_mux.

    P/F:
        table_id 0x4F

    Schedule:
        table_id 0x60..0x6F

    The TSID, ONID, services and EPG data belong to
    target_mux, not to the MUX carrying these sections.
    """

    network = target_mux.network

    if network is None:
        raise ValueError(
            f"MUX {target_mux.id} has no network"
        )

    timezone_name = (
        network.timezone
        or "America/Argentina/Buenos_Aires"
    )

    local_tz = ZoneInfo(
        timezone_name
    )

    now_local = datetime.now(
        local_tz
    )

    now_utc = (
        now_local
        .astimezone(ZoneInfo("UTC"))
        .replace(tzinfo=None)
    )

    base_local = now_local.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    base_local_naive = (
        base_local.replace(
            tzinfo=None
        )
    )

    base_utc = (
        base_local
        .astimezone(ZoneInfo("UTC"))
        .replace(tzinfo=None)
    )

    schedule_days = max(
        1,
        min(
            int(
                target_mux.eit_schedule_days
                or 7
            ),
            64,
        ),
    )

    schedule_stop = (
        base_utc
        + timedelta(
            days=schedule_days
        )
    )

    prime_days = max(
        1,
        min(
            int(
                target_mux.eit_prime_days
                or 8
            ),
            schedule_days,
        ),
    )

    language = (
        target_mux.eit_language
        or "spa"
    )

    version = int(
        target_mux.eit_version
        or 0
    ) & 0x1F

    extended_description = bool(
        target_mux.eit_extended_description
    )

    services = (
        db.query(ISDBService)
        .filter(
            ISDBService.transport_stream_id_fk
            == target_mux.id,
            ISDBService.enabled.is_(True),
        )
        .order_by(
            ISDBService.service_id
        )
        .all()
    )

    pf_sections = []
    schedule_prime_sections = []
    schedule_later_sections = []

    for service in services:

        if (
            service.epg_source_id is None
            or service.epg_channel_id is None
        ):
            continue

        channel = (
            db.query(EPGChannel)
            .filter(
                EPGChannel.id
                == service.epg_channel_id
            )
            .first()
        )

        if channel is None:
            continue

        # =====================================
        # PRESENT / FOLLOWING OTHER
        # =====================================

        if (
            generate in ("all", "pf")
            and service.eit_present_following
        ):

            present_programme, following_programme = (
                get_present_following_programmes(
                    db,
                    service,
                    now_utc,
                )
            )

            present_event = None
            following_event = None

            if present_programme is not None:

                present_event = (
                    programme_to_event(
                        db=db,
                        service=service,
                        programme=present_programme,
                        running_status=int(
                            target_mux.eit_running_status
                            or 4
                        ),
                    )
                )

                present_event = event_utc_to_local(
                    present_event,
                    local_tz,
                )

            if following_programme is not None:

                following_event = (
                    programme_to_event(
                        db=db,
                        service=service,
                        programme=following_programme,
                        running_status=1,
                    )
                )

                following_event = event_utc_to_local(
                    following_event,
                    local_tz,
                )

            if (
                present_event is not None
                or following_event is not None
            ):

                pf_events = [
                    event
                    for event in (
                        present_event,
                        following_event,
                    )
                    if event is not None
                ]

                pf_hash = eit_content_hash(
                    service_id=service.service_id,
                    transport_stream_id=(
                        target_mux.transport_stream_id
                    ),
                    original_network_id=(
                        network.original_network_id
                    ),
                    table_id=0x4F,
                    language=language,
                    extended_description=(
                        extended_description
                    ),
                    events=pf_events,
                )

                pf_version = get_eit_version(
                    db,
                    mux_id=target_mux.id,
                    service_id=service.service_id,
                    table_id=0x4F,
                    content_hash=pf_hash,
                    initial_version=version,
                )

                sections = (
                    build_present_following(
                        service_id=service.service_id,
                        transport_stream_id=(
                            target_mux.transport_stream_id
                        ),
                        original_network_id=(
                            network.original_network_id
                        ),
                        present=present_event,
                        following=following_event,
                        version=pf_version,
                        language=language,
                        extended_description=(
                            extended_description
                        ),
                        table_id=0x4F,
                    )
                )

                pf_sections.extend(
                    sections
                )

        # =====================================
        # SCHEDULE OTHER
        # =====================================

        if (
            generate not in ("all", "schedule")
            or not service.eit_schedule
        ):
            continue

        programmes = (
            get_schedule_programmes(
                db,
                service,
                base_utc,
                schedule_stop,
            )
        )

        events = []

        for programme in programmes:

            event = programme_to_event(
                db=db,
                service=service,
                programme=programme,
                running_status=0,
            )

            events.append(
                event
            )

        if not events:
            continue

        local_events = []

        for event in events:

            event = event_utc_to_local(
                event,
                local_tz,
            )

            local_events.append(
                event
            )

        schedule_groups = (
            schedule_events_by_table(
                local_events,
                base_local_naive,
                table_id_base=0x60,
            )
        )

        table_versions = {}

        for (
            table_id,
            table_events,
        ) in schedule_groups.items():

            table_hash = eit_content_hash(
                service_id=service.service_id,
                transport_stream_id=(
                    target_mux.transport_stream_id
                ),
                original_network_id=(
                    network.original_network_id
                ),
                table_id=table_id,
                language=language,
                extended_description=(
                    extended_description
                ),
                events=table_events,
            )

            table_versions[table_id] = (
                get_eit_version(
                    db,
                    mux_id=target_mux.id,
                    service_id=(
                        service.service_id
                    ),
                    table_id=table_id,
                    content_hash=table_hash,
                    initial_version=version,
                )
            )

        all_schedule_sections = (
            build_schedule(
                service_id=service.service_id,
                transport_stream_id=(
                    target_mux.transport_stream_id
                ),
                original_network_id=(
                    network.original_network_id
                ),
                events=local_events,
                base_midnight=(
                    base_local_naive
                ),
                version=version,
                language=language,
                extended_description=(
                    extended_description
                ),
                table_versions=table_versions,
                table_id_base=0x60,
            )
        )

        # =====================================
        # Separate Other prime / later
        # =====================================

        prime_table_count = (
            prime_days + 3
        ) // 4

        prime_last_table_id = (
            0x60
            + prime_table_count
            - 1
        )

        for section in all_schedule_sections:

            if not section:
                continue

            table_id = section[0]

            if (
                table_id
                <= prime_last_table_id
            ):
                schedule_prime_sections.append(
                    section
                )
            else:
                schedule_later_sections.append(
                    section
                )

    return {
        "pf": pf_sections,
        "schedule_prime": (
            schedule_prime_sections
        ),
        "schedule_later": (
            schedule_later_sections
        ),
    }



def generate_target_schedule_pair(
    db: Session,
    target_mux: ISDBTransportStream,
):
    """
    Generate Schedule Actual + Other for one target MUX.

    EPG programmes are queried and converted to EITEvent only once
    per service. From the same local events we build:

        Actual TS: 0x50..0x5F
        Other TS:  0x60..0x6F

    Version state remains independent for every table_id.
    """

    network = target_mux.network

    if network is None:
        raise ValueError(
            f"MUX {target_mux.id} has no network"
        )

    timezone_name = (
        network.timezone
        or "America/Argentina/Buenos_Aires"
    )

    local_tz = ZoneInfo(timezone_name)

    now_local = datetime.now(local_tz)

    base_local = now_local.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    base_local_naive = base_local.replace(
        tzinfo=None
    )

    base_utc = (
        base_local
        .astimezone(ZoneInfo("UTC"))
        .replace(tzinfo=None)
    )

    schedule_days = max(
        1,
        min(
            int(
                target_mux.eit_schedule_days
                or 7
            ),
            64,
        ),
    )

    schedule_stop = (
        base_utc
        + timedelta(days=schedule_days)
    )

    prime_days = max(
        1,
        min(
            int(
                target_mux.eit_prime_days
                or 8
            ),
            schedule_days,
        ),
    )

    language = (
        target_mux.eit_language
        or "spa"
    )

    initial_version = int(
        target_mux.eit_version
        or 0
    ) & 0x1F

    extended_description = bool(
        target_mux.eit_extended_description
    )

    services = (
        db.query(ISDBService)
        .filter(
            ISDBService.transport_stream_id_fk
            == target_mux.id,
            ISDBService.enabled.is_(True),
        )
        .order_by(
            ISDBService.service_id
        )
        .all()
    )

    actual_prime = []
    actual_later = []
    other_prime = []
    other_later = []

    actual_prime_table_count = (
        prime_days + 3
    ) // 4

    actual_prime_last_table_id = (
        0x50
        + actual_prime_table_count
        - 1
    )

    other_prime_last_table_id = (
        0x60
        + actual_prime_table_count
        - 1
    )

    for service in services:

        if (
            service.epg_source_id is None
            or service.epg_channel_id is None
        ):
            continue

        channel = (
            db.query(EPGChannel)
            .filter(
                EPGChannel.id
                == service.epg_channel_id
            )
            .first()
        )

        if channel is None:
            continue

        if not service.eit_schedule:
            continue

        # -------------------------------------
        # Query and convert programme data ONCE
        # -------------------------------------

        programmes = get_schedule_programmes(
            db,
            service,
            base_utc,
            schedule_stop,
        )

        events = []

        for programme in programmes:

            event = programme_to_event(
                db=db,
                service=service,
                programme=programme,
                running_status=0,
            )

            events.append(event)

        if not events:
            continue

        local_events = []

        for event in events:

            local_events.append(
                event_utc_to_local(
                    event,
                    local_tz,
                )
            )

        # =====================================
        # ACTUAL TS - 0x50..0x5F
        # =====================================

        actual_groups = schedule_events_by_table(
            local_events,
            base_local_naive,
            table_id_base=0x50,
        )

        actual_versions = {}

        for table_id, table_events in actual_groups.items():

            table_hash = eit_content_hash(
                service_id=service.service_id,
                transport_stream_id=(
                    target_mux.transport_stream_id
                ),
                original_network_id=(
                    network.original_network_id
                ),
                table_id=table_id,
                language=language,
                extended_description=(
                    extended_description
                ),
                events=table_events,
            )

            actual_versions[table_id] = (
                get_eit_version(
                    db,
                    mux_id=target_mux.id,
                    service_id=(
                        service.service_id
                    ),
                    table_id=table_id,
                    content_hash=table_hash,
                    initial_version=initial_version,
                )
            )

        actual_sections = build_schedule(
            service_id=service.service_id,
            transport_stream_id=(
                target_mux.transport_stream_id
            ),
            original_network_id=(
                network.original_network_id
            ),
            events=local_events,
            base_midnight=base_local_naive,
            version=initial_version,
            language=language,
            extended_description=(
                extended_description
            ),
            table_versions=actual_versions,
            table_id_base=0x50,
        )

        for section in actual_sections:

            if not section:
                continue

            if section[0] <= actual_prime_last_table_id:
                actual_prime.append(section)
            else:
                actual_later.append(section)

        # =====================================
        # OTHER TS - 0x60..0x6F
        # =====================================

        other_groups = schedule_events_by_table(
            local_events,
            base_local_naive,
            table_id_base=0x60,
        )

        other_versions = {}

        for table_id, table_events in other_groups.items():

            table_hash = eit_content_hash(
                service_id=service.service_id,
                transport_stream_id=(
                    target_mux.transport_stream_id
                ),
                original_network_id=(
                    network.original_network_id
                ),
                table_id=table_id,
                language=language,
                extended_description=(
                    extended_description
                ),
                events=table_events,
            )

            other_versions[table_id] = (
                get_eit_version(
                    db,
                    mux_id=target_mux.id,
                    service_id=(
                        service.service_id
                    ),
                    table_id=table_id,
                    content_hash=table_hash,
                    initial_version=initial_version,
                )
            )

        other_sections = build_schedule(
            service_id=service.service_id,
            transport_stream_id=(
                target_mux.transport_stream_id
            ),
            original_network_id=(
                network.original_network_id
            ),
            events=local_events,
            base_midnight=base_local_naive,
            version=initial_version,
            language=language,
            extended_description=(
                extended_description
            ),
            table_versions=other_versions,
            table_id_base=0x60,
        )

        for section in other_sections:

            if not section:
                continue

            if section[0] <= other_prime_last_table_id:
                other_prime.append(section)
            else:
                other_later.append(section)

    return {
        "actual_prime": actual_prime,
        "actual_later": actual_later,
        "other_prime": other_prime,
        "other_later": other_later,
    }


def generate_group_schedule_sections(
    db: Session,
    mux_id: int,
):
    """
    Generate shared Schedule caches for all MUXes visible
    from mux_id's enabled EPG/EIT group.

    Each target MUX is generated exactly once. Each output gets:

        own MUX     -> Actual Schedule 0x50..0x5F
        other MUXes -> Other Schedule  0x60..0x6F
    """

    current_mux = (
        db.query(ISDBTransportStream)
        .filter(
            ISDBTransportStream.id == mux_id
        )
        .first()
    )

    if current_mux is None:
        raise ValueError(
            f"MUX {mux_id} not found"
        )

    other_muxes = get_eit_other_muxes(
        db,
        current_mux.id,
    )

    targets = [
        current_mux,
        *other_muxes,
    ]

    target_results = {}

    for target_mux in targets:

        target_results[target_mux.id] = (
            generate_target_schedule_pair(
                db,
                target_mux,
            )
        )

    result = {}

    # Compose each output with its Actual first,
    # then Other sections from all remaining targets.
    for output_mux in targets:

        own = target_results[
            output_mux.id
        ]

        actual_prime = list(
            own["actual_prime"]
        )

        actual_later = list(
            own["actual_later"]
        )

        other_prime = []
        other_later = []

        for target_mux in targets:

            if target_mux.id == output_mux.id:
                continue

            target = target_results[
                target_mux.id
            ]

            other_prime.extend(
                target["other_prime"]
            )

            other_later.extend(
                target["other_later"]
            )

        # Keep the legacy combined caches for compatibility
        # while the broadcaster is migrated to independent
        # Actual / Other transmission cadences.
        prime = (
            actual_prime
            + other_prime
        )

        later = (
            actual_later
            + other_later
        )

        result[output_mux.id] = {
            "schedule_prime": prime,
            "schedule_later": later,
            "actual_prime": actual_prime,
            "other_prime": other_prime,
            "actual_later": actual_later,
            "other_later": other_later,
        }

    db.commit()

    return result


def generate_mux_sections(
    db: Session,
    mux_id: int,
    generate: str = "all",
):
    """
    Generate EIT sections for continuous broadcasting.

    generate:
        "all"      -> P/F + Schedule
        "pf"       -> P/F only
        "schedule" -> Schedule only

    Returns raw PSI/SI sections, not MPEG-TS packets.

    The broadcaster is responsible for packetization
    and continuity counters.
    """

    generate = (
        generate
        or "all"
    ).lower()

    if generate not in (
        "all",
        "pf",
        "schedule",
    ):
        raise ValueError(
            f"Invalid EIT generation mode: {generate}"
        )

    mux = (
        db.query(ISDBTransportStream)
        .filter(
            ISDBTransportStream.id == mux_id
        )
        .first()
    )

    if mux is None:
        raise ValueError(
            f"MUX {mux_id} not found"
        )

    network = mux.network

    if network is None:
        raise ValueError(
            f"MUX {mux_id} has no network"
        )

    timezone_name = (
        network.timezone
        or "America/Argentina/Buenos_Aires"
    )

    local_tz = ZoneInfo(
        timezone_name
    )

    now_local = datetime.now(
        local_tz
    )

    now_utc = (
        now_local
        .astimezone(ZoneInfo("UTC"))
        .replace(tzinfo=None)
    )

    base_local = now_local.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    base_local_naive = (
        base_local.replace(
            tzinfo=None
        )
    )

    base_utc = (
        base_local
        .astimezone(ZoneInfo("UTC"))
        .replace(tzinfo=None)
    )

    schedule_days = max(
        1,
        min(
            int(
                mux.eit_schedule_days
                or 7
            ),
            64,
        ),
    )

    schedule_stop = (
        base_utc
        + timedelta(
            days=schedule_days
        )
    )

    prime_days = max(
        1,
        min(
            int(
                mux.eit_prime_days
                or 8
            ),
            schedule_days,
        ),
    )

    prime_stop = (
        base_utc
        + timedelta(
            days=prime_days
        )
    )

    language = (
        mux.eit_language
        or "spa"
    )

    version = int(
        mux.eit_version
        or 0
    ) & 0x1F

    extended_description = bool(
        mux.eit_extended_description
    )

    free_ca = bool(
        mux.eit_free_ca_mode
    )

    services = (
        db.query(ISDBService)
        .filter(
            ISDBService.transport_stream_id_fk
            == mux.id,
            ISDBService.enabled.is_(True),
        )
        .order_by(
            ISDBService.service_id
        )
        .all()
    )

    pf_sections = []
    schedule_prime_sections = []
    schedule_later_sections = []

    for service in services:

        if (
            service.epg_source_id is None
            or service.epg_channel_id is None
        ):
            continue

        channel = (
            db.query(EPGChannel)
            .filter(
                EPGChannel.id
                == service.epg_channel_id
            )
            .first()
        )

        if channel is None:
            continue

        # =====================================
        # PRESENT / FOLLOWING
        # =====================================

        if (
            generate in ("all", "pf")
            and service.eit_present_following
        ):

            present_programme, following_programme = (
                get_present_following_programmes(
                    db,
                    service,
                    now_utc,
                )
            )

            present_event = None
            following_event = None

            if present_programme is not None:

                present_event = (
                    programme_to_event(
                        db=db,
                        service=service,
                        programme=present_programme,
                        running_status=int(
                            mux.eit_running_status
                            or 4
                        ),
                    )
                )

                present_event = event_utc_to_local(
                    present_event,
                    local_tz,
                )

            if following_programme is not None:

                following_event = (
                    programme_to_event(
                        db=db,
                        service=service,
                        programme=following_programme,
                        running_status=1,
                    )
                )

                following_event = event_utc_to_local(
                    following_event,
                    local_tz,
                )

            if (
                present_event is not None
                or following_event is not None
            ):

                pf_events = [
                    event
                    for event in (
                        present_event,
                        following_event,
                    )
                    if event is not None
                ]

                pf_hash = eit_content_hash(
                    service_id=service.service_id,
                    transport_stream_id=(
                        mux.transport_stream_id
                    ),
                    original_network_id=(
                        network.original_network_id
                    ),
                    table_id=0x4E,
                    language=language,
                    extended_description=(
                        extended_description
                    ),
                    events=pf_events,
                )

                pf_version = get_eit_version(
                    db,
                    mux_id=mux.id,
                    service_id=service.service_id,
                    table_id=0x4E,
                    content_hash=pf_hash,
                    initial_version=version,
                )

                sections = (
                    build_present_following(
                        service_id=service.service_id,
                        transport_stream_id=(
                            mux.transport_stream_id
                        ),
                        original_network_id=(
                            network.original_network_id
                        ),
                        present=present_event,
                        following=following_event,
                        version=pf_version,
                        language=language,
                        extended_description=(
                            extended_description
                        ),
                    )
                )

                pf_sections.extend(
                    sections
                )

        # =====================================
        # SCHEDULE
        # =====================================

        if (
            generate not in ("all", "schedule")
            or not service.eit_schedule
        ):
            continue

        programmes = (
            get_schedule_programmes(
                db,
                service,
                base_utc,
                schedule_stop,
            )
        )

        events = []

        for programme in programmes:

            event = programme_to_event(
                db=db,
                service=service,
                programme=programme,
                running_status=0,
            )

            events.append(
                event
            )

        if not events:
            continue

        local_events = []

        for event in events:

            event = event_utc_to_local(
                event,
                local_tz,
            )

            local_events.append(
                event
            )

        schedule_groups = (
            schedule_events_by_table(
                local_events,
                base_local_naive,
            )
        )

        table_versions = {}

        for (
            table_id,
            table_events,
        ) in schedule_groups.items():

            table_hash = eit_content_hash(
                service_id=service.service_id,
                transport_stream_id=(
                    mux.transport_stream_id
                ),
                original_network_id=(
                    network.original_network_id
                ),
                table_id=table_id,
                language=language,
                extended_description=(
                    extended_description
                ),
                events=table_events,
            )

            table_versions[table_id] = (
                get_eit_version(
                    db,
                    mux_id=mux.id,
                    service_id=(
                        service.service_id
                    ),
                    table_id=table_id,
                    content_hash=table_hash,
                    initial_version=version,
                )
            )

        all_schedule_sections = (
            build_schedule(
                service_id=service.service_id,
                transport_stream_id=(
                    mux.transport_stream_id
                ),
                original_network_id=(
                    network.original_network_id
                ),
                events=local_events,
                base_midnight=(
                    base_local_naive
                ),
                version=version,
                language=language,
                extended_description=(
                    extended_description
                ),
                table_versions=table_versions,
            )
        )

        # =====================================
        # Separate prime / later by table_id
        # =====================================
        #
        # One schedule table_id covers 4 days.
        #
        # With prime_days=8:
        #
        # 0x50 = days 0..3
        # 0x51 = days 4..7
        #
        # 0x52+ = later
        #

        prime_table_count = (
            prime_days + 3
        ) // 4

        prime_last_table_id = (
            0x50
            + prime_table_count
            - 1
        )

        for section in all_schedule_sections:

            if not section:
                continue

            table_id = section[0]

            if (
                table_id
                <= prime_last_table_id
            ):
                schedule_prime_sections.append(
                    section
                )
            else:
                schedule_later_sections.append(
                    section
                )

    # =====================================
    # EIT OTHER TRANSPORT STREAMS
    # =====================================
    #
    # If this MUX belongs to an enabled EPG/EIT
    # Group, include the EIT of every other enabled
    # MUX in that group.
    #
    # The remote MUX identity is preserved:
    #
    #   P/F      -> 0x4F
    #   Schedule -> 0x60..0x6F
    #
    # All sections are returned together with the
    # local Actual TS sections. The broadcaster will
    # packetize them on the current output EIT PID.
    # =====================================

    other_muxes = get_eit_other_muxes(
        db,
        mux.id,
    )

    for other_mux in other_muxes:

        other_sections = (
            generate_other_mux_sections(
                db,
                other_mux,
                generate=generate,
            )
        )

        pf_sections.extend(
            other_sections["pf"]
        )

        schedule_prime_sections.extend(
            other_sections[
                "schedule_prime"
            ]
        )

        schedule_later_sections.extend(
            other_sections[
                "schedule_later"
            ]
        )

    db.commit()

    return {
        "mux": mux,
        "pf": pf_sections,
        "schedule_prime": (
            schedule_prime_sections
        ),
        "schedule_later": (
            schedule_later_sections
        ),
    }
