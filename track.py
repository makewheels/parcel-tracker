#!/usr/bin/env python3
"""从本机管理快递订阅和飞书提醒。"""
import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CONFIG = Path.home() / ".config" / "parcel-tracker" / "config.json"
STATUSES = ("active", "completed", "paused", "aborted", "pending", "failed")


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
        for parcel in result.get("parcels", ()):
            print(format_parcel(parcel))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("subscribe", aliases=["add"], help="开始订阅快递")
    add.add_argument("number")
    add.add_argument("--name", default=None, help="商品名或备注")
    add.add_argument("--com", default="", help="快递100公司编码，省略则自动识别")
    add.add_argument("--phone", default="", help="承运商要求的预留电话或虚拟号后四位")
    note = commands.add_parser("note", help="修改已录入运单的备注")
    note.add_argument("number")
    note.add_argument("name")
    summary = commands.add_parser("summary", help="统计跟踪中、已签收等运单数量")
    listing = commands.add_parser("list", help="列出运单")
    listing.add_argument("--status", choices=STATUSES, default="", help="按状态筛选，默认全部")
    show = commands.add_parser("show", help="查看运单和最近收到的完整轨迹")
    show.add_argument("number")
    unsubscribe = commands.add_parser("unsubscribe", help="暂停本站的飞书提醒")
    unsubscribe.add_argument("number")
    resume = commands.add_parser("resume", help="恢复飞书提醒")
    resume.add_argument("number")
    for command in (add, note, summary, listing, show, unsubscribe, resume):
        command.add_argument("--json", action="store_true", help="输出 JSON，便于程序读取")
    args = parser.parse_args()

    try:
        config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
        url = os.getenv("PARCEL_SERVICE_URL", config.get("service_url", "")).rstrip("/")
        token = os.getenv("PARCEL_API_TOKEN", config.get("api_token", ""))
        if not url.startswith("https://") or not token:
            raise ValueError(f"请在 {CONFIG} 配置 service_url 和 api_token")
        payload = vars(args).copy()
        command = payload.pop("command")
        as_json = payload.pop("json")
        path = "/subscribe" if command == "add" else f"/{command}"
        request = Request(
            url + path,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=20) as response:
                result = json.load(response)
        except HTTPError as error:
            result = json.load(error)
        if as_json:
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result.get("result") else 1
        if not result.get("result"):
            raise ValueError(result.get("message") or "云端操作失败")
        print_result(result)
        return 0
    except (ValueError, OSError, URLError) as error:
        print(f"操作失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
