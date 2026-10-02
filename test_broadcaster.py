import time

from app.database import SessionLocal
from app.models import ISDBTransportStream
from app.isdb.broadcaster import EITBroadcaster


MUX_ID = 1


db = SessionLocal()

mux = (
    db.query(ISDBTransportStream)
    .filter(
        ISDBTransportStream.id == MUX_ID
    )
    .first()
)

if mux is None:
    raise RuntimeError(
        f"MUX {MUX_ID} not found"
    )


broadcaster = EITBroadcaster(
    mux_id=mux.id,
    pid=mux.eit_pid or 0x0012,
)

broadcaster.configure(
    output_mode="file",

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

db.close()


print(
    "Starting EIT broadcaster..."
)

broadcaster.start()

try:
    time.sleep(35)

finally:
    broadcaster.stop()


print(
    "EIT broadcaster stopped."
)
