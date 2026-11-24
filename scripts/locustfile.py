"""Stage O - locust load profile for a live RAG API (IMPLEMENTATION_PLAN.md §O).

This is the *real* HTTP load profile used in a nightly / release run against a deployed stack
(``SPECIFICATION.md`` §1.2: P95 retrieval < 400 ms, P95 TTFT < 1.5 s, ingest throughput). It is
intentionally separate from the pure gate in :mod:`tests.load_slo`: locust generates the traffic
and emits a stats CSV/JSON, which the gate (``python -m tests.load_slo``) then verifies against the
committed baseline.

Run (needs the ``load`` extra - ``pip install -e '.[load]'``)::

    locust -f scripts/locustfile.py \
        --host https://api.tenant.example.com --headless \
        -u 50 -r 10 -t 5m --csv=load_report \
        --tenant-id <uuid> --auth-token <jwt>

The ``retrieval`` and ``generation_ttft`` (first-token) tasks map 1:1 onto the SLO scenarios; the
``ingestion`` task exercises the enqueue path for throughput. locust is imported at module load on
purpose (the file is only ever executed by the ``locust`` CLI), so this module is NOT imported by
the test suite.
"""

from __future__ import annotations

import time

from locust import FastHttpUser, between, events, task


@events.init_command_line_parser.add_listener
def _add_args(parser: object) -> None:
    parser.add_argument("--tenant-id", type=str, default="", help="X-Tenant-ID header value")  # type: ignore[attr-defined]
    parser.add_argument("--auth-token", type=str, default="", help="Bearer token")  # type: ignore[attr-defined]
    parser.add_argument("--query", type=str, default="What is the refund policy?")  # type: ignore[attr-defined]


class RagUser(FastHttpUser):
    """A virtual user hitting the retrieval / streaming / ingestion endpoints."""

    wait_time = between(0.5, 2.0)

    def on_start(self) -> None:
        opts = self.environment.parsed_options
        self.query: str = getattr(opts, "query", "What is the refund policy?")
        self.client.headers.update({"X-Tenant-ID": getattr(opts, "tenant_id", "")})
        token = getattr(opts, "auth_token", "")
        if token:
            self.client.headers.update({"Authorization": f"Bearer {token}"})

    @task(5)
    def retrieval(self) -> None:
        with self.client.post(
            "/api/v1/chat", json={"query": self.query, "stream": False}, name="retrieval"
        ) as resp:
            if resp.status_code >= 400:
                resp.failure(f"status {resp.status_code}")

    @task(3)
    def generation_ttft(self) -> None:
        start = time.perf_counter()
        with self.client.post(
            "/api/v1/chat",
            json={"query": self.query, "stream": True},
            stream=True,
            name="generation_ttft",
            catch_response=True,
        ) as resp:
            if resp.status_code >= 400:
                resp.failure(f"status {resp.status_code}")
                return
            for chunk in resp.iter_lines():
                if chunk:
                    # Record TTFT as a custom metric and stop reading the stream.
                    ttft_ms = (time.perf_counter() - start) * 1000.0
                    events.request.fire(
                        request_type="TTFT",
                        name="generation_ttft",
                        response_time=ttft_ms,
                        response_length=len(chunk),
                        exception=None,
                        context={},
                    )
                    resp.success()
                    return

    @task(1)
    def ingestion(self) -> None:
        with self.client.post(
            "/api/v1/admin/documents",
            json={"name": "loadtest.txt", "content": "synthetic load document"},
            name="ingestion",
        ) as resp:
            if resp.status_code >= 400:
                resp.failure(f"status {resp.status_code}")
