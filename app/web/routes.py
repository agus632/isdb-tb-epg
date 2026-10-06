import logging
from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.isdb.manager import eit_manager
from app.database import get_db
from app.epg.xmltv_importer import import_xmltv
from datetime import datetime, timezone

from fastapi import Query
from sqlalchemy import or_

from app.epg.manual import regenerate_manual_epg
from app.models import EPGChannel, EPGProgramme, EPGSource

from app.models import (
    EPGChannel,
    EPGProgramme,
    EPGSource,
    ISDBNetwork,
    ISDBTransportStream,
    ISDBService,
    ISDBEventID,
    ISDBEPGGroup,
    ISDBEPGGroupMember,
    ISDBSDTGlobalConfig,
    ISDBSDTConfig,
    ISDBSDTService,
)
from app.isdb.generator import generate_mux_eit

router = APIRouter()

templates = Jinja2Templates(
    directory="app/web/templates"
)


logger = logging.getLogger(__name__)

@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    sources = db.query(EPGSource).all()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "sources": sources,
        },
    )


@router.get("/sources")
def sources(request: Request, db: Session = Depends(get_db)):
    epg_sources = db.query(EPGSource).order_by(EPGSource.id).all()

    return templates.TemplateResponse(
        request=request,
        name="sources.html",
        context={
            "sources": epg_sources,
        },
    )


@router.post("/sources/add")
def add_source(
    name: str = Form(...),
    url: str = Form(...),
    update_interval: int = Form(6),
    db: Session = Depends(get_db),
):
    source = EPGSource(
        name=name,
        source_type="xmltv",
        url=url,
        update_interval=update_interval,
        enabled=True,
    )

    db.add(source)
    db.commit()

    return RedirectResponse(
        url="/sources",
        status_code=303,
    )




@router.post("/sources/manual/add")
def add_manual_source(
    name: str = Form(...),
    days: int = Form(8),
    block_hours: int = Form(6),
    db: Session = Depends(get_db),
):
    name = name.strip()

    if not name:
        return RedirectResponse(
            url="/sources",
            status_code=303,
        )

    days = max(1, min(days, 64))
    block_hours = max(1, min(block_hours, 24))

    source = EPGSource(
        name=name,
        source_type="manual",
        url="manual://",
        update_interval=24,
        manual_days=days,
        manual_block_hours=block_hours,
        enabled=True,
    )

    db.add(source)
    db.commit()
    db.refresh(source)

    try:
        regenerate_manual_epg(
            source,
            db,
            days=days,
            block_hours=block_hours,
        )

    except Exception as exc:
        source.last_status = "ERROR"
        source.last_message = str(exc)
        db.commit()

    return RedirectResponse(
        url="/sources",
        status_code=303,
    )


@router.post("/sources/{source_id}/toggle")
def toggle_source(
    source_id: int,
    db: Session = Depends(get_db),
):
    source = db.get(EPGSource, source_id)

    if source:
        source.enabled = not source.enabled
        db.commit()

    return RedirectResponse(
        url="/sources",
        status_code=303,
    )


@router.post("/sources/{source_id}/delete")
def delete_source(
    source_id: int,
    db: Session = Depends(get_db),
):
    source = db.get(EPGSource, source_id)

    if not source:
        return RedirectResponse(
            url="/sources",
            status_code=303,
        )

    # Protect EPG sources that are still mapped to ISDB services.
    #
    # Check both references:
    #   1. isdb_services.epg_source_id
    #   2. isdb_services.epg_channel_id -> epg_channels.source_id
    #
    # This validation is intentionally done in the application because
    # existing SQLite databases may not have foreign-key enforcement enabled.
    mapped_service = (
        db.query(ISDBService.id)
        .outerjoin(
            EPGChannel,
            ISDBService.epg_channel_id == EPGChannel.id,
        )
        .filter(
            or_(
                ISDBService.epg_source_id == source_id,
                EPGChannel.source_id == source_id,
            )
        )
        .first()
    )

    if mapped_service:
        return RedirectResponse(
            url="/sources?delete_error=in_use",
            status_code=303,
        )

    try:
        # Programmes are not part of the EPGSource ORM cascade,
        # so remove them explicitly before deleting the source.
        db.query(EPGProgramme).filter(
            EPGProgramme.source_id == source_id
        ).delete(synchronize_session=False)

        # EPGChannel rows are removed by the EPGSource.channels
        # delete-orphan cascade.
        db.delete(source)
        db.commit()

    except Exception:
        db.rollback()
        logger.exception(
            "Error deleting EPG source %s",
            source_id,
        )

        return RedirectResponse(
            url="/sources?delete_error=database",
            status_code=303,
        )

    return RedirectResponse(
        url="/sources?deleted=1",
        status_code=303,
    )
@router.post("/sources/{source_id}/update")
def update_source(
    source_id: int,
    db: Session = Depends(get_db),
):
    source = db.get(EPGSource, source_id)

    if source:
        try:
            if source.source_type == "manual":
                regenerate_manual_epg(
                    source,
                    db,
                    days=source.manual_days or 8,
                    block_hours=source.manual_block_hours or 6,
                )
            else:
                import_xmltv(source, db)

        except Exception as exc:
            source.last_status = "ERROR"
            source.last_message = str(exc)
            db.commit()

    return RedirectResponse(
        url="/sources",
        status_code=303,
    )

