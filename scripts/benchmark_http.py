"""Sequential, low-impact Jellyfin GET baseline; never clears server caches."""

import argparse
import getpass
import json
import os
import statistics
import time

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    parser.add_argument("paths", nargs="*", default=[])
    parser.add_argument("--username", default="lazarr-local")
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 100:
        parser.error("repeat must be between 1 and 100")
    paths = args.paths or [
        "/Items?Recursive=true&IncludeItemTypes=Series,Movie&Limit=100",
        "/Items/Latest?Limit=12",
        "/Shows/NextUp?Limit=12",
        "/UserItems/Resume?Limit=12",
    ]
    if any(not p.startswith("/") or p.startswith("//") or "api_key=" in p.lower() for p in paths):
        parser.error("Use relative paths without credentials")
    password = os.getenv("LAZARR_BENCH_PASSWORD") or getpass.getpass("Test account password: ")
    with httpx.Client(base_url=args.base_url, timeout=120, follow_redirects=False) as client:
        response = client.post("/Users/AuthenticateByName", json={"Username": args.username, "Pw": password})
        if response.status_code != 200:
            raise SystemExit(f"Login failed: HTTP {response.status_code}")
        client.headers["X-Emby-Token"] = response.json()["AccessToken"]
        for path in paths:
            samples = []
            for index in range(args.repeat):
                start = time.perf_counter()
                try:
                    response = client.get(path)
                except httpx.RequestError as exc:
                    print(json.dumps({"path": path, "error": type(exc).__name__}), flush=True)
                    break
                elapsed = round((time.perf_counter() - start) * 1000, 3)
                print(
                    json.dumps(
                        {
                            "path": path,
                            "sample": index + 1,
                            "status": response.status_code,
                            "duration_ms": elapsed,
                            "bytes": len(response.content),
                            "server_timing": response.headers.get("server-timing"),
                            "request_id": response.headers.get("x-request-id"),
                        }
                    ),
                    flush=True,
                )
                if response.status_code != 200:
                    break
                samples.append(elapsed)
                time.sleep(0.2)
            if samples:
                print(
                    json.dumps(
                        {
                            "path": path,
                            "successful_samples": len(samples),
                            "median_ms": statistics.median(samples),
                            "max_ms": max(samples),
                        }
                    ),
                    flush=True,
                )


if __name__ == "__main__":
    main()
