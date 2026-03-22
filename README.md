# Celery Chain, Group & Chord Tutorial

A hands-on tutorial for Celery's three core orchestration primitives — **chain**, **group**, and **chord** — demonstrated through a real `A → (B | C) → D` DAG pipeline with retry strategies, a mock model service, and [celery-director](https://github.com/ovh/celery-director) visualization.

[Chinese version (中文版)](./README_CN.md)

## What You'll Learn

- How **chain** links tasks sequentially via callbacks embedded in messages
- How **group** dispatches tasks in parallel to different worker processes
- How **chord** (group + callback) uses a Redis atomic counter as a barrier
- Why Celery workers are **stateless** and all state lives in Redis
- Four different **retry strategies**: autoretry with backoff, manual retry, jitter, and no-retry

## The DAG

```
TASK_A ──► TASK_B ──┐
    │               ├──► TASK_D (merge)
    └────► TASK_C ──┘
```

| Task | Role | Retry Strategy |
|------|------|----------------|
| A | Call model service to preprocess text | `autoretry_for` + exponential backoff |
| B | Call model service to generate embeddings | Manual `self.retry()` with fixed delay |
| C | Call model service to classify text | `autoretry_for` + jitter (thundering herd prevention) |
| D | Aggregate results from B and C | None |

## Quick Start

```bash
git clone https://github.com/forrestIsRunning/celery-chain-group-chord-tutorial.git
cd celery-chain-group-chord-tutorial

# Start all services (Redis + Model Service + Director Web + Worker)
docker-compose up

# Wait for "celery@xxx ready" in worker logs, then trigger a workflow:
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"happy"}}'
```

**Director UI**: http://localhost:8000 | **Model Service**: http://localhost:9000/health

---

## Architecture

```
┌──────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────┐
│  Director │────►│ Redis db 0   │◄───►│  Worker(s)   │────►│  Model   │
│  Web UI   │     │ (Broker)     │     │  (Celery)    │     │  Service │
│  :8000    │     │              │     │              │     │  :9000   │
└──────────┘     │ Redis db 1   │     └──────────────┘     └──────────┘
                  │ (Backend)    │       stateless           Flask mock
                  └──────────────┘       scale horizontally  LLM API
                    source of truth
```

---

## Core Concept: What Is a Broker?

Think of Celery like a delivery platform:

| Analogy | Celery Component | Role |
|---------|-----------------|------|
| Customer places order | `apply_async()` | Submit a task to the queue |
| **Dispatch center** | **Broker (Redis db 0)** | **Receive, store, and distribute task messages** |
| Delivery driver | Worker | Pick up tasks and execute them |
| Delivery receipt | Result Backend (Redis db 1) | Store task results |

The broker is just a **message queue**. It doesn't execute any logic — it only stores messages and distributes them in order. All orchestration logic (sequencing, parallelism) is carried within the **`callbacks` field inside each message body**.

```
Producer                       Broker (Redis)                    Consumer (Worker)
                         ┌─────────────────────┐
  apply_async() ───────► │  celery queue         │ ──────────►  Worker LPOP picks task
                         │  [msg1, msg2, msg3]  │              executes, stores result
                         └─────────────────────┘

  Message format:
  {
    "task": "TASK_A",
    "id": "uuid-xxx",
    "args": [...],
    "kwargs": {"payload": {...}},
    "callbacks": [full signature of TASK_B]    ← key: next step is embedded here
  }
```

### Redis Plays Two Roles

| Redis DB | Role | What It Stores |
|----------|------|----------------|
| db 0 | Broker (message queue) | Pending task messages |
| db 1 | Result Backend | Task results + chord counters |

In production, the broker is typically RabbitMQ (stronger delivery guarantees) while the backend stays Redis (fast reads/writes).

---

## How the DAG Executes: Step by Step

### Phase 0 — Canvas Construction

When you `POST /api/workflows`, Director's `builder.py` reads `workflows.yml` and builds a Celery **Canvas**:

