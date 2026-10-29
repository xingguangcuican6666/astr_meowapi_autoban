"""AstrBot 插件：QQ 成员退群时自动禁用其在 meow-api 中对应的账户，并提供账户查询命令。

账户匹配规则：QQ 号 + "@" + 配置的邮箱域名（默认 qq.com）即为账户邮箱。
仅支持 aiocqhttp（OneBot v11，如 NapCat）平台适配器：QQ 官方 Bot 适配器既不下发退群通知，
也无法 @ 成员。
"""

import json
import re
import time
from datetime import datetime
from typing import Optional

from aiohttp import ClientSession, ClientTimeout

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

# GreedyStr 未在 astrbot.api 暴露，AstrBot 内置插件同样从 core 导入。
# 用它让最后一个参数吃掉全部剩余文本，避免 @ 群名片含空格时把参数挤掉；
# 参数注解必须是框架内那个 GreedyStr 类本身（框架用 `is` 判断），故不可自建替身。
try:
    from astrbot.core.star.filter.command import GreedyStr
except ImportError as e:  # pragma: no cover - 仅在 AstrBot 版本过旧时触发
    raise ImportError(
        "本插件需要支持 GreedyStr 参数的 AstrBot（4.x）。"
        "未能从 astrbot.core.star.filter.command 导入 GreedyStr，请升级 AstrBot。"
    ) from e

# meow-api 用户状态常量（见 meow-api common/constants.go：1=启用，2=禁用）
_STATUS_ENABLED = 1
_STATUS_DISABLED = 2
_STATUS_NAMES = {1: "启用", 2: "禁用"}
# meow-api 用户角色常量（common/constants.go：0=访客，1=普通用户，10=管理员，100=站长）
_ROLE_NAMES = {0: "访客", 1: "普通用户", 10: "管理员", 100: "站长"}

# meow-api 日志类型常量（model/log.go：1=充值 2=消费 3=管理 4=系统 5=错误 6=退款 7=登录）
_LOG_TYPE_NAMES = {
    1: "充值",
    2: "消费",
    3: "管理",
    4: "系统",
    5: "错误",
    6: "退款",
    7: "登录",
}
_LOG_TYPE_CONSUME = 2

# 时间跨度单位表：支持中文与英文缩写；数字与单位之间可有空格。
_SPAN_UNITS = {
    "个月": 2592000,
    "个星期": 604800,
    "分钟": 60,
    "小时": 3600,
    "星期": 604800,
    "mo": 2592000,
    "min": 60,
    "月": 2592000,
    "周": 604800,
    "天": 86400,
    "日": 86400,
    "时": 3600,
    "分": 60,
    "w": 604800,
    "d": 86400,
    "h": 3600,
    "m": 60,
}
_SPAN_RE = re.compile(r"^(\d+)\s*([A-Za-z一-鿿]+)$")
# 跨度上限与 meow-api 自身 /api/data/self 的"不能超过 1 个月"保持一致：
# /api/data/ 按 小时 × 模型 × 令牌 × 渠道 聚合返回，跨度太大响应体会过大。
_SPAN_MAX_SECONDS = 31 * 86400
# aiocqhttp 适配器会把 @ 成员额外写进 message_str（形如 " @群名片(QQ号)"），
# 它不是用户手动输入的参数，解析查询参数时需要剔除。群名片本身可能含空格，
# 按空格切分后会变成 "@群名片" 与 "名片(QQ号)" 两段，故两种形态都要剔除。
_AT_HEAD_RE = re.compile(r"^@")
_AT_TAIL_RE = re.compile(r"\(\d{5,}\)$")
# QQ 号：5~12 位纯数字。用来把 "7d" 这类跨度参数与纯数字 QQ 号区分开。
_QQ_RE = re.compile(r"^\d{5,12}$")


def _looks_like_qq(text: str) -> bool:
    return bool(_QQ_RE.match((text or "").strip()))


def _strip_bearer(token: str) -> str:
    """去掉可能存在的 "Bearer " 前缀；meow-api 两种写法都接受，统一发裸令牌。"""
    if token.lower().startswith("bearer "):
        return token[7:].strip()
    return token


