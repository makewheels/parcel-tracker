"""快递100订阅与 HTTP 回调。"""
import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode
from urllib.request import Request, urlopen

from store import Store

KUAIDI100_URL = "https://kd100api.kuaidi100.com/express/poll"
ALIASES = {"yto": "yuantong", "zto": "zhongtong", "sto": "shentong", "sf": "shunfeng"}
CARRIERS = {
    "yuantong": "圆通速递", "zhongtong": "中通快递", "shentong": "申通快递",
    "shunfeng": "顺丰速运", "shunfengkuaiyun": "顺丰快运", "yunda": "韵达速递",
    "jtexpress": "极兔速递", "jd": "京东物流", "ems": "EMS",
}


def respond(code, message):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps({"result": code == 200, "returnCode": str(code), "message": message},
                           ensure_ascii=False),
    }


def post_json(url, payload, headers=None, timeout=6):
    request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
                      headers={"Content-Type": "application/json", **(headers or {})})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def callback_key(number):
    return hmac.new(os.environ["PARCEL_CALLBACK_SECRET"].encode(), number.encode(),
                    hashlib.sha256).hexdigest()


def subscribe(store, event, data, number):
    company = (data.get("com") or "").strip().lower()
    company = ALIASES.get(company, company)
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    if not re.fullmatch(r"[a-z0-9]{0,40}", company) or len(name) > 120 or len(phone) > 40:
        return respond(400, "请检查快递公司编码、备注或电话号码")
    if not phone and company in {"shunfeng", "shunfengkuaiyun"}:
        return respond(400, "顺丰运单需要填写下单时预留的电话号码")
    if not (os.getenv("FEISHU_WEBHOOK") or os.getenv("FEISHU_CHAT_ID")):
        return respond(503, "飞书接收方尚未配置")

    row = store.get(number)
    if row and row["subscription"] == "active":
        if data.get("name") is not None:
            store.update(number, {"name": name})
        label = {"shutdown": "运单已签收", "abort": "运单跟踪已中止"}.get(
            row.get("monitor_status"), "已在跟踪"
        )
        saved_name = name if data.get("name") is not None else row.get("name", "")
        return respond(200, label + (f" · {saved_name}" if saved_name else ""))
    now = int(time.time())
    if row and row["subscription"] == "submitting" and now - row["attempted_at"] < 30:
        return respond(409, "正在确认订阅结果，请稍后再试")
    version = uuid.uuid4().hex
    values = {"subscription": "submitting", "attempted_at": now, "attempt": version,
              "company": company, "name": name if data.get("name") is not None else (row or {}).get("name", "")}
    if row:
        claimed = store.update(number, values, ("attempt", row["attempt"]))
    else:
        claimed = store.create(number, {**values, "delivery_lock": "", "delivery_until": 0})
    if not claimed:
        return respond(409, "运单正在提交，请稍后再试")

    domain = event["requestContext"]["domainName"]
    callback = f"https://{domain}/callback?" + urlencode({"n": number, "t": callback_key(number)})
    parameters = {"callbackurl": callback, "resultv2": "4"}
    if not company:
        parameters["autoCom"] = "1"
    if phone:
        parameters["phone"] = phone
    request = {"company": company, "number": number, "parameters": parameters}
    try:
        result = post_json(KUAIDI100_URL, request, {
            "Authorization": f'APPCODE {os.environ["KUAIDI100_APPCODE"]}',
        }, timeout=7)
    except HTTPError as error:
        try:
            result = json.load(error)
        except ValueError:
            result = {"returnCode": error.code}
    except (OSError, ValueError):
        return respond(502, "快递100未确认订阅，稍后可再试")

    accepted = result.get("result") is True and str(result.get("returnCode")) == "200"
    condition = ("attempt", version)
    if accepted:
        store.update(number, {"subscription": "active"}, condition)
        return respond(200, f"已开始跟踪 {number}" + (f" · {values['name']}" if values["name"] else ""))
    current = store.get(number)
    if current and current["subscription"] == "active":
        return respond(200, f"已开始跟踪 {number}")
    store.update(number, {"subscription": "failed"}, condition)
    code = str(result.get("returnCode", "未知"))
    reasons = {"501": "该运单已被订阅，请等待回调确认", "600": "请检查 AppCode 或套餐额度",
               "601": "请检查试用是否过期", "700": "快递公司编码不受支持",
               "701": "请检查运单资料或回调地址", "702": "请提供快递公司或检查套餐额度"}
    return respond(422, f"快递100未确认订阅（{code}）{reasons.get(code, '')}")


def read_callback(body, content_type):
    # 不同网关可能透传 JSON，也可能用表单字段 param 包裹 JSON。
    if "application/x-www-form-urlencoded" in content_type:
        fields = dict((key, items[-1]) for key, items in parse_qs(body).items())
        if "param" in fields:
            return json.loads(fields["param"])
        if "lastResult" in fields:
            fields["lastResult"] = json.loads(fields["lastResult"])
        return fields
    return json.loads(body)


