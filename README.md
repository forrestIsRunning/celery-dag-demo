# Celery DAG Demo

使用 [celery-director](https://github.com/ovh/celery-director) 构建的 Celery DAG 演示项目。

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
