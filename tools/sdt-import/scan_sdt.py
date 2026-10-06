#!/usr/bin/env python3

"""
SDT Import Scanner for isdb-tb-epg

Scans SDT Actual tables (PID 0x0011, table_id 0x42) from one or more
UDP/RTP MPEG-TS inputs using TSDuck and generates a normalized JSON file
which can be imported by the ISDB-Tb EPG SDT Generator.

This tool does not modify the input transport streams.
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


FORMAT_NAME = "isdb-tb-epg-sdt-import"
FORMAT_VERSION = 1


class ScanError(Exception):
    pass


def load_config(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        raise ScanError(f"Config file not found: {path}")
    except json.JSONDecodeError as exc:
        raise ScanError(f"Invalid JSON in config file: {exc}")

    if not isinstance(config, dict):
        raise ScanError("Configuration root must be a JSON object")

    inputs = config.get("inputs")

    if not isinstance(inputs, list) or not inputs:
        raise ScanError("Configuration must contain a non-empty 'inputs' list")

    return config


def validate_input(item, index):
    if not isinstance(item, dict):
        raise ScanError(f"Input #{index} must be an object")

    for field in ("name", "address", "port"):
        if field not in item:
            raise ScanError(f"Input #{index} is missing '{field}'")

    name = str(item["name"]).strip()
    address = str(item["address"]).strip()

    if not name:
        raise ScanError(f"Input #{index} has an empty name")

    if not address:
        raise ScanError(f"Input #{index} has an empty address")

    try:
        port = int(item["port"])
    except (TypeError, ValueError):
        raise ScanError(f"Input '{name}' has an invalid port")

    if not 1 <= port <= 65535:
        raise ScanError(f"Input '{name}' port must be between 1 and 65535")

    return name, address, port


def find_nodes(node, name):
    if not isinstance(node, dict):
        return []

    return [
        child
        for child in node.get("#nodes", [])
        if isinstance(child, dict) and child.get("#name") == name
    ]


def find_service_descriptor(service):
    descriptors = find_nodes(service, "service_descriptor")

    if not descriptors:
        return None

    return descriptors[0]


def parse_tsduck_sdt(data, input_name):
    if not isinstance(data, list):
        raise ScanError(f"{input_name}: TSDuck JSON root is not an array")

    sdts = [
        table
        for table in data
        if isinstance(table, dict)
        and table.get("#name") == "SDT"
        and table.get("actual") is True
        and table.get("current") is True
    ]

    if not sdts:
        raise ScanError(f"{input_name}: no current SDT Actual found")

    # There should normally be one current SDT Actual in the filtered input.
    sdt = sdts[-1]

    tsid = sdt.get("transport_stream_id")
    onid = sdt.get("original_network_id")

    if tsid is None:
        raise ScanError(f"{input_name}: SDT has no transport_stream_id")

    if onid is None:
        raise ScanError(f"{input_name}: SDT has no original_network_id")

    services = []

    for service in find_nodes(sdt, "service"):
        sid = service.get("service_id")

        if sid is None:
            raise ScanError(f"{input_name}: service without service_id")

        descriptor = find_service_descriptor(service)

        if descriptor is None:
            raise ScanError(
                f"{input_name}: service {sid} has no service_descriptor"
            )

        required_descriptor_fields = (
            "service_type",
            "service_name",
            "service_provider_name",
        )

        for field in required_descriptor_fields:
            if field not in descriptor:
                raise ScanError(
                    f"{input_name}: service {sid} descriptor "
                    f"is missing '{field}'"
                )

        services.append(
            {
                "service_id": int(sid),
                "service_type": int(descriptor["service_type"]),
                "service_name": descriptor["service_name"],
                "provider_name": descriptor["service_provider_name"],
                "running_status": service.get("running_status"),
                "free_ca_mode": bool(service.get("ca_mode", False)),
                "eit_schedule": bool(service.get("eit_schedule", False)),
                "eit_present_following": bool(
                    service.get("eit_present_following", False)
                ),
            }
        )

    if not services:
        raise ScanError(f"{input_name}: SDT contains no services")

    return {
        "name": input_name,
        "transport_stream_id": int(tsid),
        "original_network_id": int(onid),
        "services": services,
    }


def scan_input(name, address, port, interface, timeout_seconds):
    with tempfile.TemporaryDirectory(prefix="sdt-scan-") as temp_dir:
        json_file = Path(temp_dir) / "sdt.json"

        command = [
            "tsp",
            "-I",
            "ip",
            f"{address}:{port}",
        ]

        if interface:
            command.extend(["--local-address", interface])

        command.extend(
            [
                "-P",
                "tables",
                "--pid",
                "0x0011",
                "--tid",
                "0x42",
                "--max-tables",
                "1",
                "--json-output",
                str(json_file),
                "-O",
                "drop",
            ]
        )

        try:
            subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # Expected for a live multicast input. By this point the tables
            # plugin should already have written the SDT JSON file.
            pass
        except OSError as exc:
            raise ScanError(f"{name}: failed to execute TSDuck: {exc}")

        if not json_file.exists():
            raise ScanError(
                f"{name}: TSDuck did not produce an SDT JSON file"
            )

        try:
            with json_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise ScanError(f"{name}: invalid JSON produced by TSDuck: {exc}")

        return parse_tsduck_sdt(data, name)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Scan MPEG-TS SDT tables and create an import file "
            "for the isdb-tb-epg SDT Generator."
        )
    )

    parser.add_argument(
        "--config",
        required=True,
        help="JSON file containing the MPEG-TS inputs",
    )

    parser.add_argument(
        "--output",
        default="sdt-import.json",
        help="Output JSON file (default: sdt-import.json)",
    )

    args = parser.parse_args()

    if shutil.which("tsp") is None:
        print("ERROR: TSDuck 'tsp' command was not found.", file=sys.stderr)
        print("Install TSDuck before using this tool.", file=sys.stderr)
        return 1

    try:
        config = load_config(args.config)

        interface = config.get("interface")
        timeout_seconds = int(config.get("timeout", 5))

        if timeout_seconds < 1:
            raise ScanError("'timeout' must be at least 1 second")

        muxes = []
        errors = []

        print()
        print("SDT Import Scanner")
        print("=" * 60)
        print()

        for index, item in enumerate(config["inputs"], start=1):
            try:
                name, address, port = validate_input(item, index)

                print(f"Scanning {name} ({address}:{port})...")

                mux = scan_input(
                    name=name,
                    address=address,
                    port=port,
                    interface=interface,
                    timeout_seconds=timeout_seconds,
                )

                muxes.append(mux)

                print(
                    f"  TSID: {mux['transport_stream_id']}  "
                    f"ONID: {mux['original_network_id']}  "
                    f"Services: {len(mux['services'])}"
                )
                print("  OK")
                print()

            except ScanError as exc:
                errors.append(str(exc))
                print(f"  ERROR: {exc}")
                print()

        if not muxes:
            raise ScanError("No valid SDT tables were captured")

        output = {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "source": {
                "tool": "TSDuck",
                "table": "SDT Actual",
                "table_id": 66,
                "pid": 17,
            },
            "muxes": muxes,
        }

        output_path = Path(args.output)

        output_path.parent.mkdir(parents=True, exist_ok=True)

        with output_path.open("w", encoding="utf-8") as f:
            json.dump(
                output,
                f,
                ensure_ascii=False,
                indent=2,
            )
            f.write("\n")

        service_count = sum(len(m["services"]) for m in muxes)

        print("=" * 60)
        print(f"MUX scanned:     {len(muxes)}")
        print(f"Services found: {service_count}")
        print(f"Errors:          {len(errors)}")
        print(f"Output:          {output_path}")
        print("=" * 60)

        if errors:
            print()
            print("Some inputs could not be scanned:")
            for error in errors:
                print(f"  - {error}")

            return 2

        return 0

    except (ScanError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
