"""CLI channel console entrypoint (`services/cli.py`, `core/channels.md` §2, §3).

Provides air-gapped Box interactive console interface for asking questions to the RAG system.
Supports direct streaming token rendering, plain text source citations, and commands (/help, /exit).
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import Any
from uuid import UUID, uuid4

from rag_core.channels.cli import CLIChannel
from rag_core.contracts.enums import AccessLevel, ChannelType
from rag_core.contracts.models import RAGResponse, UserContext
from rag_core.contracts.settings import Settings
from rag_core.generation.pipeline import GenerationPipeline


class CLIRunner:
    """Console interactive chat runner."""

    def __init__(
        self,
        pipeline: GenerationPipeline | None = None,
        settings: Settings | None = None,
        tenant_id: UUID | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.settings = settings or Settings()
        self.tenant_id = tenant_id or UUID("00000000-0000-0000-0000-000000000001")
        self.channel = CLIChannel(output_sink=self._console_write)
        self.user_id = uuid4()
        self.session_id = f"cli_session_{uuid4().hex[:8]}"

    def _console_write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    async def execute_query(self, query: str) -> None:
        """Executes a single RAG query and streams output to terminal."""
        clean_text = query.strip()
        if not clean_text:
            return

        if clean_text in ("/help", "help"):
            print(
                "Commands:\n"
                "  /help  - Show this help\n"
                "  /clear - Start new session\n"
                "  /exit  - Quit console"
            )
            return

        if clean_text in ("/clear", "clear"):
            self.session_id = f"cli_session_{uuid4().hex[:8]}"
            print("Session cleared.")
            return

        user_ctx = UserContext(
            user_id=self.user_id,
            tenant_id=self.tenant_id,
            channel_user_id=str(self.user_id),
            channel=ChannelType.CLI,
            access_level=AccessLevel.PUBLIC,
        )

        await self.channel.normalize_command(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            command=query,
            session_id=self.session_id,
        )

        if self.pipeline is not None:
            rag_response = await self.pipeline.generate_response(
                user=user_ctx,
                query=query,
                session_id=self.session_id,
            )

            async def token_gen() -> Any:
                for tok in rag_response.answer.split(" "):
                    yield tok + " "
                    await asyncio.sleep(0.01)

            print("\nAI: ", end="", flush=True)
            await self.channel.stream_response(
                user_id=str(self.user_id),
                token_stream=token_gen(),
                citations=rag_response.citations,
                confidence_passed=rag_response.confidence_passed,
                trace_id=rag_response.trace_id,
            )
            print()
        else:
            fallback = RAGResponse(
                answer=f"AI (Offline CLI): Echoing '{query}'",
                citations=[],
                confidence_passed=True,
            )
            await self.channel.send_response(str(self.user_id), fallback)

    async def run_interactive_loop(self) -> None:
        """Runs the interactive REPL loop."""
        print("=== Enterprise RAG Box Console (CLI Channel) ===")
        print("Type your question or /help. Press Ctrl+C or type /exit to quit.\n")

        while True:
            try:
                line = input("You > ")
            except (EOFError, KeyboardInterrupt):
                print("\nExiting.")
                break

            if line.strip() in ("/exit", "/quit", "exit", "quit"):
                print("Bye!")
                break

            await self.execute_query(line)


def main() -> None:
    """CLI script entrypoint."""
    runner = CLIRunner()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runner.run_interactive_loop())


if __name__ == "__main__":
    main()
