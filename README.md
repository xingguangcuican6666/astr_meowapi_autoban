# astrbot_plugin_meowapi_ban

AstrBot 插件：QQ 成员退群时自动禁用其在 [meow-api](https://github.com/xingguangcuican6666/meow-api)
中对应的账户，并提供余额 / 使用日志 / 用量查询命令。

账户匹配规则：`QQ号 + "@" + email_domain`（默认 `qq.com`）即为 meow-api 账户邮箱。

**平台要求：仅支持 aiocqhttp（OneBot v11，如 NapCat / Lagrange / LLOneBot）。**
QQ 官方 Bot 适配器既不下发退群通知，也无法 @ 群成员，两个功能都依赖这两项能力。

## 命令

命令前缀按 AstrBot 的 `wake_prefix` 配置，下面以默认的 `/` 为例。群聊里若开启了「需要 @ 机器人」，
则写成 `@机器人 /查余额`。

| 命令 | 参数 | 说明 |
| --- | --- | --- |
| `/查余额` | `[@某人\|<QQ号>]` | 余额、已用额度、请求次数、分组、状态、角色 |
| `/查使用日志` | `[@某人\|<QQ号>] [条数]` | 最近使用日志，默认 10 条，最多 20 条 |
| `/查用量` | `<时间跨度> [@某人\|<QQ号>]` | 指定时间段内的消费合计与按模型拆分 |
| `/meowapi_ban` | `@某人\|<QQ号>` | 手动禁用账户（仅 AstrBot 管理员） |
| `/meowapi_unban` | `@某人\|<QQ号>` | 手动解禁账户（仅 AstrBot 管理员） |

别名：`/查余额` = `/balance` = `/余额`；`/查使用日志` = `/logs` = `/使用日志` = `/查日志`；
`/查用量` = `/usage` = `/用量`。

**可选参数不填时始终指向发送者自己**，填 `@某人` 或纯 QQ 号则指向该成员。
默认情况下查询他人数据要求发送者是 AstrBot 管理员（`query_others_admin_only`），
避免群成员互相查看余额与使用记录。

时间跨度支持 `7d`、`24h`、`90m`、`2w`、`7天`、`24小时`、`90分钟`、`2周`、`1个月`，
以及 4 位以内的纯数字（按天算，如 `/查用量 7` = 7 天）；上限 31 天。

### 关于 `/签到`

meow-api 有签到接口（`GET|POST /api/user/checkin`），但它挂在 `selfRoute` + `UserAuth()` 下，
只作用于「令牌所属的那个用户」。本插件持有的是管理员系统访问令牌，调用它等于给管理员自己签到，
无法代某个 QQ 用户签到；meow-api 也没有管理端代签接口。因此在不改动 meow-api 的前提下，
`/签到` 未实现。若后续需要，可在 meow-api 侧补一个 `adminRoute.POST("/:id/checkin", ...)`，
插件这边按同样的目标解析方式接上即可。

## 配置

| 配置项 | 说明 |
| --- | --- |
| `meowapi_base_url` | meow-api 后端地址，如 `http://127.0.0.1:3000`（不带 `/api`，不以 `/` 结尾） |
| `meowapi_access_token` | 管理员/root 的系统访问令牌，在「个人设置 → 生成系统访问令牌」获取，角色需 ≥ 管理员 |
| `email_domain` | 账户邮箱域名，默认 `qq.com` |
| `handle_kick` | 被踢出群时是否也禁用，默认开启（机器人自身被踢始终忽略） |
| `enabled_groups` | 生效群号列表，留空 = 所有群 |
| `notify_group` | 是否在群内回报自动封禁结果，默认关闭 |
| `dry_run` | 演练模式：只做邮箱匹配与日志，不真正禁用。仅影响退群自动禁用，手动命令始终真实执行 |
| `query_others_admin_only` | 查询他人数据是否仅限 AstrBot 管理员，默认开启 |
| `verify_ssl` | 校验 HTTPS 证书；自签名证书可关闭 |
| `request_timeout` | 单次请求超时秒数，默认 10 |

## 实现要点

- **邮箱精确匹配**：`/api/user/search` 对 username/email/display_name 做 `LIKE '%keyword%'`，
  QQ 号互为后缀的账户（`123456@` 与 `7123456@`）都会命中，且默认每页仅 10 条按 id 倒序，
  目标可能被挤出首页而漏封。故按完整邮箱分页翻查（`page_size=100`）后做精确过滤。
  该接口走 `Unscoped()`，已软删除的账户也会返回，因此额外用 `DeletedAt` 字段排除。
- **额度格式化**：从 `/api/status` 读取站点的 `quota_per_unit` 与 `quota_display_type`，
  按 USD / CNY / CUSTOM / TOKENS 与站点展示口径一致地格式化金额，结果缓存 5 分钟。
- **用量口径**：合计消费取 `/api/log/stat`（直接 `SUM(type=消费)`，不受数据看板导出开关与
  5 分钟落库延迟影响）；按模型拆分明细取 `/api/data/`（`quota_data` 预聚合表）。
  任一可用即出结果，两者都拿不到才报错。
- **@ 成员解析**：命令行里的 `@某人` 是 At 消息段，不进文本参数，需单独从消息链取；
  同时 aiocqhttp 会把被 @ 的成员额外拼成 `@群名片(QQ号)` 写进 `message_str`，
  群名片含空格时还会被切成两段，所以参数用 `GreedyStr` 整体收下后自行分词并剔除这些占位符。
- **退群事件**：监听 `post_type=notice` + `notice_type=group_decrease`，`sub_type` 为
  `leave`（主动退群）或 `kick`（被踢，可配置）；`kick_me`（机器人自己被踢）始终忽略。
  目标取 `user_id`（退群者）而非 `operator_id`（操作者）。
