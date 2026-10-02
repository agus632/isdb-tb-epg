import logging
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Optional
from app.database import SessionLocal
from app.models import ISDBTransportStream
from app.isdb.generator import generate_mux_sections
from app.isdb.packetizer import packetize_sections
from app.isdb.time_tables import (
    TIME_PID,
    build_time_tables,
)

logger = logging.getLogger(__name__)


OUTPUT_DIR = Path(
    "/opt/isdb-epg/data/generated"
)


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

        # TDT/TOT repetition interval.
        self.time_tables_interval = 5

        self.output_mode = "none"
        self.output_address = "239.255.0.1"
        self.output_port = 5000
        self.output_ttl = 1
        self.output_interface = ""

        self.pf_interval = 2
        self.schedule_prime_interval = 10
        self.schedule_later_interval = 30
        self.prime_days = 8

        # Schedule cache.
        #
        # Raw EIT sections are cached here. Packetization is
        # intentionally performed only when transmitting so
        # continuity_counter remains continuous on PID 0x0012.
        self.schedule_cache_interval = 60

        self._schedule_prime_cache = []
        self._schedule_later_cache = []

        self._schedule_cache_ready = False

        # Continuous Schedule carousel state.
        self._schedule_prime_index = 0
        self._schedule_later_index = 0

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
        next_schedule_prime = 0.0
        next_schedule_later = 0.0
        next_time_tables = 0.0

        try:
            while self.running:

                now = time.monotonic()

                if now >= next_time_tables:
                    self.broadcast_time_tables()

                    next_time_tables = (
                        now
                        + self.time_tables_interval
                    )

                if now >= next_pf:
                    self.broadcast_pf()

                    next_pf = (
                        now
                        + self.pf_interval
                    )

                if now >= next_schedule_refresh:
                    self.refresh_schedule_cache()

                    next_schedule_refresh = (
                        now
                        + self.schedule_cache_interval
                    )

                if now >= next_schedule_prime:
                    self.broadcast_schedule_prime()

                    prime_count = len(
                        self._schedule_prime_cache
                    )

                    if prime_count:
                        prime_step = (
                            self.schedule_prime_interval
                            / prime_count
                        )
                    else:
                        prime_step = (
                            self.schedule_prime_interval
                        )

                    next_schedule_prime = (
                        now
                        + max(0.05, prime_step)
                    )

                if now >= next_schedule_later:
                    self.broadcast_schedule_later()

                    later_count = len(
                        self._schedule_later_cache
                    )

                    if later_count:
                        later_step = (
                            self.schedule_later_interval
                            / later_count
                        )
                    else:
                        later_step = (
                            self.schedule_later_interval
                        )

                    next_schedule_later = (
                        now
                        + max(0.05, later_step)
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

    def broadcast_pf(self):
        db = SessionLocal()

        try:
            result = generate_mux_sections(
                db,
                self.mux_id,
                generate="pf",
            )

            sections = result["pf"]

            if not sections:
                return

            data = self._packetize(
                sections
            )

            self.send(data)

        except Exception:
            logger.exception(
                "Error generating EIT P/F "
                "for MUX %s",
                self.mux_id,
            )

        finally:
            db.close()

    def refresh_schedule_cache(self):
        """
        Regenerate raw Schedule EIT sections.

        Sections are cached before MPEG-TS packetization.
        This guarantees that the continuity counter advances
        only when sections are actually transmitted.
        """

        db = SessionLocal()

        try:
            result = generate_mux_sections(
                db,
                self.mux_id,
                generate="schedule",
            )

            prime = list(
                result["schedule_prime"]
            )

            later = list(
                result["schedule_later"]
            )

            self._schedule_prime_cache = prime
            self._schedule_later_cache = later
            self._schedule_cache_ready = True

            if prime:
                self._schedule_prime_index %= len(
                    prime
                )
            else:
                self._schedule_prime_index = 0

            if later:
                self._schedule_later_index %= len(
                    later
                )
            else:
                self._schedule_later_index = 0

            logger.debug(
                "MUX %s Schedule cache refreshed: "
                "prime=%s later=%s",
                self.mux_id,
                len(prime),
                len(later),
            )

        except Exception:
            logger.exception(
                "Error refreshing EIT Schedule "
                "cache for MUX %s",
                self.mux_id,
            )

        finally:
            db.close()

    def broadcast_schedule_prime(self):

        if not self._schedule_cache_ready:
            self.refresh_schedule_cache()

        sections = self._schedule_prime_cache

        if not sections:
            return

        index = (
            self._schedule_prime_index
            % len(sections)
        )

        section = sections[index]

        self._schedule_prime_index = (
            index + 1
        ) % len(sections)

        data = self._packetize(
            [section]
        )

        self.send(data)

    def broadcast_schedule_later(self):

        if not self._schedule_cache_ready:
            self.refresh_schedule_cache()

        sections = self._schedule_later_cache

        if not sections:
            return

        index = (
            self._schedule_later_index
            % len(sections)
        )

        section = sections[index]

        self._schedule_later_index = (
            index + 1
        ) % len(sections)

        data = self._packetize(
            [section]
        )

        self.send(data)

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