def notify(message, event_id):
    webhook = os.getenv("FEISHU_WEBHOOK")
    if webhook:
        data = {"msg_type": "text", "content": {"text": message}}
        secret = os.getenv("FEISHU_SECRET")
        if secret:
            timestamp = str(int(time.time()))
            key = f"{timestamp}\n{secret}".encode()
            data["timestamp"] = timestamp
            data["sign"] = base64.b64encode(hmac.digest(key, b"", "sha256")).decode()
        result = post_json(webhook, data)
        if result.get("code", result.get("StatusCode")) != 0:
            raise RuntimeError("Feishu webhook rejected message")
        return
    token = post_json("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal", {
        "app_id": os.environ["FEISHU_APP_ID"], "app_secret": os.environ["FEISHU_APP_SECRET"],
    })
    response = post_json("https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id", {
        "receive_id": os.environ["FEISHU_CHAT_ID"], "msg_type": "text",
        "content": json.dumps({"text": message}, ensure_ascii=False), "uuid": event_id[:48],
    }, {"Authorization": f"Bearer {token['tenant_access_token']}"})
    if response.get("code") != 0:
        raise RuntimeError("Feishu app rejected message")


def callback(store, data, number):
    result = data.get("lastResult") or {}
    if result.get("nu") and result["nu"].upper() != number:
        return respond(400, "回调运单不匹配")
    status = data.get("status")
    if status not in {"polling", "shutdown", "abort", "updateall"}:
        return respond(400, "未知回调状态")
    row = store.get(number)
    if not row:
        return respond(404, "运单未录入")
    if row.get("monitor_status") == "shutdown" and status != "shutdown":
        return respond(200, "成功")
    if status == "abort" and data.get("comNew") and data["comNew"] != data.get("comOld"):
        return respond(200, "成功")

    tracks = [(item.get("ftime") or item.get("time") or "", item.get("context") or "")
              for item in result.get("data") or []]
    if not tracks and status == "abort" and row.get("tracks"):
        tracks = json.loads(row["tracks"])
    carrier = result.get("com") or row.get("company") or ""
    encoded = json.dumps(tracks, ensure_ascii=False)
    event_id = hashlib.sha256(f"{number}\n{status}\n{encoded}".encode()).hexdigest()
    if row.get("sent") == event_id:
        return respond(200, "成功")
    now = int(time.time())
    if row["delivery_until"] > now:
        return respond(503, "回调正在处理")
    lock = uuid.uuid4().hex
    if not store.update(number, {"delivery_lock": lock, "delivery_until": now + 45,
                                 "tracks": encoded, "subscription": "active"},
                        ("delivery_lock", row["delivery_lock"])):
        return respond(503, "回调正在处理")

    note = row.get("name") or "快递提醒"
    label = {"shutdown": "已签收", "abort": "跟踪中止"}.get(status, "物流更新")
    lines = [f"📦 {note} · {label}", f"{CARRIERS.get(carrier, carrier)} {number}".strip()]
    if status == "abort" and data.get("message"):
        lines.append(data["message"])
    if tracks:
        lines.append(f"完整轨迹（{len(tracks)} 条，最新在上）")
        lines.extend(f"{when}  {detail}".strip() for when, detail in tracks)
    else:
        lines.append("暂无物流轨迹")
    try:
        notify("\n".join(lines), event_id)
    except Exception:
        store.update(number, {"delivery_until": 0}, ("delivery_lock", lock))
        raise
    store.update(number, {"monitor_status": status, "sent": event_id, "delivery_until": 0},
                 ("delivery_lock", lock))
    return respond(200, "成功")


def handler(event, context):
    try:
        event = json.loads(event)
        path = event.get("rawPath") or event.get("requestContext", {}).get("http", {}).get("path")
        if path not in {"/subscribe", "/note", "/callback"}:
            return respond(404, "未知路径")
        headers = {key.lower(): value for key, value in event.get("headers", {}).items()}
        body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode()
        if path == "/callback":
            query = event.get("queryParameters") or {}
            number = query.get("n", "")
            if not re.fullmatch(r"[A-Z0-9-]{6,32}", number) or not hmac.compare_digest(
                query.get("t", ""), callback_key(number)
            ):
                return respond(403, "回调认证失败")
            return callback(Store(), read_callback(body, headers.get("content-type", "")), number)
        if not hmac.compare_digest(headers.get("authorization", ""),
                                   "Bearer " + os.environ["PARCEL_API_TOKEN"]):
            return respond(403, "录单认证失败")
        data = json.loads(body)
        number = data["number"].strip().upper()
        if not re.fullmatch(r"[A-Z0-9-]{6,32}", number):
            return respond(400, "运单号格式错误")
        store = Store()
        if path == "/subscribe":
            return subscribe(store, event, data, number)
        name = data["name"].strip()
        if len(name) > 120:
            return respond(400, "备注最多 120 字")
        if not store.update(number, {"name": name}):
            return respond(404, "运单未录入")
        return respond(200, f"已更新备注 · {number}")
    except (ValueError, TypeError, AttributeError, KeyError):
        return respond(400, "请求数据格式错误")
    except Exception as error:
        print("运单请求处理失败", type(error).__name__)
        return respond(502, "处理失败，请稍后重试")
