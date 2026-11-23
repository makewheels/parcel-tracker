#!/usr/bin/env python3
"""从本机管理快递订阅和飞书提醒。"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CONFIG = Path.home() / ".config" / "parcel-tracker" / "config.json"
STATUSES = ("active", "completed", "paused", "aborted", "pending", "failed")
KINDS = ("buy", "return", "send", "other")
KIND_LABELS = {"buy": "买的", "return": "退货", "send": "寄出", "other": "其他"}


def grouped(parcels):
    """按类型分组输出，买的在前、退货其次，其余随后。"""
    buckets = {}
    for parcel in parcels:
        buckets.setdefault(parcel.get("kind") or "other", []).append(parcel)
    for kind in KINDS + tuple(k for k in buckets if k not in KINDS):
        if buckets.get(kind):
            yield kind, buckets[kind]


def format_parcel(parcel):
    return " · ".join(filter(None, (parcel["number"], parcel["name"], parcel["carrier"],
                                     parcel["status_label"])))


def print_result(result):
    if "parcel" in result:
        print(format_parcel(result["parcel"]))
        for track in result["tracks"]:
            print(f"\n{track['time'] or '时间未知'}\n{track['detail']}")
        if not result["tracks"]:
            print("暂无物流轨迹")
    else:
        print(result["message"])
        parcels = result.get("parcels", ())
        kinds = {p.get("kind") or "other" for p in parcels}
        for kind, items in grouped(parcels):
            if len(kinds) > 1:
                print(f"〔{KIND_LABELS.get(kind, kind)}〕")
            for parcel in items:
                print(("  " if len(kinds) > 1 else "") + format_parcel(parcel))


def call(path, payload):
    """调云端服务，返回解析后的 dict（HTTP 4xx 也按 JSON 处理）。"""
    config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    url = os.getenv("PARCEL_SERVICE_URL", config.get("service_url", "")).rstrip("/")
    token = os.getenv("PARCEL_API_TOKEN", config.get("api_token", ""))
    if not url.startswith("https://") or not token:
        raise ValueError(f"请在 {CONFIG} 配置 service_url 和 api_token")
    request = Request(
        url + path,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=35 if path == "/subscribe" else 20) as response:
            return json.load(response)
    except HTTPError as error:
        return json.load(error)


def hours_since(text):
    """把 '2026-11-20 14:40:43' 转成 '18 小时前'；解析失败返回空串。"""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            delta = datetime.now() - datetime.strptime(text, fmt)
        except (ValueError, TypeError):
            continue
        if delta.days >= 1:
            return f"{delta.days} 天前"
        return f"{max(delta.seconds // 3600, 0)} 小时前"
    return ""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("subscribe", aliases=["add"], help="开始订阅快递")
    add.add_argument("number")
    add.add_argument("--name", default=None, help="商品名或备注")
    add.add_argument("--com", default="", help="快递100公司编码，省略则自动识别")
    add.add_argument("--phone", default="", help="承运商要求的预留电话或虚拟号后四位")
    add.add_argument("--kind", choices=KINDS, default="buy",
                     help="运单类型：buy 买的（默认）、return 退货、send 寄出、other 其他")
    add.add_argument("--force", action="store_true", help="已在跟踪也重新提交（例如补手机尾号）")
    note = commands.add_parser("note", help="修改已录入运单的备注")
    note.add_argument("number")
    note.add_argument("name", nargs="?", default="")
    note.add_argument("--kind", choices=KINDS, default="", help="同时修改运单类型")
    summary = commands.add_parser("summary", help="统计跟踪中、已签收等运单数量")
    listing = commands.add_parser("list", help="列出运单")
    listing.add_argument("--status", choices=STATUSES, default="", help="按状态筛选，默认全部")
    listing.add_argument("--kind", choices=KINDS, default="", help="按类型筛选，默认全部")
    show = commands.add_parser("show", help="查看运单和最近收到的完整轨迹")
    show.add_argument("number")
    unsubscribe = commands.add_parser("unsubscribe", help="暂停本站的飞书提醒")
    unsubscribe.add_argument("number")
    resume = commands.add_parser("resume", help="恢复飞书提醒")
    resume.add_argument("number")
    board = commands.add_parser("board", help="一览：所有在途件 + 最新一条进度")
    board.add_argument("--status", choices=STATUSES, default="active", help="默认只看在途")
    board.add_argument("--kind", choices=KINDS, default="", help="只看某一类")
    find = commands.add_parser("find", help="按单号或备注模糊查找")
    find.add_argument("keyword")
    for command in (add, note, summary, listing, show, unsubscribe, resume, board, find):
        command.add_argument("--json", action="store_true", help="输出 JSON，便于程序读取")
    args = parser.parse_args()

    try:
        payload = vars(args).copy()
        command = payload.pop("command")
        as_json = payload.pop("json")
        if command == "board":
            status = payload.get("status", "active")
            list_req = {"status": payload.get("status", "active")}
            if payload.get("kind"):
                list_req["kind"] = payload["kind"]
            listing_res = call("/list", list_req)
            rows = []
            for parcel in listing_res.get("parcels", ()):
                detail = call("/show", {"number": parcel["number"]})
                tracks = detail.get("tracks") or []
                newest = tracks[0] if tracks else {}
                rows.append({**parcel,
                             "last_time": newest.get("time", ""),
                             "last_detail": newest.get("detail", ""),
                             "last_ago": hours_since(newest.get("time", ""))})
            result = {"result": True, "message": f"在途 {len(rows)} 件", "parcels": rows}
        elif command == "find":
            keyword = payload["keyword"].lower()
            listing_res = call("/list", {})
            hits = [p for p in listing_res.get("parcels", ())
                    if keyword in (p["number"] + " " + (p.get("name") or "")).lower()]
            result = {"result": True, "message": f"匹配 {len(hits)} 件", "parcels": hits}
        else:
            path = "/subscribe" if command == "add" else f"/{command}"
            result = call(path, payload)

        if as_json:
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result.get("result") else 1
        if not result.get("result"):
            raise ValueError(result.get("message") or "云端操作失败")

        if command == "board":
            print(f"{result['message']}\n")
            for kind, items in grouped(result["parcels"]):
                print(f"〔{KIND_LABELS.get(kind, kind)}〕")
                for p in items:
                    print(f"  【{p.get('name') or '未备注'}】{p['number']}  {p.get('carrier') or '待识别'}  {p['status_label']}")
                    if p["last_time"]:
                        print(f"    {p['last_time']}（{p['last_ago']}） {p['last_detail']}")
                    else:
                        print("    暂无轨迹")
                print()
        else:
            print_result(result)
        return 0
    except (ValueError, OSError, URLError) as error:
        print(f"操作失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
