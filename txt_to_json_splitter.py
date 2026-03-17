import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Optional


WRAPPER_HINTS = {
    "works": None,
    "master": "master",
    "masters": "master",
    "episode": "episode",
    "episodes": "episode",
    "broadcast": "broadcast",
    "broadcasts": "broadcast",
}


def clean_text(text: str) -> str:
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = re.sub(r"^```\w*\n|```$", "", text.strip(), flags=re.MULTILINE)
    return text.strip()


def parse_json_stream(text: str) -> List[dict]:
    decoder = json.JSONDecoder()
    items = []
    pos = 0

    while pos < len(text):
        match = re.search(r"[\[{]", text[pos:])
        if not match:
            break

        start = pos + match.start()
        try:
            obj, end = decoder.raw_decode(text[start:])
            if isinstance(obj, dict):
                items.append(obj)
            elif isinstance(obj, list):
                items.extend(obj)
            pos = start + end
        except json.JSONDecodeError:
            pos = start + 1

    return items


def classify_item(item: dict) -> Optional[str]:
    keys = set(item.keys())

    if keys & {"ep_num", "episode_num", "episode_number", "synopsis"}:
        return "episode"

    if "station_id" in keys and "start_time" in keys:
        return "broadcast"
    if ("master_id" in keys or "anime_id" in keys) and ("day" in keys or "time" in keys):
        return "broadcast"

    is_broadcast_like = (
        ("broadcast_day" in keys or "day" in keys)
        and ("time" in keys or "broadcast_time" in keys)
        and ("station" in keys or "stations" in keys or "channel" in keys)
        and "official_url" not in keys
        and "start_date" not in keys
    )
    if is_broadcast_like:
        return "broadcast"

    if keys & {"official_url", "start_date", "studio", "source", "cast", "staff", "title"}:
        return "master"

    return None


def split_items(items: List[dict]) -> Dict[str, List[dict]]:
    out = {"master": [], "episode": [], "broadcast": []}

    for item in items:
        if not isinstance(item, dict):
            continue

        wrapper_keys = set(item.keys()) & set(WRAPPER_HINTS.keys())
        if wrapper_keys:
            for key in wrapper_keys:
                raw = item.get(key)
                if not isinstance(raw, list):
                    continue

                default_cat = WRAPPER_HINTS[key]
                for row in raw:
                    if not isinstance(row, dict):
                        continue
                    cat = default_cat if default_cat is not None else classify_item(row)
                    if cat is None:
                        cat = "master"
                    out[cat].append(row)
            continue

        cat = classify_item(item)
        if cat:
            out[cat].append(item)

    return out


def dedupe_master(rows: List[dict]) -> List[dict]:
    best = {}
    no_title = []

    for row in rows:
        title = row.get("title")
        if not title:
            no_title.append(row)
            continue
        if title not in best or len(row) > len(best[title]):
            best[title] = row

    return list(best.values()) + no_title


def dedupe_exact(rows: List[dict]) -> List[dict]:
    uniq = {}
    for row in rows:
        key = json.dumps(row, ensure_ascii=False, sort_keys=True)
        uniq[key] = row
    return list(uniq.values())


def write_outputs(classified: Dict[str, List[dict]], out_dir: Path, null_for_empty: bool) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    payload_map = {
        "master": {"master": dedupe_master(classified["master"])},
        "episode": {"episode": dedupe_exact(classified["episode"])},
        "broadcast": {"broadcast": dedupe_exact(classified["broadcast"])},
    }

    for name, payload in payload_map.items():
        key = next(iter(payload.keys()))
        data = payload[key]
        if null_for_empty and len(data) == 0:
            payload[key] = None

        out_path = out_dir / f"{name}.json"
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        count = 0 if payload[key] is None else len(payload[key])
        print(f"{name}.json: {count} records")


def main() -> None:
    parser = argparse.ArgumentParser(description="Grokのtxtから master/episode/broadcast JSON を生成")
    parser.add_argument("input", help="入力txtファイル")
    parser.add_argument("-o", "--out-dir", default=".", help="出力先ディレクトリ")
    parser.add_argument("--empty-as-null", action="store_true", help="空配列を null で出力")
    args = parser.parse_args()

    src = Path(args.input)
    if not src.exists():
        raise FileNotFoundError(f"input not found: {src}")

    text = src.read_text(encoding="utf-8")
    cleaned = clean_text(text)
    items = parse_json_stream(cleaned)
    classified = split_items(items)
    write_outputs(classified, Path(args.out_dir), null_for_empty=args.empty_as_null)


if __name__ == "__main__":
    main()