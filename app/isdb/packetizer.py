TS_PACKET_SIZE = 188
TS_SYNC_BYTE = 0x47


def packetize_sections(
    sections: list[bytes],
    pid: int = 0x0012,
    continuity_counter: int = 0,
) -> tuple[bytes, int]:
    """
    Packetiza secciones PSI/SI en paquetes MPEG-TS
    de 188 bytes.

    Retorna:
        (transport_stream_bytes, next_cc)
    """

    if not 0 <= pid <= 0x1FFF:
        raise ValueError(
            "PID fuera de rango"
        )

    packets = bytearray()

    cc = continuity_counter & 0x0F

    for section in sections:

        offset = 0
        first_packet = True

        while offset < len(section):

            payload_unit_start = (
                1 if first_packet else 0
            )

            header = bytearray(4)

            header[0] = TS_SYNC_BYTE

            header[1] = (
                (payload_unit_start << 6)
                | ((pid >> 8) & 0x1F)
            )

            header[2] = (
                pid & 0xFF
            )

            # adaptation_field_control = 01
            # payload only

            header[3] = (
                0x10
                | cc
            )

            cc = (
                cc + 1
            ) & 0x0F

            payload_capacity = 184

            payload = bytearray()

            if first_packet:

                # pointer_field = 0
                payload.append(0x00)

                payload_capacity -= 1

            remaining = (
                len(section)
                - offset
            )

            take = min(
                payload_capacity,
                remaining,
            )

            payload.extend(
                section[
                    offset:
                    offset + take
                ]
            )

            offset += take

            packet = (
                header
                + payload
            )

            if len(packet) < TS_PACKET_SIZE:

                packet.extend(
                    b"\xFF"
                    * (
                        TS_PACKET_SIZE
                        - len(packet)
                    )
                )

            if len(packet) != TS_PACKET_SIZE:
                raise RuntimeError(
                    "Paquete MPEG-TS inválido"
                )

            packets.extend(packet)

            first_packet = False

    return bytes(packets), cc
