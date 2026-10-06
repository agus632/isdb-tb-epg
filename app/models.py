from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class EPGSource(Base):
    __tablename__ = "epg_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), default="xmltv")
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    update_interval: Mapped[int] = mapped_column(Integer, default=6)

    # Manual EPG generation settings.
    # NULL for non-manual/XMLTV sources.
    manual_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    manual_block_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)

    last_update: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    channel_count: Mapped[int] = mapped_column(Integer, default=0)
    programme_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    channels = relationship(
        "EPGChannel",
        back_populates="source",
        cascade="all, delete-orphan",
    )


class EPGChannel(Base):
    __tablename__ = "epg_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    source_id: Mapped[int] = mapped_column(
        ForeignKey("epg_sources.id"),
        nullable=False,
    )

    xmltv_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    icon_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    source = relationship("EPGSource", back_populates="channels")

    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "xmltv_id",
            name="uq_source_xmltv_channel",
        ),
    )


class EPGProgramme(Base):
    __tablename__ = "epg_programmes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    source_id: Mapped[int] = mapped_column(
        ForeignKey("epg_sources.id"),
        nullable=False,
    )

    channel_xmltv_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    start_time: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        index=True,
    )

    stop_time: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    title: Mapped[str] = mapped_column(String(500), nullable=False)

    subtitle: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )

    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    category: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

class ISDBNetwork(Base):
    __tablename__ = "isdb_networks"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    network_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    original_network_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    country_code: Mapped[str] = mapped_column(
        String(3),
        default="ARG",
    )

    timezone: Mapped[str] = mapped_column(
        String(100),
        default="America/Argentina/Buenos_Aires",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    transport_streams = relationship(
        "ISDBTransportStream",
        back_populates="network",
        cascade="all, delete-orphan",
    )


class ISDBTransportStream(Base):
    __tablename__ = "isdb_transport_streams"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    network_id_fk: Mapped[int] = mapped_column(
        ForeignKey("isdb_networks.id"),
        nullable=False,
    )

    services = relationship(
        "ISDBService",
        back_populates="transport_stream",
        cascade="all, delete-orphan",
    )

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    transport_stream_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    physical_channel: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    frequency_hz: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    eit_pid: Mapped[int] = mapped_column(
        Integer,
        default=0x0012,
    )

    eit_version: Mapped[int] = mapped_column(
        Integer,
        default=0,
    )

    eit_schedule_days: Mapped[int] = mapped_column(
        Integer,
        default=7,
    )

    eit_language: Mapped[str] = mapped_column(
        String(3),
        default="spa",
    )

    eit_running_status: Mapped[int] = mapped_column(
        Integer,
        default=4,
    )

    eit_free_ca_mode: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )

    eit_extended_description: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
    )

    # EIT repetition / broadcast
    eit_pf_interval: Mapped[int] = mapped_column(
        Integer,
        default=2,
    )

    eit_schedule_prime_interval: Mapped[int] = mapped_column(
        Integer,
        default=10,
    )

    eit_schedule_later_interval: Mapped[int] = mapped_column(
        Integer,
        default=30,
    )

    eit_prime_days: Mapped[int] = mapped_column(
        Integer,
        default=8,
    )

    # Output
    eit_output_mode: Mapped[str] = mapped_column(
        String(16),
        default="none",
    )

    eit_output_address: Mapped[str] = mapped_column(
        String(64),
        default="239.255.0.1",
    )

    eit_output_port: Mapped[int] = mapped_column(
        Integer,
        default=5000,
    )

    eit_output_ttl: Mapped[int] = mapped_column(
        Integer,
        default=1,
    )

    eit_output_interface: Mapped[str] = mapped_column(
        String(64),
        default="",
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    epg_group_memberships = relationship(
        "ISDBEPGGroupMember",
        back_populates="transport_stream",
        cascade="all, delete-orphan",
    )

    sdt_config = relationship(
        "ISDBSDTConfig",
        back_populates="transport_stream",
        cascade="all, delete-orphan",
        uselist=False,
    )

    network = relationship(
        "ISDBNetwork",
        back_populates="transport_streams",
    )


class ISDBSDTGlobalConfig(Base):
    __tablename__ = "isdb_sdt_global_config"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    default_provider_name: Mapped[str] = mapped_column(
        String(255),
        default="",
        nullable=False,
    )

    default_service_type: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False,
    )

    default_running_status: Mapped[str] = mapped_column(
        String(32),
        default="running",
        nullable=False,
    )

    default_free_ca_mode: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    default_eit_present_following: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    default_eit_schedule: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    default_service_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )


class ISDBSDTConfig(Base):
    __tablename__ = "isdb_sdt_configs"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    transport_stream_id_fk: Mapped[int] = mapped_column(
        ForeignKey(
            "isdb_transport_streams.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        unique=True,
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    version: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    output_mode: Mapped[str] = mapped_column(
        String(16),
        default="none",
        nullable=False,
    )

    output_address: Mapped[str] = mapped_column(
        String(64),
        default="239.255.1.1",
        nullable=False,
    )

    output_port: Mapped[int] = mapped_column(
        Integer,
        default=5100,
        nullable=False,
    )

    output_ttl: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False,
    )

    output_interface: Mapped[str] = mapped_column(
        String(64),
        default="",
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    transport_stream = relationship(
        "ISDBTransportStream",
        back_populates="sdt_config",
    )

    services = relationship(
        "ISDBSDTService",
        back_populates="sdt_config",
        cascade="all, delete-orphan",
    )


class ISDBSDTService(Base):
    __tablename__ = "isdb_sdt_services"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    sdt_config_id_fk: Mapped[int] = mapped_column(
        ForeignKey(
            "isdb_sdt_configs.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    service_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    service_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    provider_name: Mapped[str] = mapped_column(
        String(255),
        default="",
        nullable=False,
    )

    service_type: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
    )

    running_status: Mapped[str] = mapped_column(
        String(32),
        default="running",
        nullable=False,
    )

    free_ca_mode: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    eit_present_following: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    eit_schedule: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    sdt_config = relationship(
        "ISDBSDTConfig",
        back_populates="services",
    )

    __table_args__ = (
        UniqueConstraint(
            "sdt_config_id_fk",
            "service_id",
            name="uq_isdb_sdt_service",
        ),
    )


class ISDBEPGGroup(Base):
    __tablename__ = "isdb_epg_groups"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    members = relationship(
        "ISDBEPGGroupMember",
        back_populates="group",
        cascade="all, delete-orphan",
    )


class ISDBEPGGroupMember(Base):
    __tablename__ = "isdb_epg_group_members"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    group_id: Mapped[int] = mapped_column(
        ForeignKey(
            "isdb_epg_groups.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    transport_stream_id_fk: Mapped[int] = mapped_column(
        ForeignKey(
            "isdb_transport_streams.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    group = relationship(
        "ISDBEPGGroup",
        back_populates="members",
    )

    transport_stream = relationship(
        "ISDBTransportStream",
        back_populates="epg_group_memberships",
    )

    __table_args__ = (
        UniqueConstraint(
            "group_id",
            "transport_stream_id_fk",
            name="uq_epg_group_mux",
        ),
        UniqueConstraint(
            "transport_stream_id_fk",
            name="uq_epg_group_mux_single_membership",
        ),
    )


class ISDBService(Base):
    __tablename__ = "isdb_services"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    transport_stream_id_fk: Mapped[int] = mapped_column(
        ForeignKey("isdb_transport_streams.id"),
        nullable=False,
    )

    service_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    service_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    service_type: Mapped[int] = mapped_column(
        Integer,
        default=0x01,
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
    )

    eit_present_following: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
    )

    eit_schedule: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
    )

    epg_source_id: Mapped[int | None] = mapped_column(
        ForeignKey("epg_sources.id"),
        nullable=True,
    )

    epg_channel_id: Mapped[int | None] = mapped_column(
        ForeignKey("epg_channels.id"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    transport_stream = relationship(
        "ISDBTransportStream",
        back_populates="services",
    )

    epg_source = relationship(
        "EPGSource",
    )

    epg_channel = relationship(
        "EPGChannel",
    )

    __table_args__ = (
        UniqueConstraint(
            "transport_stream_id_fk",
            "service_id",
            name="uq_mux_service_id",
        ),
    )
class ISDBEventID(Base):
    __tablename__ = "isdb_event_ids"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    service_id_fk: Mapped[int] = mapped_column(
        ForeignKey("isdb_services.id"),
        nullable=False,
        index=True,
    )

    event_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    event_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    __table_args__ = (
        UniqueConstraint(
            "service_id_fk",
            "event_key",
            name="uq_service_event_key",
        ),
        UniqueConstraint(
            "service_id_fk",
            "event_id",
            name="uq_service_event_id",
        ),
    )