```python
canvas = chain(
    start.si(wf_id),              # mark workflow as started
    TASK_A.s(kwargs={payload}),   # your task A
    group(                        # B and C in parallel
        TASK_B.s(kwargs={payload}),  # ← group inside chain auto-converts to chord
        TASK_C.s(kwargs={payload}),
    ),
    TASK_D.s(kwargs={payload}),   # chord callback
    end.si(wf_id),                # mark workflow as completed
)

canvas.apply_async()
# ^ only sends the FIRST task (start) to Redis broker — not all of them
```

The three primitives:
- **`chain(A, B, C)`** — Sequential: A finishes → triggers B → triggers C
- **`group(B, C)`** — Parallel: B and C are sent to the queue simultaneously
- **`chord(group, callback)`** — Barrier: wait for all group members to finish, then trigger callback

### Phase 1 — chain: A Completes, Triggers Next Task

```
  Redis Broker (db 0)              Worker Process
  ┌───────────────────┐
  │ celery queue:      │
  │   [start]          │ ─────►  Worker picks start, executes it
  └───────────────────┘         returns None
                                  │
                                  ▼  Worker inspects the callbacks field in start's message
                                     callbacks = [TASK_A's full signature]
                                  │
                                  ▼  Worker builds TASK_A message, sends to broker
  ┌───────────────────┐
  │ celery queue:      │
  │   [TASK_A]         │ ─────►  Worker picks TASK_A(args=(None,), kwargs={payload})
  └───────────────────┘         Calls model service, returns {processed, length}
                                  │
                                  ▼  Worker writes result to Redis Backend (db 1)
                                     key: celery-task-meta-<task_a_uuid>
                                     val: {"status":"SUCCESS","result":{"processed":"HELLO WORLD",...}}
```

**There is no "event notification" system.** The worker that finished A reads the callbacks from A's own message body, constructs the next message, and pushes it to the broker. The baton is passed via messages, not events.

### Phase 2 — group: Parallel Dispatch + Chord Counter

```
                                  Worker finishes TASK_A, inspects callbacks
                                  callbacks = chord(group(B, C), callback=TASK_D)
                                  │
                                  ▼  Worker does three things:
                                     ① Sends TASK_B to broker (with A's result as args[0])
                                     ② Sends TASK_C to broker (with A's result as args[0])
                                     ③ Sets chord counter in Redis (db 1):
                                        key: chord-unlock-<group_id>
                                        val: 2  (waiting for 2 tasks)

  ┌───────────────────┐
  │ celery queue:      │
  │   [TASK_B, TASK_C] │ ─────►  Worker-8 picks TASK_B  (parallel!)
  └───────────────────┘ ─────►  Worker-9 picks TASK_C  (parallel!)
```

B and C are pushed into the queue simultaneously. Any idle worker process can pick either one. This is what "parallel" means — two independent messages consumed by different processes.

### Phase 3 — chord: Atomic Counter as a Barrier

This is the most critical part — how does Celery know both B and C are done?

```
  Worker-8 finishes TASK_B:
    ① Stores result:   celery-task-meta-<task_b_uuid>
    ② Atomic Redis op: DECR chord-unlock-<group_id>  →  returns 1
    ③ 1 ≠ 0 → not all done yet, stop here

  Worker-9 finishes TASK_C:
    ① Stores result:   celery-task-meta-<task_c_uuid>
    ② Atomic Redis op: DECR chord-unlock-<group_id>  →  returns 0
    ③ 0 == 0 → all done! Trigger chord callback:
       - Read B and C results from Redis
       - Assemble list: [B_result, C_result]
       - Send TASK_D to broker with args=([B_result, C_result],)

  ┌───────────────────┐
  │ celery queue:      │
  │   [TASK_D]         │ ─────►  Worker executes TASK_D, merges B + C results
  └───────────────────┘
```

`DECR` is a Redis atomic decrement. Even if B and C finish simultaneously on different machines, Redis guarantees only one reaches 0. No double-trigger, no missed trigger.

### Phase 4 — Completion

```
  TASK_D finishes → callback = end.si(wf_id) → marks workflow as SUCCESS
```

### Actual Timeline (From Logs)

