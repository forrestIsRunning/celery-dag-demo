"""
Mock Model Service — 模拟真实 LLM/模型推理 API

功能：
  POST /v1/predict
  - task_type: preprocess | embed | classify | merge
  - request_id: 用于追踪同一个 Celery task 的重试次数
  - fail_times: 前 N 次请求返回 500，之后成功（模拟 flaky 服务）
  - delay_seconds: 额外延迟（模拟慢推理）

  GET /v1/stats
  - 查看当前请求计数器（调试用）

  POST /v1/reset
  - 重置计数器
"""

import time
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)

# 线程安全的请求计数器: {request_id: call_count}
_counter_lock = threading.Lock()
_counters: dict[str, int] = {}


def get_call_count(request_id: str) -> int:
    """获取并递增某个 request_id 的调用次数，返回当前次数（从 1 开始）"""
    with _counter_lock:
        _counters[request_id] = _counters.get(request_id, 0) + 1
        return _counters[request_id]


# ── 模型推理结果模拟 ──────────────────────────────────────
def mock_predict(text: str, task_type: str) -> dict:
    """根据 task_type 返回不同的模拟推理结果"""
    if task_type == "preprocess":
        return {
            "model": "text-preprocessor-v1",
            "result": {
                "processed": text.upper(),
                "tokens": text.split(),
                "length": len(text),
            },
        }
    elif task_type == "embed":
        # 模拟 embedding 向量（简化为长度相关的 fake vector）
        return {
            "model": "embedding-v1",
            "result": {
                "embedding": [round(len(text) * 0.1 + i * 0.01, 4) for i in range(8)],
                "dimension": 8,
            },
        }
    elif task_type == "classify":
        # 模拟文本分类
        return {
            "model": "classifier-v1",
            "result": {
                "label": "positive" if len(text) > 5 else "neutral",
                "confidence": 0.92,
                "categories": ["positive", "neutral", "negative"],
            },
        }
    elif task_type == "merge":
        return {
            "model": "aggregator-v1",
            "result": {
                "summary": f"Merged analysis for: {text[:50]}",
                "status": "complete",
            },
        }
    else:
        return {"model": "unknown", "result": {"raw": text}}


# ── API Endpoints ─────────────────────────────────────────
@app.route("/v1/predict", methods=["POST"])
def predict():
    data = request.get_json(force=True)
    text = data.get("text", "")
    task_type = data.get("task_type", "preprocess")
    request_id = data.get("request_id", "anonymous")
    fail_times = data.get("fail_times", 0)
    delay_seconds = data.get("delay_seconds", 0)

    # 计数当前 request_id 的调用次数
    call_count = get_call_count(request_id)

    app.logger.info(
        f"[Model] request_id={request_id} task_type={task_type} "
        f"call#{call_count} fail_times={fail_times} delay={delay_seconds}s"
    )

    # 模拟延迟（慢推理）
    if delay_seconds > 0:
        app.logger.info(f"[Model] Sleeping {delay_seconds}s to simulate slow inference...")
        time.sleep(delay_seconds)

    # 模拟前 N 次失败
    if call_count <= fail_times:
        app.logger.warning(
            f"[Model] FAIL #{call_count}/{fail_times} for request_id={request_id}"
        )
        return jsonify({
            "error": "Model inference failed",
            "detail": f"Simulated failure #{call_count} of {fail_times}",
            "request_id": request_id,
        }), 500

    # 正常推理
    result = mock_predict(text, task_type)
    result["request_id"] = request_id
    result["call_count"] = call_count

    app.logger.info(f"[Model] SUCCESS for request_id={request_id} on call#{call_count}")
    return jsonify(result), 200


@app.route("/v1/stats", methods=["GET"])
def stats():
    """查看请求计数器（调试用）"""
    with _counter_lock:
        return jsonify(dict(_counters)), 200


@app.route("/v1/reset", methods=["POST"])
def reset():
    """重置计数器"""
    with _counter_lock:
        _counters.clear()
    return jsonify({"status": "reset"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200
