#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["alibabacloud-fc20230330", "oss2"]
# ///
"""将代码部署到函数计算 3.0，并输出 HTTP 触发器地址。"""
import argparse
import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

from alibabacloud_fc20230330 import models as fc
from alibabacloud_fc20230330.client import Client
from alibabacloud_tea_openapi.models import Config
from alibabacloud_tea_util.models import RuntimeOptions

SOURCE = Path(__file__).resolve().parent / "fc"
REQUIRED = (
    "KUAIDI100_APPCODE", "PARCEL_API_TOKEN", "PARCEL_CALLBACK_SECRET",
    "TABLESTORE_ENDPOINT", "TABLESTORE_INSTANCE",
)
OPTIONAL = (
    "TABLESTORE_TABLE", "FEISHU_WEBHOOK", "FEISHU_SECRET",
    "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID",
)


def package():
    root = Path(tempfile.gettempdir()).resolve()
    build = Path(tempfile.mkdtemp(prefix="parcel-deploy-", dir=root))
    try:
        dependencies = build / "dependencies"
        subprocess.run(
            ["uv", "pip", "install", "--quiet", "--link-mode", "hardlink",
             "--target", str(dependencies), "--python-version", "3.12",
             "--python-platform", "x86_64-manylinux_2_28",
             "-r", str(SOURCE / "requirements.txt")],
            check=True,
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(dependencies.rglob("*")):
                if file.is_file() and file.suffix != ".pyc":
                    archive.write(file, file.relative_to(dependencies))
            for file in SOURCE.glob("*.py"):
                archive.write(file, file.name)
        return buffer.getvalue()
    finally:
        if build.resolve().parent == root and build.name.startswith("parcel-deploy-") and not build.is_symlink():
            shutil.rmtree(build)


def code_location(zip_bytes, bucket=None, endpoint=None, object_prefix="parcel-tracker"):
    """小包直接内联；跨网慢（GitHub 跑者写超时）时改为先传 OSS，再让 FC 从 OSS 拉。"""
    if not bucket:
        return fc.InputCodeLocation(zip_file=base64.b64encode(zip_bytes).decode())
    import hashlib

    import oss2

    endpoint = endpoint or os.getenv("OSS_ENDPOINT") or "https://oss-cn-beijing.aliyuncs.com"
    auth = oss2.Auth(os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"], os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"])
    client = oss2.Bucket(auth, endpoint, bucket)
    object_name = f"{object_prefix}/{hashlib.sha256(zip_bytes).hexdigest()[:16]}.zip"
    client.put_object(object_name, zip_bytes)
    print(f"代码包已上传 OSS：oss://{bucket}/{object_name}", flush=True)
    return fc.InputCodeLocation(oss_bucket_name=bucket, oss_object_name=object_name)


def call_with_retry(func, attempts=4, delay=3):
    """FC 接口偶发写超时/连接中断，重试几次；4xx 这类配置错误不重试。"""
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as error:  # noqa: BLE001
            status = getattr(error, "status_code", None)
            if status is not None and 400 <= int(status) < 500:
                raise
            if attempt == attempts:
                raise
            print(f"第 {attempt} 次调用失败（{type(error).__name__}），{delay * attempt}s 后重试", flush=True)
            time.sleep(delay * attempt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="cn-beijing")
    parser.add_argument("--name", default="parcel-tracker")
    parser.add_argument("--role", required=True, help="函数的 RAM 角色 ARN")
    parser.add_argument("--oss-bucket", default=os.getenv("OSS_BUCKET", ""),
                        help="代码包先传到此 OSS 桶（跨网部署时避免写超时）")
    parser.add_argument("--oss-endpoint", default=os.getenv("OSS_ENDPOINT", ""),
                        help="OSS endpoint，默认 https://oss-cn-beijing.aliyuncs.com")
    args = parser.parse_args()
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        raise SystemExit("缺少变量 " + ", ".join(missing))
    environment = {name: os.environ[name] for name in REQUIRED + OPTIONAL if os.environ.get(name)}
    if not environment.get("FEISHU_WEBHOOK") and any(
        environment.get(name) for name in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID")
    ) and not all(environment.get(name) for name in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID")):
        raise SystemExit("飞书应用配置需要 App ID、App Secret 和群聊 ID")
    config = Config(
        access_key_id=os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"],
        access_key_secret=os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"],
        security_token=os.getenv("ALIBABA_CLOUD_SECURITY_TOKEN"),
        endpoint=f"fcv3.{args.region}.aliyuncs.com",
        region_id=args.region,
    )
    client = Client(config)
    code = code_location(package(), args.oss_bucket, args.oss_endpoint)
    runtime = RuntimeOptions(connect_timeout=30000, read_timeout=180000)
    try:
        client.get_function(args.name, fc.GetFunctionRequest())
    except Exception as error:
        if getattr(error, "status_code", None) != 404 and getattr(error, "code", None) != "FunctionNotFound":
            raise
        request = fc.CreateFunctionRequest(body=fc.CreateFunctionInput(
            function_name=args.name, handler="index.handler", runtime="python3.12", code=code,
            role=args.role, cpu=0.1, memory_size=256, disk_size=512, timeout=30,
            instance_concurrency=1, internet_access=True, environment_variables=environment,
        ))
        call_with_retry(lambda: client.create_function_with_options(request, {}, runtime))
    else:
        request = fc.UpdateFunctionRequest(body=fc.UpdateFunctionInput(
            code=code, role=args.role, environment_variables=environment,
        ))
        call_with_retry(lambda: client.update_function_with_options(args.name, request, {}, runtime))
    trigger = "http"
    try:
        result = client.get_trigger(args.name, trigger)
    except Exception as error:
        if getattr(error, "status_code", None) != 404 and getattr(error, "code", None) != "TriggerNotFound":
            raise
        request = fc.CreateTriggerRequest(body=fc.CreateTriggerInput(
            trigger_name=trigger, trigger_type="http",
            trigger_config=json.dumps({"authType": "anonymous", "methods": ["POST"]}),
        ))
        result = client.create_trigger(args.name, request)
    trigger_data = result.body.to_map()
    print(trigger_data["httpTrigger"]["urlInternet"])


if __name__ == "__main__":
    main()
