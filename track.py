#!/usr/bin/env python3
"""从本机提交快递订阅并修改备注。"""
import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CONFIG = Path.home() / ".config" / "parcel-tracker" / "config.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="开始订阅快递")
    add.add_argument("number")
    add.add_argument("--name", default=None, help="商品名或备注")
    add.add_argument("--com", default="", help="快递100公司编码，省略则自动识别")
    add.add_argument("--phone", default="", help="承运商要求的预留电话或虚拟号后四位")
    note = commands.add_parser("note", help="修改已录入运单的备注")
    note.add_argument("number")
    note.add_argument("name")
    args = parser.parse_args()

    try:
        config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
        url = os.getenv("PARCEL_SERVICE_URL", config.get("service_url", "")).rstrip("/")
        token = os.getenv("PARCEL_API_TOKEN", config.get("api_token", ""))
        if not url.startswith("https://") or not token:
            raise ValueError(f"请在 {CONFIG} 配置 service_url 和 api_token")
        payload = vars(args).copy()
        path = "/subscribe" if payload.pop("command") == "add" else "/note"
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
        if not result.get("result"):
            raise ValueError(result.get("message") or "云端操作失败")
        print(result["message"])
        return 0
    except (ValueError, OSError, URLError) as error:
        print(f"操作失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
