"""Simulate a detection device (Raspberry Pi + camera): upload one photo to the backend.

Usage:
    python scripts/send_detection.py uploads/sample_a.jpg
    python scripts/send_detection.py photo.jpg --serial RPI-A-PANGYO-01 --zone B-01
    python scripts/send_detection.py photo.jpg --key pgk_xxx          (a real registered machine)
    python scripts/send_detection.py uploads/sample_b.jpg --repeat 2     (two sightings confirm a violation)
    python scripts/send_detection.py --heartbeat

Without --key the fixed demo key of a seeded machine is used (see `flask --app wsgi seed`).
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("image", nargs="?", help="photo to upload")
    parser.add_argument("--url", default="http://127.0.0.1:5000/api/v1", help="API base URL")
    parser.add_argument("--serial", default="RPI-A-GANGNAM-01", help="seeded demo machine serial number")
    parser.add_argument("--key", help="device API key (overrides --serial)")
    parser.add_argument("--zone", help="zone name inside the machine's parking lot")
    parser.add_argument("--heartbeat", action="store_true", help="send a heartbeat instead of a photo")
    parser.add_argument("--repeat", type=int, default=1,
                        help="send the photo this many times, like a device uploading every minute")
    args = parser.parse_args()

    headers = {"X-Device-Key": args.key or f"pgk_dev_{args.serial.lower()}"}

    if args.heartbeat:
        response = requests.post(f"{args.url}/device/heartbeat", headers=headers, timeout=10)
        return show(response)

    if not args.image:
        parser.error("image is required unless --heartbeat is given")
    path = Path(args.image)
    exit_code = 0
    for attempt in range(max(1, args.repeat)):
        form = {"detected_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if args.zone:
            form["zone_name"] = args.zone
        with path.open("rb") as handle:
            response = requests.post(f"{args.url}/device/detections", headers=headers, data=form,
                                     files={"file": (path.name, handle)}, timeout=180)
        if args.repeat > 1:
            print(f"--- upload {attempt + 1}/{args.repeat} ---")
        exit_code = show(response) or exit_code
    return exit_code


def show(response) -> int:
    print(f"HTTP {response.status_code}")
    try:
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    except ValueError:
        print(response.text)
    return 0 if response.ok else 1


if __name__ == "__main__":
    sys.exit(main())
