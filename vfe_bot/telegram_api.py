from __future__ import annotations

import asyncio
from typing import Any

import httpx


class TelegramAPI:
    def __init__(self, token: str):
        self.client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}/",
            timeout=httpx.Timeout(45, connect=15),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        response = await self.client.post(method, json=payload or {})
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", f"Telegram method failed: {method}"))
        return data.get("result")

    async def send(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        disable_notification: bool = False,
    ) -> Any:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": disable_notification,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return await self.call("sendMessage", payload)

    async def edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self.call("editMessageText", payload)

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            await self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:180]})
        except Exception:
            pass

    async def send_long(self, chat_id: int, text: str, header: str = "") -> None:
        max_chunk = 3500
        if not text:
            await self.send(chat_id, f"{header}<i>No output</i>")
            return
        for index in range(0, len(text), max_chunk):
            chunk = text[index:index + max_chunk]
            prefix = header if index == 0 else ""
            escaped = (
                chunk.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            await self.send(chat_id, f"{prefix}<pre>{escaped}</pre>")
            if index + max_chunk < len(text):
                await asyncio.sleep(0.1)
