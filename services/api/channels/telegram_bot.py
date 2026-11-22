"""Telegram Bot service runner (`services/api/channels/telegram_bot.py`).

Runs the white-label Telegram Bot (`core/channels.md` §6, `core/eval_and_ops.md` §3.2).
Uses TelegramChannel adapter with stream throttling (0.8-1.0s), Markdown formatting,
inline feedback buttons, commands (/start, /help, /clear), and 4096-char splitting.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID, uuid4

from rag_core.channels.branding import sanitize_telegram_branding
from rag_core.channels.telegram import TelegramChannel
from rag_core.contracts.enums import AccessLevel, ChannelEventType, ChannelType
from rag_core.contracts.models import (
    RAGResponse,
    TelegramBranding,
    UserContext,
)
from rag_core.contracts.settings import Settings
from rag_core.generation.pipeline import GenerationPipeline

logger = logging.getLogger(__name__)


class TelegramBotRunner:
    """Telegram Bot dispatcher and message handler runner."""

    def __init__(
        self,
        settings: Settings | None = None,
        generation_pipeline: GenerationPipeline | None = None,
        channel: TelegramChannel | None = None,
        branding: TelegramBranding | None = None,
        tenant_id: UUID | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.pipeline = generation_pipeline
        self.channel = channel or TelegramChannel()
        self.branding = sanitize_telegram_branding(
            branding
            or TelegramBranding(
                bot_name="Enterprise RAG Bot",
                description="Corporate AI Knowledge Assistant",
                welcome_message=(
                    "Hello! I am your corporate RAG assistant. "
                    "Ask me anything about our company documents."
                ),
                commands={
                    "start": "Start dialog with AI assistant",
                    "help": "Show available commands and usage info",
                    "clear": "Clear conversation history",
                },
            )
        )
        self.tenant_id = tenant_id or UUID("00000000-0000-0000-0000-000000000001")
        self._user_sessions: dict[str, str] = {}

    async def handle_message(
        self,
        telegram_user_id: int | str,
        text: str,
        chat_id: int | str | None = None,
    ) -> str:
        """Processes incoming user message through RAG Generation Pipeline."""
        str_user_id = str(telegram_user_id)
        session_id = self._user_sessions.get(str_user_id)
        if not session_id:
            session_id = f"tg_session_{str_user_id}_{uuid4().hex[:8]}"
            self._user_sessions[str_user_id] = session_id

        # Normalize incoming event
        await self.channel.normalize_native_event(
            tenant_id=self.tenant_id,
            user_id=UUID(int=int(str_user_id) if str_user_id.isdigit() else 1),
            channel_user_id=str_user_id,
            text=text,
            event_type=ChannelEventType.TEXT_MESSAGE,
            session_id=session_id,
        )

        clean_text = text.strip()
        if clean_text == "/start":
            welcome = self.branding.welcome_message or "Welcome to Enterprise RAG Bot!"
            return welcome

        if clean_text == "/help":
            cmd_lines = [f"/{k} - {v}" for k, v in self.branding.commands.items()]
            return "Available commands:\n" + "\n".join(cmd_lines)

        if clean_text == "/clear":
            self._user_sessions[str_user_id] = f"tg_session_{str_user_id}_{uuid4().hex[:8]}"
            return "Conversation history cleared."

        # RAG pipeline execution
        user_ctx = UserContext(
            user_id=UUID(int=int(str_user_id) if str_user_id.isdigit() else 1),
            tenant_id=self.tenant_id,
            channel_user_id=str_user_id,
            channel=ChannelType.TELEGRAM,
            access_level=AccessLevel.PUBLIC,
        )

        if self.pipeline is not None:
            rag_response = await self.pipeline.generate_response(
                user=user_ctx,
                query=text,
                session_id=session_id,
            )
            await self.channel.send_response(str_user_id, rag_response)
            formatted, _ = self.channel.format_response_payload(rag_response)
            return formatted

        # Default fallback response if pipeline is not wired
        return f"RAG Bot received: {text}"

    async def handle_callback_query(
        self,
        telegram_user_id: int | str,
        callback_data: str,
    ) -> str:
        """Processes inline feedback buttons (👍 / 👎)."""
        str_user_id = str(telegram_user_id)
        await self.channel.normalize_native_event(
            tenant_id=self.tenant_id,
            user_id=UUID(int=int(str_user_id) if str_user_id.isdigit() else 1),
            channel_user_id=str_user_id,
            text=None,
            event_type=ChannelEventType.CALLBACK,
            payload={"callback_data": callback_data},
        )

        if callback_data.startswith("feedback:thumb_up:"):
            trace_id = callback_data.split(":")[-1]
            logger.info("Recorded positive feedback for trace %s", trace_id)
            if self.pipeline is not None and trace_id:
                updated = await self.pipeline.session_manager.update_feedback(
                    trace_id=trace_id, feedback=1, tenant_id=self.tenant_id
                )
                if updated:
                    await self.pipeline.trace_manager.log_score(
                        trace_id, name="user_feedback", value=1.0
                    )
            return "Thank you for your feedback! 👍"

        if callback_data.startswith("feedback:thumb_down:"):
            trace_id = callback_data.split(":")[-1]
            logger.info("Recorded negative feedback for trace %s", trace_id)
            if self.pipeline is not None and trace_id:
                updated = await self.pipeline.session_manager.update_feedback(
                    trace_id=trace_id, feedback=-1, tenant_id=self.tenant_id
                )
                if updated:
                    await self.pipeline.trace_manager.log_score(
                        trace_id, name="user_feedback", value=-1.0
                    )
            return "Thank you for your feedback! 👎 We will improve."

        return "Feedback received."

    async def stream_message_response(
        self,
        telegram_user_id: int | str,
        text: str,
        initial_message: Any | None = None,
    ) -> RAGResponse:
        """Processes message using streaming and throttled Telegram message edits."""
        str_user_id = str(telegram_user_id)
        session_id = self._user_sessions.get(str_user_id)
        if not session_id:
            session_id = f"tg_session_{str_user_id}_{uuid4().hex[:8]}"
            self._user_sessions[str_user_id] = session_id

        await self.channel.normalize_native_event(
            tenant_id=self.tenant_id,
            user_id=UUID(int=int(str_user_id) if str_user_id.isdigit() else 1),
            channel_user_id=str_user_id,
            text=text,
            event_type=ChannelEventType.TEXT_MESSAGE,
            session_id=session_id,
        )

        user_ctx = UserContext(
            user_id=UUID(int=int(str_user_id) if str_user_id.isdigit() else 1),
            tenant_id=self.tenant_id,
            channel_user_id=str_user_id,
            channel=ChannelType.TELEGRAM,
            access_level=AccessLevel.PUBLIC,
        )

        if self.pipeline is not None:
            rag_response = await self.pipeline.generate_response(
                user=user_ctx,
                query=text,
                session_id=session_id,
            )

            async def token_gen() -> Any:
                for token in rag_response.answer.split(" "):
                    yield token + " "
                    await asyncio.sleep(0.05)

            msg_id = str(getattr(initial_message, "message_id", "msg_0"))
            return await self.channel.stream_response(
                user_id=str_user_id,
                token_stream=token_gen(),
                initial_message_id=msg_id,
                citations=rag_response.citations,
                confidence_passed=rag_response.confidence_passed,
                trace_id=rag_response.trace_id,
            )

        # Fallback when no pipeline
        fallback_resp = RAGResponse(
            answer=f"RAG Bot received: {text}",
            citations=[],
            confidence_passed=True,
        )
        await self.channel.send_response(str_user_id, fallback_resp)
        return fallback_resp

    async def start_polling(self) -> None:
        """Starts aiogram polling when bot token is provided."""
        token = self.settings.telegram_bot_token
        if not token:
            logger.warning("TELEGRAM_BOT_TOKEN not configured; polling not started.")
            return

        try:
            from aiogram import Bot, Dispatcher, F
            from aiogram.filters import Command
        except ImportError:
            logger.warning(
                "aiogram is not installed; run `pip install aiogram` to run Telegram Bot service."
            )
            return

        bot = Bot(token=token)
        dp = Dispatcher()

        # Wire live editing and sending functions into the channel
        async def send_tg_msg(
            user_id: str,
            text: str,
            reply_markup: dict[str, Any] | None = None,
        ) -> None:
            chat_id = int(user_id) if user_id.isdigit() else user_id
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

        async def edit_tg_msg(
            user_id: str,
            message_id: str,
            text: str,
            reply_markup: dict[str, Any] | None = None,
        ) -> None:
            chat_id = int(user_id) if user_id.isdigit() else user_id
            msg_id = int(message_id) if message_id.isdigit() else 0
            if msg_id:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=text,
                    reply_markup=reply_markup,
                )

        self.channel._send_fn = send_tg_msg
        self.channel._edit_fn = edit_tg_msg

        async def cmd_start(message: Any) -> None:
            if message.from_user:
                res = await self.handle_message(message.from_user.id, "/start")
                await message.answer(res)

        async def cmd_help(message: Any) -> None:
            if message.from_user:
                res = await self.handle_message(message.from_user.id, "/help")
                await message.answer(res)

        async def cmd_clear(message: Any) -> None:
            if message.from_user:
                res = await self.handle_message(message.from_user.id, "/clear")
                await message.answer(res)

        async def msg_handler(message: Any) -> None:
            if message.from_user and message.text:
                if message.text.startswith("/"):
                    res = await self.handle_message(message.from_user.id, message.text)
                    await message.answer(res)
                    return

                # Send initial placeholder message and stream updates into it with throttling
                init_msg = await message.answer("⏳ ...")
                await self.stream_message_response(
                    telegram_user_id=message.from_user.id,
                    text=message.text,
                    initial_message=init_msg,
                )

        async def feedback_handler(callback: Any) -> None:
            if callback.from_user and callback.data:
                res = await self.handle_callback_query(callback.from_user.id, callback.data)
                await callback.answer(res)

        dp.message.register(cmd_start, Command("start"))
        dp.message.register(cmd_help, Command("help"))
        dp.message.register(cmd_clear, Command("clear"))
        dp.message.register(msg_handler)
        dp.callback_query.register(feedback_handler, F.data.startswith("feedback:"))

        logger.info("Starting Telegram Bot polling...")
        await dp.start_polling(bot)


def main() -> None:
    """Entry point for python -m services.api.channels.telegram_bot."""
    runner = TelegramBotRunner()
    asyncio.run(runner.start_polling())


if __name__ == "__main__":
    main()