@router.get("/channels")
def channels(
    request: Request,
    q: str = Query(default=""),
    source_id: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
):
    per_page = 50

    query = db.query(EPGChannel)

    # Convertimos source_id solamente si viene con valor.
    selected_source = None

    if source_id.strip():
        try:
            selected_source = int(source_id)

            query = query.filter(
                EPGChannel.source_id == selected_source
            )

        except ValueError:
            selected_source = None

    # Buscador por nombre o ID XMLTV.
    if q.strip():
        search = f"%{q.strip()}%"

        query = query.filter(
            or_(
                EPGChannel.display_name.ilike(search),
                EPGChannel.xmltv_id.ilike(search),
            )
        )

    total = query.count()

    channels = (
        query
        .order_by(EPGChannel.display_name)
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    total_pages = max(
        1,
        (total + per_page - 1) // per_page,
    )

    sources = (
        db.query(EPGSource)
        .order_by(EPGSource.name)
        .all()
    )

    return templates.TemplateResponse(
        request=request,
        name="channels.html",
        context={
            "channels": channels,
            "sources": sources,
            "q": q,
            "selected_source": selected_source,
            "page": page,
            "total": total,
            "total_pages": total_pages,
        },
    )

@router.get("/channels/{channel_id}")
def channel_detail(
    channel_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    channel = db.get(EPGChannel, channel_id)

    if not channel:
        return RedirectResponse(
            url="/channels",
            status_code=303,
        )

    now_utc = datetime.now(
        timezone.utc
    ).replace(tzinfo=None)

    programmes = (
        db.query(EPGProgramme)
        .filter(
            EPGProgramme.source_id == channel.source_id,
            EPGProgramme.channel_xmltv_id == channel.xmltv_id,
            EPGProgramme.stop_time >= now_utc,
        )
        .order_by(EPGProgramme.start_time)
        .limit(100)
        .all()
    )

    return templates.TemplateResponse(
        request=request,
        name="channel_detail.html",
        context={
            "channel": channel,
            "programmes": programmes,
            "now_utc": now_utc,
        },
    )

# =========================================================
# RED ISDB-Tb
# =========================================================

@router.get("/isdb")
def isdb_config(
    request: Request,
    db: Session = Depends(get_db),
):
    networks = (
        db.query(ISDBNetwork)
        .order_by(ISDBNetwork.id)
        .all()
    )

    return templates.TemplateResponse(
        request=request,
        name="isdb.html",
        context={
            "networks": networks,
        },
    )


@router.post("/isdb/network/add")
def add_isdb_network(
    name: str = Form(...),
    network_id: int = Form(...),
    original_network_id: int = Form(...),
    country_code: str = Form("ARG"),
    timezone_name: str = Form(
        "America/Argentina/Buenos_Aires"
    ),
    db: Session = Depends(get_db),
):
    if not 0 <= network_id <= 65535:
        return RedirectResponse(
            url="/isdb",
            status_code=303,
        )

    if not 0 <= original_network_id <= 65535:
        return RedirectResponse(
            url="/isdb",
            status_code=303,
        )

    network = ISDBNetwork(
        name=name.strip(),
        network_id=network_id,
        original_network_id=original_network_id,
        country_code=country_code.strip().upper(),
        timezone=timezone_name.strip(),
    )

    db.add(network)
    db.commit()

    return RedirectResponse(
        url="/isdb",
        status_code=303,
    )


@router.post("/isdb/network/{network_id}/delete")
def delete_isdb_network(
    network_id: int,
    db: Session = Depends(get_db),
):
    network = db.get(ISDBNetwork, network_id)

    if network:
        db.delete(network)
        db.commit()

    return RedirectResponse(
        url="/isdb",
        status_code=303,
    )



# =========================================================
# EPG / EIT GROUPS
# =========================================================

@router.get("/isdb/epg-groups")
def isdb_epg_groups(
    request: Request,
    db: Session = Depends(get_db),
):
    groups = (
        db.query(ISDBEPGGroup)
        .order_by(ISDBEPGGroup.name)
        .all()
    )

    muxes = (
        db.query(ISDBTransportStream)
        .order_by(
            ISDBTransportStream.network_id_fk,
            ISDBTransportStream.transport_stream_id,
        )
        .all()
    )

    return templates.TemplateResponse(
        request=request,
        name="isdb_epg_groups.html",
        context={
            "groups": groups,
            "muxes": muxes,
        },
    )


@router.post("/isdb/epg-groups/add")
def add_isdb_epg_group(
    name: str = Form(...),
    mux_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    name = name.strip()

    if not name:
        return RedirectResponse(
            url="/isdb/epg-groups?error=name",
            status_code=303,
        )

    existing_name = (
        db.query(ISDBEPGGroup)
        .filter(ISDBEPGGroup.name == name)
        .first()
    )

    if existing_name is not None:
        return RedirectResponse(
            url="/isdb/epg-groups?error=duplicate_name",
            status_code=303,
        )

    selected_mux_ids = set(mux_ids)

    if selected_mux_ids:
        already_assigned = (
            db.query(ISDBEPGGroupMember)
            .filter(
                ISDBEPGGroupMember.transport_stream_id_fk.in_(
                    selected_mux_ids
                )
            )
            .first()
        )

        if already_assigned is not None:
            return RedirectResponse(
                url="/isdb/epg-groups?error=mux_assigned",
                status_code=303,
            )

    group = ISDBEPGGroup(
        name=name,
        enabled=True,
    )

    db.add(group)

    try:
        db.flush()

        for mux_id in sorted(selected_mux_ids):
            mux = db.get(
                ISDBTransportStream,
                mux_id,
            )

            if mux is None:
                continue

            db.add(
                ISDBEPGGroupMember(
                    group_id=group.id,
                    transport_stream_id_fk=mux.id,
                )
            )

        db.commit()

    except Exception:
        db.rollback()
        logger.exception(
            "Error creating EPG/EIT Group"
        )

        return RedirectResponse(
            url="/isdb/epg-groups?error=database",
            status_code=303,
        )

    return RedirectResponse(
        url="/isdb/epg-groups",
        status_code=303,
    )


@router.get("/isdb/epg-groups/{group_id}")
def isdb_epg_group_detail(
    group_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    group = db.get(
        ISDBEPGGroup,
        group_id,
    )

    if group is None:
        return RedirectResponse(
            url="/isdb/epg-groups",
            status_code=303,
        )

    muxes = (
        db.query(ISDBTransportStream)
        .order_by(
            ISDBTransportStream.network_id_fk,
            ISDBTransportStream.transport_stream_id,
        )
        .all()
    )

    return templates.TemplateResponse(
        request=request,
        name="isdb_epg_group_detail.html",
        context={
            "group": group,
            "muxes": muxes,
        },
    )


@router.post("/isdb/epg-groups/{group_id}/edit")
def edit_isdb_epg_group(
    group_id: int,
    name: str = Form(...),
    mux_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    group = db.get(
        ISDBEPGGroup,
        group_id,
    )

    if group is None:
        return RedirectResponse(
            url="/isdb/epg-groups",
            status_code=303,
        )

    name = name.strip()

    if not name:
        return RedirectResponse(
            url="/isdb/epg-groups?error=name",
            status_code=303,
        )

    duplicate_name = (
        db.query(ISDBEPGGroup)
        .filter(
            ISDBEPGGroup.name == name,
            ISDBEPGGroup.id != group.id,
        )
        .first()
    )

    if duplicate_name is not None:
        return RedirectResponse(
            url="/isdb/epg-groups?error=duplicate_name",
            status_code=303,
        )

    selected_mux_ids = set(mux_ids)

    if selected_mux_ids:
        conflicting_member = (
            db.query(ISDBEPGGroupMember)
            .filter(
                ISDBEPGGroupMember.transport_stream_id_fk.in_(
                    selected_mux_ids
                ),
                ISDBEPGGroupMember.group_id != group.id,
            )
            .first()
        )

        if conflicting_member is not None:
            return RedirectResponse(
                url="/isdb/epg-groups?error=mux_assigned",
                status_code=303,
            )

    try:
        group.name = name

        (
            db.query(ISDBEPGGroupMember)
            .filter(
                ISDBEPGGroupMember.group_id
                == group.id
            )
            .delete(
                synchronize_session=False
            )
        )

        for mux_id in sorted(selected_mux_ids):
            mux = db.get(
                ISDBTransportStream,
                mux_id,
            )

            if mux is None:
                continue

            db.add(
                ISDBEPGGroupMember(
                    group_id=group.id,
                    transport_stream_id_fk=mux.id,
                )
            )

        db.commit()

    except Exception:
        db.rollback()

        logger.exception(
            "Error updating EPG/EIT Group %s",
            group_id,
        )

        return RedirectResponse(
            url="/isdb/epg-groups?error=database",
            status_code=303,
        )

    return RedirectResponse(
        url=f"/isdb/epg-groups/{group_id}",
        status_code=303,
    )


@router.post("/isdb/epg-groups/{group_id}/toggle")
def toggle_isdb_epg_group(
    group_id: int,
    db: Session = Depends(get_db),
):
    group = db.get(
        ISDBEPGGroup,
        group_id,
    )

    if group is not None:
        group.enabled = not group.enabled
        db.commit()

    return RedirectResponse(
        url=f"/isdb/epg-groups/{group_id}",
        status_code=303,
    )


@router.post("/isdb/epg-groups/{group_id}/delete")
def delete_isdb_epg_group(
    group_id: int,
    db: Session = Depends(get_db),
):
    group = db.get(
        ISDBEPGGroup,
        group_id,
    )

    if group is not None:
        db.delete(group)
        db.commit()

    return RedirectResponse(
        url="/isdb/epg-groups",
        status_code=303,
    )


# =========================================================
# SDT
# =========================================================

@router.get("/isdb/sdt/global")
def isdb_sdt_global(
    request: Request,
    db: Session = Depends(get_db),
):
    config = db.query(ISDBSDTGlobalConfig).first()

    if config is None:
        config = ISDBSDTGlobalConfig(
            default_provider_name="",
            default_service_type=1,
            default_running_status="running",
            default_free_ca_mode=False,
            default_eit_present_following=True,
            default_eit_schedule=True,
            default_service_enabled=True,
        )
        db.add(config)
        db.commit()
        db.refresh(config)

    return templates.TemplateResponse(
        request=request,
        name="isdb_sdt_global.html",
        context={
            "config": config,
        },
    )


@router.post("/isdb/sdt/global")
def isdb_sdt_global_save(
    default_provider_name: str = Form(""),
    default_service_type: str = Form("1"),
    default_running_status: str = Form("running"),
    default_free_ca_mode: str | None = Form(None),
    default_eit_present_following: str | None = Form(None),
    default_eit_schedule: str | None = Form(None),
    default_service_enabled: str | None = Form(None),
    db: Session = Depends(get_db),
):
    config = db.query(ISDBSDTGlobalConfig).first()

    if config is None:
        config = ISDBSDTGlobalConfig()
        db.add(config)

    if len(default_provider_name) > 255:
        raise HTTPException(
            status_code=400,
            detail="default_provider_name is too long",
        )

    try:
        parsed_service_type = int(default_service_type, 10)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid default_service_type",
        )

    if not 0 <= parsed_service_type <= 0xFF:
        raise HTTPException(
            status_code=400,
            detail="default_service_type must be between 0 and 255",
        )

    valid_running_status = {
        "undefined",
        "not-running",
        "starts-soon",
        "pausing",
        "running",
        "off-air",
    }

    if default_running_status not in valid_running_status:
        raise HTTPException(
            status_code=400,
            detail="Invalid default_running_status",
        )

    # Do not strip provider_name.
    config.default_provider_name = default_provider_name
    config.default_service_type = parsed_service_type
    config.default_running_status = default_running_status
    config.default_free_ca_mode = (
        default_free_ca_mode is not None
    )
    config.default_eit_present_following = (
        default_eit_present_following is not None
    )
    config.default_eit_schedule = (
        default_eit_schedule is not None
    )
    config.default_service_enabled = (
        default_service_enabled is not None
    )

    db.commit()

    return RedirectResponse(
        url="/isdb/sdt/global",
        status_code=303,
    )


@router.get("/isdb/sdt")
def isdb_sdt(
    request: Request,
    db: Session = Depends(get_db),
):
    configs = (
        db.query(ISDBSDTConfig)
        .join(ISDBSDTConfig.transport_stream)
        .order_by(ISDBTransportStream.transport_stream_id)
        .all()
    )

    return templates.TemplateResponse(
        request=request,
        name="isdb_sdt.html",
        context={
            "configs": configs,
        },
    )


@router.get("/isdb/sdt/new")
def isdb_sdt_new(
    request: Request,
    db: Session = Depends(get_db),
):
    used_mux_ids = {
        row[0]
        for row in (
            db.query(ISDBSDTConfig.transport_stream_id_fk)
            .all()
        )
    }

    available_muxes = (
        db.query(ISDBTransportStream)
        .filter(
            ~ISDBTransportStream.id.in_(used_mux_ids)
        )
        .order_by(ISDBTransportStream.transport_stream_id)
        .all()
    )

    return templates.TemplateResponse(
        request=request,
        name="isdb_sdt_new.html",
        context={
            "available_muxes": available_muxes,
        },
    )


@router.post("/isdb/sdt/new")
def isdb_sdt_create(
    transport_stream_id_fk: int = Form(...),
    db: Session = Depends(get_db),
):
    mux = db.get(
        ISDBTransportStream,
        transport_stream_id_fk,
    )

    if not mux:
        raise HTTPException(
            status_code=404,
            detail="Transport Stream not found",
        )

    existing = (
        db.query(ISDBSDTConfig)
        .filter(
            ISDBSDTConfig.transport_stream_id_fk == mux.id
        )
        .first()
    )

    if existing:
        raise HTTPException(
            status_code=400,
            detail=(
                "Selected Transport Stream already has "
                "an SDT configuration"
            ),
        )

    config = ISDBSDTConfig(
        transport_stream_id_fk=mux.id,
        enabled=False,
        version=0,
        output_mode="none",
        output_address="239.255.1.1",
        output_port=5100,
        output_ttl=1,
        output_interface="",
    )

    db.add(config)
    db.commit()
    db.refresh(config)

    return RedirectResponse(
        url=f"/isdb/sdt/{config.id}",
        status_code=303,
    )


@router.post("/isdb/sdt/{config_id}/toggle")
def isdb_sdt_toggle(
    config_id: int,
    db: Session = Depends(get_db),
):
    config = db.get(ISDBSDTConfig, config_id)

    if not config:
        raise HTTPException(
            status_code=404,
            detail="SDT configuration not found",
        )

    config.enabled = not config.enabled

    db.commit()

    return RedirectResponse(
        url=f"/isdb/sdt/{config.id}",
        status_code=303,
    )


@router.post("/isdb/sdt/{config_id}/link")
def isdb_sdt_link_mux(
    config_id: int,
    transport_stream_id_fk: int = Form(...),
    db: Session = Depends(get_db),
):
    config = db.get(ISDBSDTConfig, config_id)

    if not config:
        raise HTTPException(
            status_code=404,
            detail="SDT configuration not found",
        )

    mux = db.get(
        ISDBTransportStream,
        transport_stream_id_fk,
    )

    if not mux:
        raise HTTPException(
            status_code=404,
            detail="Transport Stream not found",
        )

    conflict = (
        db.query(ISDBSDTConfig)
        .filter(
            ISDBSDTConfig.transport_stream_id_fk == mux.id,
            ISDBSDTConfig.id != config.id,
        )
        .first()
    )

    if conflict:
        raise HTTPException(
            status_code=400,
            detail=(
                "Selected Transport Stream already has "
                "an SDT configuration"
            ),
        )

    if config.transport_stream_id_fk != mux.id:
        config.transport_stream_id_fk = mux.id

        # The generated SDT identity changes because TSID/ONID
        # are obtained from the linked MUX.
        config.version = (config.version + 1) % 32

        db.commit()

    return RedirectResponse(
        url=f"/isdb/sdt/{config.id}",
        status_code=303,
    )


@router.get("/isdb/sdt/{config_id}")
def isdb_sdt_detail(
    config_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    config = db.get(ISDBSDTConfig, config_id)

    if not config:
        raise HTTPException(
            status_code=404,
            detail="SDT configuration not found",
        )

    mux = config.transport_stream

    services = (
        db.query(ISDBSDTService)
        .filter(
            ISDBSDTService.sdt_config_id_fk == config.id
        )
        .order_by(ISDBSDTService.service_id)
        .all()
    )

    used_mux_ids = {
        row[0]
        for row in (
            db.query(ISDBSDTConfig.transport_stream_id_fk)
            .filter(ISDBSDTConfig.id != config.id)
            .all()
        )
    }

    available_muxes = (
        db.query(ISDBTransportStream)
        .filter(
            ~ISDBTransportStream.id.in_(used_mux_ids)
        )
        .order_by(ISDBTransportStream.transport_stream_id)
        .all()
    )

    return templates.TemplateResponse(
        request=request,
        name="isdb_sdt_detail.html",
        context={
            "config": config,
            "mux": mux,
            "network": mux.network,
            "services": services,
            "available_muxes": available_muxes,
        },
    )


@router.get("/isdb/sdt/{config_id}/service/add")
def add_sdt_service_page(
    config_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    config = db.get(ISDBSDTConfig, config_id)

    if not config:
        raise HTTPException(
            status_code=404,
            detail="SDT config not found",
        )

    mux = config.transport_stream

    global_config = db.query(ISDBSDTGlobalConfig).first()

    if global_config is None:
        global_config = ISDBSDTGlobalConfig(
            default_provider_name="",
            default_service_type=1,
            default_running_status="running",
            default_free_ca_mode=False,
            default_eit_present_following=True,
            default_eit_schedule=True,
            default_service_enabled=True,
        )
        db.add(global_config)
        db.commit()
        db.refresh(global_config)

    return templates.TemplateResponse(
        request=request,
        name="isdb_sdt_service_add.html",
        context={
            "config": config,
            "mux": mux,
            "network": mux.network,
            "global_config": global_config,
        },
    )


@router.post("/isdb/sdt/{config_id}/service/add")
def add_sdt_service(
    config_id: int,
    service_id: str = Form(...),
    service_name: str = Form(...),
    provider_name: str = Form(""),
    service_type: str = Form("1"),
    running_status: str = Form("running"),
    free_ca_mode: str | None = Form(None),
    eit_present_following: str | None = Form(None),
    eit_schedule: str | None = Form(None),
    enabled: str | None = Form(None),
    db: Session = Depends(get_db),
):
    config = db.get(ISDBSDTConfig, config_id)

    if not config:
        raise HTTPException(
            status_code=404,
            detail="SDT config not found",
        )

    try:
        parsed_service_id = int(service_id, 10)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid service_id",
        )

    if not 0 <= parsed_service_id <= 0xFFFF:
        raise HTTPException(
            status_code=400,
            detail="service_id must be between 0 and 65535",
        )

    duplicate = (
        db.query(ISDBSDTService)
        .filter(
            ISDBSDTService.sdt_config_id_fk == config.id,
            ISDBSDTService.service_id == parsed_service_id,
        )
        .first()
    )

    if duplicate:
        raise HTTPException(
            status_code=400,
            detail=(
                f"service_id {parsed_service_id} already exists "
                "in this SDT"
            ),
        )

    if len(service_name) > 255:
        raise HTTPException(
            status_code=400,
            detail="service_name is too long",
        )

    if len(provider_name) > 255:
        raise HTTPException(
            status_code=400,
            detail="provider_name is too long",
        )

    try:
        parsed_service_type = int(service_type, 0)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid service_type",
        )

    if not 0 <= parsed_service_type <= 0xFF:
        raise HTTPException(
            status_code=400,
            detail="service_type must be between 0 and 255",
        )

    valid_running_status = {
        "undefined",
        "not-running",
        "starts-soon",
        "pausing",
        "running",
        "off-air",
    }

    if running_status not in valid_running_status:
        raise HTTPException(
            status_code=400,
            detail="Invalid running_status",
        )

    service = ISDBSDTService(
        sdt_config_id_fk=config.id,
        service_id=parsed_service_id,
        service_name=service_name,
        provider_name=provider_name,
        service_type=parsed_service_type,
        running_status=running_status,
        free_ca_mode=free_ca_mode is not None,
        eit_present_following=eit_present_following is not None,
        eit_schedule=eit_schedule is not None,
        enabled=enabled is not None,
    )

    db.add(service)

    # Any SDT content change gets a new 5-bit version.
    config.version = (config.version + 1) % 32

    db.commit()

    return RedirectResponse(
        url=f"/isdb/sdt/{config.id}",
        status_code=303,
    )


@router.post("/isdb/sdt/service/{service_id}/delete")
def delete_sdt_service(
    service_id: int,
    db: Session = Depends(get_db),
):
    service = db.get(ISDBSDTService, service_id)

    if not service:
        raise HTTPException(
            status_code=404,
            detail="SDT service not found",
        )

    config = service.sdt_config
    config_id = config.id

    db.delete(service)

    # SDT version_number is 5 bits: 0..31.
    config.version = (config.version + 1) % 32

    db.commit()

    return RedirectResponse(
        url=f"/isdb/sdt/{config_id}",
        status_code=303,
    )


@router.get("/isdb/sdt/service/{service_id}/edit")
def edit_sdt_service_page(
    service_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    service = db.get(ISDBSDTService, service_id)

    if not service:
        raise HTTPException(
            status_code=404,
            detail="SDT service not found",
        )

    config = service.sdt_config
    mux = config.transport_stream

    return templates.TemplateResponse(
        request=request,
        name="isdb_sdt_service_edit.html",
        context={
            "service": service,
            "config": config,
            "mux": mux,
            "network": mux.network,
        },
    )


@router.post("/isdb/sdt/service/{service_id}/edit")
def edit_sdt_service(
    service_id: int,
    sdt_service_id: str = Form(..., alias="service_id"),
    service_name: str = Form(...),
    provider_name: str = Form(""),
    service_type: str = Form(...),
    running_status: str = Form(...),
    free_ca_mode: str | None = Form(None),
    eit_present_following: str | None = Form(None),
    eit_schedule: str | None = Form(None),
    enabled: str | None = Form(None),
    db: Session = Depends(get_db),
):
    service = db.get(ISDBSDTService, service_id)

    if not service:
        raise HTTPException(
            status_code=404,
            detail="SDT service not found",
        )

    config = service.sdt_config

    try:
        parsed_service_id = int(sdt_service_id, 0)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid service_id",
        )

    if not 0 <= parsed_service_id <= 0xFFFF:
        raise HTTPException(
            status_code=400,
            detail="service_id must be between 0x0000 and 0xFFFF",
        )

    duplicate = (
        db.query(ISDBSDTService)
        .filter(
            ISDBSDTService.sdt_config_id_fk == config.id,
            ISDBSDTService.service_id == parsed_service_id,
            ISDBSDTService.id != service.id,
        )
        .first()
    )

    if duplicate:
        raise HTTPException(
            status_code=400,
            detail=(
                f"service_id {parsed_service_id} already exists "
                "in this SDT"
            ),
        )

    # Do not strip service_name/provider_name:
    # imported SDT strings must remain under explicit operator control.
    if len(service_name) > 255:
        raise HTTPException(
            status_code=400,
            detail="service_name is too long",
        )

    if len(provider_name) > 255:
        raise HTTPException(
            status_code=400,
            detail="provider_name is too long",
        )

    try:
        parsed_service_type = int(service_type, 0)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid service_type",
        )

    if not 0 <= parsed_service_type <= 0xFF:
        raise HTTPException(
            status_code=400,
            detail="service_type must be between 0x00 and 0xFF",
        )

    valid_running_status = {
        "undefined",
        "not-running",
        "starts-soon",
        "pausing",
        "running",
        "off-air",
    }

    if running_status not in valid_running_status:
        raise HTTPException(
            status_code=400,
            detail="Invalid running_status",
        )

    service.service_id = parsed_service_id
    service.service_name = service_name
    service.provider_name = provider_name
    service.service_type = parsed_service_type
    service.running_status = running_status

    service.free_ca_mode = free_ca_mode is not None
    service.eit_present_following = (
        eit_present_following is not None
    )
    service.eit_schedule = eit_schedule is not None
    service.enabled = enabled is not None

    # SDT version_number is 5 bits: 0..31.
    config.version = (config.version + 1) % 32

    db.commit()

    return RedirectResponse(
        url=f"/isdb/sdt/{config.id}",
        status_code=303,
    )


# =========================================================
# PORTADORAS / TRANSPORT STREAMS
# =========================================================

@router.get("/isdb/network/{network_id}")
def isdb_network_detail(
    network_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    network = db.get(ISDBNetwork, network_id)

    if not network:
        return RedirectResponse(
            url="/isdb",
            status_code=303,
        )

    transport_streams = (
        db.query(ISDBTransportStream)
        .filter(
            ISDBTransportStream.network_id_fk
            == network.id
        )
        .order_by(
            ISDBTransportStream.transport_stream_id
        )
        .all()
    )

    return templates.TemplateResponse(
        request=request,
        name="isdb_network.html",
        context={
            "network": network,
            "transport_streams": transport_streams,
        },
    )


@router.post("/isdb/network/{network_id}/mux/add")
def add_isdb_mux(
    network_id: int,
    name: str = Form(...),
    transport_stream_id: int = Form(...),
    physical_channel: str = Form(""),
    frequency_hz: str = Form(""),
    eit_pid: str = Form("0x0012"),
    db: Session = Depends(get_db),
):
    network = db.get(ISDBNetwork, network_id)

    if not network:
        return RedirectResponse(
            url="/isdb",
            status_code=303,
        )

    if not 0 <= transport_stream_id <= 65535:
        return RedirectResponse(
            url=f"/isdb/network/{network_id}",
            status_code=303,
        )

    try:
        parsed_eit_pid = int(eit_pid, 0)
    except ValueError:
        parsed_eit_pid = 0x0012

    mux = ISDBTransportStream(
        network_id_fk=network.id,
        name=name.strip(),
        transport_stream_id=transport_stream_id,
        physical_channel=(
            int(physical_channel)
            if physical_channel.strip()
            else None
        ),
        frequency_hz=(
            int(frequency_hz)
            if frequency_hz.strip()
            else None
        ),
        eit_pid=parsed_eit_pid,
        enabled=True,
    )

    db.add(mux)
    db.commit()
    db.refresh(mux)

    eit_manager.reload_mux(mux.id)

    return RedirectResponse(
        url=f"/isdb/network/{network_id}",
        status_code=303,
    )


@router.post("/isdb/mux/{mux_id}/toggle")
def toggle_isdb_mux(
    mux_id: int,
    db: Session = Depends(get_db),
):
    mux = db.get(ISDBTransportStream, mux_id)

    if not mux:
        return RedirectResponse(
            url="/isdb",
            status_code=303,
        )

    mux.enabled = not mux.enabled

    network_id = mux.network_id_fk

    db.commit()

    eit_manager.reload_mux(mux_id)

    return RedirectResponse(
        url=f"/isdb/network/{network_id}",
        status_code=303,
    )


@router.post("/isdb/mux/{mux_id}/delete")
def delete_isdb_mux(
    mux_id: int,
    db: Session = Depends(get_db),
):
    mux = db.get(ISDBTransportStream, mux_id)

    if not mux:
        return RedirectResponse(
            url="/isdb",
            status_code=303,
        )

    network_id = mux.network_id_fk

    db.delete(mux)
    db.commit()

    eit_manager.reload_mux(mux_id)

    return RedirectResponse(
        url=f"/isdb/network/{network_id}",
        status_code=303,
    )

@router.get("/isdb/mux/{mux_id}")
def isdb_mux_detail(
    mux_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    mux = db.get(ISDBTransportStream, mux_id)

    if not mux:
        return RedirectResponse(
            url="/isdb",
            status_code=303,
        )

    services = (
        db.query(ISDBService)
        .filter(
            ISDBService.transport_stream_id_fk
            == mux.id
        )
        .order_by(ISDBService.service_id)
        .all()
    )

    sources = (
        db.query(EPGSource)
        .filter(EPGSource.enabled == True)
        .order_by(EPGSource.name)
        .all()
    )

    return templates.TemplateResponse(
        request=request,
        name="isdb_mux.html",
        context={
            "mux": mux,
            "network": mux.network,
            "services": services,
            "sources": sources,
        },
    )
@router.get("/api/epg/channels")
def api_epg_channels(
    source_id: int,
    q: str = Query(default=""),
    db: Session = Depends(get_db),
):
    query = (
        db.query(EPGChannel)
        .filter(
            EPGChannel.source_id == source_id
        )
    )

    if q.strip():
        search = f"%{q.strip()}%"

        query = query.filter(
            or_(
                EPGChannel.display_name.ilike(search),
                EPGChannel.xmltv_id.ilike(search),
            )
        )

    channels = (
        query
        .order_by(EPGChannel.display_name)
        .limit(100)
        .all()
    )

    return [
        {
            "id": channel.id,
            "xmltv_id": channel.xmltv_id,
            "display_name": channel.display_name,
        }
        for channel in channels
    ]

@router.post("/isdb/mux/{mux_id}/service/add")
def add_isdb_service(
    mux_id: int,
    service_name: str = Form(...),
    service_id: int = Form(...),
    service_type: str = Form("0x01"),
    epg_source_id: str = Form(""),
    epg_channel_id: str = Form(""),
    eit_present_following: str | None = Form(None),
    eit_schedule: str | None = Form(None),
    db: Session = Depends(get_db),
):
    mux = db.get(ISDBTransportStream, mux_id)

    if not mux:
        return RedirectResponse(
            url="/isdb",
            status_code=303,
        )

    if not 0 <= service_id <= 65535:
        return RedirectResponse(
            url=f"/isdb/mux/{mux_id}",
            status_code=303,
        )

    try:
        parsed_service_type = int(
            service_type.strip(),
            0,
        )
    except ValueError:
        parsed_service_type = 0x01

    if not 0 <= parsed_service_type <= 255:
        parsed_service_type = 0x01

    source_id_value = None
    channel_id_value = None

    if epg_source_id.strip():
        try:
            source_id_value = int(epg_source_id)
        except ValueError:
            pass

    if epg_channel_id.strip():
        try:
            channel_id_value = int(epg_channel_id)
        except ValueError:
            pass

    # Validamos que el canal pertenezca
    # realmente a la fuente seleccionada.

    if channel_id_value is not None:
        channel = db.get(
            EPGChannel,
            channel_id_value,
        )

        if (
            not channel
            or channel.source_id != source_id_value
        ):
            channel_id_value = None

    service = ISDBService(
        transport_stream_id_fk=mux.id,
        service_id=service_id,
        service_name=service_name.strip(),
        service_type=parsed_service_type,
        enabled=True,
        eit_present_following=(
            eit_present_following is not None
        ),
        eit_schedule=(
            eit_schedule is not None
        ),
        epg_source_id=source_id_value,
        epg_channel_id=channel_id_value,
    )

    db.add(service)

    try:
        db.commit()

    except Exception:
        db.rollback()

    return RedirectResponse(
        url=f"/isdb/mux/{mux_id}",
        status_code=303,
    )



@router.get("/isdb/service/{service_id}/edit")
def edit_isdb_service(
    service_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    service = db.get(
        ISDBService,
        service_id,
    )

    if not service:
        return RedirectResponse(
            url="/isdb",
            status_code=303,
        )

    sources = (
        db.query(EPGSource)
        .filter(EPGSource.enabled == True)
        .order_by(EPGSource.name)
        .all()
    )

    return templates.TemplateResponse(
        request=request,
        name="isdb_service_edit.html",
        context={
            "service": service,
            "mux": service.transport_stream,
            "network": service.transport_stream.network,
            "sources": sources,
        },
    )


@router.post("/isdb/service/{service_id}/edit")
def save_isdb_service(
    service_id: int,
    service_name: str = Form(...),
    new_service_id: int = Form(...),
    service_type: str = Form("0x01"),
    epg_source_id: str = Form(""),
    epg_channel_id: str = Form(""),
    eit_present_following: str | None = Form(None),
    eit_schedule: str | None = Form(None),
    db: Session = Depends(get_db),
):
    service = db.get(
        ISDBService,
        service_id,
    )

    if not service:
        return RedirectResponse(
            url="/isdb",
            status_code=303,
        )

    mux_id = service.transport_stream_id_fk

    if not 0 <= new_service_id <= 65535:
        return RedirectResponse(
            url=f"/isdb/service/{service.id}/edit?error=sid",
            status_code=303,
        )

    try:
        parsed_service_type = int(
            service_type.strip(),
            0,
        )
    except ValueError:
        parsed_service_type = 0x01

    if not 0 <= parsed_service_type <= 255:
        parsed_service_type = 0x01

    source_id_value = None
    channel_id_value = None

    if epg_source_id.strip():
        try:
            source_id_value = int(
                epg_source_id
            )
        except ValueError:
            pass

    if epg_channel_id.strip():
        try:
            channel_id_value = int(
                epg_channel_id
            )
        except ValueError:
            pass

    # El canal EPG debe pertenecer a
    # la fuente EPG seleccionada.
    if channel_id_value is not None:
        channel = db.get(
            EPGChannel,
            channel_id_value,
        )

        if (
            not channel
            or channel.source_id != source_id_value
        ):
            channel_id_value = None

    # Evitar SID duplicado dentro del mismo MUX.
    duplicate = (
        db.query(ISDBService)
        .filter(
            ISDBService.transport_stream_id_fk
            == mux_id,
            ISDBService.service_id
            == new_service_id,
            ISDBService.id
            != service.id,
        )
        .first()
    )

    if duplicate:
        return RedirectResponse(
            url=f"/isdb/service/{service.id}/edit?error=duplicate",
            status_code=303,
        )

    service.service_name = service_name.strip()
    service.service_id = new_service_id
    service.service_type = parsed_service_type

    service.epg_source_id = source_id_value
    service.epg_channel_id = channel_id_value

    service.eit_present_following = (
        eit_present_following is not None
    )

    service.eit_schedule = (
        eit_schedule is not None
    )

    try:
        db.commit()

    except Exception:
        db.rollback()

        return RedirectResponse(
            url=f"/isdb/service/{service.id}/edit?error=database",
            status_code=303,
        )

    eit_manager.reload_mux(mux_id)

    return RedirectResponse(
        url=f"/isdb/mux/{mux_id}",
        status_code=303,
    )


@router.post("/isdb/service/{service_id}/toggle")
def toggle_isdb_service(
    service_id: int,
    db: Session = Depends(get_db),
):
    service = db.get(
        ISDBService,
        service_id,
    )

    if not service:
        return RedirectResponse(
            url="/isdb",
            status_code=303,
        )

    mux_id = service.transport_stream_id_fk

    service.enabled = not service.enabled

    db.commit()

    return RedirectResponse(
        url=f"/isdb/mux/{mux_id}",
        status_code=303,
    )


@router.post("/isdb/service/{service_id}/delete")
def delete_isdb_service(
    service_id: int,
    db: Session = Depends(get_db),
):
    service = db.get(
        ISDBService,
        service_id,
    )

    if not service:
        return RedirectResponse(
            url="/isdb",
            status_code=303,
        )

    mux_id = service.transport_stream_id_fk

    db.delete(service)
    db.commit()

    return RedirectResponse(
        url=f"/isdb/mux/{mux_id}",
        status_code=303,
    )

@router.post("/isdb/mux/{mux_id}/eit/config")
def save_eit_config(
    mux_id: int,
    eit_version: int = Form(0),
    eit_schedule_days: int = Form(7),
    eit_language: str = Form("spa"),
    eit_running_status: int = Form(4),
    eit_free_ca_mode: str | None = Form(None),
    eit_extended_description: str | None = Form(None),
    db: Session = Depends(get_db),
):
    mux = db.get(
        ISDBTransportStream,
        mux_id,
    )

    if not mux:
        return RedirectResponse(
            url="/isdb",
            status_code=303,
        )

    mux.eit_version = max(
        0,
        min(
            eit_version,
            31,
        ),
    )

    mux.eit_schedule_days = max(
        1,
        min(
            eit_schedule_days,
            64,
        ),
    )

    language = (
        eit_language.strip().lower()
    )

    if len(language) != 3:
        language = "spa"

    mux.eit_language = language

    mux.eit_running_status = max(
        0,
        min(
            eit_running_status,
            7,
        ),
    )

    mux.eit_free_ca_mode = (
        eit_free_ca_mode is not None
    )

    mux.eit_extended_description = (
        eit_extended_description is not None
    )

    db.commit()

    return RedirectResponse(
        url=f"/isdb/mux/{mux_id}",
        status_code=303,
    )

@router.post("/isdb/mux/{mux_id}/eit/generate")
def generate_eit(
    mux_id: int,
    db: Session = Depends(get_db),
):
    try:

        stats = generate_mux_eit(
            db,
            mux_id,
        )

        return RedirectResponse(
            url=(
                f"/isdb/mux/{mux_id}"
                f"?eit_generated=1"
                f"&pf={stats['pf_sections']}"
                f"&schedule={stats['schedule_sections']}"
                f"&events={stats['schedule_events']}"
                f"&packets={stats['packets']}"
            ),
            status_code=303,
        )

    except Exception as exc:

        db.rollback()

        logger.exception(
            "Error generating EIT manually "
            "for MUX %s",
            mux_id,
        )

        return RedirectResponse(
            url=(
                f"/isdb/mux/{mux_id}"
                f"?eit_error=1"
            ),
            status_code=303,
        )
@router.post(
    "/isdb/mux/{mux_id}/eit/broadcast-config"
)
def update_eit_broadcast_config(
    mux_id: int,
    eit_pf_interval: int = Form(...),
    eit_schedule_prime_interval: int = Form(...),
    eit_schedule_later_interval: int = Form(...),
    eit_prime_days: int = Form(...),
    eit_output_mode: str = Form(...),
    eit_output_address: str = Form(...),
    eit_output_port: int = Form(...),
    eit_output_ttl: int = Form(...),
    eit_output_interface: str = Form(""),
    db: Session = Depends(get_db),
):
    mux = (
        db.query(ISDBTransportStream)
        .filter(
            ISDBTransportStream.id == mux_id
        )
        .first()
    )

    if mux is None:
        raise HTTPException(
            status_code=404,
            detail="MUX no encontrado",
        )

    if eit_pf_interval < 1:
        raise HTTPException(
            status_code=400,
            detail="Intervalo P/F inválido",
        )

    if eit_schedule_prime_interval < 1:
        raise HTTPException(
            status_code=400,
            detail="Intervalo Schedule prime inválido",
        )

    if eit_schedule_later_interval < 1:
        raise HTTPException(
            status_code=400,
            detail="Intervalo Schedule later inválido",
        )

    if not 1 <= eit_prime_days <= 64:
        raise HTTPException(
            status_code=400,
            detail="Prime days inválido",
        )

    if eit_output_mode not in {
        "none",
        "udp",
        "file",
    }:
        raise HTTPException(
            status_code=400,
            detail="Modo de salida inválido",
        )

    if not 1 <= eit_output_port <= 65535:
        raise HTTPException(
            status_code=400,
            detail="Puerto UDP inválido",
        )

    if not 1 <= eit_output_ttl <= 255:
        raise HTTPException(
            status_code=400,
            detail="TTL inválido",
        )

    mux.eit_pf_interval = (
        eit_pf_interval
    )

    mux.eit_schedule_prime_interval = (
        eit_schedule_prime_interval
    )

    mux.eit_schedule_later_interval = (
        eit_schedule_later_interval
    )

    mux.eit_prime_days = (
        eit_prime_days
    )

    mux.eit_output_mode = (
        eit_output_mode
    )

    mux.eit_output_address = (
        eit_output_address.strip()
    )

    mux.eit_output_port = (
        eit_output_port
    )

    mux.eit_output_ttl = (
        eit_output_ttl
    )

    mux.eit_output_interface = (
        eit_output_interface.strip()
    )

    db.commit()

    eit_manager.reload_mux(mux_id)

    return RedirectResponse(
        url=f"/isdb/mux/{mux_id}",
        status_code=303,
    )
