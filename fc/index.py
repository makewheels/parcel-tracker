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
    "youzhengguonei": "邮政快递包裹", "huitongkuaidi": "百世快递", "tiantian": "天天快递",
    "debangwuliu": "德邦物流", "debangkuaidi": "德邦快递", "annengwuliu": "安能物流",
    "kuayue": "跨越速运", "youshuwuliu": "优速快递", "fengwang": "丰网速运",
    "cainiao": "菜鸟速递", "danniao": "丹鸟", "yimidida": "壹米滴答",
}
STATES = {
    "active": "跟踪中", "completed": "已签收", "paused": "已暂停提醒",
    "aborted": "跟踪中止", "pending": "待确认", "failed": "订阅失败",
}
# 运单用途：买的排在前面，退货随后，其次是我寄出的、其他。
KINDS = {"buy": "买的", "return": "退货", "send": "寄出", "other": "其他"}
KIND_ORDER = ("buy", "return", "send", "other")


def respond(code, message, **data):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps({"result": code == 200, "returnCode": str(code), "message": message, **data},
                           ensure_ascii=False),
    }


def log_subscribe(store, number, outcome, company="", phone="", detail=""):
    """把每次订阅动作和快递100 的返回记到运单上（最多留 10 条，手机号只留后四位）。"""
    row = store.get(number) or {}
    try:
        entries = json.loads(row.get("subscribe_log") or "[]")
    except ValueError:
        entries = []
    entries.append({
        "at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        "outcome": outcome,
        "company": company or "自动识别",
        "phone": f"****{phone[-4:]}" if phone else "",
        "detail": (detail or "")[:200],
    })
    store.update(number, {"subscribe_log": json.dumps(entries[-10:], ensure_ascii=False)})


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
    kind = (data.get("kind") or "").strip().lower()
    if kind and kind not in KINDS:
        return respond(400, "运单类型只能是 buy、return、send、other")
    if not re.fullmatch(r"[a-z0-9]{0,40}", company) or len(name) > 120 or len(phone) > 40:
        return respond(400, "请检查快递公司编码、备注或电话号码")
    if not phone and company in {"shunfeng", "shunfengkuaiyun", "zhongtong"}:
        return respond(400, "顺丰、中通运单需要填写收件人手机号后四位（快递100 校验用）")
    if not (os.getenv("FEISHU_WEBHOOK") or os.getenv("FEISHU_CHAT_ID")):
        return respond(503, "飞书接收方尚未配置")

    row = store.get(number)
    state = parcel_state(row) if row else None
    was_active = bool(row and row.get("subscription") == "active")
    force = bool(data.get("force"))
    # 跟踪中止（abort）的运单允许重新订阅：快递100 要求 abort 后隔 30 分钟再提交。
    # force=true 时即使仍在跟踪也重新提交（例如补手机尾号）。
    if row and row["subscription"] == "active" and state != "aborted" and not force:
        updates = {}
        if data.get("name") is not None:
            updates["name"] = name
        if kind:
            updates["kind"] = kind
        if updates:
            store.update(number, updates)
            row.update(updates)
        label = "已在跟踪" if state == "active" else STATES[state]
        message = label + (f" · {row['name']}" if row.get("name") else "")
        log_subscribe(store, number, "跳过", row.get("company") or "", phone, f"{label}，未重新提交")
        if row.get("confirmation_retry_at", -1) >= 0:
            return send_subscription_confirmation(store, number, row, message)
        return respond(200, message)
    now = int(time.time())
    if row and row["subscription"] == "submitting" and now - row["attempted_at"] < 30:
        return respond(409, "正在确认订阅结果，请稍后再试")
    version = uuid.uuid4().hex
    values = {"subscription": "submitting", "attempted_at": now, "attempt": version,
              "company": company or (row or {}).get("company") or "",
              "kind": kind or (row or {}).get("kind") or "buy", "monitor_status": "",
              "confirmation_retry_at": 0,
              "name": name if data.get("name") is not None else (row or {}).get("name", "")}
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
        log_subscribe(store, number, "网络错误", company, phone, "快递100 未响应")
        return respond(502, "快递100未确认订阅，稍后可再试")

    accepted = result.get("result") is True and str(result.get("returnCode")) == "200"
    log_subscribe(store, number, "成功" if accepted else "被拒", company, phone,
                  f"returnCode={result.get('returnCode')} message={result.get('message') or ''}")
    condition = ("attempt", version)
    if accepted:
        if not store.update(number, {"subscription": "active", "confirmation_retry_at": 0}, condition):
            return respond(409, "运单状态已变化，请重查订阅")
        message = f"已开始跟踪 {number}" + (f" · {values['name']}" if values["name"] else "")
        return send_subscription_confirmation(store, number, store.get(number), message)
    current = store.get(number)
    if current and current["subscription"] == "active":
        return respond(200, f"已开始跟踪 {number}")
    # 快递100 对同一单号重复提交返回 501「重复订阅」：订阅本来就在，不算失败。
    if str(result.get("returnCode")) == "501" and (was_active or current and current.get("subscription") == "active"):
        store.update(number, {"subscription": "active", "confirmation_retry_at": -1}, condition)
        return respond(200, f"已在跟踪 {number}（快递100 提示重复订阅，原订阅仍有效）")
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
        notify_with_log(store, number, "subscribed", event_id, card, text)
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
    kind = row.get("kind") or "other"
    details = {"number": number, "name": row.get("name") or "", "company": company,
               "carrier": CARRIERS.get(company, company), "status": status, "status_label": STATES[status],
               "kind": kind, "kind_label": KINDS.get(kind, kind)}
    if row.get("abort_reason"):
        details["abort_reason"] = row["abort_reason"]
    return details