```
12:08:02.210  [A] attempt #1 → call model → success
12:08:02.230  [B] attempt #1 → call model → success     ┐ parallel (Worker-8)
12:08:02.249  [C] attempt #1 → call model → success     ┘ parallel (Worker-9)
12:08:02.261  [D] received [B_result, C_result] → merged → done
```

---

## Are Celery Workers Stateless?

**Yes. Workers are stateless. All state lives in Redis.**

```
  ┌──────────┐       ┌──────────────────────────────────┐
  │ Worker 1  │◄────►│  Redis                            │
  │ Worker 2  │◄────►│    db 0: Broker (pending msgs)    │
  │ Worker N  │◄────►│    db 1: Backend (results+chords) │
  └──────────┘       └──────────────────────────────────┘
    stateless           stateful (single source of truth)
```

This means:
- Workers can be killed/restarted at any time without breaking the DAG
- You can scale workers horizontally (more machines) as long as they share the same Redis
- A worker doesn't remember what it has executed or know what the full DAG looks like
- Each worker does one thing: pick message → execute → store result → check callbacks → send next message

The "shape" of the DAG is never stored in any worker's memory — it's carried forward through **callback chains embedded in messages** at runtime.

---

## Test Scenarios

Control different failure modes via the `scenario` field in the payload:

### Case 1: Happy Path

```bash
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"happy"}}'
```

All tasks complete on the first attempt. No retries.

### Case 2: Flaky — Fail Twice, Succeed on Third

```bash
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"flaky"}}'
```

TASK_A hits a 500 error twice, then succeeds. Observe exponential backoff:
- Attempt 1 → fail → wait **2s**
- Attempt 2 → fail → wait **4s**
- Attempt 3 → success

### Case 3: Slow — Model Timeout

```bash
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"slow"}}'
```

Model service delays 8s, exceeding the 5s request timeout. Triggers `ReadTimeout` with retries at 2s → 4s → 8s intervals.

### Case 4: Down — Model Permanently Unavailable

```bash
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"down"}}'
```

TASK_A exhausts all 3 retries. Workflow status becomes `error`. B/C/D remain `pending`.

### Case 5: Partial Failure — B Fails, C Succeeds

```bash
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"partial_fail"}}'
```

A succeeds → B retries 3x and fails, C succeeds → chord dependency failed → `ChordError` → TASK_D never runs → workflow `error`.

---

## Inspecting Task Status & Errors

The Director REST API exposes workflow and task status at `GET /api/workflows/<workflow_id>`. Here's a real-world example — a `partial_fail` scenario where TASK_B fails while TASK_C succeeds.

### Step 1: Get the Workflow ID

When you trigger a workflow, the response includes its ID:

```bash
curl -s -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"partial_fail"}}'
```

```json
{"id": "53c7faf7-1a4b-4eff-af1e-bc3e993a7c9c", "status": "pending", ...}
```

### Step 2: Query the Workflow Detail

```bash
curl -s http://localhost:8000/api/workflows/53c7faf7-1a4b-4eff-af1e-bc3e993a7c9c
```

<details>
<summary>Full API response (click to expand)</summary>

