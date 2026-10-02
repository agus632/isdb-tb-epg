import time

from app.database import SessionLocal
from app.models import ISDBTransportStream
from app.isdb.broadcaster import EITBroadcaster


MUX_ID = 1
TEST_SECONDS = 35


db = SessionLocal()

try:
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
        pf_interval=mux.eit_pf_interval or 2,
        schedule_prime_interval=(
            mux.eit_schedule_prime_interval or 10
        ),
        schedule_later_interval=(
            mux.eit_schedule_later_interval or 30
        ),
        prime_days=mux.eit_prime_days or 8,
        output_mode=mux.eit_output_mode or "none",
        output_address=(
            mux.eit_output_address
            or "239.255.0.1"
        ),
        output_port=mux.eit_output_port or 5000,
        output_ttl=mux.eit_output_ttl or 1,
        output_interface=(
            mux.eit_output_interface or ""
        ),
    )

finally:
    db.close()


print("======================================")
print(" EIT UDP TEST")
print("======================================")
print(f"MUX:       {MUX_ID}")
print(f"PID:       0x{broadcaster.pid:04X}")
print(f"Mode:      {broadcaster.output_mode}")
print(
    f"Output:    "
    f"{broadcaster.output_address}:"
    f"{broadcaster.output_port}"
)
print(f"TTL:       {broadcaster.output_ttl}")
print(
    f"Interface: "
    f"{broadcaster.output_interface or 'auto'}"
)
print(f"P/F:       {broadcaster.pf_interval}s")
print(
    f"Schedule:  "
    f"{broadcaster.schedule_prime_interval}s / "
    f"{broadcaster.schedule_later_interval}s"
)
print(f"Duration:  {TEST_SECONDS}s")
print("======================================")

broadcaster.start()

try:
    time.sleep(TEST_SECONDS)
finally:
    broadcaster.stop()

print("EIT UDP test finished")
