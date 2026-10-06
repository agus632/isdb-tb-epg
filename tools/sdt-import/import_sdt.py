#!/usr/bin/env python3

import argparse
import json
import sys
from pathlib import Path

# Allow this tool to be executed directly from tools/sdt-import/
# while importing the project's app package.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.orm import joinedload

from app.database import SessionLocal
from app.models import (
    ISDBTransportStream,
    ISDBSDTConfig,
    ISDBSDTService,
)


FORMAT_NAME = "isdb-tb-epg-sdt-import"
FORMAT_VERSION = 1


def load_import_file(path: Path):
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"file not found: {path}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}")

    if not isinstance(data, dict):
        raise ValueError("root JSON value must be an object")

    if data.get("format") != FORMAT_NAME:
        raise ValueError(
            f"unsupported format: {data.get('format')!r}"
        )

    if data.get("version") != FORMAT_VERSION:
        raise ValueError(
            f"unsupported format version: {data.get('version')!r}"
        )

    muxes = data.get("muxes")

    if not isinstance(muxes, list) or not muxes:
        raise ValueError("'muxes' must be a non-empty list")

    return data


def require_int(obj, field, context):
    value = obj.get(field)

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{context}: '{field}' must be an integer"
        )

    return value


def require_bool(obj, field, context):
    value = obj.get(field)

    if not isinstance(value, bool):
        raise ValueError(
            f"{context}: '{field}' must be a boolean"
        )

    return value


def require_string(obj, field, context):
    value = obj.get(field)

    if not isinstance(value, str):
        raise ValueError(
            f"{context}: '{field}' must be a string"
        )

    # Intentionally do not strip().
    # SDT service/provider strings are imported exactly as captured.
    return value


def validate_and_resolve(db, data):
    db_muxes = (
        db.query(ISDBTransportStream)
        .options(joinedload(ISDBTransportStream.network))
        .all()
    )

    mux_index = {}

    for mux in db_muxes:
        key = (
            mux.transport_stream_id,
            mux.network.original_network_id,
        )

        if key in mux_index:
            raise ValueError(
                "database contains duplicate TSID/ONID "
                f"combination {key}"
            )

        mux_index[key] = mux

    resolved = []
    seen_muxes = set()

    for pos, imported_mux in enumerate(data["muxes"], start=1):
        if not isinstance(imported_mux, dict):
            raise ValueError(f"MUX #{pos}: must be an object")

        name = imported_mux.get("name", f"MUX #{pos}")

        tsid = require_int(
            imported_mux,
            "transport_stream_id",
            str(name),
        )

        onid = require_int(
            imported_mux,
            "original_network_id",
            str(name),
        )

        if not 0 <= tsid <= 0xFFFF:
            raise ValueError(f"{name}: invalid TSID {tsid}")

        if not 0 <= onid <= 0xFFFF:
            raise ValueError(f"{name}: invalid ONID {onid}")

        key = (tsid, onid)

        if key in seen_muxes:
            raise ValueError(
                f"{name}: duplicate TSID/ONID {key} in import file"
            )

        seen_muxes.add(key)

        db_mux = mux_index.get(key)

        if db_mux is None:
            raise ValueError(
                f"{name}: no database MUX matches "
                f"TSID={tsid} ONID={onid}"
            )

        services = imported_mux.get("services")

        if not isinstance(services, list):
            raise ValueError(
                f"{name}: 'services' must be a list"
            )

        validated_services = []
        seen_sids = set()

        for service_pos, svc in enumerate(services, start=1):
            context = f"{name} service #{service_pos}"

            if not isinstance(svc, dict):
                raise ValueError(f"{context}: must be an object")

            sid = require_int(svc, "service_id", context)

            if not 0 <= sid <= 0xFFFF:
                raise ValueError(
                    f"{context}: invalid service_id {sid}"
                )

            if sid in seen_sids:
                raise ValueError(
                    f"{name}: duplicate service_id {sid}"
                )

            seen_sids.add(sid)

            service_type = require_int(
                svc,
                "service_type",
                context,
            )

            if not 0 <= service_type <= 0xFF:
                raise ValueError(
                    f"{context}: invalid service_type {service_type}"
                )

            validated_services.append({
                "service_id": sid,
                "service_name": require_string(
                    svc, "service_name", context
                ),
                "provider_name": require_string(
                    svc, "provider_name", context
                ),
                "service_type": service_type,
                "running_status": require_string(
                    svc, "running_status", context
                ),
                "free_ca_mode": require_bool(
                    svc, "free_ca_mode", context
                ),
                "eit_present_following": require_bool(
                    svc, "eit_present_following", context
                ),
                "eit_schedule": require_bool(
                    svc, "eit_schedule", context
                ),
            })

        resolved.append({
            "import_name": name,
            "mux": db_mux,
            "tsid": tsid,
            "onid": onid,
            "services": validated_services,
        })

    return resolved


def import_sdt(db, resolved):
    mux_count = 0
    service_count = 0

    for item in resolved:
        mux = item["mux"]

        config = (
            db.query(ISDBSDTConfig)
            .filter(
                ISDBSDTConfig.transport_stream_id_fk == mux.id
            )
            .one_or_none()
        )

        if config is None:
            config = ISDBSDTConfig(
                transport_stream_id_fk=mux.id,
                enabled=False,
                version=0,
                output_mode="none",
            )
            db.add(config)
            db.flush()

        # Import represents a complete SDT snapshot.
        # Replace the service set for this MUX.
        config.services.clear()
        db.flush()

        for svc in item["services"]:
            config.services.append(
                ISDBSDTService(
                    service_id=svc["service_id"],
                    service_name=svc["service_name"],
                    provider_name=svc["provider_name"],
                    service_type=svc["service_type"],
                    running_status=svc["running_status"],
                    free_ca_mode=svc["free_ca_mode"],
                    eit_present_following=svc[
                        "eit_present_following"
                    ],
                    eit_schedule=svc["eit_schedule"],
                    enabled=True,
                )
            )

        mux_count += 1
        service_count += len(item["services"])

    return mux_count, service_count


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Import normalized SDT data into the ISDB-Tb EPG database."
        )
    )

    parser.add_argument(
        "input",
        type=Path,
        help="JSON file generated by scan_sdt.py",
    )

    parser.add_argument(
        "--check",
        action="store_true",
        help="validate and resolve the import without modifying the database",
    )

    args = parser.parse_args()

    db = SessionLocal()

    try:
        data = load_import_file(args.input)
        resolved = validate_and_resolve(db, data)

        mux_count = len(resolved)
        service_count = sum(
            len(item["services"])
            for item in resolved
        )

        print(f"MUX validated:     {mux_count}")
        print(f"Services validated: {service_count}")

        for item in resolved:
            print(
                f"  {item['import_name']:<8} "
                f"TSID={item['tsid']:<5} "
                f"ONID={item['onid']:<5} "
                f"DB_MUX_ID={item['mux'].id:<4} "
                f"Services={len(item['services'])}"
            )

        if args.check:
            print()
            print("CHECK OK - database was not modified")
            return 0

        imported_muxes, imported_services = import_sdt(
            db,
            resolved,
        )

        db.commit()

        print()
        print(f"MUX imported:      {imported_muxes}")
        print(f"Services imported: {imported_services}")
        print("IMPORT OK")

        return 0

    except Exception as exc:
        db.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
