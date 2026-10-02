import signal
import time

from app.database import SessionLocal
from app.models import ISDBTransportStream
from app.isdb.broadcaster import EITBroadcaster


def load_broadcasters():
    db = SessionLocal()
    broadcasters = []

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
                print(
                    f"MUX {mux.id} ({mux.name}): "
                    f"output disabled, skipping"
                )
                continue

            broadcaster = EITBroadcaster(
                mux_id=mux.id,
                pid=mux.eit_pid or 0x0012,
            )

            broadcaster.configure(
                output_mode=output_mode,
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

            broadcasters.append(
                (
                    mux.id,
                    mux.name,
                    broadcaster,
                )
            )

    finally:
        db.close()

    return broadcasters


def print_configuration(broadcasters):
    print("======================================")
    print(" ISDB-T EIT STREAMER")
    print("======================================")

    for mux_id, mux_name, broadcaster in broadcasters:
        print(
            f"MUX {mux_id}: {mux_name}"
        )
        print(
            f"  PID:       "
            f"0x{broadcaster.pid:04X}"
        )
        print(
            f"  Mode:      "
            f"{broadcaster.output_mode}"
        )
        print(
            f"  Output:    "
            f"{broadcaster.output_address}:"
            f"{broadcaster.output_port}"
        )
        print(
            f"  Interface: "
            f"{broadcaster.output_interface or 'auto'}"
        )
        print(
            f"  TTL:       "
            f"{broadcaster.output_ttl}"
        )
        print(
            f"  P/F:       "
            f"{broadcaster.pf_interval}s"
        )
        print(
            f"  Schedule:  "
            f"{broadcaster.schedule_prime_interval}s / "
            f"{broadcaster.schedule_later_interval}s"
        )
        print("--------------------------------------")

    print(
        f"Active broadcasters: "
        f"{len(broadcasters)}"
    )
    print("======================================")


def main():
    broadcasters = load_broadcasters()

    if not broadcasters:
        raise RuntimeError(
            "No enabled MUX has an active EIT output"
        )

    stopping = False

    def stop_handler(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(
        signal.SIGTERM,
        stop_handler,
    )

    signal.signal(
        signal.SIGINT,
        stop_handler,
    )

    print_configuration(broadcasters)

    started = []

    try:
        for mux_id, mux_name, broadcaster in broadcasters:
            print(
                f"Starting MUX {mux_id} "
                f"({mux_name})..."
            )

            broadcaster.start()
            started.append(
                (
                    mux_id,
                    mux_name,
                    broadcaster,
                )
            )

        print(
            "All EIT broadcasters started"
        )

        while not stopping:
            time.sleep(1)

    finally:
        print(
            "Stopping EIT broadcasters..."
        )

        for mux_id, mux_name, broadcaster in reversed(started):
            try:
                print(
                    f"Stopping MUX {mux_id} "
                    f"({mux_name})..."
                )
                broadcaster.stop()
            except Exception as exc:
                print(
                    f"Error stopping MUX {mux_id}: "
                    f"{exc}"
                )

        print(
            "All EIT broadcasters stopped"
        )


if __name__ == "__main__":
    main()
