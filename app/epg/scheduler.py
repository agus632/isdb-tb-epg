import logging
import threading
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models import EPGSource
from app.epg.xmltv_importer import import_xmltv
from app.epg.manual import regenerate_manual_epg


logger = logging.getLogger("uvicorn.error")


class EPGScheduler:
    """
    Automatic updater for enabled EPG sources.

    update_interval is interpreted as hours.

    Only one source is updated at a time. Each import receives
    its own SQLAlchemy session so large XMLTV imports are never
    executed concurrently by this scheduler.
    """

    def __init__(
        self,
        check_interval: int = 60,
        source_delay: int = 10,
    ):
        self.check_interval = check_interval
        self.source_delay = source_delay

        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._run,
            name="epg-source-scheduler",
            daemon=True,
        )

        self._thread.start()

        logger.info(
            "EPG scheduler started: "
            "check_interval=%ss source_delay=%ss",
            self.check_interval,
            self.source_delay,
        )

    def stop(self):
        self._stop_event.set()

        thread = self._thread

        if (
            thread
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=30)

        logger.info("EPG scheduler stopped")

    def _run(self):
        # Small initial delay. This lets the EIT broadcasters
        # finish their initial startup work before XMLTV imports.
        if self._stop_event.wait(30):
            return

        while not self._stop_event.is_set():

            try:
                self._check_sources()

            except Exception:
                logger.exception(
                    "Unhandled EPG scheduler error"
                )

            if self._stop_event.wait(self.check_interval):
                break

    def _get_due_source_ids(self):
        db = SessionLocal()

        try:
            now = datetime.now(timezone.utc).replace(tzinfo=None)

            sources = (
                db.query(EPGSource)
                .filter(
                    EPGSource.enabled.is_(True),
                )
                .order_by(EPGSource.id)
                .all()
            )

            due = []

            for source in sources:
                interval = timedelta(
                    hours=max(1, source.update_interval)
                )

                if source.last_update is None:
                    due.append(source.id)
                    continue

                if source.last_update + interval <= now:
                    due.append(source.id)

            return due

        finally:
            db.close()

    def _check_sources(self):
        due_ids = self._get_due_source_ids()

        if not due_ids:
            return

        logger.info(
            "EPG scheduler: %d source(s) due: %s",
            len(due_ids),
            ", ".join(str(x) for x in due_ids),
        )

        for position, source_id in enumerate(due_ids):

            if self._stop_event.is_set():
                return

            db = SessionLocal()

            try:
                source = db.get(EPGSource, source_id)

                if source is None:
                    continue

                if not source.enabled:
                    continue

                # Recheck because this source could have been
                # updated manually since due_ids was calculated.
                now = datetime.now(timezone.utc).replace(tzinfo=None)

                if source.last_update is not None:
                    due_at = (
                        source.last_update
                        + timedelta(
                            hours=max(
                                1,
                                source.update_interval,
                            )
                        )
                    )

                    if due_at > now:
                        continue

                logger.info(
                    "EPG scheduler updating source %d: %s",
                    source.id,
                    source.name,
                )

                try:
                    if source.source_type == "manual":
                        days = source.manual_days or 8
                        block_hours = source.manual_block_hours or 6

                        regenerate_manual_epg(
                            source,
                            db,
                            days=days,
                            block_hours=block_hours,
                        )

                        logger.info(
                            "EPG scheduler updated manual source %d: "
                            "%d programmes, %d days, %d h blocks",
                            source.id,
                            source.programme_count,
                            days,
                            block_hours,
                        )

                    else:
                        result = import_xmltv(source, db)

                        logger.info(
                            "EPG scheduler updated source %d: "
                            "%d channels / %d programmes",
                            source.id,
                            result["channels"],
                            result["programmes"],
                        )

                except Exception as exc:
                    logger.error(
                        "EPG scheduler source %d failed: %s",
                        source.id,
                        exc,
                    )

            finally:
                db.close()

            # Avoid starting several large imports back-to-back
            # without giving SQLite / broadcasters some room.
            if position < len(due_ids) - 1:
                if self._stop_event.wait(self.source_delay):
                    return


epg_scheduler = EPGScheduler()