```json
{
  "id": "53c7faf7-1a4b-4eff-af1e-bc3e993a7c9c",
  "fullname": "demo.PIPELINE",
  "status": "error",
  "payload": {"raw": "hello world", "scenario": "partial_fail"},
  "tasks": [
    {
      "id": "3a75c9e1-616f-4859-b6e4-b71f98ecd8b4",
      "key": "TASK_A",
      "status": "success",
      "previous": [],
      "result": {
        "processed": "HELLO WORLD",
        "length": 11,
        "model": "text-preprocessor-v1",
        "retries_used": 0
      }
    },
    {
      "id": "12c5fa43-f070-4004-bf13-75707ba72e1d",
      "key": "TASK_B",
      "status": "error",
      "previous": ["3a75c9e1-616f-4859-b6e4-b71f98ecd8b4"],
      "result": {
        "exception": "500 Server Error: INTERNAL SERVER ERROR for url: http://model-service:9000/v1/predict",
        "traceback": "Traceback (most recent call last):\n  File \".../trace.py\", line 453, in trace_task\n    R = retval = fun(*args, **kwargs)\n  ...\n  File \"/app/tasks/pipeline.py\", line 174, in task_b\n    raise self.retry(exc=exc, countdown=3)\n  ...\nrequests.exceptions.HTTPError: 500 Server Error: INTERNAL SERVER ERROR for url: http://model-service:9000/v1/predict\n"
      }
    },
    {
      "id": "cb4435ef-7164-4481-a1b7-a4d858d50d2c",
      "key": "TASK_C",
      "status": "success",
      "previous": ["3a75c9e1-616f-4859-b6e4-b71f98ecd8b4"],
      "result": {
        "c_result": "classified by C: positive (0.92)",
        "confidence": 0.92,
        "label": "positive",
        "model": "classifier-v1",
        "retries_used": 0
      }
    },
    {
      "id": "fcc1dfc8-2e07-4e35-9d92-71c41e225144",
      "key": "TASK_D",
      "status": "pending",
      "previous": ["12c5fa43-...", "cb4435ef-..."],
      "result": null
    }
  ]
}
```

</details>

### Step 3: Diagnose the Problem

Read the response top-down like a diagnostic checklist:

```
1. Workflow status = "error"
   → Something failed. Check individual tasks.

2. TASK_A: status = "success"
   → Preprocessing worked. retries_used = 0, so no hiccups.

3. TASK_B: status = "error"  ← root cause
   → result.exception = "500 Server Error: INTERNAL SERVER ERROR"
   → result.traceback shows:
       pipeline.py:174  raise self.retry(exc=exc, countdown=3)
       pipeline.py:162  result = call_model(...)
       pipeline.py:66   resp.raise_for_status()
   → Conclusion: Model service returned HTTP 500.
     TASK_B retried 3 times (self.retry with countdown=3),
     all attempts failed, max_retries exhausted.

4. TASK_C: status = "success"
   → Classification worked fine (different model endpoint, not affected).

5. TASK_D: status = "pending", result = null
   → Never executed. TASK_D depends on both B and C (chord).
     B failed → chord raised ChordError → D was never dispatched.
```

**Task states in Director**: `pending` → `progress` → `success` | `error` | `canceled`

### Key Fields for Debugging

| Field | What It Tells You |
|-------|-------------------|
| `status` | Current state of the task |
| `result` (success) | The dict your task returned — business data |
| `result.exception` (error) | The exception message string |
| `result.traceback` (error) | Full Python traceback — pinpoints the exact line |
| `previous` | IDs of upstream tasks — trace the dependency chain |
| `retries_used` | How many retries were consumed before success |

---

## Integrating as an AI Agent Tool

The Director API is a natural fit for an AI agent's tool belt. An agent can trigger workflows, monitor execution, and diagnose failures — all through HTTP.

### Available API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/workflows` | `POST` | Trigger a new workflow |
| `/api/workflows` | `GET` | List all workflows (with status) |
| `/api/workflows/<id>` | `GET` | Get workflow detail (tasks, results, errors) |

### Tool Definition (OpenAI-style function calling)

```json
{
  "type": "function",
  "function": {
    "name": "trigger_pipeline",
    "description": "Trigger the text processing pipeline with a given input and scenario",
    "parameters": {
      "type": "object",
      "properties": {
        "text": {"type": "string", "description": "The input text to process"},
        "scenario": {
          "type": "string",
          "enum": ["happy", "flaky", "slow", "down", "partial_fail"],
          "description": "Failure simulation mode"
        }
      },
      "required": ["text"]
    }
  }
}
```

```json
{
  "type": "function",
  "function": {
    "name": "check_workflow",
    "description": "Check the status and results of a workflow by its ID. Returns task-level status, results, and error tracebacks.",
    "parameters": {
      "type": "object",
      "properties": {
        "workflow_id": {"type": "string", "description": "The workflow UUID"}
      },
      "required": ["workflow_id"]
    }
  }
}
```

### Agent Workflow Example