def _parse_span(text: str) -> int:
    """把 "7d" / "24小时" / "1个月" 这类跨度写法换算成秒。

    4 位以内的纯数字按“天”处理（5 位以上视为 QQ 号，由调用方当作目标而非跨度）。
    抛出 ValueError 时由调用方转成对用户友好的用法提示。
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("缺少时间跨度")
    if text.isdigit():
        if len(text) > 4:
            raise ValueError(f"无法识别的时间跨度：{text}")
        text = f"{text}d"
    lowered = text.lower().replace(" ", "")
    match = _SPAN_RE.match(lowered)
    if not match:
        raise ValueError(f"无法识别的时间跨度：{text}")
    amount = int(match.group(1))
    unit = match.group(2)
    if unit not in _SPAN_UNITS:
        raise ValueError(f"不支持的时间单位：{unit}（支持 分钟/小时/天/日/周/星期/月/个月 及 m/h/d/w/mo）")
    seconds = amount * _SPAN_UNITS[unit]
    if seconds <= 0:
        raise ValueError("时间跨度必须大于 0")
    if seconds > _SPAN_MAX_SECONDS:
        raise ValueError(f"时间跨度最长 31 天，当前为 {amount}{unit}")
    return seconds


def _humanize_span(seconds: int) -> str:
    """把秒数换回便于展示的中文写法。"""
    for unit, size in (("天", 86400), ("小时", 3600), ("分钟", 60)):
        if seconds % size == 0:
            return f"{seconds // size} {unit}"
    return f"{seconds} 秒"


def _format_tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.2f}M"
    if count >= 1000:
        return f"{count / 1000:.1f}k"
    return str(count)


def _format_time(timestamp: int) -> str:
    if not timestamp:
        return "-"
    return datetime.fromtimestamp(timestamp).strftime("%m-%d %H:%M")


@register(
    "astrbot_plugin_meowapi_ban",
    "xingguangcuican",
    "QQ 退群时自动禁用其对应的 meow-api 账户（邮箱=QQ号@域名），并提供余额/日志/用量查询",
    "0.2.0",
    "https://github.com/xingguangcuican6666/astr_meowapi_autoban",
)
class MeowApiBanPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.base_url = str(config.get("meowapi_base_url", "")).strip().rstrip("/")
        self.access_token = _strip_bearer(str(config.get("meowapi_access_token", "")).strip())
        _domain = str(config.get("email_domain", "qq.com")).strip().lstrip("@")
        self.email_domain = _domain or "qq.com"
        self.handle_kick = bool(config.get("handle_kick", True))
        self.enabled_groups = [
            str(g).strip() for g in (config.get("enabled_groups", []) or []) if str(g).strip()
        ]
        self.notify_group = bool(config.get("notify_group", False))
        self.dry_run = bool(config.get("dry_run", False))
        # 默认开启：查询他人余额/日志/用量仅限 AstrBot 管理员，普通成员只能查自己。
        self.query_others_admin_only = bool(config.get("query_others_admin_only", True))
        self.verify_ssl = bool(config.get("verify_ssl", True))
        try:
            self.request_timeout = int(config.get("request_timeout", 10))
        except (TypeError, ValueError):
            self.request_timeout = 10
        if self.request_timeout <= 0:
            self.request_timeout = 10
        self._http: Optional[ClientSession] = None
        # /api/status 里的额度展示配置（币种、汇率、每单位额度），查询命令格式化金额时用。
        self._quota_snapshot: Optional[dict] = None
        self._quota_snapshot_at = 0.0

    async def initialize(self):
        # ClientSession 需在事件循环内创建，故放在异步的 initialize 中
        self._http = ClientSession(timeout=ClientTimeout(total=self.request_timeout))
        if not self.base_url or not self.access_token:
            logger.warning(
                "[meowapi-ban] 未配置 meowapi_base_url / meowapi_access_token，"
                "退群事件与查询命令都将无法工作。"
            )
        else:
            logger.info(
                f"[meowapi-ban] 已就绪 base_url={self.base_url} dry_run={self.dry_run} "
                f"handle_kick={self.handle_kick} 生效群={self.enabled_groups or '全部'}"
            )

    async def terminate(self):
        if self._http is not None:
            await self._http.close()
            self._http = None

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_group_decrease(self, event: AstrMessageEvent):
        """监听 aiocqhttp 的所有事件，仅处理 group_decrease（退群）通知。

        注意：该装饰器会对每条 aiocqhttp 事件触发，但只会把事件标记为 is_wake，
        不会设置 is_at_or_wake_command，因此不会导致机器人对普通消息自动回复
        （已核对 AstrBot core 的 WakingCheckStage / ProcessStage 逻辑）。
        """
        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            return
        if raw.get("post_type") != "notice" or raw.get("notice_type") != "group_decrease":
            return

        sub_type = raw.get("sub_type")
        if sub_type == "kick_me":
            # 机器人自身被移出群，忽略——绝不能去禁用机器人自己的 QQ
            return
        if sub_type == "kick" and not self.handle_kick:
            return

        group_id = str(raw.get("group_id") or "")
        qq = str(raw.get("user_id") or "").strip()  # 退群者的 QQ 号（不是 operator_id）
        if not qq:
            return
        if self.enabled_groups and group_id not in self.enabled_groups:
            return
        if not self.base_url or not self.access_token:
            logger.warning(f"[meowapi-ban] 收到退群事件(qq={qq})但未配置 meow-api，已跳过。")
            return

        email = f"{qq}@{self.email_domain}"
        reason = {"leave": "主动退群", "kick": "被踢出群"}.get(sub_type, f"退群({sub_type})")
        logger.info(f"[meowapi-ban] 检测到退群 group={group_id} qq={qq} email={email} 类型={reason}")

        try:
            ok, summary = await self._manage_by_email(email, "disable")
        except Exception as e:  # 兜底：任何异常都不应影响 AstrBot 其它流程
            logger.error(f"[meowapi-ban] 处理退群事件异常 qq={qq}: {e}", exc_info=True)
            ok, summary = False, f"内部异常：{e}"

        (logger.info if ok else logger.warning)(
            f"[meowapi-ban] 处理完成 email={email} 成功={ok} 详情={summary}"
        )

        if self.notify_group:
            head = "[meow-api 自动封禁]" + ("（演练）" if self.dry_run else "")
            yield event.plain_result(f"{head} {qq} {reason}\n{summary}")

    @filter.command("meowapi_ban")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_ban(self, event: AstrMessageEvent, rest: GreedyStr):
        """手动禁用某 QQ 对应的 meow-api 账户：/meowapi_ban @某人 或 /meowapi_ban <QQ号>（仅机器人管理员）。"""
        async for r in self._cmd_manage(event, rest, "disable"):
            yield r

    @filter.command("meowapi_unban")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_unban(self, event: AstrMessageEvent, rest: GreedyStr):
        """手动解禁某 QQ 对应的 meow-api 账户：/meowapi_unban @某人 或 /meowapi_unban <QQ号>（仅机器人管理员）。"""
        async for r in self._cmd_manage(event, rest, "enable"):
            yield r

    # ---------------- 查询命令 ----------------
    # 三个查询命令共用一套目标解析：不带参数时指向发送者自己，
    # 带 @某人 或 QQ 号时指向该成员（与 /meowapi_ban 的解析方式一致）。
    # 参数一律用 GreedyStr 收下再自行分词，原因有二：
    # 1) aiocqhttp 会把 @ 成员额外拼成 " @群名片(QQ号)" 写进 message_str，群名片含空格时
    #    会被切成两段，固定位置的参数会被这些占位符挤掉；
    # 2) GreedyStr 参数不能写默认值——CommandFilter 只在"参数值 is GreedyStr"时才把剩余
    #    文本整体拼进来；给了默认值 handler_params 记的就是默认值，只剩第一个词。
    #    空参数时 GreedyStr 会收到 ""，不会报"必要参数缺失"。

    @filter.command("查余额", alias={"balance", "余额"})
    async def cmd_balance(self, event: AstrMessageEvent, rest: GreedyStr):
        """查询 meow-api 账户余额：/查余额 或 /查余额 @某人 或 /查余额 <QQ号>。"""
        async for r in self._cmd_balance(event, rest):
            yield r

    @filter.command("查使用日志", alias={"logs", "使用日志", "查日志"})
    async def cmd_logs(self, event: AstrMessageEvent, rest: GreedyStr):
        """查询 meow-api 使用日志：/查使用日志 [@某人|<QQ号>] [条数]，默认 10 条。"""
        async for r in self._cmd_logs(event, rest):
            yield r

    @filter.command("查用量", alias={"usage", "用量"})
    async def cmd_usage(self, event: AstrMessageEvent, rest: GreedyStr):
        """查询一段时间内的 meow-api 用量：/查用量 <时间跨度> [@某人|<QQ号>]，如 /查用量 7d。"""
        async for r in self._cmd_usage(event, rest):
            yield r

    def _at_qq(self, event: AstrMessageEvent) -> str:
        """取消息里被 @ 的成员 QQ（排除 @全体 与 @机器人自己）。"""
        self_id = str(event.get_self_id())
        for seg in event.get_messages():
            if isinstance(seg, Comp.At) and not isinstance(seg, Comp.AtAll):
                at_qq = str(getattr(seg, "qq", "")).strip()
                if at_qq and at_qq.lower() != "all" and at_qq != self_id:
                    return at_qq
        return ""

    @staticmethod
    def _split_args(rest: str) -> list[str]:
        """把 GreedyStr 收下的剩余文本切成参数，并剔除适配器塞进来的 @ 占位符。

        aiocqhttp 会把被 @ 的成员额外拼成 " @群名片(QQ号)" 写进 message_str；群名片含空格时，
        按空格切分后会变成 "@张" 与 "三(123456789)" 两段，两种形态都要丢掉。
        而用户手打的 "@123456789"（纯 @ + QQ 号）应当保留为目标。
        """
        cleaned = []
        for item in (rest or "").split():
            if not item:
                continue
            if _AT_HEAD_RE.match(item):
                tail = item[1:]
                if tail.isdigit():
                    cleaned.append(tail)  # 用户手打的 @QQ号
                continue  # "@群名片" 或 "@群名片(QQ号)"
            if _AT_TAIL_RE.search(item):
                continue  # "名片(QQ号)"：群名片被空格切开的后半段
            cleaned.append(item)
        return cleaned

    def _resolve_query_target(self, event: AstrMessageEvent, args: list[str]) -> tuple[str, str]:
        """解析查询目标，返回 (目标 QQ, 拒绝原因)。

        规则：@ 成员优先，其次第一个像 QQ 号的纯数字参数，最后回退到发送者自己。
        查自己永远放行；查他人时，若开启 query_others_admin_only 则要求 AstrBot 管理员，
        避免群成员随意互查余额与使用日志。
        """
        sender = str(event.get_sender_id()).strip()
        at_qq = self._at_qq(event)
        if at_qq:
            target = at_qq
        else:
            target = next((item for item in args if _looks_like_qq(item)), "") or sender
        if target == sender:
            return target, ""
        if not self.query_others_admin_only or event.is_admin():
            return target, ""
        return "", f"只有机器人管理员可以查询他人（{target}）的数据，不带参数则查询你自己。"

    async def _cmd_balance(self, event: AstrMessageEvent, rest: str):
        args = self._split_args(rest)
        qq, deny = self._resolve_query_target(event, args)
        if not self._ready:
            yield event.plain_result("[meow-api] 插件未配置 meowapi_base_url / meowapi_access_token。")
            return
        if deny:
            yield event.plain_result(f"[meow-api 余额]\n{deny}")
            return
        if not qq:
            yield event.plain_result("用法：/查余额  或  /查余额 @某人  或  /查余额 <QQ号>")
            return

        user, err = await self._single_user_by_qq(qq)
        if user is None:
            yield event.plain_result(f"[meow-api 余额] {qq}\n{err}")
            return

        snap = await self._quota_snapshot_get()
        lines = [
            f"[meow-api 余额] {qq}",
            f"账户：{user.get('username') or '-'}（id={user.get('id')}）",
            f"邮箱：{user.get('email') or '-'}",
            f"余额：{self._fmt_quota(user.get('quota') or 0, snap)}",
            f"已用：{self._fmt_quota(user.get('used_quota') or 0, snap)}",
            f"请求：{int(user.get('request_count') or 0)} 次",
            "分组：{}  状态：{}  角色：{}".format(
                user.get("group") or "default",
                _STATUS_NAMES.get(int(user.get("status") or 0), f"未知({user.get('status')})"),
                _ROLE_NAMES.get(int(user.get("role") or 0), f"未知({user.get('role')})"),
            ),
        ]
        extra_groups = (user.get("groups") or "").strip()
        if extra_groups:
            lines.append(f"附加分组：{extra_groups}")
        yield event.plain_result("\n".join(lines))

    async def _cmd_logs(self, event: AstrMessageEvent, rest: str):
        args = self._split_args(rest)
        qq, deny = self._resolve_query_target(event, args)
        # 条数参数：取第一个"不是目标 QQ 的纯数字"参数，避免把 QQ 号当条数。
        limit = 10
        for item in args:
            if item.isdigit() and item != qq:
                limit = max(1, min(int(item), 20))
                break
        if not self._ready:
            yield event.plain_result("[meow-api] 插件未配置 meowapi_base_url / meowapi_access_token。")
            return
        if deny:
            yield event.plain_result(f"[meow-api 使用日志]\n{deny}")
            return
        if not qq:
            yield event.plain_result("用法：/查使用日志 [@某人|<QQ号>] [条数]")
            return

        user, err = await self._single_user_by_qq(qq)
        if user is None:
            yield event.plain_result(f"[meow-api 使用日志] {qq}\n{err}")
            return
        username = str(user.get("username") or "").strip()
        if not username:
            yield event.plain_result(f"[meow-api 使用日志] {qq}\n账户缺少用户名，无法按用户名过滤日志。")
            return

        try:
            resp = await self._api_get(
                "/api/log/",
                {"username": username, "p": 1, "page_size": limit},
            )
        except Exception as e:
            logger.error(f"[meowapi-ban] 查询日志异常 qq={qq}: {e}", exc_info=True)
            yield event.plain_result(f"[meow-api 使用日志] {qq}\n请求失败：{e}")
            return
        if not resp.get("success"):
            yield event.plain_result(
                f"[meow-api 使用日志] {qq}\n查询失败：{resp.get('message') or '未知错误'}"
            )
            return

        payload = resp.get("data") or {}
        items = payload.get("items") or []
        total = payload.get("total") or 0
        if not items:
            yield event.plain_result(f"[meow-api 使用日志] {qq}（{username}）\n暂无日志记录。")
            return

        snap = await self._quota_snapshot_get()
        lines = [f"[meow-api 使用日志] {qq}（{username}）近 {len(items)} 条 / 共 {total} 条"]
        for entry in items:
            lines.append(self._format_log_line(entry, snap))
        yield event.plain_result("\n".join(lines))

    @staticmethod
    def _format_log_line(entry: dict, snap: dict) -> str:
        head = "{} {}".format(
            _format_time(int(entry.get("created_at") or 0)),
            _LOG_TYPE_NAMES.get(int(entry.get("type") or 0), "其他"),
        )
        log_type = int(entry.get("type") or 0)
        if log_type == _LOG_TYPE_CONSUME:
            tokens = int(entry.get("prompt_tokens") or 0) + int(entry.get("completion_tokens") or 0)
            parts = [
                head,
                str(entry.get("model_name") or "-"),
                MeowApiBanPlugin._fmt_quota(entry.get("quota") or 0, snap),
            ]
            if tokens:
                parts.append(f"{_format_tokens(tokens)} tok")
            if entry.get("token_name"):
                parts.append(f"令牌「{entry.get('token_name')}」")
            return " | ".join(parts)
        content = str(entry.get("content") or "").replace("\n", " ").strip()
        if len(content) > 60:
            content = content[:57] + "..."
        return f"{head} | {content}" if content else head

    async def _cmd_usage(self, event: AstrMessageEvent, rest: str):
        args = self._split_args(rest)
        # 时间跨度：第一个"不像 QQ 号"的参数；目标：@ 成员 / QQ 号 / 发送者自己。
        span_text = next((item for item in args if not _looks_like_qq(item)), "")
        qq, deny = self._resolve_query_target(event, args)

        if not self._ready:
            yield event.plain_result("[meow-api] 插件未配置 meowapi_base_url / meowapi_access_token。")
            return
        try:
            span_seconds = _parse_span(span_text)
        except ValueError as e:
            yield event.plain_result(
                f"{e}\n用法：/查用量 <时间跨度> [@某人|<QQ号>]\n"
                "例：/查用量 24h、/查用量 7d、/查用量 7天、/查用量 1个月"
            )
            return
        if deny:
            yield event.plain_result(f"[meow-api 用量]\n{deny}")
            return
        if not qq:
            yield event.plain_result("用法：/查用量 <时间跨度> [@某人|<QQ号>]")
            return

        user, err = await self._single_user_by_qq(qq)
        if user is None:
            yield event.plain_result(f"[meow-api 用量] {qq}\n{err}")
            return
        username = str(user.get("username") or "").strip()
        if not username:
            yield event.plain_result(f"[meow-api 用量] {qq}\n账户缺少用户名，无法统计用量。")
            return

        end_ts = int(time.time())
        start_ts = end_ts - span_seconds
        head = f"[meow-api 用量] {qq}（{username}）近 {_humanize_span(span_seconds)}"

        # 合计以日志统计为准：/api/log/stat 直接 SUM(type=消费) 的 quota，
        # 不受"数据看板导出"开关与 5 分钟落库延迟影响，是最准确的口径。
        total_quota = 0
        stat_ok = False
        try:
            stat = await self._api_get(
                "/api/log/stat",
                {"username": username, "start_timestamp": start_ts, "end_timestamp": end_ts},
            )
            if stat.get("success"):
                total_quota = int((stat.get("data") or {}).get("quota") or 0)
                stat_ok = True
            else:
                logger.warning(
                    f"[meowapi-ban] 用量统计接口失败 qq={qq}: {stat.get('message')}"
                )
        except Exception as e:
            logger.error(f"[meowapi-ban] 查询用量统计异常 qq={qq}: {e}", exc_info=True)

        # 明细取数据看板（quota_data，已按 小时 × 模型 预聚合），用于按模型拆分。
        by_model: dict[str, list[int]] = {}
        board_rows = 0
        board_total_quota = 0
        board_total_count = 0
        board_total_tokens = 0
        board_error = ""
        try:
            resp = await self._api_get(
                "/api/data/",
                {
                    "username": username,
                    "start_timestamp": start_ts,
                    "end_timestamp": end_ts,
                },
            )
            if resp.get("success"):
                rows = resp.get("data") or []
                board_rows = len(rows)
                for row in rows:
                    quota = int(row.get("quota") or 0)
                    count = int(row.get("count") or 0)
                    tokens = int(row.get("token_used") or 0)
                    board_total_quota += quota
                    board_total_count += count
                    board_total_tokens += tokens
                    model = str(row.get("model_name") or "-")
                    bucket = by_model.setdefault(model, [0, 0, 0])
                    bucket[0] += count
                    bucket[1] += quota
                    bucket[2] += tokens
            else:
                board_error = str(resp.get("message") or "未知错误")
        except Exception as e:
            logger.error(f"[meowapi-ban] 查询数据看板异常 qq={qq}: {e}", exc_info=True)
            board_error = str(e)

        if not stat_ok and not board_rows:
            detail = board_error or (
                "数据看板无记录（其数据每 5 分钟落库一次，"
                "请确认 meow-api 已开启数据看板导出）。"
            )
            yield event.plain_result(f"{head}\n查询失败：{detail}")
            return

        snap = await self._quota_snapshot_get()
        quota_for_show = total_quota if stat_ok else board_total_quota
        lines = [
            head,
            "合计消费：{}{}".format(
                self._fmt_quota(quota_for_show, snap),
                "" if stat_ok else "（来自数据看板）",
            ),
        ]
        if board_rows:
            lines.append(
                "请求：{} 次 · {} tok（数据看板）".format(
                    board_total_count, _format_tokens(board_total_tokens)
                )
            )
        else:
            lines.append("明细不可用：" + (board_error or "数据看板无记录。"))
        ranked = sorted(by_model.items(), key=lambda kv: kv[1][1], reverse=True)
        if ranked:
            lines.append("按模型：")
            for model, (count, quota, tokens) in ranked[:8]:
                lines.append(
                    f"  {model}：{self._fmt_quota(quota, snap)} · {count} 次 · {_format_tokens(tokens)} tok"
                )
            if len(ranked) > 8:
                lines.append(f"  …另有 {len(ranked) - 8} 个模型")
        yield event.plain_result("\n".join(lines))

    @property
    def _ready(self) -> bool:
        """meow-api 连接参数是否已配置。"""
        return bool(self.base_url and self.access_token)

    async def _single_user_by_qq(self, qq: str):
        """按 QQ 号找到唯一账户，返回 (user 或 None, 失败说明)。"""
        email = f"{qq}@{self.email_domain}"
        try:
            users = await self._find_users_by_email(email)
        except Exception as e:
            logger.error(f"[meowapi-ban] 查询账户异常 email={email}: {e}", exc_info=True)
            return None, f"查询账户失败：{e}"
        if not users:
            return None, f"未找到邮箱为 {email} 的账户。"
        if len(users) > 1:
            ids = "、".join(str(u.get("id")) for u in users)
            logger.warning(f"[meowapi-ban] email={email} 命中多个账户 id={ids}，仅展示第一个。")
        return users[0], ""

    async def _find_users_by_email(self, email: str) -> list[dict]:
        """按邮箱精确匹配 meow-api 账户（分页翻查全部模糊结果）。"""
        key = email.lower()
        targets: list[dict] = []
        seen = 0
        max_pages = 50  # 防御性上限：最多翻 50 页（约 5000 条）
        for page in range(1, max_pages + 1):
            data = await self._api_get(
                "/api/user/search", {"keyword": email, "p": page, "page_size": 100}
            )
            if not data.get("success"):
                raise RuntimeError(data.get("message") or "未知错误")
            payload = data.get("data") or {}
            items = payload.get("items") or []
            try:
                total = int(payload.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
            targets.extend(u for u in items if self._is_exact_match(u, key))
            seen += len(items)
            if not items or seen >= total:
                break
        else:
            logger.warning(
                f"[meowapi-ban] 搜索 {email} 翻页达到上限 {max_pages} 页，可能未覆盖全部匹配。"
            )
        return targets

    @staticmethod
    def _is_exact_match(user: dict, email_lower: str) -> bool:
        """邮箱完全相等，且账户未被软删除。

        meow-api 的 /api/user/search 走 Unscoped() 查询，已删除账户也会命中；
        gorm.DeletedAt 未删除时序列化为 null，故用 DeletedAt 字段判断。
        """
        if str(user.get("email", "")).strip().lower() != email_lower:
            return False
        return not user.get("DeletedAt")

    # ---------------- 金额展示 ----------------
    async def _quota_snapshot_get(self) -> dict:
        """读取 /api/status 里的额度展示配置，缓存 5 分钟，失败则退回默认值。"""
        now = time.time()
        if self._quota_snapshot is not None and now - self._quota_snapshot_at < 300:
            return self._quota_snapshot
        snap = {
            "per_unit": 500000.0,
            "display": "USD",
            "usd_rate": 7.3,
            "symbol": "$",
            "rate": 1.0,
        }
        try:
            resp = await self._api_get("/api/status", {}, auth=False)
            data = resp.get("data") or {}
            if resp.get("success") and isinstance(data, dict):
                per_unit = float(data.get("quota_per_unit") or 0)
                if per_unit > 0:
                    snap["per_unit"] = per_unit
                snap["display"] = str(data.get("quota_display_type") or "USD").upper()
                usd_rate = float(data.get("usd_exchange_rate") or 0)
                if usd_rate > 0:
                    snap["usd_rate"] = usd_rate
                symbol = str(data.get("custom_currency_symbol") or "").strip()
                if symbol:
                    snap["symbol"] = symbol
                rate = float(data.get("custom_currency_exchange_rate") or 0)
                if rate > 0:
                    snap["rate"] = rate
        except Exception as e:
            logger.debug(f"[meowapi-ban] 读取 /api/status 失败，使用默认额度展示配置: {e}")
        self._quota_snapshot = snap
        self._quota_snapshot_at = now
        return snap

    @staticmethod
    def _fmt_quota(quota, snap: dict) -> str:
        """把 meow-api 的整数额度按站点配置的展示方式（USD/CNY/CUSTOM/TOKENS）格式化。"""
        value = float(quota or 0)
        display = snap.get("display") or "USD"
        if display == "TOKENS":
            return f"{int(value):,} 点"
        usd = value / (snap.get("per_unit") or 500000.0)
        if display == "CNY":
            return f"¥{usd * (snap.get('usd_rate') or 7.3):,.4f}"
        if display == "CUSTOM":
            symbol = snap.get("symbol") or "¤"
            return f"{symbol}{usd * (snap.get('rate') or 1.0):,.4f}"
        return f"${usd:,.4f}"

    # ---------------- 禁用/解禁 ----------------
    def _resolve_target_qq(self, event: AstrMessageEvent, rest: str) -> str:
        """解析管理命令的目标 QQ：@ 成员优先，其次手打的 QQ 号（可带 @ 前缀）。"""
        at_qq = self._at_qq(event)
        if at_qq:
            return at_qq
        args = self._split_args(rest)
        return next((item for item in args if _looks_like_qq(item)), "")

    async def _cmd_manage(self, event: AstrMessageEvent, rest: str, action: str):
        """手动命令共用逻辑；手动操作始终真实执行（不受 dry_run 影响）。"""
        name = "meowapi_ban" if action == "disable" else "meowapi_unban"
        verb = "禁用" if action == "disable" else "解禁"
        target = self._resolve_target_qq(event, rest)
        if not target:
            yield event.plain_result(f"用法：/{name} @某人  或  /{name} <QQ号>")
            return
        if not self._ready:
            yield event.plain_result("[meow-api] 插件未配置 meowapi_base_url / meowapi_access_token。")
            return
        email = f"{target}@{self.email_domain}"
        try:
            ok, summary = await self._manage_by_email(email, action, respect_dry_run=False)
        except Exception as e:
            logger.error(f"[meowapi-ban] 手动{verb} qq={target} 异常: {e}", exc_info=True)
            ok, summary = False, f"内部异常：{e}"
        logger.info(f"[meowapi-ban] 手动{verb} email={email} 成功={ok} 详情={summary}")
        yield event.plain_result(f"[meow-api 手动{verb}] {target}\n{summary}")

    async def _manage_by_email(self, email: str, action: str, respect_dry_run: bool = True):
        """按邮箱精确匹配 meow-api 账户并执行 action（disable/enable）。返回 (是否全部成功, 文字说明)。"""
        # 后端对 username/email/display_name 做 LIKE '%keyword%' 模糊匹配，默认每页仅 10 条、
        # 按 id 倒序。用完整邮箱作关键字时，QQ 号互为后缀的账户（如 123456@ 与 7123456@）
        # 也会命中，可能把目标挤出首页而漏封；因此需分页翻查、按完整邮箱精确过滤
        # （page_size 上限为 100）。
        try:
            targets = await self._find_users_by_email(email)
        except Exception as e:
            return False, f"查询失败：{e}"

        if not targets:
            return False, f"未找到邮箱为 {email} 的账户"

        want_status = _STATUS_DISABLED if action == "disable" else _STATUS_ENABLED
        verb = "禁用" if action == "disable" else "启用"
        results = []
        all_ok = True
        for u in targets:
            uid = u.get("id")
            uname = u.get("username") or ""
            if u.get("status") == want_status:
                results.append(f"id={uid}({uname}) 已是{verb}状态")
                continue
            if respect_dry_run and self.dry_run:
                results.append(f"id={uid}({uname}) [演练] 将被{verb}（未实际执行）")
                continue
            try:
                resp = await self._api_post(
                    "/api/user/manage", {"id": int(uid), "action": action}
                )
            except Exception as e:
                all_ok = False
                results.append(f"id={uid}({uname}) {verb}请求出错：{e}")
                continue
            if resp.get("success"):
                results.append(f"id={uid}({uname}) 已{verb}")
            else:
                all_ok = False
                results.append(f"id={uid}({uname}) {verb}失败：{resp.get('message') or '未知错误'}")
        return all_ok, "；".join(results)

    # ---------------- meow-api HTTP 封装 ----------------

    async def _ensure_session(self) -> ClientSession:
        if self._http is None or self._http.closed:
            self._http = ClientSession(timeout=ClientTimeout(total=self.request_timeout))
        return self._http

    def _request_kwargs(self, auth: bool = True) -> dict:
        # meow-api 以 Authorization 头识别管理员访问令牌（可带/不带 Bearer）；令牌为机密，不记录。
        kwargs: dict = {}
        if auth:
            kwargs["headers"] = {"Authorization": self.access_token}
        if not self.verify_ssl:
            kwargs["ssl"] = False
        return kwargs

    async def _api_get(self, path: str, params: dict, auth: bool = True) -> dict:
        session = await self._ensure_session()
        async with session.get(
            f"{self.base_url}{path}", params=params, **self._request_kwargs(auth)
        ) as resp:
            return await self._parse(resp)

    async def _api_post(self, path: str, json_body: dict) -> dict:
        session = await self._ensure_session()
        async with session.post(
            f"{self.base_url}{path}", json=json_body, **self._request_kwargs()
        ) as resp:
            return await self._parse(resp)

    @staticmethod
    async def _parse(resp) -> dict:
        text = await resp.text()
        try:
            body = json.loads(text)
        except Exception:
            raise RuntimeError(f"HTTP {resp.status}: 响应非 JSON：{text[:200]}")
        if not isinstance(body, dict):
            raise RuntimeError(f"HTTP {resp.status}: 响应结构非预期")
        return body