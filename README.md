# 快递提醒

通过阿里云市场的快递100订阅物流。快递100主动回调阿里云函数计算，函数从 Tablestore 读取运单备注，把完整轨迹按最新在上发到飞书卡片。卡片底部显示推送时间。快递100签收后结束跟踪。

## CLI 使用教程

本地需要 Python 3.10 或更新版本。将云函数 HTTPS 地址和录单令牌写入 `~/.config/parcel-tracker/config.json`，文件权限设为 `0600`。

```json
{"service_url": "https://your-function.example.com", "api_token": "your-token"}
```

环境变量 `PARCEL_SERVICE_URL` 和 `PARCEL_API_TOKEN` 可以覆盖配置。

```sh
python3 track.py subscribe EXAMPLE123456 --com yuantong --name '泳镜'
python3 track.py summary --json
python3 track.py list --status active
python3 track.py list --status completed
python3 track.py show EXAMPLE123456
python3 track.py note EXAMPLE123456 '黑色泳镜'
python3 track.py unsubscribe EXAMPLE123456
python3 track.py resume EXAMPLE123456
```

- `subscribe`（也可用 `add`）提交快递100订阅。首次获确认时，云函数会立即发一张「订阅成功」飞书卡片。若飞书发送失败，重复提交同一单号可补发确认卡片，订阅仍使用原有记录。`--name` 是可选备注。知道快递公司时用 `--com` 传快递100公司编码，省略则自动识别。**顺丰、顺丰快运、中通**需用 `--phone` 提供面单上的电话号码后四位（开启电商号码保护的填虚拟号后四位）；没传时回退到 `config.json` 的 `default_phone_tail`，两者都没有才会报错。`--force` 可在已跟踪状态下强制重新提交（例如补手机尾号）。重复录入已订阅的运单会保留或更新备注；每次订阅动作与快递100 返回都会记到运单的订阅记录里，`show` 可见（手机号只留后四位）。
- `summary` 按跟踪中、已签收、已暂停提醒、跟踪中止、待确认和订阅失败统计，统计的是本服务保存的运单。
- `list` 默认列出所有运单，也可按 `active`、`completed`、`paused`、`aborted`、`pending`、`failed` 筛选。`show` 按单号查看状态、备注及最近收到的完整轨迹，最新节点在上。
- `unsubscribe` 停止本服务发飞书提醒。快递100仍会跟踪，轨迹和签收状态照常记录。`resume` 恢复未结束运单的提醒，从下一次物流更新起发送。
- 所有命令支持 `--json`，便于本机助手读取状态和数量。退出码 `0` 表示成功，`1` 表示请求失败。

给本机助手使用时，传入 `track.py` 的绝对路径。用户询问「有几个快递在路上」时用 `summary --json`，询问「哪些已签收」时用 `list --status completed --json`。订阅成功后再回复已开始跟踪。用户要求解除订阅时，说明操作停的是飞书提醒，快递100会继续更新轨迹。

CLI 入口是 [track.py](track.py)，服务端路由和快递100回调在 [fc/index.py](fc/index.py)，表读写在 [fc/store.py](fc/store.py)。改动后先测试 CLI 和回调，再用 [deploy.py](deploy.py) 部署。

## 部署

生产 CLI 和新订阅指向腾讯云北京区 SCF 的 `parcel-tracker`（Python 3.10，入口 `scf.handler`）。已有快递100订阅可能继续回调阿里云 FC。两套函数共用阿里云 Tablestore。定时发布入口是 [bin/auto-deploy.sh](bin/auto-deploy.sh)，按 GitHub `main` 更新两套代码，并分别记录已发布的提交。

SCF 的 `PARCEL_CALLBACK_BASE_URL` 设为它的 HTTPS 函数 URL。跨云访问表格存储使用 `TABLESTORE_ACCESS_KEY_ID` 和 `TABLESTORE_ACCESS_KEY_SECRET`，运行身份须具备下列两张表的权限。更新 SCF 代码时使用已授权的腾讯云 CLI 身份执行 `uv run deploy.py --target tencent`，函数的环境配置和 URL 随现有资源保留。

