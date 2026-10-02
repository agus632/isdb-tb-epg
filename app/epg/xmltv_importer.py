import gzip
import io
from datetime import datetime, timezone

import requests
from lxml import etree
from sqlalchemy.orm import Session

from app.models import EPGChannel, EPGProgramme, EPGSource


USER_AGENT = "ISDB-Tb-EPG/0.4"


def utc_now_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_xmltv_datetime(value: str | None):
    if not value:
        return None

    value = value.strip()

    formats = [
        "%Y%m%d%H%M%S %z",
        "%Y%m%d%H%M %z",
        "%Y%m%d%H%M%S",
        "%Y%m%d%H%M",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(value, fmt)

            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)

            return dt

        except ValueError:
            continue

    raise ValueError(f"Fecha XMLTV inválida: {value}")


def get_text(element, tag):
    node = element.find(tag)

    if node is not None and node.text:
        return node.text.strip()

    return None


def download_xmltv(url: str) -> bytes:
    response = requests.get(
        url,
        timeout=(15, 120),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/xml,text/xml,application/gzip,*/*",
        },
    )

    response.raise_for_status()

    data = response.content

    if not data:
        raise ValueError("La fuente EPG devolvió un archivo vacío")

    # Detectar GZIP mediante magic bytes.
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except gzip.BadGzipFile as exc:
            raise ValueError(
                "La fuente devolvió un GZIP inválido"
            ) from exc

    if not data.strip():
        raise ValueError("El XMLTV descomprimido está vacío")

    return data


def parse_and_validate_xmltv(xml_data: bytes):
    try:
        root = etree.parse(
            io.BytesIO(xml_data),
            etree.XMLParser(
                recover=False,
                huge_tree=True,
                resolve_entities=False,
                no_network=True,
            ),
        ).getroot()

    except etree.XMLSyntaxError as exc:
        raise ValueError(
            f"XMLTV inválido: {exc}"
        ) from exc

    # Algunos XML pueden incluir namespace.
    root_name = etree.QName(root).localname

    if root_name != "tv":
        raise ValueError(
            f"El documento no parece XMLTV. Root: {root_name}"
        )

    channels = root.findall("channel")
    programmes = root.findall("programme")

    if len(channels) == 0:
        raise ValueError(
            "La fuente XMLTV no contiene canales"
        )

    if len(programmes) == 0:
        raise ValueError(
            "La fuente XMLTV no contiene programación"
        )

    return root, channels, programmes


def import_xmltv(source: EPGSource, db: Session):
    source_id = source.id

    try:
        #
        # PASO 1
        # Descargar SIN tocar todavía la base actual.
        #
        xml_data = download_xmltv(source.url)

        #
        # PASO 2
        # Validar completamente antes de borrar datos.
        #
        root, channels_xml, programmes_xml = (
            parse_and_validate_xmltv(xml_data)
        )

        #
        # PASO 3
        # Preparar canales en memoria.
        #
        parsed_channels = []

        for channel in channels_xml:
            xmltv_id = channel.get("id")

            if not xmltv_id:
                continue

            display_name = xmltv_id

            for node in channel.findall("display-name"):
                if node.text and node.text.strip():
                    display_name = node.text.strip()
                    break

            icon_url = None

            icon = channel.find("icon")

            if icon is not None:
                icon_url = icon.get("src")

            parsed_channels.append(
                {
                    "xmltv_id": xmltv_id,
                    "display_name": display_name,
                    "icon_url": icon_url,
                }
            )

        if not parsed_channels:
            raise ValueError(
                "No se pudo extraer ningún canal válido"
            )

        #
        # PASO 4
        # Preparar programas en memoria.
        #
        parsed_programmes = []

        for programme in programmes_xml:
            channel_id = programme.get("channel")
            start = programme.get("start")
            stop = programme.get("stop")

            if not channel_id or not start:
                continue

            try:
                start_time = parse_xmltv_datetime(start)
                stop_time = parse_xmltv_datetime(stop)

            except ValueError:
                continue

            title = get_text(programme, "title")

            if not title:
                title = "Sin título"

            parsed_programmes.append(
                {
                    "channel_xmltv_id": channel_id,
                    "start_time": start_time,
                    "stop_time": stop_time,
                    "title": title,
                    "subtitle": get_text(
                        programme,
                        "sub-title",
                    ),
                    "description": get_text(
                        programme,
                        "desc",
                    ),
                    "category": get_text(
                        programme,
                        "category",
                    ),
                }
            )

        if not parsed_programmes:
            raise ValueError(
                "No se pudo extraer ninguna programación válida"
            )

        #
        # PASO 5
        # Recién AHORA reemplazamos la información anterior.
        #
        db.query(EPGProgramme).filter(
            EPGProgramme.source_id == source_id
        ).delete(synchronize_session=False)

        db.query(EPGChannel).filter(
            EPGChannel.source_id == source_id
        ).delete(synchronize_session=False)

        db.flush()

        #
        # Insertar canales
        #
        for channel in parsed_channels:
            db.add(
                EPGChannel(
                    source_id=source_id,
                    **channel,
                )
            )

        db.flush()

        #
        # Insertar programación
        #
        for index, programme in enumerate(
            parsed_programmes,
            start=1,
        ):
            db.add(
                EPGProgramme(
                    source_id=source_id,
                    **programme,
                )
            )

            if index % 5000 == 0:
                db.flush()

        source.channel_count = len(parsed_channels)
        source.programme_count = len(parsed_programmes)

        source.last_update = utc_now_naive()
        source.last_status = "OK"

        source.last_message = (
            f"{len(parsed_channels)} canales / "
            f"{len(parsed_programmes)} programas"
        )

        db.commit()

        return {
            "success": True,
            "channels": len(parsed_channels),
            "programmes": len(parsed_programmes),
        }

    except Exception as exc:

        #
        # Rollback significa que conservamos la última
        # programación válida.
        #
        db.rollback()

        source = db.get(EPGSource, source_id)

        if source:
            source.last_update = utc_now_naive()
            source.last_status = "ERROR"
            source.last_message = (
                "Actualización fallida. "
                "Se conserva la última EPG válida. "
                f"Error: {str(exc)[:700]}"
            )

            db.commit()

        raise
