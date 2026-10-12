"""AstrBot 插件：QQ 成员退群时，自动禁用其在 meow-api 中对应的账户。

账户匹配规则：退群成员的 QQ 号 + "@" + 配置的邮箱域名（默认 qq.com）即为账户邮箱。
依赖 aiocqhttp（OneBot v11）平台适配器——QQ 官方 Bot 适配器不会下发退群通知。
"""

import json
from typing import Optional

from aiohttp import ClientSession, ClientTimeout

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# meow-api 用户状态常量（见 meow-api common/constants.go：1=启用，2=禁用）
_STATUS_ENABLED = 1
_STATUS_DISABLED = 2


def _strip_bearer(token: str) -> str:
    """去掉可能存在的 "Bearer " 前缀；meow-api 两种写法都接受，统一发裸令牌。"""
    if token.lower().startswith("bearer "):
        return token[7:].strip()
    return token


@register(
    "astrbot_plugin_meowapi_ban",
    "xingguangcuican",
    "QQ 退群时自动禁用其对应的 meow-api 账户（邮箱=QQ号@域名）",
    "0.1.0",
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
        self.verify_ssl = bool(config.get("verify_ssl", True))
        try:
            self.request_timeout = int(config.get("request_timeout", 10))
        except (TypeError, ValueError):
            self.request_timeout = 10
        if self.request_timeout <= 0:
            self.request_timeout = 10
        self._http: Optional[ClientSession] = None

    async def initialize(self):
        # ClientSession 需在事件循环内创建，故放在异步的 initialize 中
        self._http = ClientSession(timeout=ClientTimeout(total=self.request_timeout))
        if not self.base_url or not self.access_token:
            logger.warning(
                "[meowapi-ban] 未配置 meowapi_base_url / meowapi_access_token，退群事件将不会执行禁用。"
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
    async def cmd_ban(self, event: AstrMessageEvent, qq: str = ""):
        """手动禁用某 QQ 对应的 meow-api 账户：/meowapi_ban <QQ号>（仅机器人管理员）。"""
        async for r in self._cmd_manage(event, qq, "disable"):
            yield r

    @filter.command("meowapi_unban")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_unban(self, event: AstrMessageEvent, qq: str = ""):
        """手动解禁某 QQ 对应的 meow-api 账户：/meowapi_unban <QQ号>（仅机器人管理员）。"""
        async for r in self._cmd_manage(event, qq, "enable"):
            yield r

    async def _cmd_manage(self, event: AstrMessageEvent, qq: str, action: str):
        """手动命令共用逻辑；手动操作始终真实执行（不受 dry_run 影响）。"""
        name = "meowapi_ban" if action == "disable" else "meowapi_unban"
        verb = "禁用" if action == "disable" else "解禁"
        qq = (qq or "").strip()
        if not qq.isdigit():
            yield event.plain_result(f"用法：/{name} <QQ号>")
            return
        if not self.base_url or not self.access_token:
            yield event.plain_result("[meow-api] 插件未配置 meowapi_base_url / meowapi_access_token。")
            return
        email = f"{qq}@{self.email_domain}"
        try:
            ok, summary = await self._manage_by_email(email, action, respect_dry_run=False)
        except Exception as e:
            logger.error(f"[meowapi-ban] 手动{verb} qq={qq} 异常: {e}", exc_info=True)
            ok, summary = False, f"内部异常：{e}"
        logger.info(f"[meowapi-ban] 手动{verb} email={email} 成功={ok} 详情={summary}")
        yield event.plain_result(f"[meow-api 手动{verb}] {qq}\n{summary}")

    async def _manage_by_email(self, email: str, action: str, respect_dry_run: bool = True):
        """按邮箱精确匹配 meow-api 账户并执行 action（disable/enable）。返回 (是否全部成功, 文字说明)。"""
        # 1) 搜索：后端对 username/email/display_name 做 LIKE '%keyword%' 模糊匹配，
        #    默认每页仅 10 条、按 id 倒序。用完整邮箱作关键字时，QQ 号互为后缀的账户
        #    （如 123456@ 与 7123456@）也会命中，可能把目标挤出首页而漏封；因此需分页
        #    翻查、按完整邮箱精确过滤，并收集所有精确匹配（page_size 上限为 100）。
        key = email.lower()
        targets = []
        seen = 0
        max_pages = 50  # 防御性上限：最多翻 50 页（约 5000 条）
        for page in range(1, max_pages + 1):
            data = await self._api_get(
                "/api/user/search", {"keyword": email, "p": page, "page_size": 100}
            )
            if not data.get("success"):
                return False, f"查询失败：{data.get('message') or '未知错误'}"
            payload = data.get("data") or {}
            items = payload.get("items") or []
            try:
                total = int(payload.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
            targets.extend(
                u for u in items if str(u.get("email", "")).strip().lower() == key
            )
            seen += len(items)
            if not items or seen >= total:
                break
        else:
            logger.warning(
                f"[meowapi-ban] 搜索 {email} 翻页达到上限 {max_pages} 页，可能未覆盖全部匹配。"
            )

        if not targets:
            return False, f"未找到邮箱为 {email} 的账户（共扫描 {seen} 条模糊结果）"

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

    def _request_kwargs(self) -> dict:
        # meow-api 以 Authorization 头识别管理员访问令牌（可带/不带 Bearer）；令牌为机密，不记录。
        kwargs = {"headers": {"Authorization": self.access_token}}
        if not self.verify_ssl:
            kwargs["ssl"] = False
        return kwargs

    async def _api_get(self, path: str, params: dict) -> dict:
        session = await self._ensure_session()
        async with session.get(
            f"{self.base_url}{path}", params=params, **self._request_kwargs()
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


