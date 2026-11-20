# 快递提醒

通过阿里云市场的快递100订阅物流。快递100主动回调阿里云函数计算，函数从 Tablestore 读取运单备注，把完整物流轨迹按最新在上发到飞书。快递100签收后结束跟踪。

## 录单

本地需要 Python 3.10 或更新版本。将云函数 HTTPS 地址和录单令牌写入 `~/.config/parcel-tracker/config.json`，文件权限设为 `0600`。

```json
{"service_url": "https://your-function.example.com", "api_token": "your-token"}
```

环境变量 `PARCEL_SERVICE_URL` 和 `PARCEL_API_TOKEN` 可以覆盖配置。

```sh
python3 track.py add EXAMPLE123456 --com yuantong --name '泳镜'
python3 track.py note EXAMPLE123456 '黑色泳镜'
```

`--name` 是可选备注。知道快递公司时用 `--com` 传快递100公司编码，省略则自动识别。顺丰和顺丰快运需要用 `--phone` 提供下单时预留的电话号码，电商虚拟号码填写后四位。重复录入已订阅的运单会保留或更新备注。

## 部署

1. 在阿里云市场开通 [快递100订阅推送](https://market.aliyun.com/detail/cmapi00072132)，准备该商品的 AppCode。
2. 在函数计算所在地域创建 Tablestore CU 模式容量型实例，建一张宽表 `parcels`。唯一主键为 STRING 类型的 `number`，最大版本数为 1，预留读写吞吐量为 0。
3. 创建一个可以由 `fc.aliyuncs.com` 扮演的 RAM 角色，只允许对 `parcels` 表执行 `ots:GetRow`、`ots:PutRow` 和 `ots:UpdateRow`。函数使用角色自动注入的短期凭据。
4. 准备下面的环境变量，通过密钥管理工具注入部署进程。运行 `uv run deploy.py --role <角色 ARN>` 编译依赖、部署函数和创建 POST 触发器。地域默认为 `cn-beijing`。部署身份需要函数及触发器的读取、创建、更新权限和使用该 RAM 角色的权限。
5. 把 HTTPS 触发器地址和录单令牌配置到客户端。正式使用时可为函数绑定已备案的 HTTPS 自定义域名，客户端使用这个域名提交订阅。

| 环境变量 | 用途 |
| --- | --- |
| `ALIBABA_CLOUD_ACCESS_KEY_ID`, `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | 部署身份凭据，由部署进程使用 |
| `ALIBABA_CLOUD_SECURITY_TOKEN` | 部署身份使用临时凭据时一并注入 |
| `KUAIDI100_APPCODE` | 快递100云市场订阅凭据 |
| `PARCEL_API_TOKEN` | 录单和修改备注的 Bearer token |
| `PARCEL_CALLBACK_SECRET` | 生成运单专用回调地址，已订阅运单仍会使用旧地址 |
| `TABLESTORE_ENDPOINT`, `TABLESTORE_INSTANCE` | Tablestore 访问地址和实例名 |
| `TABLESTORE_TABLE` | 表名，默认 `parcels` |
| `FEISHU_WEBHOOK`, `FEISHU_SECRET` | 群自定义机器人的 Webhook 及可选签名密钥 |
| `FEISHU_APP_ID`, `FEISHU_APP_SECRET`, `FEISHU_CHAT_ID` | 使用应用机器人发消息时的应用凭据和目标会话 |

飞书可以配置自定义机器人或应用机器人。应用机器人需具备发送消息权限，并能在目标会话发消息。函数先写入最新轨迹，再发一条完整消息。飞书确认成功后，函数记录已发送并确认回调；重复到达的同一版本通常会跳过。飞书应用机器人还使用消息 API 的幂等键处理重试。

## 许可

[MIT](LICENSE)
