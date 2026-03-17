import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Optional


JP_DAY_MAP = {
    "月": "月曜日",
    "火": "火曜日",
    "水": "水曜日",
    "木": "木曜日",
    "金": "金曜日",
    "土": "土曜日",
    "日": "日曜日",
}

STATION_MAP = {
    "TOKYO MX": "mx",
    "MX": "mx",
    "BS11": "bs11",
    "テレビ東京": "tx",
    "テレ東": "tx",
    "日本テレビ": "ntv",
    "日テレ": "ntv",
    "MBS": "mbs",
    "TBS": "tbs",
    "フジテレビ": "fujitv",
    "ABEMA": "abema",
    "NHK": "nhk",
}


def clean_text(text: str) -> str:
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = re.sub(r"^```\w*\n|```$", "", text.strip(), flags=re.MULTILINE)
    return text.strip()


def parse_json_stream(text: str) -> List[dict]:
    decoder = json.JSONDecoder()
    pos = 0
    items: List[dict] = []

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
                for x in obj:
                    if isinstance(x, dict):
                        items.append(x)
            pos = start + end
        except json.JSONDecodeError:
            pos = start + 1
    return items


def normalize_station(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    s = raw.strip()
    if s == "":
        return None

    for k, v in STATION_MAP.items():
        if k in s:
            return v

    lowered = re.sub(r"[^a-zA-Z0-9]", "", s).lower()
    return lowered or s.lower()


def parse_broadcast_text(broadcast: str) -> Dict[str, Optional[str]]:
    day_of_week = None
    time = None
    station = None

    day_match = re.search(r"([月火水木金土日])曜", broadcast)
    if day_match:
        day_of_week = JP_DAY_MAP.get(day_match.group(1))

    time_match = re.search(r"(\d{1,2}:\d{2})", broadcast)
    if time_match:
        time = time_match.group(1)

    # 時刻以降を局名候補として扱う
    if time_match:
        tail = broadcast[time_match.end():].strip(" ～-・/ほか")
        station = normalize_station(tail)
    else:
        # 時刻がなければ全文から推定
        station = normalize_station(broadcast)

    return {
        "station": station,
        "day_of_week": day_of_week,
        "time": time,
    }


def ensure_title_item(store: Dict[str, dict], title: str) -> dict:
    if title not in store:
        store[title] = {
            "title": title,
            "official_url": None,
            "ep_num": None,
            "schedules": [],
        }
    return store[title]


def append_schedule(item: dict, schedule: Dict[str, Optional[str]]) -> None:
    if schedule["station"] is None and schedule["day_of_week"] is None and schedule["time"] is None:
        return

    # None項目は落とし、フォーマットをwatch_listに寄せる
    compact = {
        "station": schedule.get("station"),
        "day_of_week": schedule.get("day_of_week"),
        "time": schedule.get("time"),
    }
    if compact not in item["schedules"]:
        item["schedules"].append(compact)


def compact_schedules(schedules: List[dict]) -> List[dict]:
    # station/day_of_week が同一なら、time がある方を優先
    best: Dict[tuple, dict] = {}
    for sch in schedules:
        key = (sch.get("station"), sch.get("day_of_week"))
        if key not in best:
            best[key] = sch
            continue
        old = best[key]
        old_score = 1 if old.get("time") else 0
        new_score = 1 if sch.get("time") else 0
        if new_score >= old_score:
            best[key] = sch
    return list(best.values())


def build_watch_list(items: List[dict]) -> List[dict]:
    by_title: Dict[str, dict] = {}

    for top in items:
        # works/master/masters など
        if "works" in top and isinstance(top["works"], list):
            rows = top["works"]
        elif "master" in top and isinstance(top["master"], list):
            rows = top["master"]
        elif "masters" in top and isinstance(top["masters"], list):
            rows = top["masters"]
        else:
            rows = []

        for row in rows:
            if not isinstance(row, dict) or "title" not in row:
                continue
            item = ensure_title_item(by_title, row["title"])
            if item["official_url"] is None and row.get("official_url"):
                item["official_url"] = row.get("official_url")
            if item["ep_num"] is None and isinstance(row.get("ep_num"), int):
                item["ep_num"] = row.get("ep_num")

            # broadcast文字列からスケジュール抽出
            if isinstance(row.get("broadcast"), str):
                append_schedule(item, parse_broadcast_text(row["broadcast"]))

            # 明示フィールドの放送情報にも対応
            bday = row.get("broadcast_day")
            btime = row.get("broadcast_time")
            bstation = row.get("stations") or row.get("station")
            if bday or btime or bstation:
                d = None
                if isinstance(bday, str):
                    m = re.search(r"([月火水木金土日])", bday)
                    if m:
                        d = JP_DAY_MAP.get(m.group(1))
                append_schedule(item, {
                    "station": normalize_station(bstation) if isinstance(bstation, str) else None,
                    "day_of_week": d,
                    "time": btime if isinstance(btime, str) else None,
                })

        # broadcast/broadcasts 側で title を持つケース
        for bkey in ("broadcast", "broadcasts"):
            b_rows = top.get(bkey)
            if not isinstance(b_rows, list):
                continue
            for brow in b_rows:
                if not isinstance(brow, dict):
                    continue
                title = brow.get("title")
                if not isinstance(title, str):
                    continue
                item = ensure_title_item(by_title, title)
                day = brow.get("broadcast_day") or brow.get("day")
                day_norm = None
                if isinstance(day, str) and day:
                    day_norm = JP_DAY_MAP.get(day[0], day)
                append_schedule(item, {
                    "station": normalize_station(brow.get("station") or brow.get("channel")),
                    "day_of_week": day_norm,
                    "time": brow.get("time") if isinstance(brow.get("time"), str) else None,
                })

    result = list(by_title.values())
    for item in result:
        item["schedules"] = compact_schedules(item["schedules"])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="混在txtからwatch_list.json形式を生成")
    parser.add_argument("input", help="入力txt")
    parser.add_argument("-o", "--output", default="watch_list_generated.json", help="出力JSONパス")
    args = parser.parse_args()

    src = Path(args.input)
    if not src.exists():
        raise FileNotFoundError(f"input not found: {src}")

    cleaned = clean_text(src.read_text(encoding="utf-8"))
    items = parse_json_stream(cleaned)
    watch_list = build_watch_list(items)

    Path(args.output).write_text(
        json.dumps(watch_list, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"written: {args.output} ({len(watch_list)} titles)")


if __name__ == "__main__":
    main()