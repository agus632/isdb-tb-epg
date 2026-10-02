import logging
import threading

from app.database import SessionLocal
from app.models import ISDBTransportStream
from app.isdb.broadcaster import EITBroadcaster


logger = logging.getLogger(__name__)


class EITManager:
    """
    Manages all EIT broadcasters for the application.

    One EITBroadcaster is created for each enabled MUX
    with an active output configuration.
    """

    def __init__(self):
        self._broadcasters = {}
        self._lock = threading.Lock()

    def _build_broadcaster(
        self,
        mux: ISDBTransportStream,
    ) -> EITBroadcaster:

        broadcaster = EITBroadcaster(
            mux_id=mux.id,
            pid=mux.eit_pid or 0x0012,
        )

        broadcaster.configure(
            output_mode=(
                mux.eit_output_mode or "none"
            ).lower(),
            output_address=(
                mux.eit_output_address
                or "239.255.0.1"
            ),
            output_port=(
                mux.eit_output_port
                or 5000
            ),
            output_ttl=(
                mux.eit_output_ttl
                or 1
            ),
            output_interface=(
                mux.eit_output_interface
                or ""
            ),
            pf_interval=(
                mux.eit_pf_interval
                or 2
            ),
            schedule_prime_interval=(
                mux.eit_schedule_prime_interval
                or 10
            ),
            schedule_later_interval=(
                mux.eit_schedule_later_interval
                or 30
            ),
            prime_days=(
                mux.eit_prime_days
                or 8
            ),
        )

        return broadcaster

    def start(self):
        with self._lock:
            if self._broadcasters:
                logger.warning(
                    "EIT Manager already running"
                )
                return

            db = SessionLocal()

            try:
                muxes = (
                    db.query(ISDBTransportStream)
                    .filter(
                        ISDBTransportStream.enabled.is_(True)
                    )
                    .order_by(ISDBTransportStream.id)
                    .all()
                )

                for mux in muxes:
                    output_mode = (
                        mux.eit_output_mode or "none"
                    ).lower()

                    if output_mode == "none":
                        logger.info(
                            "MUX %s (%s): "
                            "EIT output disabled",
                            mux.id,
                            mux.name,
                        )
                        continue

                    broadcaster = (
                        self._build_broadcaster(mux)
                    )

                    broadcaster.start()

                    self._broadcasters[mux.id] = (
                        broadcaster
                    )

                    logger.info(
                        "MUX %s (%s): EIT started "
                        "PID=0x%04X output=%s:%s",
                        mux.id,
                        mux.name,
                        broadcaster.pid,
                        broadcaster.output_address,
                        broadcaster.output_port,
                    )

            except Exception:
                logger.exception(
                    "Error starting EIT Manager"
                )

                self._stop_unlocked()
                raise

            finally:
                db.close()

            logger.info(
                "EIT Manager started: "
                "%s broadcaster(s)",
                len(self._broadcasters),
            )

    def reload_mux(self, mux_id: int):
        """
        Reload one MUX without affecting the other
        broadcasters or the FastAPI process.
        """

        with self._lock:
            old_broadcaster = (
                self._broadcasters.pop(
                    mux_id,
                    None,
                )
            )

            if old_broadcaster is not None:
                logger.info(
                    "MUX %s: stopping old "
                    "EIT broadcaster",
                    mux_id,
                )

                old_broadcaster.stop()

            db = SessionLocal()

            try:
                mux = (
                    db.query(ISDBTransportStream)
                    .filter(
                        ISDBTransportStream.id
                        == mux_id
                    )
                    .first()
                )

                if mux is None:
                    logger.info(
                        "MUX %s no longer exists",
                        mux_id,
                    )
                    return

                if not mux.enabled:
                    logger.info(
                        "MUX %s (%s): disabled, "
                        "EIT broadcaster stopped",
                        mux.id,
                        mux.name,
                    )
                    return

                output_mode = (
                    mux.eit_output_mode or "none"
                ).lower()

                if output_mode == "none":
                    logger.info(
                        "MUX %s (%s): "
                        "EIT output disabled",
                        mux.id,
                        mux.name,
                    )
                    return

                broadcaster = (
                    self._build_broadcaster(mux)
                )

                broadcaster.start()

                self._broadcasters[mux.id] = (
                    broadcaster
                )

                logger.info(
                    "MUX %s (%s): EIT reloaded "
                    "PID=0x%04X output=%s:%s",
                    mux.id,
                    mux.name,
                    broadcaster.pid,
                    broadcaster.output_address,
                    broadcaster.output_port,
                )

            finally:
                db.close()

    def _stop_unlocked(self):
        for mux_id, broadcaster in list(
            self._broadcasters.items()
        ):
            try:
                logger.info(
                    "Stopping EIT broadcaster "
                    "for MUX %s",
                    mux_id,
                )

                broadcaster.stop()

            except Exception:
                logger.exception(
                    "Error stopping MUX %s",
                    mux_id,
                )

        self._broadcasters.clear()

    def stop(self):
        with self._lock:
            self._stop_unlocked()

            logger.info(
                "EIT Manager stopped"
            )

    def status(self):
        with self._lock:
            return {
                mux_id: {
                    "running": broadcaster.running,
                    "pid": broadcaster.pid,
                    "mode": broadcaster.output_mode,
                    "address": (
                        broadcaster.output_address
                    ),
                    "port": (
                        broadcaster.output_port
                    ),
                }
                for mux_id, broadcaster
                in self._broadcasters.items()
            }


eit_manager = EITManager()
