#!/usr/bin/env python3
"""Update DeshStream.m3u stream URLs from IPTV-org.

The existing DeshStream.m3u is the source of truth for which stream IDs
(channel@feed) are allowed. IPTV-org supplies the current URLs.

The updater is deliberately fail-safe:
- only tvg-ids already present in DeshStream.m3u are considered
- channel@feed is matched exactly when a feed is present
- the old playlist is left untouched if IPTV-org cannot be downloaded
- a missing individual stream does not delete the old entry
- duplicate URLs are removed
- stream referrer/user-agent are preserved when IPTV-org supplies them
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PLAYLIST = Path("DeshStream.m3u")
STREAMS_URL = "https://iptv-org.github.io/api/streams.json"
TIMEOUT = 90


def download_json(url: str):
    request = Request(
        url,
        headers={
            "User-Agent": "DeshStream-Updater/1.0 (+https://github.com/blurryrizz/DeshStream)"
        },
    )
    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not download/parse IPTV-org streams.json: {exc}") from exc


def split_stream_id(stream_id: str) -> tuple[str, str | None]:
    if "@" in stream_id:
        channel, feed = stream_id.split("@", 1)
        return channel, feed
    return stream_id, None


def parse_playlist(text: str):
    """Return header lines and entries while preserving the first entry's metadata."""
    lines = text.splitlines()
    header = []
    entries = []
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        if line.startswith("#EXTINF"):
            extinf = lines[i]
            url = lines[i + 1].strip() if i + 1 < len(lines) else ""
            match = re.search(r'tvg-id="([^"]+)"', extinf)
            if match and url and not url.startswith("#"):
                entries.append({
                    "tvg_id": match.group(1).strip(),
                    "extinf": extinf,
                    "url": url,
                })
                i += 2
                continue

        header.append(lines[i])
        i += 1

    return header, entries


def selected_ids(entries):
    return sorted({entry["tvg_id"] for entry in entries if entry["tvg_id"]})


def stream_matches(stream: dict, tvg_id: str) -> bool:
    channel = stream.get("channel")
    feed = stream.get("feed")
    wanted_channel, wanted_feed = split_stream_id(tvg_id)

    if channel != wanted_channel:
        return False

    if wanted_feed is not None:
        return feed == wanted_feed

    return True


def quality_score(value: str | None) -> int:
    if not value:
        return 0
    match = re.search(r"(\d{3,4})p", value.lower())
    return int(match.group(1)) if match else 0


def stream_sort_key(stream: dict):
    # Prefer streams without an availability warning, then higher quality.
    label = (stream.get("label") or "").lower()
    warning = 1 if label else 0
    return (warning, -quality_score(stream.get("quality")), stream.get("url", ""))


def attr_escape(value: str) -> str:
    return value.replace('"', "'").replace("\n", " ").replace("\r", " ").strip()


def add_or_replace_attribute(extinf: str, name: str, value: str) -> str:
    pattern = rf'\s{name}="[^"]*"'
    replacement = f' {name}="{attr_escape(value)}"'
    if re.search(pattern, extinf):
        return re.sub(pattern, replacement, extinf, count=1)
    return extinf


def remove_attribute(extinf: str, name: str) -> str:
    return re.sub(rf'\s{name}="[^"]*"', "", extinf, count=1)


def build_playlist(original_text: str, streams: list[dict]) -> tuple[str, dict]:
    header, entries = parse_playlist(original_text)
    ids = selected_ids(entries)

    # Use the first occurrence of each tvg-id as the metadata template.
    templates = {}
    for entry in entries:
        templates.setdefault(entry["tvg_id"], entry["extinf"])

    matched = {stream_id: [] for stream_id in ids}
    for stream in streams:
        for stream_id in ids:
            if stream_matches(stream, stream_id) and stream.get("url"):
                matched[stream_id].append(stream)

    output = list(header)
    stats = {"selected": len(ids), "found": 0, "missing": 0, "urls": 0}
    seen_global = set()

    for stream_id in ids:
        candidates = matched[stream_id]
        unique = []
        seen_urls = set()

        for stream in sorted(candidates, key=stream_sort_key):
            url = stream["url"].strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            unique.append(stream)

        if not unique:
            stats["missing"] += 1
            # Preserve every old entry for this ID if IPTV-org has no match.
            for entry in entries:
                if entry["tvg_id"] == stream_id:
                    key = (stream_id, entry["url"])
                    if key not in seen_global:
                        output.extend([entry["extinf"], entry["url"]])
                        seen_global.add(key)
            print(f"MISSING  {stream_id}")
            continue

        stats["found"] += 1
        stats["urls"] += len(unique)
        print(f"FOUND    {stream_id}: {len(unique)} stream(s)")

        template = templates[stream_id]
        for stream in unique:
            extinf = template

            # IPTV-org may provide request headers needed by the stream.
            referrer = stream.get("referrer")
            user_agent = stream.get("user_agent")

            if referrer:
                extinf = add_or_replace_attribute(extinf, "http-referrer", referrer)
            else:
                extinf = remove_attribute(extinf, "http-referrer")

            if user_agent:
                extinf = add_or_replace_attribute(extinf, "http-user-agent", user_agent)
            else:
                extinf = remove_attribute(extinf, "http-user-agent")

            output.extend([extinf, stream["url"].strip()])

    # Always write a valid M3U even when the original file had no trailing newline.
    result = "\n".join(output).rstrip() + "\n"
    return result, stats


def main() -> int:
    if not PLAYLIST.exists():
        print(f"ERROR: {PLAYLIST} does not exist.")
        return 1

    original = PLAYLIST.read_text(encoding="utf-8")
    _, entries = parse_playlist(original)
    ids = selected_ids(entries)

    if not ids:
        print("ERROR: No tvg-id values were found in DeshStream.m3u.")
        return 1

    print(f"DeshStream: {len(ids)} selected stream IDs")
    print("Downloading IPTV-org streams.json...")

    try:
        streams = download_json(STREAMS_URL)
    except RuntimeError as exc:
        # Never destroy a working playlist because the upstream API is unavailable.
        print(f"ERROR: {exc}")
        print("Leaving DeshStream.m3u unchanged.")
        return 1

    if not isinstance(streams, list):
        print("ERROR: IPTV-org response is not a JSON array. Playlist unchanged.")
        return 1

    print(f"IPTV-org returned {len(streams)} stream records.")
    updated, stats = build_playlist(original, streams)

    if updated == original:
        print("No playlist changes detected.")
        print(f"Found: {stats['found']}/{stats['selected']} IDs; URLs: {stats['urls']}; Missing: {stats['missing']}")
        return 0

    PLAYLIST.write_text(updated, encoding="utf-8")
    print("\nPlaylist updated successfully.")
    print(f"Found: {stats['found']}/{stats['selected']} IDs")
    print(f"URLs written: {stats['urls']}")
    print(f"Missing IDs preserved from old playlist: {stats['missing']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
