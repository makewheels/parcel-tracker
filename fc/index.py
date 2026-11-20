"""快递100订阅与 HTTP 回调。"""
import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from html import escape
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
STATES = {
    "active": "跟踪中", "completed": "已签收", "paused": "已暂停提醒",
    "aborted": "跟踪中止", "pending": "待确认", "failed": "订阅失败",
}


def respond(code, message, **data):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps({"result": code == 200, "returnCode": str(code), "message": message, **data},
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
            row["name"] = name
        state = parcel_state(row)
        label = "已在跟踪" if state == "active" else STATES[state]
        message = label + (f" · {row['name']}" if row.get("name") else "")
        if row.get("confirmation_retry_at", -1) >= 0:
            return send_subscription_confirmation(store, number, row, message)
        return respond(200, message)
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
        if not store.update(number, {"subscription": "active", "confirmation_retry_at": 0}, condition):
            return respond(409, "运单状态已变化，请重查订阅")
        message = f"已开始跟踪 {number}" + (f" · {values['name']}" if values["name"] else "")
        return send_subscription_confirmation(store, number, store.get(number), message)
    current = store.get(number)
    if current and current["subscription"] == "active":
        return respond(200, f"已开始跟踪 {number}")
    store.update(number, {"subscription": "failed"}, condition)
    code = str(result.get("returnCode", "未知"))
    reasons = {"501": "该运单已被订阅，请等待回调确认", "600": "请检查 AppCode 或套餐额度",
               "601": "请检查试用是否过期", "700": "快递公司编码不受支持",
               "701": "请检查运单资料或回调地址", "702": "请提供快递公司或检查套餐额度"}
    return respond(422, f"快递100未确认订阅（{code}）{reasons.get(code, '')}")


def send_subscription_confirmation(store, number, row, message):
    if row.get("notification_paused"):
        return respond(200, message + " · 飞书提醒已暂停", confirmation="paused")
    # 0 表示待发送，正值表示最早可重试时间，-1 表示已发送。
    retry_at = row["confirmation_retry_at"]
    if retry_at == -1:
        return respond(200, message, confirmation="sent")
    now = int(time.time())
    deadline = now + 35
    if retry_at > now or not store.update(
        number, {"confirmation_retry_at": deadline}, ("confirmation_retry_at", retry_at)
    ):
        return respond(200, message + " · 飞书确认处理中", confirmation="pending")

    card, text = format_notice(row.get("name") or "快递提醒", row.get("company") or "", number,
                               "subscribed", "快递100已确认订阅，有新轨迹时会发到这里。", [])
    event_id = hashlib.sha256(f"subscription\n{number}\n{row['attempt']}".encode()).hexdigest()
    condition = ("confirmation_retry_at", deadline)
    try:
        notify(card, text, event_id)
    except Exception as error:
        store.update(number, {"confirmation_retry_at": 0}, condition)
        print("订阅确认发送失败", type(error).__name__)
        return respond(200, message + " · 飞书确认发送失败，可重试同一单号补发", confirmation="pending")
    store.update(number, {"confirmation_retry_at": -1}, condition)
    return respond(200, message + " · 飞书已发送订阅确认", confirmation="sent")


def parcel_state(row):
    terminal = {"shutdown": "completed", "abort": "aborted"}.get(row.get("monitor_status"))
    if terminal:
        return terminal
    if row.get("notification_paused") and row.get("subscription") in {"active", "submitting"}:
        return "paused"
    return {"active": "active", "submitting": "pending"}.get(row.get("subscription"), "failed")


def parcel_details(number, row):
    status = parcel_state(row)
    company = row.get("company") or ""
    return {"number": number, "name": row.get("name") or "", "company": company,
            "carrier": CARRIERS.get(company, company), "status": status, "status_label": STATES[status]}


def scan_parcels(store, status=None):
    if status not in (None, "") and status not in STATES:
        return respond(400, "运单状态筛选值无效")
    counts = dict.fromkeys(STATES, 0)
    matches = []
    for number, row in store.scan():
        state = parcel_state(row)
        counts[state] += 1
        # 空字符串表示列出所有运单，None 表示仅统计。
        if status == "" or state == status:
            matches.append((row.get("attempted_at", 0), parcel_details(number, row)))
    total = sum(counts.values())
    if status is None:
        message = f"共 {total} 件｜" + " · ".join(f"{label} {counts[state]}" for state, label in STATES.items())
        return respond(200, message, total=total, counts=counts)
    matches.sort(key=lambda match: match[0], reverse=True)
    return respond(200, f"{STATES.get(status, '全部运单')} · {len(matches)} 件",
                   parcels=[details for _, details in matches])


def set_paused(store, number, paused):
    row = store.get(number)
    if not row:
        return respond(404, "运单未录入")
    status = parcel_state(row)
    if status in {"completed", "aborted", "failed"}:
        return respond(409, f"{STATES[status]}，当前没有可更改的提醒")
    if bool(row.get("notification_paused")) == paused:
        return respond(200, "提醒已暂停" if paused else "提醒已开启")
    if row["delivery_until"] > time.time() or not store.update(
        number, {"notification_paused": paused}, ("delivery_lock", row["delivery_lock"])
    ):
        return respond(409, "运单正在处理回调，请稍后再试")
    if paused:
        return respond(200, "已停止飞书提醒，快递100仍会跟踪，轨迹可继续查询")
    return respond(200, "已恢复飞书提醒，从下一次物流更新起发送")


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


def markdown_text(value):
    return re.sub(r"([\\`*_~\[\]()#|])", r"\\\1", escape(" ".join(value.split()), quote=False))


def format_notice(note, carrier, number, status, reason, tracks, sent_at=None):
    label, color = {
        "subscribed": ("订阅成功", "green"),
        "shutdown": ("已签收", "green"),
        "abort": ("跟踪中止", "orange"),
    }.get(status, ("最新动态", "blue"))
    title = f"📦 {note}"
    subtitle = " · ".join(filter(None, (CARRIERS.get(carrier, carrier), number)))
    if sent_at is None:
        sent_at = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    footer = f"推送时间  {sent_at}"

    elements = []
    latest = [f"**{label}**"]
    if reason:
        latest.append(markdown_text(reason))
    if tracks:
        when, detail = tracks[0]
        latest.extend((f"**{markdown_text(when) or '时间未知'}**", markdown_text(detail)))
    elif status != "subscribed":
        latest.append("暂无物流轨迹")
    elements.append({"tag": "markdown", "content": "<br>".join(latest)})
    if len(tracks) > 1:
        history = [f"**此前轨迹 · {len(tracks) - 1} 条**"]
        history.extend(
            f"**{markdown_text(when) or '时间未知'}**<br>{markdown_text(detail)}"
            for when, detail in tracks[1:]
        )
        elements.extend(({"tag": "hr"}, {"tag": "markdown", "content": "\n\n".join(history)}))
    elements.extend(({"tag": "hr"}, {"tag": "markdown", "content": f"*{footer}*"}))
    card = {
        "schema": "2.0",
        "header": {
            "template": color,
            "title": {"tag": "plain_text", "content": title},
            "subtitle": {"tag": "plain_text", "content": subtitle},
        },
        "body": {"elements": elements},
    }
    text_blocks = [f"{title} · {label}\n{subtitle}"]
    if reason:
        text_blocks.append(reason)
    text_blocks.extend(f"{when or '时间未知'}\n{detail}" for when, detail in tracks)
    if not tracks and status != "subscribed":
        text_blocks.append("暂无物流轨迹")
    text_blocks.append(footer)
    return card, "\n\n".join(text_blocks)


def notify(card, message, event_id):
    webhook = os.getenv("FEISHU_WEBHOOK")
    if webhook:
        data = {"msg_type": "interactive", "card": card}
        # 给自定义机器人的签名字段预留空间。
        if len(json.dumps(data, ensure_ascii=False).encode()) > 19_500:
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
    data = {
        "receive_id": os.environ["FEISHU_CHAT_ID"], "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False, separators=(",", ":")),
        "uuid": event_id[:48],
    }
    if len(json.dumps(data, ensure_ascii=False).encode()) > 29_000:
        data.update(msg_type="text", content=json.dumps({"text": message}, ensure_ascii=False))
    response = post_json("https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id", data,
                         {"Authorization": f"Bearer {token['tenant_access_token']}"})
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
    if (row.get("handled_event") or row.get("sent")) == event_id:
        return respond(200, "成功")
    now = int(time.time())
    if row["delivery_until"] > now:
        return respond(503, "回调正在处理")
    lock = uuid.uuid4().hex
    if not store.update(number, {"delivery_lock": lock, "delivery_until": now + 45,
                                 "tracks": encoded, "subscription": "active",
                                 "monitor_status": status, "company": carrier},
                        ("delivery_lock", row["delivery_lock"])):
        return respond(503, "回调正在处理")

    note = row.get("name") or "快递提醒"
    reason = (data.get("message") or "") if status == "abort" else ""
    try:
        # 停止提醒和回调共用同一运单锁。取得锁后读取最新的提醒设置。
        if store.get(number).get("notification_paused"):
            store.update(number, {"handled_event": event_id, "delivery_until": 0}, ("delivery_lock", lock))
            return respond(200, "成功")
        card, message = format_notice(note, carrier, number, status, reason, tracks)
        notify(card, message, event_id)
    except Exception:
        store.update(number, {"delivery_until": 0}, ("delivery_lock", lock))
        raise
    store.update(number, {"sent": event_id, "handled_event": event_id, "delivery_until": 0},
                 ("delivery_lock", lock))
    return respond(200, "成功")


def handler(event, context):
    try:
        event = json.loads(event)
        path = event.get("rawPath") or event.get("requestContext", {}).get("http", {}).get("path")
        if path not in {"/subscribe", "/note", "/summary", "/list", "/show",
                        "/unsubscribe", "/resume", "/callback"}:
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
        store = Store()
        if path == "/summary":
            return scan_parcels(store)
        if path == "/list":
            return scan_parcels(store, data.get("status", ""))
        number = data["number"].strip().upper()
        if not re.fullmatch(r"[A-Z0-9-]{6,32}", number):
            return respond(400, "运单号格式错误")
        if path == "/subscribe":
            return subscribe(store, event, data, number)
        if path == "/show":
            row = store.get(number)
            if not row:
                return respond(404, "运单未录入")
            tracks = [{"time": when, "detail": detail} for when, detail in json.loads(row.get("tracks") or "[]")]
            return respond(200, "查询成功", parcel=parcel_details(number, row), tracks=tracks)
        if path in {"/unsubscribe", "/resume"}:
            return set_paused(store, number, path == "/unsubscribe")
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
