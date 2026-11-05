"""群唤醒插件：指定群平时完全静默，只有被 @ 时短暂唤醒。

原理：MaiBot 的回复频率 = talk_value × adjust。插件把目标群的 adjust 压到 0，
该群进入“静默消费”（消息照常入库、进上下文，但不会进 Planner，不花 token）；
收到 @ 时把 adjust 抬回正常值并开一个窗口，窗口内正常说话，到期再压回 0。

注意：目标群的 talk_value 必须大于 0（建议 0.01~0.2）。如果配了 0 的规则，
0 × 任何倍率都是 0，@ 也醒不了；插件会在日志里提醒。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode, HookOrder


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class GroupWakeConfig(PluginConfigBase):
    """群唤醒配置。"""

    __ui_label__ = "群唤醒"
    __ui_icon__ = "bell"
    __ui_order__ = 1

    groups: list[str] = Field(
        default_factory=list,
        description="要静默的群号：这些群平时不说话，只有被 @ 才短暂唤醒",
    )
    wake_seconds: int = Field(
        default=120,
        description="被 @ 之后保持可说话的秒数，到期自动回到静默",
    )
    silent_adjust: float = Field(
        default=0.0,
        description="平时使用的频率倍率，0 = 完全静默（不生成回复）",
    )
    wake_adjust: float = Field(
        default=1.0,
        description="@ 唤醒后的频率倍率，1.0 = 按群的正常频率说话",
    )


class GroupWakePluginConfig(PluginConfigBase):
    """群唤醒插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    group_wake: GroupWakeConfig = Field(default_factory=GroupWakeConfig)


class GroupWakePlugin(MaiBotPlugin):
    """指定群静默、@ 才短暂唤醒。"""

    config_model = GroupWakePluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._groups: set[str] = set()
        self._session_group: dict[str, str] = {}
        self._wake_until: dict[str, float] = {}
        self._warned_sessions: set[str] = set()
        self._timer: asyncio.Task[None] | None = None

    # ---------- 生命周期 ----------

    async def on_load(self) -> None:
        self._refresh_config()
        self._timer = asyncio.create_task(self._tick())
        self._logger().info(
            "群唤醒插件已加载：静默群=%s，唤醒窗口=%s 秒",
            sorted(self._groups),
            self.config.group_wake.wake_seconds,
        )

    async def on_unload(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        # 卸载时尽量把倍率恢复，避免群被永久压成静默
        for session_id in list(self._session_group):
            try:
                await self.ctx.frequency.set_adjust(session_id, self.config.group_wake.wake_adjust)
            except Exception:  # noqa: BLE001
                pass

    async def on_config_update(self, scope: str = "", config_data: Any = None, version: Any = None) -> None:
        del scope, config_data, version
        self._refresh_config()

    def _refresh_config(self) -> None:
        self._groups = {str(g).strip() for g in self.config.group_wake.groups if str(g).strip()}

    def _logger(self) -> logging.Logger:
        try:
            return self.ctx.logger
        except Exception:  # noqa: BLE001
            return logging.getLogger("plugin.dsh.group-wake")

    # ---------- 消息钩子 ----------

    @HookHandler(
        "chat.receive.after_process",
        name="group_wake_gate",
        description="指定群平时静默，被 @ 时短暂唤醒",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
    )
    async def on_message(self, message: Any = None, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if not self.config.plugin.enabled or not isinstance(message, dict):
            return {"action": "continue"}

        group = (message.get("message_info") or {}).get("group_info") or {}
        group_id = str(group.get("group_id") or "")
        if not group_id or group_id not in self._groups:
            return {"action": "continue"}

        session_id = str(message.get("session_id") or "")
        if not session_id:
            return {"action": "continue"}
        self._session_group[session_id] = group_id

        now = time.time()
        if self._is_at_bot(message):
            window = max(1, int(self.config.group_wake.wake_seconds))
            self._wake_until[session_id] = now + window
            await self._apply_adjust(session_id, self.config.group_wake.wake_adjust)
            await self._check_talk_value(session_id)
            self._logger().info("群 %s 被 @，唤醒 %s 秒", group_id, window)
            return {"action": "continue"}

        if now >= self._wake_until.get(session_id, 0.0):
            await self._apply_adjust(session_id, self.config.group_wake.silent_adjust)
        return {"action": "continue"}

    @staticmethod
    def _is_at_bot(message: dict[str, Any]) -> bool:
        if bool(message.get("is_at")):
            return True
        account = str(message.get("account_id") or "")
        for segment in message.get("raw_message") or []:
            if not isinstance(segment, dict) or str(segment.get("type") or "") != "at":
                continue
            data = segment.get("data") or {}
            target = str(data.get("target_user_id") or "")
            if target and (not account or target == account):
                return True
        return False

    # ---------- 频率控制 ----------

    async def _apply_adjust(self, session_id: str, value: float) -> None:
        try:
            ok = await self.ctx.frequency.set_adjust(session_id, float(value))
        except Exception as exc:  # noqa: BLE001
            self._logger().warning("设置群频率倍率失败 session=%s: %s", session_id, exc)
            return
        if ok:
            return
        # 会话运行时可能还没建出来，稍后补两次
        asyncio.create_task(self._retry_adjust(session_id, value))

    async def _retry_adjust(self, session_id: str, value: float) -> None:
        for delay in (1.0, 3.0):
            await asyncio.sleep(delay)
            try:
                if await self.ctx.frequency.set_adjust(session_id, float(value)):
                    return
            except Exception:  # noqa: BLE001
                return

    async def _check_talk_value(self, session_id: str) -> None:
        if session_id in self._warned_sessions:
            return
        try:
            value = await self.ctx.frequency.get_current_talk_value(session_id)
        except Exception:  # noqa: BLE001
            return
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return
        if numeric > 0:
            return
        self._warned_sessions.add(session_id)
        self._logger().warning(
            "会话 %s 的 talk_value 是 0，@ 也唤醒不了；"
            "请把该群的 talk_value 规则删掉或改成 0.01~0.2（插件靠 adjust 静默）",
            session_id,
        )

    async def _tick(self) -> None:
        try:
            while True:
                await asyncio.sleep(5)
                now = time.time()
                for session_id, until in list(self._wake_until.items()):
                    if now < until:
                        continue
                    self._wake_until.pop(session_id, None)
                    await self._apply_adjust(session_id, self.config.group_wake.silent_adjust)
                    self._logger().info("会话 %s 唤醒窗口结束，回到静默", session_id)
        except asyncio.CancelledError:
            return


def create_plugin() -> GroupWakePlugin:
    """插件工厂函数，供宿主加载。"""
    return GroupWakePlugin()
