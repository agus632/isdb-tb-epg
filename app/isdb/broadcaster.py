import logging
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Optional
from app.database import SessionLocal
from app.models import (
    ISDBEPGGroup,
    ISDBEPGGroupMember,
    ISDBTransportStream,
)
from app.isdb.generator import (
    generate_mux_sections,
    generate_group_pf_sections,
    generate_group_schedule_sections,
)
from app.isdb.packetizer import packetize_sections
from app.isdb.sdt import (
    SDT_PID,
    generate_mux_sdt_sections,
)
from app.isdb.time_tables import (
    TIME_PID,
    build_time_tables,
)

logger = logging.getLogger("uvicorn.error")


# Shared Schedule generation.
#
# Generation remains globally serialized to protect SQLite,
# while cache state is isolated per EPG/EIT group.
_schedule_generation_lock = threading.Lock()
_schedule_shared_state_lock = threading.Lock()
_schedule_shared_states = {}


# Shared P/F generation.
#
# Generation remains globally serialized to protect SQLite,
# while cache state is isolated per EPG/EIT group.
_pf_generation_lock = threading.Lock()
_pf_shared_state_lock = threading.Lock()
_pf_shared_states = {}


def _get_epg_group_key(db, mux_id):
    """
    Return the logical shared-cache key for a MUX.

    MUXes in the same enabled EPG/EIT group share one key.

    A MUX without an enabled group receives its own independent
    key, preserving Actual-only standalone operation.
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
        return ("mux", int(mux_id))

    return ("group", int(membership.group_id))


def _get_pf_shared_state(key):
    state = _pf_shared_states.get(key)

    if state is None:
        state = {
            "caches": {},
            "last_refresh": 0.0,
            "refresh_running": False,
        }
        _pf_shared_states[key] = state

    return state


def _get_schedule_shared_state(key):
    state = _schedule_shared_states.get(key)

    if state is None:
        state = {
            "caches": {},
            "last_refresh": 0.0,
            "refresh_running": False,
        }
        _schedule_shared_states[key] = state

    return state


OUTPUT_DIR = Path(
    "/opt/isdb-epg/data/generated"
)


def _interleave_eit_schedule_sections(sections):
    """
    Interleave EIT Schedule sections by target transport stream.

    generate_mux_sections() returns Schedule sections grouped as:
        Actual MUX,
        Other MUX 1,
        Other MUX 2,
        ...

    Sending that list sequentially can leave one transport stream
    absent from the carousel for a long time.  This function keeps
    the original order inside each transport stream while performing
    a round-robin between transports.

    EIT long-section layout:
        byte 0      table_id
        bytes 3-4   service_id
        bytes 8-9   transport_stream_id
        bytes 10-11 original_network_id
    """

    if not sections:
        return []

    groups = {}
    group_order = []

    for section in sections:
        if len(section) < 12:
            # Preserve malformed/unexpected data instead of dropping it.
            key = ("raw", id(section))
        else:
            tsid = int.from_bytes(
                section[8:10],
                "big",
            )
            onid = int.from_bytes(
                section[10:12],
                "big",
            )
            key = (tsid, onid)

        if key not in groups:
            groups[key] = []
            group_order.append(key)

        groups[key].append(section)

    positions = {
        key: 0
        for key in group_order
    }

    result = []

    while True:
        added = False

        for key in group_order:
            pos = positions[key]
            group = groups[key]

            if pos >= len(group):
                continue

            result.append(group[pos])
            positions[key] = pos + 1
            added = True

        if not added:
            break

    return result


class EITBroadcaster:
    """
    Continuous EIT MPEG-TS broadcaster.

    Responsibilities:
      - Keep one continuity counter per PID.
      - Schedule P/F and Schedule independently.
      - Send MPEG-TS over UDP.
      - Optionally write a continuous diagnostic file.
      - Allow clean start/stop.
    """

    def __init__(
        self,
        mux_id: int,
        pid: int = 0x0012,
    ):
        self.mux_id = mux_id
        self.pid = pid

        # Logical EPG/EIT shared-cache key.
        #
        # Resolved lazily once per broadcaster instance so the
        # 50 ms transmission loop never queries SQLite for it.
        self._epg_group_key = None

        self.running = False

        self.thread: Optional[
            threading.Thread
        ] = None

        self.socket: Optional[
            socket.socket
        ] = None

        # Independent continuity counters per PID.
        self.continuity_counter = 0
        self.time_continuity_counter = 0
        self.sdt_continuity_counter = 0

        # TDT/TOT repetition interval.
        self.time_tables_interval = 5

        # SDT Actual repetition interval.
        self.sdt_interval = 2

        # SDT generation is independent from its transmission
        # cadence. Raw SDT sections are cached and packetized
        # only when transmitted so PID 0x0011 keeps its own
        # continuous continuity_counter.
        self.sdt_cache_interval = 30
        self._sdt_cache = []
        self._sdt_cache_ready = False
        self._sdt_refresh_running = False
        self._sdt_last_refresh = 0.0

        self.output_mode = "none"
        self.output_address = "239.255.0.1"
        self.output_port = 5000
        self.output_ttl = 1
        self.output_interface = ""

        # P/F transmission interval.
        self.pf_interval = 2

        # P/F is regenerated independently from its
        # transmission cadence. The cached sections are
        # transmitted every pf_interval seconds.
        self.pf_cache_interval = 30
        self._pf_cache = []
        self._pf_cache_ready = False
        self._pf_refresh_running = False
        self._pf_last_refresh = 0.0

        # Recommended terrestrial EIT Schedule carousel periods.
        #
        # Actual TS:
        #   prime -> 10 s
        #   later -> 30 s
        #
        # Other TS:
        #   prime -> 60 s
        #   later -> 300 s
        self.schedule_prime_interval = 10
        self.schedule_later_interval = 30
        self.schedule_other_prime_interval = 60
        self.schedule_other_later_interval = 300

        self.schedule_tick_interval = 0.05

        self.prime_days = 8

        # Schedule cache.
        #
        # Raw EIT sections are cached here. Packetization is
        # intentionally performed only when transmitting so
        # continuity_counter remains continuous on PID 0x0012.
        self.schedule_cache_interval = 900

        self._schedule_actual_prime_cache = []
        self._schedule_other_prime_cache = []
        self._schedule_actual_later_cache = []
        self._schedule_other_later_cache = []

        self._schedule_cache_ready = False

        # Schedule regeneration runs outside the broadcaster
        # transmission thread. Only one regeneration may run
        # globally at a time, but waiting for it must never
        # block P/F, TDT/TOT or UDP transmission.
        self._schedule_refresh_running = False
        self._schedule_last_refresh = 0.0

        # Independent Schedule carousel state.
        self._schedule_actual_prime_index = 0
        self._schedule_other_prime_index = 0
        self._schedule_actual_later_index = 0
        self._schedule_other_later_index = 0

        self._lock = threading.Lock()

        OUTPUT_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

    # -------------------------------------------------
    # Configuration
    # -------------------------------------------------

    def configure(
        self,
        *,
        output_mode: str,
        output_address: str,
        output_port: int,
        output_ttl: int,
        output_interface: str,
        pf_interval: int,
        schedule_prime_interval: int,
        schedule_later_interval: int,
        prime_days: int,
    ):
        output_mode = (
            output_mode or "none"
        ).lower()

        if output_mode not in (
            "none",
            "udp",
            "file",
        ):
            raise ValueError(
                "Invalid EIT output mode"
            )

        if not 1 <= output_port <= 65535:
            raise ValueError(
                "Invalid UDP port"
            )

        if not 1 <= output_ttl <= 255:
            raise ValueError(
                "Invalid multicast TTL"
            )

        if pf_interval < 1:
            raise ValueError(
                "P/F interval must be >= 1"
            )

        if schedule_prime_interval < 1:
            raise ValueError(
                "Schedule prime interval must be >= 1"
            )

        if schedule_later_interval < 1:
            raise ValueError(
                "Schedule later interval must be >= 1"
            )

        if not 1 <= prime_days <= 64:
            raise ValueError(
                "Prime days must be between 1 and 64"
            )

        self.output_mode = output_mode
        self.output_address = output_address
        self.output_port = output_port
        self.output_ttl = output_ttl
        self.output_interface = (
            output_interface or ""
        )

        self.pf_interval = pf_interval
        self.schedule_prime_interval = (
            schedule_prime_interval
        )
        self.schedule_later_interval = (
            schedule_later_interval
        )
        self.prime_days = prime_days

    # -------------------------------------------------
    # UDP
    # -------------------------------------------------

    def _open_udp_socket(self):
        sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
            socket.IPPROTO_UDP,
        )

        sock.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_REUSEADDR,
            1,
        )

        ttl = struct.pack(
            "B",
            self.output_ttl,
        )

        sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_MULTICAST_TTL,
            ttl,
        )

        if self.output_interface:
            interface_ip = socket.inet_aton(
                self.output_interface
            )

            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                interface_ip,
            )

        self.socket = sock

        logger.info(
            "MUX %s EIT UDP output %s:%s",
            self.mux_id,
            self.output_address,
            self.output_port,
        )

    def _close_udp_socket(self):
        if self.socket is not None:
            try:
                self.socket.close()
            finally:
                self.socket = None

    # -------------------------------------------------
    # Output
    # -------------------------------------------------

    def _send_udp(
        self,
        data: bytes,
    ):
        if not data:
            return

        if self.socket is None:
            self._open_udp_socket()

        # 7 MPEG-TS packets = 1316 bytes.
        # This is convenient for UDP transport.
        packet_size = 188
        packets_per_datagram = 7
        chunk_size = (
            packet_size
            * packets_per_datagram
        )

        for offset in range(
            0,
            len(data),
            chunk_size,
        ):
            chunk = data[
                offset:
                offset + chunk_size
            ]

            self.socket.sendto(
                chunk,
                (
                    self.output_address,
                    self.output_port,
                ),
            )

    def _write_file(
        self,
        data: bytes,
    ):
        if not data:
            return

        path = (
            OUTPUT_DIR
            / f"eit_mux_{self.mux_id}_live.ts"
        )

        with path.open("ab") as fp:
            fp.write(data)

    def send(
        self,
        data: bytes,
    ):
        if not data:
            return

        if len(data) % 188 != 0:
            raise ValueError(
                "EIT output is not aligned "
                "to 188-byte TS packets"
            )

        if self.output_mode == "none":
            return

        if self.output_mode == "udp":
            self._send_udp(data)

        elif self.output_mode == "file":
            self._write_file(data)

    # -------------------------------------------------
    # Continuous loop
    # -------------------------------------------------

    def _run(self):
        logger.info(
            "EIT broadcaster started for MUX %s",
            self.mux_id,
        )

        next_pf = 0.0
        next_schedule_refresh = 0.0
        next_schedule_tick = 0.0
        next_time_tables = 0.0
        next_sdt = 0.0

        try:
            while self.running:

                now = time.monotonic()

                if now >= next_time_tables:
                    self.broadcast_time_tables()

                    next_time_tables = (
                        now
                        + self.time_tables_interval
                    )

                if (
                    not self._sdt_refresh_running
                    and (
                        not self._sdt_cache_ready
                        or (
                            self._sdt_last_refresh > 0
                            and now >= (
                                self._sdt_last_refresh
                                + self.sdt_cache_interval
                            )
                        )
                    )
                ):
                    self.refresh_sdt_cache()

                if now >= next_sdt:
                    self.broadcast_sdt()

                    next_sdt = (
                        now
                        + self.sdt_interval
                    )

                self._sync_pf_shared_cache()

                pf_key = self._get_pf_group_key()

                with _pf_shared_state_lock:
                    pf_state = _get_pf_shared_state(
                        pf_key
                    )
                    pf_shared_running = (
                        pf_state["refresh_running"]
                    )
                    pf_shared_last_refresh = (
                        pf_state["last_refresh"]
                    )

                if (
                    not pf_shared_running
                    and (
                        pf_shared_last_refresh <= 0
                        or now >= (
                            pf_shared_last_refresh
                            + self.pf_cache_interval
                        )
                    )
                ):
                    self.refresh_pf_cache()

                if now >= next_pf:
                    self.broadcast_pf()

                    next_pf = (
                        now
                        + self.pf_interval
                    )

                self._sync_schedule_shared_cache()

                schedule_key = (
                    self._get_schedule_group_key()
                )

                with _schedule_shared_state_lock:
                    schedule_state = (
                        _get_schedule_shared_state(
                            schedule_key
                        )
                    )
                    schedule_shared_running = (
                        schedule_state[
                            "refresh_running"
                        ]
                    )
                    schedule_shared_last_refresh = (
                        schedule_state[
                            "last_refresh"
                        ]
                    )

                if (
                    not schedule_shared_running
                    and (
                        schedule_shared_last_refresh <= 0
                        or now >= (
                            schedule_shared_last_refresh
                            + self.schedule_cache_interval
                        )
                    )
                ):
                    self.refresh_schedule_cache()

                if now >= next_schedule_tick:
                    self.broadcast_schedule_carousels()

                    next_schedule_tick = (
                        now
                        + self.schedule_tick_interval
                    )

                time.sleep(0.05)

        except Exception:
            logger.exception(
                "EIT broadcaster failed "
                "for MUX %s",
                self.mux_id,
            )

        finally:
            self._close_udp_socket()

            self.running = False

            logger.info(
                "EIT broadcaster stopped "
                "for MUX %s",
                self.mux_id,
            )

        # -------------------------------------------------
    # EIT generation hooks
    # -------------------------------------------------

    def refresh_sdt_cache(self):
        """
        Regenerate raw SDT Actual sections in background.

        Transmission continues using the last valid cache so
        SQLite access never blocks the broadcaster loop.
        """

        if self._sdt_refresh_running:
            return

        self._sdt_refresh_running = True

        worker = threading.Thread(
            target=self._sdt_refresh_worker,
            name=f"sdt-refresh-mux-{self.mux_id}",
            daemon=True,
        )

        worker.start()

    def _sdt_refresh_worker(self):
        db = SessionLocal()

        try:
            sections = generate_mux_sdt_sections(
                db,
                self.mux_id,
            )

            # An empty result is also a valid cache state:
            # SDT may be disabled for this MUX.
            self._sdt_cache = list(sections)
            self._sdt_cache_ready = True

        except Exception:
            logger.exception(
                "Error refreshing SDT cache for MUX %s",
                self.mux_id,
            )

        finally:
            db.close()
            self._sdt_last_refresh = time.monotonic()
            self._sdt_refresh_running = False

    def broadcast_sdt(self):
        """
        Transmit cached SDT Actual on PID 0x0011.

        No database access or SDT generation is performed
        during transmission.
        """

        if not self._sdt_cache_ready:
            return

        sections = self._sdt_cache

        if not sections:
            return

        data, next_cc = packetize_sections(
            sections,
            pid=SDT_PID,
            continuity_counter=(
                self.sdt_continuity_counter
            ),
        )

        self.sdt_continuity_counter = next_cc

        self.send(data)

    def broadcast_time_tables(self):
        try:
            sections = build_time_tables()

            data, next_cc = packetize_sections(
                sections,
                pid=TIME_PID,
                continuity_counter=(
                    self.time_continuity_counter
                ),
            )

            self.time_continuity_counter = (
                next_cc
            )

            self.send(data)

        except Exception:
            logger.exception(
                "Error generating TDT/TOT "
                "for MUX %s",
                self.mux_id,
            )

    def _get_shared_group_key(self):
        if self._epg_group_key is not None:
            return self._epg_group_key

        db = SessionLocal()

        try:
            self._epg_group_key = _get_epg_group_key(
                db,
                self.mux_id,
            )
        finally:
            db.close()

        return self._epg_group_key

    def _get_pf_group_key(self):
        return self._get_shared_group_key()

    def refresh_pf_cache(self):
        """
        Refresh P/F for this MUX's EPG/EIT group.

        Only one broadcaster per group starts a refresh.
        Heavy generation remains globally serialized.
        """

        key = self._get_pf_group_key()

        with _pf_shared_state_lock:
            state = _get_pf_shared_state(key)

            if state["refresh_running"]:
                return

            state["refresh_running"] = True

        worker = threading.Thread(
            target=self._pf_refresh_worker,
            args=(key,),
            name=(
                f"eit-pf-shared-"
                f"{key[0]}-{key[1]}"
            ),
            daemon=True,
        )

        worker.start()

    def _pf_refresh_worker(self, key):
        started = time.monotonic()

        logger.info(
            "Shared P/F cache generation started "
            "by MUX %s key=%s:%s",
            self.mux_id,
            key[0],
            key[1],
        )

        db = SessionLocal()

        try:
            with _pf_generation_lock:
                caches = generate_group_pf_sections(
                    db,
                    self.mux_id,
                )

            finished = time.monotonic()

            with _pf_shared_state_lock:
                state = _get_pf_shared_state(key)

                state["caches"] = {
                    mux_id: list(sections)
                    for mux_id, sections
                    in caches.items()
                }

                state["last_refresh"] = finished

            logger.info(
                "Shared P/F cache refreshed: "
                "key=%s:%s muxes=%s sections=%s "
                "elapsed=%.3fs",
                key[0],
                key[1],
                len(caches),
                sum(
                    len(sections)
                    for sections in caches.values()
                ),
                finished - started,
            )

        except Exception:
            logger.exception(
                "Error refreshing shared EIT P/F cache "
                "key=%s:%s",
                key[0],
                key[1],
            )

        finally:
            db.close()

            with _pf_shared_state_lock:
                state = _get_pf_shared_state(key)
                state["refresh_running"] = False

    def _sync_pf_shared_cache(self):
        """
        Copy this MUX's latest group-local P/F sections
        into its local transmission cache.
        """

        key = self._get_pf_group_key()

        with _pf_shared_state_lock:
            state = _get_pf_shared_state(key)

            sections = state["caches"].get(
                self.mux_id
            )

            refreshed = state["last_refresh"]

            if sections is None:
                return

            if (
                self._pf_cache_ready
                and refreshed <= self._pf_last_refresh
            ):
                return

            self._pf_cache = list(sections)
            self._pf_cache_ready = True
            self._pf_last_refresh = refreshed

    def broadcast_pf(self):
        if not self._pf_cache_ready:
            return

        sections = self._pf_cache

        if not sections:
            return

        data = self._packetize(
            sections
        )

        self.send(data)

    def _get_schedule_group_key(self):
        return self._get_shared_group_key()

    def refresh_schedule_cache(self):
        """
        Start Schedule regeneration for this MUX's
        EPG/EIT group.

        Only one broadcaster per group starts a refresh.
        Heavy generation remains globally serialized.
        """

        key = self._get_schedule_group_key()

        with _schedule_shared_state_lock:
            state = _get_schedule_shared_state(key)

            if state["refresh_running"]:
                return

            state["refresh_running"] = True

        worker = threading.Thread(
            target=self._schedule_refresh_worker,
            args=(key,),
            name=(
                f"eit-schedule-shared-"
                f"{key[0]}-{key[1]}"
            ),
            daemon=True,
        )

        worker.start()

    def _schedule_refresh_worker(self, key):
        started = time.monotonic()

        logger.info(
            "Shared Schedule cache generation "
            "started by MUX %s",
            self.mux_id,
        )

        db = SessionLocal()

        try:
            with _schedule_generation_lock:

                result = (
                    generate_group_schedule_sections(
                        db,
                        self.mux_id,
                    )
                )

                caches = {}

                total_prime = 0
                total_later = 0

                for mux_id, mux_result in result.items():

                    prime = (
                        _interleave_eit_schedule_sections(
                            list(
                                mux_result[
                                    "schedule_prime"
                                ]
                            )
                        )
                    )

                    later = (
                        _interleave_eit_schedule_sections(
                            list(
                                mux_result[
                                    "schedule_later"
                                ]
                            )
                        )
                    )

                    actual_prime = (
                        _interleave_eit_schedule_sections(
                            list(
                                mux_result[
                                    "actual_prime"
                                ]
                            )
                        )
                    )

                    other_prime = (
                        _interleave_eit_schedule_sections(
                            list(
                                mux_result[
                                    "other_prime"
                                ]
                            )
                        )
                    )

                    actual_later = (
                        _interleave_eit_schedule_sections(
                            list(
                                mux_result[
                                    "actual_later"
                                ]
                            )
                        )
                    )

                    other_later = (
                        _interleave_eit_schedule_sections(
                            list(
                                mux_result[
                                    "other_later"
                                ]
                            )
                        )
                    )

                    caches[mux_id] = {
                        "actual_prime": actual_prime,
                        "other_prime": other_prime,
                        "actual_later": actual_later,
                        "other_later": other_later,
                    }

                    total_prime += (
                        len(actual_prime)
                        + len(other_prime)
                    )

                    total_later += (
                        len(actual_later)
                        + len(other_later)
                    )

                finished = time.monotonic()

                with _schedule_shared_state_lock:
                    state = _get_schedule_shared_state(key)
                    state["caches"] = caches
                    state["last_refresh"] = finished

                logger.info(
                    "Shared Schedule cache refreshed: "
                    "key=%s:%s muxes=%s prime=%s later=%s "
                    "elapsed=%.3fs",
                    key[0],
                    key[1],
                    len(caches),
                    total_prime,
                    total_later,
                    finished - started,
                )

        except Exception:
            logger.exception(
                "Error refreshing shared EIT "
                "Schedule cache"
            )

        finally:
            db.close()

            with _schedule_shared_state_lock:
                state = _get_schedule_shared_state(key)
                state["refresh_running"] = False

    def _sync_schedule_shared_cache(self):
        """
        Copy this MUX raw Schedule sections from the shared
        cache when a newer generation is available.
        """

        key = self._get_schedule_group_key()

        with _schedule_shared_state_lock:
            state = _get_schedule_shared_state(key)

            cached = state["caches"].get(
                self.mux_id
            )

            refreshed = state["last_refresh"]

            if cached is None:
                return

            if (
                self._schedule_cache_ready
                and refreshed
                <= self._schedule_last_refresh
            ):
                return

            actual_prime = list(
                cached["actual_prime"]
            )

            other_prime = list(
                cached["other_prime"]
            )

            actual_later = list(
                cached["actual_later"]
            )

            other_later = list(
                cached["other_later"]
            )

        self._schedule_actual_prime_cache = actual_prime
        self._schedule_other_prime_cache = other_prime
        self._schedule_actual_later_cache = actual_later
        self._schedule_other_later_cache = other_later

        self._schedule_cache_ready = True
        self._schedule_last_refresh = refreshed

        if actual_prime:
            self._schedule_actual_prime_index %= len(
                actual_prime
            )
        else:
            self._schedule_actual_prime_index = 0

        if other_prime:
            self._schedule_other_prime_index %= len(
                other_prime
            )
        else:
            self._schedule_other_prime_index = 0

        if actual_later:
            self._schedule_actual_later_index %= len(
                actual_later
            )
        else:
            self._schedule_actual_later_index = 0

        if other_later:
            self._schedule_other_later_index %= len(
                other_later
            )
        else:
            self._schedule_other_later_index = 0

    def _schedule_batch_size(
        self,
        section_count: int,
        target_interval: float,
    ) -> int:
        """
        Number of sections to transmit on each Schedule tick
        so the complete carousel is repeated within the
        requested target interval.
        """

        if section_count <= 0:
            return 0

        ticks = max(
            1,
            int(
                target_interval
                / self.schedule_tick_interval
            ),
        )

        return max(
            1,
            (section_count + ticks - 1) // ticks,
        )

    def _broadcast_schedule_batch(
        self,
        sections,
        index_attr: str,
        target_interval: float,
    ):
        """
        Transmit one batch from an independent Schedule
        carousel while preserving PID 0x0012 continuity.
        """

        if not sections:
            return

        count = len(sections)

        batch_size = self._schedule_batch_size(
            count,
            target_interval,
        )

        index = getattr(
            self,
            index_attr,
        ) % count

        batch = []

        for _ in range(batch_size):
            batch.append(
                sections[index]
            )

            index += 1

            if index >= count:
                index = 0

        setattr(
            self,
            index_attr,
            index,
        )

        data = self._packetize(
            batch
        )

        self.send(data)

    def broadcast_schedule_carousels(self):

        if not self._schedule_cache_ready:
            return

        self._broadcast_schedule_batch(
            self._schedule_actual_prime_cache,
            "_schedule_actual_prime_index",
            self.schedule_prime_interval,
        )

        self._broadcast_schedule_batch(
            self._schedule_other_prime_cache,
            "_schedule_other_prime_index",
            self.schedule_other_prime_interval,
        )

        self._broadcast_schedule_batch(
            self._schedule_actual_later_cache,
            "_schedule_actual_later_index",
            self.schedule_later_interval,
        )

        self._broadcast_schedule_batch(
            self._schedule_other_later_cache,
            "_schedule_other_later_index",
            self.schedule_other_later_interval,
        )

    # -------------------------------------------------
    # Lifecycle
    # -------------------------------------------------

    def start(self):
        with self._lock:

            if self.running:
                return

            self.running = True

            self.thread = threading.Thread(
                target=self._run,
                name=(
                    f"eit-mux-{self.mux_id}"
                ),
                daemon=True,
            )

            self.thread.start()

    def stop(self):
        with self._lock:

            if not self.running:
                return

            self.running = False

        if self.thread is not None:
            self.thread.join(
                timeout=5,
            )

            self.thread = None

        self._close_udp_socket()

    def _packetize(
        self,
        sections,
    ):
        if not sections:
            return b""

        data, next_cc = packetize_sections(
            sections,
            pid=self.pid,
            continuity_counter=(
                self.continuity_counter
            ),
        )

        self.continuity_counter = (
            next_cc
        )

        return data
