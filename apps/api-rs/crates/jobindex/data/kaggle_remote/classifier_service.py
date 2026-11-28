"""Detached Kaggle service: the department and level encoders on the notebook's GPU, behind a local HTTP endpoint.

Started by jobindex.kaggle next to the Nimble decision model (which serves form filling only). The app builds each
job's input text itself (the same build_text it uses on the CPU) and sends texts; this service only runs the ONNX
models, so a job gets the same verdict on the notebook as on the local CPU.

    python classifier_service.py serve     # config JSON on stdin: {"port", "key", "deployment_id", "gpus", "models"}

POST /classify  {"model": "department"|"level", "texts": [...]}  ->  {"results": [[label, p], ...]}
GET  /health
Every request needs "Authorization: Bearer <key>". State is written to classifier_state.json beside this file.
"""
import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent
STATE = ROOT / "classifier_state.json"
BATCH = 64  # length-sorted batches per GPU call


def write_state(state):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE)


def ensure_runtime():
    """onnxruntime with CUDA (the Kaggle image has neither onnxruntime nor tokenizers' fast path guaranteed)."""
    if os.environ.get("CLASSIFIER_ALLOW_CPU"):
        return
    try:
        import onnxruntime as ort
        if "CUDAExecutionProvider" in ort.get_available_providers():
            return
    except ImportError:
        pass
    subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "-q", "onnxruntime"], check=False)
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "onnxruntime-gpu", "tokenizers"], check=True)


class Model:
    """One encoder on one GPU: its own session, tokenizer and a work queue served by one thread."""

    def __init__(self, path, gpu):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        meta = json.loads((path / "labels.json").read_text())
        self.labels = meta["labels"]
        self.tok = Tokenizer.from_file(str(path / "tokenizer.json"))
        self.tok.enable_truncation(int(meta.get("max_len", 320)))
        self.tok.no_padding()  # same set-up as the app's local runtime (level_model / department_model _runtime)
        self.tok.enable_padding(pad_id=int(meta.get("pad_id", 0)))
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        self.sess = ort.InferenceSession(str(path / meta.get("model_file", "model_w4.onnx")), opts,
                                         providers=[("CUDAExecutionProvider", {"device_id": gpu}), "CPUExecutionProvider"])
        if "CUDAExecutionProvider" not in self.sess.get_providers() and not os.environ.get("CLASSIFIER_ALLOW_CPU"):
            raise RuntimeError(f"GPU {gpu} not usable by onnxruntime")  # CLASSIFIER_ALLOW_CPU=1: local tests only
        self.work = queue.Queue()
        threading.Thread(target=self._serve, daemon=True).start()

    def run(self, texts):
        import numpy as np

        encs = self.tok.encode_batch(texts)
        ids = np.array([e.ids for e in encs], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
        logits = self.sess.run(None, {"input_ids": ids, "attention_mask": mask})[0]
        probs = np.exp(logits - logits.max(-1, keepdims=True))
        probs /= probs.sum(-1, keepdims=True)
        return [[self.labels[int(r.argmax())], round(float(r.max()), 4)] for r in probs]

    def _serve(self):
        while True:
            texts, slot, done = self.work.get()
            try:
                slot.extend(self.run(texts))
            except Exception as exc:  # noqa: BLE001
                slot.append({"error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            done.set()


class Pool:
    """Each model on every configured GPU; a request's batches are spread over the GPUs and run in parallel."""

    def __init__(self, models, gpus):
        self.replicas = {name: [Model(pathlib.Path(path), g) for g in gpus] for name, path in models.items()}
        self.rr = {name: 0 for name in models}
        self.lock = threading.Lock()

    def classify(self, name, texts):
        reps = self.replicas[name]
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        jobs = []
        for start in range(0, len(order), BATCH):
            idx = order[start:start + BATCH]
            with self.lock:
                rep = reps[self.rr[name] % len(reps)]
                self.rr[name] += 1
            slot, done = [], threading.Event()
            rep.work.put(([texts[i] for i in idx], slot, done))
            jobs.append((idx, slot, done))
        out = [None] * len(texts)
        for idx, slot, done in jobs:
            done.wait()
            if slot and isinstance(slot[0], dict):
                raise RuntimeError(slot[0]["error"])
            for i, res in zip(idx, slot):
                out[i] = res
        return out


def serve(config):
    state = {"deployment_id": config["deployment_id"], "phase": "installing", "pid": os.getpid(), "gpus": config["gpus"]}
    write_state(state)
    try:
        ensure_runtime()
        state["phase"] = "loading"; write_state(state)
        pool = Pool(config["models"], config["gpus"])
        for name in config["models"]:  # warm up: the first CUDA run compiles kernels
            pool.classify(name, ["warm up"] * 4)
        t = time.time(); pool.classify("department", ["Senior Software Engineer <eos> Acme <eos> Build APIs in Go"] * 256)
        state["jobs_per_s"] = round(256 / (time.time() - t), 1)
    except Exception as exc:  # noqa: BLE001
        state.update(phase="failed", error=f"{type(exc).__name__}: {str(exc)[:300]}"); write_state(state)
        raise
    key = config["key"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            return self.headers.get("Authorization", "") == "Bearer " + key

        def do_GET(self):
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            self._send(200, {"ok": True, "models": list(config["models"]), "gpus": config["gpus"]})

        def do_POST(self):
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                if body.get("model") not in pool.replicas:
                    return self._send(400, {"error": "unknown model"})
                self._send(200, {"results": pool.classify(body["model"], [str(t) for t in body.get("texts", [])])})
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"error": f"{type(exc).__name__}: {str(exc)[:200]}"})

    server = ThreadingHTTPServer(("127.0.0.1", int(config["port"])), Handler)
    state["phase"] = "ready"; write_state(state)
    server.serve_forever()


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "serve":
    serve(json.loads(sys.stdin.readline()))