def scan_parcels(store, status=None, kind=None):
    if status not in (None, "", *STATES):
        return respond(400, "运单状态筛选值无效")
    valid_kinds = (None, "", *KINDS)
    if kind not in valid_kinds:
        return respond(400, "运单类型筛选值无效")
    counts = dict.fromkeys(STATES, 0)
    matches = []
    for number, row in store.scan():
        state = parcel_state(row)
        counts[state] += 1
        row_kind = row.get("kind") or "other"
        if kind:
            if row_kind != kind:
                continue
        # 空字符串表示列出所有运单，None 表示仅统计。
        if status == "" or state == status:
            matches.append((row.get("attempted_at", 0), parcel_details(number, row)))
    total = sum(counts.values())
    if status is None and not kind:
        message = f"共 {total} 件｜" + " · ".join(f"{label} {counts[state]}" for state, label in STATES.items())
        return respond(200, message, total=total, counts=counts)
    matches.sort(key=lambda match: (KIND_ORDER.index(match[1]["kind"]) if match[1]["kind"] in KIND_ORDER
                                    else len(KIND_ORDER), -int(match[0] or 0)))
    label = KINDS.get(kind) or STATES.get(status, "全部运单")
    return respond(200, f"{label} · {len(matches)} 件",
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


def merge_tracks(stored, incoming):
    """合并新旧轨迹：按（时间, 内容）去重，时间新的在前。

    快递100 有时推增量（append）、有时推全量（override），统一按合并处理可以保证
    订阅期内收到的所有节点都留在库里，不会被后续推送覆盖掉。
    """
    seen, merged = set(), []
    for when, detail in list(incoming) + list(stored):
        key = (when, detail)
        if key in seen or not (when or detail):
            continue
        seen.add(key)
        merged.append((when, detail))
    merged.sort(key=lambda item: item[0], reverse=True)
    return merged


def format_notice(note, carrier, number, status, reason, tracks, sent_at=None):
    label, color = {
        "subscribed": ("订阅成功", "green"),
        "shutdown": ("已签收", "green"),
        "abort": ("跟踪中止", "orange"),
    }.get(status, ("最新动态", "blue"))
    # 标题只放状态；正文是名字（突出显示）、快递公司（知道才显示）、单号；底部只留发送时间。
    title = label
    code = (carrier or "").strip().lower()
    carrier_name = CARRIERS.get(ALIASES.get(code, code), carrier or "")
    if sent_at is None:
        sent_at = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    footer = f"发送时间  {sent_at}"

    elements = []
    if note:
        elements.append({"tag": "markdown", "text_size": "heading",
                         "content": f"**{markdown_text(note)}**"})
    if carrier_name:
        elements.append({"tag": "markdown", "content": f"快递公司  {markdown_text(carrier_name)}"})
    elements.append({"tag": "markdown", "content": f"单号  `{number}`"})
    if status != "subscribed":
        block = []
        if reason:
            block.append(markdown_text(reason))
        if tracks:
            # 只发最近两条轨迹，两条之间空一行隔开
            for when, detail in tracks[:2]:
                block.append(f"**{markdown_text(when) or '时间未知'}**<br>{markdown_text(detail)}")
        else:
            block.append("暂无物流轨迹")
        elements.extend(({"tag": "hr"}, {"tag": "markdown", "content": "<br><br>".join(block)}))
    elements.extend(({"tag": "hr"}, {"tag": "markdown", "content": f"*{footer}*"}))
    card = {
        "schema": "2.0",
        "header": {
            "template": color,
            "title": {"tag": "plain_text", "content": title},
        },
        "body": {"elements": elements},
    }
    text_blocks = [" · ".join(filter(None, (label, note)))]
    if carrier_name:
        text_blocks.append(f"快递公司 {carrier_name}")
    text_blocks.append(f"单号 {number}")
    if reason:
        text_blocks.append(reason)
    text_blocks.extend(f"{when or '时间未知'}\n{detail}" for when, detail in tracks)
    if not tracks and status != "subscribed":
        text_blocks.append("暂无物流轨迹")
    text_blocks.append(footer)
    return card, "\n\n".join(text_blocks)


class FeishuError(RuntimeError):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def feishu_post(url, payload, headers=None, *, stage="send"):
    uncertain = "unknown" if stage == "send" else "failed"
    try:
        response = post_json(url, payload, headers)
    except HTTPError as error:
        error.close()
        rejected = 400 <= error.code < 500 and error.code != 408
        raise FeishuError("failed" if rejected else uncertain,
                          f"{stage}:http_{error.code}") from error
    except (OSError, ValueError) as error:
        cause = getattr(error, "reason", error)
        raise FeishuError(uncertain, f"{stage}:{type(cause).__name__}") from error
    code = response.get("code", response.get("StatusCode")) if isinstance(response, dict) else None
    if type(code) is not int:
        raise FeishuError(uncertain, f"{stage}:invalid_response")
    if code != 0:
        raise FeishuError("failed", f"{stage}:{code}")
    return response


def notify_with_log(store, number, notification_type, event_id, card, message):
    key = store.start_notification(number, notification_type, event_id)
    result = {"status": "unknown"}
    try:
        message_id = notify(card, message, event_id)
    except FeishuError as error:
        result.update(status=error.status, error_code=error.code)
        raise
    except Exception as error:
        result["error_code"] = type(error).__name__
        raise
    else:
        result["status"] = "sent"
        if message_id:
            result["message_id"] = message_id
    finally:
        try:
            store.finish_notification(key, result)
        except Exception as error:
            # 保留已写入的「结果未知」记录，避免因日志回写失败重发已成功的消息。
            print("飞书发送结果回写失败", key[-1][1], result["status"], type(error).__name__)


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
        feishu_post(webhook, data)
        return None
    auth = feishu_post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal", {
        "app_id": os.environ["FEISHU_APP_ID"], "app_secret": os.environ["FEISHU_APP_SECRET"],
    }, stage="auth")
    token = auth.get("tenant_access_token")
    if not token:
        raise FeishuError("failed", "auth:missing_token")
    data = {
        "receive_id": os.environ["FEISHU_CHAT_ID"], "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False, separators=(",", ":")),
        "uuid": event_id[:48],
    }
    if len(json.dumps(data, ensure_ascii=False).encode()) > 29_000:
        data.update(msg_type="text", content=json.dumps({"text": message}, ensure_ascii=False))
    response = feishu_post("https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id", data,
                           {"Authorization": f"Bearer {token}"})
    metadata = response.get("data")
    return metadata.get("message_id") if isinstance(metadata, dict) else None


