"""Stage O workloads: the offline self-check and the real HTTP load driver.

Two families of :class:`~tests.load_slo.gate.LoadScenario` are built here:

* :func:`build_self_check_scenarios` - fully offline, dependency-free workloads that sleep for a
  deterministic, SLO-compliant duration. They exercise the whole gate (driver -> metrics ->
  thresholds -> baseline -> report) in CI without a live Postgres/LLM, proving the plumbing works.
* :func:`build_http_scenarios` - the real nightly profile: it hammers a running API's retrieval /
  streaming-answer / ingestion endpoints over HTTP. ``httpx`` is imported lazily so importing this
  module (and running the self-check) never requires the optional ``load`` extra.

The load *concurrency* and *iterations* are taken from an :class:`LoadProfile` so the same
scenarios can run as a quick smoke (few iterations) or a full nightly soak.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from tests.load_slo.gate import LoadScenario, SimpleScenario
from tests.load_slo.slo import SCENARIO_INGEST, SCENARIO_RETRIEVAL, SCENARIO_TTFT


@dataclass(frozen=True, slots=True)
class LoadProfile:
    """How hard to drive each scenario (closed-loop iterations x concurrency)."""

    iterations: int = 200
    concurrency: int = 16

    @classmethod
    def smoke(cls) -> LoadProfile:
        """A tiny profile for the self-check / CI smoke step."""
        return cls(iterations=30, concurrency=8)


def build_self_check_scenarios(profile: LoadProfile | None = None) -> list[LoadScenario]:
    """Offline scenarios that comfortably meet the SLO - used by ``--self-check``."""
    profile = profile or LoadProfile.smoke()

    # Deterministic, well-under-SLO synthetic latencies (seconds).
    async def retrieval() -> None:
        await asyncio.sleep(0.05)  # ~50 ms << 400 ms

    async def ttft() -> None:
        await asyncio.sleep(0.1)  # ~100 ms << 1500 ms

    async def ingest() -> None:
        await asyncio.sleep(0.02)

    return [
        SimpleScenario(SCENARIO_RETRIEVAL, retrieval, profile.iterations, profile.concurrency),
        SimpleScenario(SCENARIO_TTFT, ttft, profile.iterations, profile.concurrency),
        SimpleScenario(SCENARIO_INGEST, ingest, profile.iterations, profile.concurrency),
    ]


def build_http_scenarios(
    base_url: str,
    *,
    tenant_id: str,
    auth_token: str | None = None,
    query: str = "What is the refund policy?",
    profile: LoadProfile | None = None,
) -> list[LoadScenario]:
    """Real nightly scenarios driving a live API over HTTP (``httpx`` imported lazily).

    - ``retrieval``: POST the search/answer endpoint and read the full response (retrieval path);
    - ``generation_ttft``: open the SSE stream and measure up to the FIRST token only;
    - ``ingestion``: enqueue a small synthetic document (throughput of the ingest path).
    """
    try:
        import httpx
    except ModuleNotFoundError as exc:  # pragma: no cover - requires the optional extra
        raise RuntimeError(
            "the HTTP load profile requires the 'load' extra (pip install -e '.[load]')"
        ) from exc

    headers = {"X-Tenant-ID": tenant_id}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    base_url = base_url.rstrip("/")
    timeout = httpx.Timeout(30.0)

    async def retrieval() -> None:
        async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client:
            resp = await client.post("/api/v1/chat", json={"query": query, "stream": False})
            resp.raise_for_status()

    async def ttft() -> None:
        async with (
            httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client,
            client.stream("POST", "/api/v1/chat", json={"query": query, "stream": True}) as resp,
        ):
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                if chunk.strip():
                    return  # first token received -> stop timing TTFT

    async def ingest() -> None:
        async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client:
            resp = await client.post(
                "/api/v1/admin/documents",
                json={"name": "loadtest.txt", "content": "synthetic load document"},
            )
            resp.raise_for_status()

    profile = profile or LoadProfile()
    return [
        SimpleScenario(SCENARIO_RETRIEVAL, retrieval, profile.iterations, profile.concurrency),
        SimpleScenario(SCENARIO_TTFT, ttft, profile.iterations, profile.concurrency),
        SimpleScenario(
            SCENARIO_INGEST, ingest, max(10, profile.iterations // 4), profile.concurrency
        ),
    ]