### 阿里云 FC

1. 在阿里云市场开通 [快递100订阅推送](https://market.aliyun.com/detail/cmapi00072132)，准备该商品的 AppCode。
2. 在函数计算所在地域创建 Tablestore CU 模式容量型实例。建宽表 `parcels`，主键是 `number`（STRING）。再建宽表 `notification_logs`，主键依次是 `number`（STRING）、`started_at`（INTEGER）和 `attempt_id`（STRING）。两张表都设置最大版本数为 1，预留读写吞吐量为 0，数据永不过期。
3. 创建一个可以由 `fc.aliyuncs.com` 扮演的 RAM 角色。授予 `parcels` 表的 `ots:GetRow`、`ots:GetRange`、`ots:PutRow` 和 `ots:UpdateRow`，以及 `notification_logs` 表的 `ots:PutRow` 和 `ots:UpdateRow`。函数使用角色自动注入的短期凭据。
4. 准备下面的环境变量，通过密钥管理工具注入部署进程。运行 `uv run deploy.py --role <角色 ARN>` 编译依赖、部署函数和创建 POST 触发器。地域默认为 `cn-beijing`。部署身份需要函数及触发器的读取、创建、更新权限和使用该 RAM 角色的权限。
5. 把 HTTPS 触发器地址和录单令牌配置到客户端。正式使用时可为函数绑定已备案的 HTTPS 自定义域名，客户端使用这个域名提交订阅。

| 环境变量 | 用途 |
| --- | --- |
| `ALIBABA_CLOUD_ACCESS_KEY_ID`, `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | 部署身份凭据，由部署进程使用 |
| `ALIBABA_CLOUD_SECURITY_TOKEN` | 部署身份使用临时凭据时一并注入 |
| `KUAIDI100_APPCODE` | 快递100云市场订阅凭据 |
| `PARCEL_API_TOKEN` | CLI 管理接口的 Bearer token |
| `PARCEL_CALLBACK_SECRET` | 生成运单专用回调地址，已订阅运单仍会使用旧地址 |
| `TABLESTORE_ENDPOINT`, `TABLESTORE_INSTANCE` | Tablestore 访问地址和实例名 |
| `TABLESTORE_TABLE` | 表名，默认 `parcels` |
| `FEISHU_WEBHOOK`, `FEISHU_SECRET` | 群自定义机器人的 Webhook 及可选签名密钥 |
| `FEISHU_APP_ID`, `FEISHU_APP_SECRET`, `FEISHU_CHAT_ID` | 使用应用机器人发消息时的应用凭据和目标会话 |

飞书可以配置自定义机器人或应用机器人。应用机器人需具备发送消息权限，并能在目标会话发消息。函数先写入最新轨迹，再发一条完整消息。飞书确认成功后，函数记录已发送并确认回调；重复到达的同一版本通常会跳过。飞书应用机器人还使用消息 API 的幂等键处理重试。

## 查询飞书发送记录

在 Tablestore 控制台打开 `notification_logs`，按主键 `number` 查询运单。每次发送尝试是一行，同一通知的各次尝试共用 `event_id`，各自拥有独立的 `attempt_id`。

| 字段 | 含义 |
| --- | --- |
| `notification_type` | `subscribed` 为订阅确认，`polling` 或 `updateall` 为物流更新，`shutdown` 为签收，`abort` 为跟踪中止 |
| `started_at`、`finished_at` | 尝试开始和完成的 Unix 时间戳，单位为毫秒 |
| `status` | `sent` 为飞书确认成功，`failed` 为发送失败，`unknown` 为结果未知 |
| `message_id` | 飞书应用机器人返回的消息 ID。自定义机器人成功时通常没有此字段 |
| `error_code` | 失败或结果未知时的错误码，例如 `send:http_429`、`send:TimeoutError` 或带 `auth:` 前缀的认证错误 |

函数先创建记录，再请求飞书。`unknown` 且没有 `finished_at` 时，发送可能仍在进行，也可能是处理被中断或结果回写失败。表内只保存发送元数据，逐条记录从启用后开始积累。

## 许可

[MIT](LICENSE)
