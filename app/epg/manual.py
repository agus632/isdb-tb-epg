from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import (
    EPGChannel,
    EPGProgramme,
    EPGSource,
)


MANUAL_SOURCE_TYPE = "manual"
DEFAULT_BLOCK_HOURS = 6
DEFAULT_DAYS = 8


def manual_xmltv_id(source_id: int) -> str:
    return f"manual.{source_id}"


def regenerate_manual_epg(
    source: EPGSource,
    db: Session,
    *,
    days: int = DEFAULT_DAYS,
    block_hours: int = DEFAULT_BLOCK_HOURS,
) -> None:
    """
    Regenerate synthetic EPG for one manual source.

    EPGProgramme timestamps are stored as UTC-naive,
    matching the rest of the application.
    """

    if source.source_type != MANUAL_SOURCE_TYPE:
        raise ValueError(
            f"Source {source.id} is not a manual EPG source"
        )

    if days < 1 or days > 64:
        raise ValueError("days must be between 1 and 64")

    if block_hours < 1 or block_hours > 24:
        raise ValueError(
            "block_hours must be between 1 and 24"
        )

    channel_id = manual_xmltv_id(source.id)

    channel = (
        db.query(EPGChannel)
        .filter(
            EPGChannel.source_id == source.id,
            EPGChannel.xmltv_id == channel_id,
        )
        .one_or_none()
    )

    if channel is None:
        channel = EPGChannel(
            source_id=source.id,
            xmltv_id=channel_id,
            display_name=source.name,
            icon_url=None,
        )
        db.add(channel)
    else:
        channel.display_name = source.name

    # Remove previous synthetic programmes for this source.
    (
        db.query(EPGProgramme)
        .filter(
            EPGProgramme.source_id == source.id,
            EPGProgramme.channel_xmltv_id == channel_id,
        )
        .delete(synchronize_session=False)
    )

    now_utc = datetime.now(timezone.utc)

    # Argentina is UTC-3.
    #
    # Build blocks aligned to Argentine local clock:
    # 00:00, 06:00, 12:00, 18:00.
    argentina_offset = timedelta(hours=-3)

    now_local = now_utc + argentina_offset

    local_day_start = now_local.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    end_local = (
        local_day_start
        + timedelta(days=days + 1)
    )

    current_local = local_day_start

    programme_count = 0

    while current_local < end_local:
        next_local = (
            current_local
            + timedelta(hours=block_hours)
        )

        start_utc = (
            current_local
            - argentina_offset
        ).replace(tzinfo=None)

        stop_utc = (
            next_local
            - argentina_offset
        ).replace(tzinfo=None)

        programme = EPGProgramme(
            source_id=source.id,
            channel_xmltv_id=channel_id,
            start_time=start_utc,
            stop_time=stop_utc,
            title=source.name,
            subtitle=None,
            description=(
                f"Programación de {source.name}"
            ),
            category=None,
        )

        db.add(programme)

        programme_count += 1
        current_local = next_local

    source.channel_count = 1
    source.programme_count = programme_count
    source.last_update = datetime.utcnow()
    source.last_status = "OK"
    source.last_message = (
        f"EPG manual generado: "
        f"{programme_count} programas, "
        f"{days} días, bloques de {block_hours} h"
    )

    db.commit()
