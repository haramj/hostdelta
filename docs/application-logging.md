# Structured application logging

For a copyable Linux exercise from log generation through collection and duplicate
checks, follow the [JSONL walkthrough](jsonl-walkthrough.md).

## Design

A useful operational record answers six questions: when, which service, what event,
what severity, which operation, and what evidence. HostDelta keeps these separate:

| Field | Contract |
| --- | --- |
| `timestamp` | RFC 3339 with an explicit timezone; stored in UTC |
| `service.name` | Stable service identity; use the same value across runs |
| `event.name` | Stable dotted event name, such as `backup.verify.completed` |
| `level` | TRACE/DEBUG/INFO/NOTICE/WARN/WARNING/ERROR/CRITICAL/FATAL |
| `message` | A concise string for operators, not a container for arbitrary JSON |
| `trace_id`, `span_id` | Optional lowercase hexadecimal IDs: 32 and 16 characters |
| `run_id` | Optional task/job correlation |
| `attributes` | Bounded structured context; secrets are redacted recursively |
| `error` | Exception type and sanitized message; no local variables or traceback dump |

`schemas/application-log.schema.json` describes the SDK envelope. The ingester also
accepts flat JSON objects from other languages with `timestamp`, `time`, or
`@timestamp`; a service string or `service.name`; `level` or `severity`; and
`message` or `msg`. Numeric timestamps are Unix seconds. Naive timestamps, invalid
trace IDs, unknown severity names and non-string messages are rejected with a count.
OTLP resourceLogs batches and multiline JSON objects are not accepted.

## Python instrumentation

Install the wheel or source package in the application's Python environment.
A standalone zipapp is a CLI distribution, not an application import installation.

```python
import logging
from hostdelta.telemetry import JsonFormatter, StructuredLogger, bind_context

log = StructuredLogger("payments", environment="production", version="2.3.1")
with bind_context(run_id="reconcile-73", attributes={"region": "region-a"}):
    with log.operation("payment.reconcile", batch_size=120):
        log.event("payment.batch.loaded", "Batch loaded", attributes={"count": 120})
        log.request("POST", "/v1/reconcile", 202, duration_ms=31.2)
```

`operation` generates trace/span IDs, emits started/completed events with a monotonic
duration, and emits an ERROR event before re-raising an exception. An existing
trace ID is retained; nested operations get a new span ID. `bind_context` uses
`contextvars`, so context is scoped and restored, including in asyncio code.
Context is not automatically propagated into separately created OS threads.

For existing Python logging handlers:

```python
handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter("payments", environment="production"))
logger = logging.getLogger("payments")
logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.info("Settlement complete", extra={
    "event_name": "payment.settlement.completed",
    "attributes": {"settlement_count": 12}
})
```

A complete copyable version lives in
[`examples/stdlib_logging.py`](../examples/stdlib_logging.py). It attaches
`JsonFormatter` to a `logging.StreamHandler` bound to **stdout**, emits one
synthetic `inventory.stock.checked` event, and does not touch the root logger.
Run it with:

```bash
PYTHONPATH=src python3 examples/stdlib_logging.py
```

Redirect stdout to a file if you want HostDelta to collect the same envelope
from an `application_logs` path. Existing applications can reuse the handler
setup without switching to `StructuredLogger`.

Configure handler ownership and propagation in your application to avoid duplicate
records. The SDK never reconfigures the root logger.

## Collection and queries

Write one UTF-8 JSON object per newline to an application-owned regular file. Keep
credentials and request bodies out of the application log at the producer. Grant
the HostDelta service account read access to only the selected paths.

```json
{"version": 1, "application_logs": ["/var/log/payments/events.jsonl"]}
```

The reader waits for an incomplete last line, drains a renamed previous inode when
it is still available, and detects many copytruncate/regrowth cases using an offset
boundary fingerprint. Event insertion and the new offset commit atomically.
Rotations lost before collection, compressed history and undetectable inode reuse
cannot be reconstructed. Coverage warnings are persisted, not silently ignored.

```bash
hostdelta events --since 30m --service payments --severity critical --json
hostdelta events --since 2h --trace-id 0123456789abcdef0123456789abcdef --json
hostdelta brief --archive --since 1h --full
```

Only the normalized allowlisted envelope and bounded attributes are retained. Source
file identity and byte offset contribute to event IDs, so two identical messages at
different offsets remain two distinct events. The same committed offset is not
re-ingested after restart. Query windows use event time `(since, until]`; late writes
can require overlapping windows. Retention uses ingestion time for archived events.

## Redaction and limits

Sensitive key patterns include passwords, tokens, secrets, authorization, cookies,
API/private keys, credentials and request/response bodies. Common inline credential
assignments, Bearer/Basic values, JWT-shaped values and URL credentials/queries are
also sanitized. HTTP normalization allowlists method, route, status and duration.
Custom attributes remain potentially sensitive: producer-side data minimization is
required. Redaction cannot recognize every possible encoding or secret.

Attributes have depth, field, list and string limits. A line is limited to 64 KiB;
one file consumes at most 4 MiB or 5,000 accepted records per cycle. Remaining data
is read in later cycles; skipped malformed/oversized records are counted without
retaining raw content. Tune collection frequency to keep pace with your workload.