def log_push(store, number, row, status, incoming=None, added=None, outcome="处理"):
    """记录每次回调推送（最多留 20 条），包含被忽略的那些。"""
    try:
        entries = json.loads(row.get("push_log") or "[]")
    except ValueError:
        entries = []
    entries.append({
        "at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        "status": status or "",
        "outcome": outcome,
        "nodes": len(incoming or []),
        "added": added,
    })
    return entries[-20:]


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
        store.update(number, {"push_log": json.dumps(
            log_push(store, number, row, status, outcome="忽略·已签收"), ensure_ascii=False)})
        return respond(200, "成功")
    if status == "abort" and data.get("comNew") and data["comNew"] != data.get("comOld"):
        store.update(number, {"push_log": json.dumps(
            log_push(store, number, row, status, outcome="忽略·承运商变更"), ensure_ascii=False)})
        return respond(200, "成功")

    incoming = [(item.get("ftime") or item.get("time") or "", item.get("context") or "")
                for item in result.get("data") or []]
    stored = json.loads(row.get("tracks") or "[]")
    # 增量推送只带新节点，全量推送带整条轨迹：合并去重，历史一条都不丢。
    tracks = merge_tracks(stored, incoming)
    carrier = result.get("com") or row.get("company") or ""
    encoded = json.dumps(tracks, ensure_ascii=False)
    event_id = hashlib.sha256(f"{number}\n{status}\n{encoded}".encode()).hexdigest()
    if (row.get("handled_event") or row.get("sent")) == event_id:
        store.update(number, {"push_log": json.dumps(
            log_push(store, number, row, status, incoming, 0, "忽略·重复"), ensure_ascii=False)})
        return respond(200, "成功")
    now = int(time.time())
    if row["delivery_until"] > now:
        store.update(number, {"push_log": json.dumps(
            log_push(store, number, row, status, incoming, 0, "忽略·处理中"), ensure_ascii=False)})
        return respond(503, "回调正在处理")
    lock = uuid.uuid4().hex
    reason = (data.get("message") or "") if status == "abort" else ""
    extra = {"abort_reason": reason[:300]} if reason else {}
    pushes = log_push(store, number, row, status, incoming, len(tracks) - len(stored))
    if not store.update(number, {"delivery_lock": lock, "delivery_until": now + 45,
                                 "tracks": encoded, "subscription": "active",
                                 "monitor_status": status, "company": carrier,
                                 "push_log": json.dumps(pushes, ensure_ascii=False), **extra},
                        ("delivery_lock", row["delivery_lock"])):
        return respond(503, "回调正在处理")

    note = row.get("name") or "快递提醒"
    try:
        # 停止提醒和回调共用同一运单锁。取得锁后读取最新的提醒设置。
        if store.get(number).get("notification_paused"):
            store.update(number, {"handled_event": event_id, "delivery_until": 0}, ("delivery_lock", lock))
            return respond(200, "成功")
        card, message = format_notice(note, carrier, number, status, reason, tracks)
        notify_with_log(store, number, status, event_id, card, message)
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
            return scan_parcels(store, data.get("status", ""), data.get("kind", ""))
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
            try:
                history = json.loads(row.get("subscribe_log") or "[]")
            except ValueError:
                history = []
            try:
                pushes = json.loads(row.get("push_log") or "[]")
            except ValueError:
                pushes = []
            return respond(200, "查询成功", parcel=parcel_details(number, row), tracks=tracks,
                           subscribe_log=history, push_log=pushes)
        if path in {"/unsubscribe", "/resume"}:
            return set_paused(store, number, path == "/unsubscribe")
        name = (data.get("name") or "").strip()
        kind = (data.get("kind") or "").strip().lower()
        if len(name) > 120:
            return respond(400, "备注最多 120 字")
        if kind and kind not in KINDS:
            return respond(400, "运单类型只能是 buy、return、send、other")
        updates = {}
        if name:
            updates["name"] = name
        if kind:
            updates["kind"] = kind
        if not updates:
            return respond(400, "请提供新备注或新的运单类型")
        if not store.update(number, updates):
            return respond(404, "运单未录入")
        changed = "备注" if name and not kind else ("类型" if kind and not name else "备注和类型")
        return respond(200, f"已更新{changed} · {number}")
    except (ValueError, TypeError, AttributeError, KeyError):
        return respond(400, "请求数据格式错误")
    except Exception as error:
        print("运单请求处理失败", type(error).__name__)
        return respond(502, "处理失败，请稍后重试")