```
User: "Process the text 'hello world' and tell me the result"

Agent thinking:
  1. Call trigger_pipeline(text="hello world", scenario="happy")
     → returns workflow_id = "69766e17-..."

  2. Wait a few seconds, then call check_workflow(workflow_id="69766e17-...")
     → returns { status: "success", tasks: [...] }

  3. Extract TASK_D result:
     { "final": "merge complete", "from_b": "embedding by B: dim=8", "from_c": "classified by C: positive (0.92)" }

  4. Respond to user with the merged result.
```

```
User: "Run it again but simulate a failure"

Agent thinking:
  1. Call trigger_pipeline(text="hello world", scenario="down")
     → returns workflow_id = "abc123-..."

  2. Wait, then call check_workflow(workflow_id="abc123-...")
     → returns { status: "error", tasks: [TASK_A: error, ...] }

  3. Read TASK_A.result.exception:
     "500 Server Error: INTERNAL SERVER ERROR"

  4. Respond: "The pipeline failed at TASK_A after 3 retries.
     The model service returned HTTP 500. TASK_B, C, D were never executed."
```

### Implementation Skeleton (Python)

```python
import time
import requests

DIRECTOR_URL = "http://localhost:8000/api"

def trigger_pipeline(text: str, scenario: str = "happy") -> str:
    """Trigger workflow, return workflow_id."""
    resp = requests.post(f"{DIRECTOR_URL}/workflows", json={
        "project": "demo",
        "name": "PIPELINE",
        "payload": {"raw": text, "scenario": scenario},
    })
    return resp.json()["id"]

def check_workflow(workflow_id: str) -> dict:
    """Poll workflow until terminal state, return full detail."""
    for _ in range(30):
        resp = requests.get(f"{DIRECTOR_URL}/workflows/{workflow_id}")
        data = resp.json()
        if data["status"] in ("success", "error"):
            return data
        time.sleep(1)
    return data

def diagnose(workflow: dict) -> str:
    """Turn workflow response into a human-readable diagnosis."""
    if workflow["status"] == "success":
        task_d = next(t for t in workflow["tasks"] if t["key"] == "TASK_D")
        return f"Pipeline succeeded. Result: {task_d['result']}"

    failed = [t for t in workflow["tasks"] if t["status"] == "error"]
    pending = [t for t in workflow["tasks"] if t["status"] == "pending"]
    lines = [f"Pipeline failed. {len(failed)} task(s) errored, {len(pending)} never ran."]
    for t in failed:
        lines.append(f"  {t['key']}: {t['result']['exception']}")
    return "\n".join(lines)
```

---

## Retry Strategy Comparison

| Task | Strategy | Key Parameters | Behavior |
|------|----------|----------------|----------|
| A | `autoretry_for` | `retry_backoff=2, retry_jitter=False` | Auto-retry on exception, exponential backoff 2→4→8s |
| B | `self.retry()` | `countdown=3` | Manual retry with fixed 3s interval |
| C | `autoretry_for` | `retry_backoff=2, retry_jitter=True` | Auto-retry + random jitter to prevent thundering herd |
| D | None | — | Aggregation only, no model calls |

---

## Project Structure

```
celery-chain-group-chord-tutorial/
├── docker-compose.yml          # 5 services orchestration
├── .env                        # Director configuration
├── pyproject.toml              # Python dependencies (uv)
├── workflows.yml               # DAG definition: A → (B|C) → D
├── tasks/
│   ├── __init__.py
│   └── pipeline.py             # 4 Celery tasks with retry strategies
├── model_service/
│   └── app.py                  # Mock LLM inference API (Flask)
└── static/                     # Director UI local assets (offline CDN)
```

## Tech Stack

| Component | Role |
|-----------|------|
| [Redis](https://redis.io) | Celery broker (db 0) + result backend (db 1) |
| [Celery](https://docs.celeryq.dev) | Distributed task execution |
| [celery-director](https://github.com/ovh/celery-director) | DAG definition (YAML) + web UI |
| [uv](https://github.com/astral-sh/uv) | Fast Python package installer |
| Docker Compose | One-command local setup |

## License

MIT
