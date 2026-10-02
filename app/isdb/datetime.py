from datetime import datetime, timezone


def bcd(value: int) -> int:
    if value < 0 or value > 99:
        raise ValueError(
            f"Valor BCD fuera de rango: {value}"
        )

    return (
        ((value // 10) << 4)
        | (value % 10)
    )


def datetime_to_mjd(dt: datetime) -> int:
    """
    Convierte una fecha a Modified Julian Date.

    La EIT transporta:
        16 bits MJD
        24 bits UTC en BCD
    """

    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
        dt = dt.replace(tzinfo=None)

    year = dt.year
    month = dt.month
    day = dt.day

    if month <= 2:
        year -= 1
        month += 12

    a = year // 100
    b = 2 - a + (a // 4)

    jd = int(
        365.25 * (year + 4716)
    )

    jd += int(
        30.6001 * (month + 1)
    )

    jd += day + b - 1524

    mjd = jd - 2400001

    return mjd


def encode_datetime(dt: datetime) -> bytes:
    """
    Codifica fecha/hora como:

        MJD: 16 bits
        hour: BCD
        minute: BCD
        second: BCD

    Total: 5 bytes.
    """

    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
        dt = dt.replace(tzinfo=None)

    mjd = datetime_to_mjd(dt)

    return (
        mjd.to_bytes(2, "big")
        + bytes(
            [
                bcd(dt.hour),
                bcd(dt.minute),
                bcd(dt.second),
            ]
        )
    )


def encode_duration(seconds: int) -> bytes:
    """
    Duración EIT HH:MM:SS en BCD.
    """

    if seconds < 0:
        seconds = 0

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if hours > 99:
        hours = 99
        minutes = 59
        secs = 59

    return bytes(
        [
            bcd(hours),
            bcd(minutes),
            bcd(secs),
        ]
    )
