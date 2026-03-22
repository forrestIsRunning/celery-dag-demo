# Celery Chain, Group & Chord 教程

使用 [celery-director](https://github.com/ovh/celery-director) 构建的 Celery DAG 演示项目。

[English version](./README.md)

## DAG 结构

```
A → (B | C) → D
```

- **TASK_A**: 调用模型服务预处理文本（autoretry + 指数退避）
- **TASK_B**: 调用模型服务生成 embedding（手动 self.retry）
- **TASK_C**: 调用模型服务分类文本（autoretry + jitter 防惊群）
- **TASK_D**: 聚合 B 和 C 的结果（无 retry）

## 技术栈

| 组件 | 作用 |
|------|------|
| Redis | Celery Broker + Result Backend |
| celery-director | DAG 定义 + Web 可视化 |
| Model Service | 模拟 LLM 推理 API (Flask) |
| Docker Compose | 一键启动所有服务 |

---

## 核心概念：Broker 是什么

在理解 DAG 执行流程之前，需要先搞清楚 **Broker** 的角色。

### 类比

把 Celery 想象成一个外卖平台：

| 角色 | Celery 组件 | 说明 |
|------|-------------|------|
| 顾客下单 | `apply_async()` | 提交任务到消息队列 |
| **外卖平台（派单中心）** | **Broker (Redis db 0)** | **接收订单、存储订单、分发给骑手** |
| 骑手 | Worker | 从队列取任务并执行，可横向扩展 |
| 签收回执 | Result Backend (Redis db 1) | 存储任务执行结果 |

### Broker 的具体职责

```
Producer (提交方)                Broker (Redis)                Consumer (Worker)
                          ┌─────────────────────┐
  apply_async() ────────► │  celery 队列          │ ────────► Worker 进程 LPOP 取任务
                          │  [msg1, msg2, msg3]  │           执行后把结果写入 Backend
                          └─────────────────────┘

  消息格式:
  {
    "task": "TASK_A",
    "id": "uuid-xxx",
    "args": [...],
    "kwargs": {"payload": {...}},
    "callbacks": [TASK_B 的完整签名]    ← 关键: 下一步指令嵌在消息里
  }
```

核心理解：**Broker 就是一个消息队列**。它不执行任何逻辑，只负责「存消息」和「按顺序分发消息」。所有编排逻辑（谁先谁后、并行还是串行）都通过消息体中的 `callbacks` 字段实现。

### Redis 的两个角色

本项目中 Redis 同时担任两个角色（用不同的 db 隔离）：

| Redis DB | 角色 | 存什么 |
|----------|------|--------|
| db 0 | Broker（消息队列） | 待执行的任务消息 |
| db 1 | Result Backend（结果存储） | 任务执行结果 + chord 计数器 |

生产环境中 Broker 通常用 RabbitMQ（更强的消息保证），Backend 用 Redis（快速读写结果）。

---

## DAG 执行全流程

### Phase 0: 构建阶段

当你发起 `curl POST /api/workflows` 时，Director 的 `builder.py` 读取 `workflows.yml`，构建出一个 Celery **Canvas**（画布）对象：

```python
# builder.py 实际构建的结构
canvas = chain(
    start.si(wf_id),              # 标记 workflow 开始
    TASK_A.s(kwargs={payload}),   # 你的任务 A
    group(                        # B 和 C 并行 ← chain 中的 group 自动变为 chord
        TASK_B.s(kwargs={payload}),
        TASK_C.s(kwargs={payload}),
    ),
    TASK_D.s(kwargs={payload}),   # chord 的 callback
    end.si(wf_id),                # 标记 workflow 结束
)

canvas.apply_async()
# ↑ 只发送第一个任务 start 到 Redis broker，不是全部
```

三个关键原语：
- **`chain(A, B, C)`** — 串行：A 完了自动执行 B，B 完了自动执行 C
- **`group(B, C)`** — 并行：B 和 C 同时发到队列，被不同 Worker 取走
- **`chord(group, callback)`** — 屏障：等 group 全部完成后触发 callback。chain 中 group 后面跟 task 时自动转为 chord

### Phase 1: start → TASK_A（chain 的 callback 机制）

```
  Redis Broker (db 0)              Worker 进程
  ┌───────────────────┐
  │ celery 队列:       │
  │   [start]          │ ────►  Worker 取出 start，执行
  └───────────────────┘        start 返回 None
                                 │
                                 ▼  Worker 检查 start 消息体中的 callbacks 字段
                                    callbacks = [TASK_A 的完整签名]
                                 │
                                 ▼  Worker 构造 TASK_A 消息，发送到 broker
  ┌───────────────────┐
  │ celery 队列:       │
  │   [TASK_A]         │ ────►  Worker 取出 TASK_A(args=(None,), kwargs={payload})
  └───────────────────┘        TASK_A 调用模型服务，返回 {processed, length}
                                 │
                                 ▼  Worker 把结果写入 Redis Result Backend (db 1)
                                    key:  celery-task-meta-<task_a_uuid>
                                    val:  {"status":"SUCCESS","result":{"processed":"HELLO WORLD",...}}
```

**没有"通知"机制**。执行完 A 的那个 Worker 直接从 A 的消息体中取出 callback（即 B/C 的签名），构造新消息发到 broker。接力棒通过消息传递，不是通过事件。

### Phase 2: TASK_A → group(B, C)（group 展开 + chord 计数器）

```
                                 Worker 完成 TASK_A 后检查 callbacks
                                 callbacks = chord(group(B, C), callback=TASK_D)
                                 │
                                 ▼  Worker 做了三件事:
                                    ① 发送 TASK_B 到 broker（带 A 的结果作为 args[0]）
                                    ② 发送 TASK_C 到 broker（带 A 的结果作为 args[0]）
                                    ③ 在 Redis (db 1) 中设置 chord 计数器:
                                       key: chord-unlock-<group_id>
                                       val: 2（等待 2 个任务完成）

  ┌───────────────────┐
  │ celery 队列:       │
  │   [TASK_B, TASK_C] │ ────►  Worker-8 取出 TASK_B 执行（并行!）
  └───────────────────┘ ────►  Worker-9 取出 TASK_C 执行（并行!）
```

B 和 C 被同时推入队列。它们不需要互相等待，任何空闲 Worker 进程都可以取走执行。这就是「并行」的本质 — 两条独立的消息在队列里，被不同进程消费。

### Phase 3: group(B, C) → TASK_D（chord 屏障：原子计数器）

这是最关键的部分 — B 和 C 都完成后如何触发 D？

```
  Worker-8 完成 TASK_B:
    ① 存结果到 Redis:  celery-task-meta-<task_b_uuid>
    ② Redis 原子操作:  DECR chord-unlock-<group_id>  →  返回 1
    ③ 1 ≠ 0，还有任务没完成，不触发 callback，结束

  Worker-9 完成 TASK_C:
    ① 存结果到 Redis:  celery-task-meta-<task_c_uuid>
    ② Redis 原子操作:  DECR chord-unlock-<group_id>  →  返回 0
    ③ 0 == 0，全部完成！触发 chord callback:
       - 从 Redis 读取 B 和 C 的结果
       - 组装成列表 [B_result, C_result]
       - 发送 TASK_D 到 broker，args=([B_result, C_result],)

  ┌───────────────────┐
  │ celery 队列:       │
  │   [TASK_D]         │ ────►  Worker 执行 TASK_D，聚合 B 和 C 的结果
  └───────────────────┘
```

`DECR` 是 Redis 的原子递减操作。即使 B 和 C 在不同机器上的不同 Worker 中同时完成，Redis 也能保证只有一个减到 0，只有一个 Worker 会触发 TASK_D。不会重复触发，也不会遗漏。

### Phase 4: TASK_D → end

```
  TASK_D 完成后 → callback = end.si(wf_id) → 标记 workflow 为 SUCCESS
```

### 完整时间线（实际日志）

```
12:08:02.210  [A] 第 1 次尝试 → 调用模型 → 成功
12:08:02.230  [B] 第 1 次尝试 → 调用模型 → 成功     ┐ 并行 (Worker-8)
12:08:02.249  [C] 第 1 次尝试 → 调用模型 → 成功     ┘ 并行 (Worker-9)
12:08:02.261  [D] 收到 [B结果, C结果] → 聚合 → 完成
```

---

## Celery 是无状态的吗

**Worker 进程是无状态的，状态全在 Redis 里。**

```
  ┌──────────┐       ┌───────────────────────────────┐
  │ Worker 1  │◄────►│  Redis                         │
  │ Worker 2  │◄────►│    db 0: Broker (待执行的消息)  │
  │ Worker N  │◄────►│    db 1: Backend (结果+计数器)  │
  └──────────┘       └───────────────────────────────┘
    无状态                有状态（唯一的 source of truth）
```

这意味着：
- Worker 随时可以被杀掉、重启，不影响 DAG 整体执行
- 可以横向扩展 Worker 数量（多台机器），只要都连同一个 Redis
- Worker 不记录「我执行过什么」，也不知道「整个 DAG 长什么样」
- 每个 Worker 只做一件事：从队列取消息 → 执行 → 存结果 → 检查 callbacks → 发下一条消息

DAG 的「形状」不在任何 Worker 的内存里，而是通过**消息体中的 callbacks 链表**在运行时传递。

---

## 快速开始

```bash
# 启动所有服务 (Redis + Model Service + Director Web + Worker)
docker-compose up

# 等待 worker 日志显示 "celery@xxx ready" 后触发 workflow
```

## 测试场景

通过 payload 中的 `scenario` 参数控制不同的测试 case：

### Case 1: Happy Path — 全部成功

```bash
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"happy"}}'
```

所有任务正常完成，无 retry。

### Case 2: Flaky — 模型前 2 次失败，第 3 次成功

```bash
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"flaky"}}'
```

TASK_A 使用 `autoretry_for` + `retry_backoff=2`，可以观察到：
- 第 1 次尝试 → 失败 → 等 2s
- 第 2 次尝试 → 失败 → 等 4s
- 第 3 次尝试 → 成功

### Case 3: Slow — 模型慢响应触发超时

```bash
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"slow"}}'
```

模型服务延迟 8 秒，超过 requests timeout=5s，触发 `ReadTimeout` 后自动重试。重试间隔：2s → 4s → 8s。

### Case 4: Down — 模型完全不可用

```bash
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"down"}}'
```

TASK_A 重试 3 次后彻底失败，workflow 状态变为 `error`。

### Case 5: Partial Fail — B 失败 C 成功

```bash
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"partial_fail"}}'
```

A 成功 → B 重试 3 次后失败、C 成功 → chord 中有依赖失败 → 抛出 `ChordError` → TASK_D 不执行 → workflow `error`。

---

## 查看任务状态和错误信息

Director REST API 提供了 `GET /api/workflows/<workflow_id>` 端点来查询 workflow 和 task 状态。以下用一个真实的 `partial_fail` 场景（TASK_B 失败、TASK_C 成功）来演示如何定位问题。

### 第一步：获取 Workflow ID

触发 workflow 时，响应中包含其 ID：

```bash
curl -s -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{"project":"demo","name":"PIPELINE","payload":{"raw":"hello world","scenario":"partial_fail"}}'
```

```json
{"id": "53c7faf7-1a4b-4eff-af1e-bc3e993a7c9c", "status": "pending", ...}
```

### 第二步：查询 Workflow 详情

```bash
curl -s http://localhost:8000/api/workflows/53c7faf7-1a4b-4eff-af1e-bc3e993a7c9c
```

<details>
<summary>完整 API 响应（点击展开）</summary>

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

### 第三步：定位问题

从上往下逐个 task 检查，像诊断清单一样：

```
1. Workflow status = "error"
   → 有任务失败了，逐个检查。

2. TASK_A: status = "success"
   → 预处理正常。retries_used = 0，首次就成功了。

3. TASK_B: status = "error"  ← 根因在这里
   → result.exception = "500 Server Error: INTERNAL SERVER ERROR"
   → result.traceback 显示:
       pipeline.py:174  raise self.retry(exc=exc, countdown=3)
       pipeline.py:162  result = call_model(...)
       pipeline.py:66   resp.raise_for_status()
   → 结论: 模型服务返回了 HTTP 500。
     TASK_B 用 self.retry(countdown=3) 重试了 3 次，
     全部失败，max_retries 耗尽。

4. TASK_C: status = "success"
   → 分类正常（调的是不同的模型端点，不受影响）。

5. TASK_D: status = "pending", result = null
   → 从未执行。TASK_D 依赖 B 和 C（chord 机制）。
     B 失败 → chord 抛出 ChordError → D 永远不会被调度。
```

**Director 中任务的状态**: `pending` → `progress` → `success` | `error` | `canceled`

### 调试关键字段

| 字段 | 含义 |
|------|------|
| `status` | 任务当前状态 |
| `result`（成功时） | 你的 task 函数的返回值 — 业务数据 |
| `result.exception`（失败时） | 异常消息字符串 |
| `result.traceback`（失败时） | 完整 Python 堆栈 — 精确到代码行号 |
| `previous` | 上游任务的 ID 列表 — 追踪依赖链 |
| `retries_used` | 成功前消耗了多少次重试 |

---

## 作为 AI Agent Tool 接入

Director API 天然适合作为 AI Agent 的工具。Agent 可以通过 HTTP 触发 workflow、监控执行、诊断错误。

### 可用 API 端点

| 端点 | 方法 | 用途 |
|------|------|------|
| `/api/workflows` | `POST` | 触发新的 workflow |
| `/api/workflows` | `GET` | 列出所有 workflow（含状态） |
| `/api/workflows/<id>` | `GET` | 获取 workflow 详情（任务、结果、错误） |

### Tool 定义（OpenAI function calling 格式）

```json
{
  "type": "function",
  "function": {
    "name": "trigger_pipeline",
    "description": "触发文本处理流水线",
    "parameters": {
      "type": "object",
      "properties": {
        "text": {"type": "string", "description": "输入文本"},
        "scenario": {
          "type": "string",
          "enum": ["happy", "flaky", "slow", "down", "partial_fail"],
          "description": "失败模拟模式"
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
    "description": "按 ID 查询 workflow 的状态和结果，返回每个 task 的状态、返回值和错误堆栈",
    "parameters": {
      "type": "object",
      "properties": {
        "workflow_id": {"type": "string", "description": "Workflow UUID"}
      },
      "required": ["workflow_id"]
    }
  }
}
```

### Agent 调用示例

```
用户: "处理文本 'hello world'，告诉我结果"

Agent 思考:
  1. 调用 trigger_pipeline(text="hello world", scenario="happy")
     → 返回 workflow_id = "69766e17-..."

  2. 等几秒后调用 check_workflow(workflow_id="69766e17-...")
     → 返回 { status: "success", tasks: [...] }

  3. 提取 TASK_D 的结果:
     { "final": "merge complete", "from_b": "embedding by B: dim=8", "from_c": "classified by C: positive (0.92)" }

  4. 回复用户: "处理完成。embedding 维度为 8，分类结果为 positive（置信度 0.92）。"
```

```
用户: "再跑一次，模拟故障"

Agent 思考:
  1. 调用 trigger_pipeline(text="hello world", scenario="down")
     → 返回 workflow_id = "abc123-..."

  2. 等待后调用 check_workflow(workflow_id="abc123-...")
     → 返回 { status: "error", tasks: [TASK_A: error, ...] }

  3. 读取 TASK_A.result.exception:
     "500 Server Error: INTERNAL SERVER ERROR"

  4. 回复: "流水线在 TASK_A 阶段失败，重试 3 次后仍然报错。
     模型服务返回了 HTTP 500。TASK_B、C、D 均未执行。"
```

### 实现骨架（Python）

```python
import time
import requests

DIRECTOR_URL = "http://localhost:8000/api"

def trigger_pipeline(text: str, scenario: str = "happy") -> str:
    """触发 workflow，返回 workflow_id。"""
    resp = requests.post(f"{DIRECTOR_URL}/workflows", json={
        "project": "demo",
        "name": "PIPELINE",
        "payload": {"raw": text, "scenario": scenario},
    })
    return resp.json()["id"]

def check_workflow(workflow_id: str) -> dict:
    """轮询 workflow 直到终态，返回完整详情。"""
    for _ in range(30):
        resp = requests.get(f"{DIRECTOR_URL}/workflows/{workflow_id}")
        data = resp.json()
        if data["status"] in ("success", "error"):
            return data
        time.sleep(1)
    return data

def diagnose(workflow: dict) -> str:
    """把 workflow 响应转为可读的诊断报告。"""
    if workflow["status"] == "success":
        task_d = next(t for t in workflow["tasks"] if t["key"] == "TASK_D")
        return f"流水线成功。结果: {task_d['result']}"

    failed = [t for t in workflow["tasks"] if t["status"] == "error"]
    pending = [t for t in workflow["tasks"] if t["status"] == "pending"]
    lines = [f"流水线失败。{len(failed)} 个任务出错，{len(pending)} 个未执行。"]
    for t in failed:
        lines.append(f"  {t['key']}: {t['result']['exception']}")
    return "\n".join(lines)
```

---

## Retry 策略对比

| Task | 策略 | 关键参数 | 说明 |
|------|------|----------|------|
| A | `autoretry_for` | `retry_backoff=2, retry_jitter=False` | 遇异常自动重试，指数退避 2→4→8s |
| B | `self.retry()` | `countdown=3` | 手动控制重试，固定间隔 3s |
| C | `autoretry_for` | `retry_backoff=2, retry_jitter=True` | 自动重试 + 随机抖动防惊群 |
| D | 无 | — | 纯聚合任务 |

## 查看结果

- **Director UI**: http://localhost:8000
- **Model Service**: http://localhost:9000/health
- **Worker 日志**: `docker-compose logs -f worker`
- **查看 workflow 状态**: `curl http://localhost:8000/api/workflows`

## 项目结构

```
celery-dag-demo/
├── docker-compose.yml          # 5 个服务编排
├── .env                        # Director 配置
├── pyproject.toml              # Python 依赖 (uv)
├── workflows.yml               # DAG 定义: A → (B|C) → D
├── tasks/
│   ├── __init__.py
│   └── pipeline.py             # 4 个 Celery 任务 + retry 策略
├── model_service/
│   └── app.py                  # Mock 模型推理 API
└── static/                     # Director UI 本地静态资源
```
