"""将腾讯云函数 URL 事件转换为快递服务请求。"""
import json
import os
from urllib.parse import urlsplit

from index import handler as handle_parcel
from index import respond

REQUIRED = (
    "TABLESTORE_ENDPOINT", "TABLESTORE_INSTANCE", "TABLESTORE_ACCESS_KEY_ID",
    "TABLESTORE_ACCESS_KEY_SECRET", "KUAIDI100_APPCODE", "PARCEL_API_TOKEN",
    "PARCEL_CALLBACK_SECRET", "PARCEL_CALLBACK_BASE_URL",
)
FEISHU_APP = ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID")


def handler(event, context):
    if not isinstance(event, dict):
        return respond(400, "请求数据格式错误")
    if event.get("httpMethod") != "POST":
        return respond(405, "只支持 POST")
    if not all(os.getenv(key) for key in REQUIRED) or not (
        os.getenv("FEISHU_WEBHOOK") or all(os.getenv(key) for key in FEISHU_APP)
    ):
        return respond(503, "服务尚未配置")
    try:
        base = urlsplit(os.environ["PARCEL_CALLBACK_BASE_URL"])
    except ValueError:
        return respond(503, "服务尚未配置")
    if (base.scheme != "https" or not base.hostname or base.username is not None
            or base.path not in ("", "/") or base.query or base.fragment):
        return respond(503, "服务尚未配置")
    response = handle_parcel(json.dumps({
        "rawPath": event.get("path"),
        "headers": event.get("headers") or {},
        "queryParameters": event.get("queryString") or {},
        "body": event.get("body"),
        "isBase64Encoded": event.get("isBase64Encoded", False),
        "requestContext": {"domainName": base.netloc},
    }), context)
    return {**response, "isBase64Encoded": False}
