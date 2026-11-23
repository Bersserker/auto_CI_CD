"""Воспроизводимый closed-loop benchmark PyTorch FP32, ONNX FP32 и ONNX INT8.

CPU-профили ограничивают affinity на одном хосте и не заменяют разные инстансы.
GPU проверяется без скрытого fallback на CPU. Запуск: make benchmark-nn.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import itertools
import json
import math
import os
from pathlib import Path
import platform
import shlex
import shutil
import statistics
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time

from src.config import (
    BEST_NN_ONNX_PATH,
    BEST_NN_PATH,
    BEST_NN_SCALER_PATH,
    MODELS_DIR,
    PROCESSED_DATA_PATH,
    REPORTS_DIR,
)

ARTIFACTS = {
    "pytorch_fp32": BEST_NN_PATH,
    "onnx_fp32": BEST_NN_ONNX_PATH,
    "onnx_int8": MODELS_DIR / "credit_default_model_quant_dyn.onnx",
}


class Unavailable(RuntimeError):
    """Оборудование или backend не поддерживает этот режим."""


def load_data():
    import joblib
    import numpy as np
    import pandas as pd
    from sklearn.model_selection import train_test_split

    # Сохраняем текущий контракт train_nn.py, включая порядок колонок и ID.
    frame = pd.read_csv(PROCESSED_DATA_PATH).drop(columns=["id"])
    x = frame.drop(columns=["default"]).to_numpy(dtype=np.float32)
    y = frame["default"].to_numpy()
    _, test, _, _ = train_test_split(x, y, test_size=0.2, random_state=42, stratify=y)
    scaler = joblib.load(BEST_NN_SCALER_PATH)
    if not np.isfinite(test).all():
        raise ValueError("Неконечные входные данные")
    return test, scaler


def make_runner(model_name, device, threads, n_features):
    """Вход/выход на CPU; GPU-копирование включено в время запроса."""
    import numpy as np

    if model_name == "pytorch_fp32":
        import torch

        from src.neural.neural_network import CreditDefaultMLP

        torch.set_num_threads(threads)
        if device == "cuda" and not torch.cuda.is_available():
            raise Unavailable("PyTorch: CUDA недоступна (оборудование/драйвер)")
        # FP32 без TF32 для сравнения с FP32 ONNX.
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        model = CreditDefaultMLP(n_features)
        model.load_state_dict(
            torch.load(ARTIFACTS[model_name], map_location="cpu", weights_only=True)
        )
        model.to(device).eval()

        @torch.inference_mode()
        def run(sample):
            result = torch.sigmoid(model(torch.from_numpy(sample).to(device)))
            return result.cpu().numpy()  # синхронное ожидание результата CUDA

        return run, {"backend": "torch", "device": device}

    if device == "cuda":
        import torch

        if not torch.cuda.is_available():
            raise Unavailable("CUDA недоступна: драйвер или ограничения среды")
    import onnxruntime as ort

    if (
        device == "cuda"
        and "CUDAExecutionProvider" not in ort.get_available_providers()
    ):
        raise Unavailable("ONNX Runtime: CUDAExecutionProvider не установлен")
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    provider = "CUDAExecutionProvider" if device == "cuda" else "CPUExecutionProvider"
    providers = [(provider, {"use_tf32": "0"})] if device == "cuda" else [provider]
    placement = {}
    if device == "cuda":
        # CUDA EP может выполнять Shape на CPU. Проверяем фактическое размещение
        # вычислительных узлов профилировщиком, а не только список providers.
        options.enable_profiling = True
        with TemporaryDirectory(prefix="ort-placement-") as tmp:
            options.profile_file_prefix = str(Path(tmp) / "profile")
            try:
                probe = ort.InferenceSession(
                    str(ARTIFACTS[model_name]), options, providers=providers
                )
                probe.disable_fallback()
                probe.run(
                    None, {"features": np.zeros((2, n_features), dtype=np.float32)}
                )
                events = json.loads(Path(probe.end_profiling()).read_text())
                del probe
            except Exception as exc:
                raise Unavailable(f"CUDA session недоступна: {exc}") from exc
        nodes = [
            e.get("args", {})
            for e in events
            if e.get("cat") == "Node" and e.get("args", {}).get("provider")
        ]
        gpu_nodes = [n for n in nodes if n["provider"] == "CUDAExecutionProvider"]
        cpu_ops = sorted(
            {
                n.get("op_name", "unknown")
                for n in nodes
                if n["provider"] == "CPUExecutionProvider"
            }
        )
        shape_ops = {
            "Shape",
            "Size",
            "Gather",
            "Concat",
            "Squeeze",
            "Unsqueeze",
            "Reshape",
        }
        if not gpu_nodes or set(cpu_ops) - shape_ops:
            raise Unavailable(
                f"Нет чистого GPU-инференса: CPU operators={cpu_ops}; "
                f"CUDA nodes={len(gpu_nodes)}"
            )
        placement = {"cuda_nodes": len(gpu_nodes), "cpu_metadata_ops": cpu_ops}
        options.enable_profiling = False
    session = ort.InferenceSession(
        str(ARTIFACTS[model_name]), options, providers=providers
    )
    session.disable_fallback()
    if provider not in session.get_providers():
        raise Unavailable(f"Запрошенный provider {provider} не активен")

    def run(sample):
        return session.run(["default_probability"], {"features": sample})[0]

    result = run(np.zeros((1, n_features), dtype=np.float32))
    if result.shape != (1, 1):
        raise ValueError("Неправильный контракт выхода")
    return run, {
        "backend": "onnxruntime",
        "providers": session.get_providers(),
        "placement": placement,
    }


def measure(run, scaler, pool, concurrency, duration, warmup, cpus, device="cpu"):
    import numpy as np
    import psutil

    def request(sample):
        scaled = scaler.transform(sample).astype(np.float32)
        return run(scaled)

    for i in range(warmup):
        result = request(pool[i % len(pool)])
        if not np.isfinite(result).all():
            raise ValueError("Предсказания содержат NaN/inf")
    process = psutil.Process()
    rss = [process.memory_info().rss]
    stopped = threading.Event()
    gpu_samples = []

    def monitor_gpu():
        while not stopped.is_set():
            try:
                probe = subprocess.run(
                    [
                        "nvidia-smi",
                        "--id=0",
                        "--query-gpu=utilization.gpu,memory.used,power.draw",
                        "--format=csv,noheader,nounits",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
                if probe.returncode == 0:
                    gpu_samples.append(
                        [float(v) for v in probe.stdout.strip().split(",")]
                    )
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            stopped.wait(0.2)

    gpu_sampler = None
    if device == "cuda" and shutil.which("nvidia-smi"):
        gpu_sampler = threading.Thread(target=monitor_gpu, daemon=True)
        gpu_sampler.start()

    def monitor():
        while not stopped.wait(0.02):
            rss.append(process.memory_info().rss)

    sampler = threading.Thread(target=monitor, daemon=True)
    sampler.start()
    timing = {}

    def start_clock():
        timing["start"] = time.perf_counter()
        timing["cpu"] = sum(process.cpu_times()[:2])

    barrier = threading.Barrier(concurrency, action=start_clock)

    def client(client_id):
        latencies = []
        i = client_id
        barrier.wait()
        while time.perf_counter() - timing["start"] < duration:
            sample = pool[i % len(pool)]
            begin = time.perf_counter_ns()
            request(sample)
            latencies.append((time.perf_counter_ns() - begin) / 1e6)
            i += concurrency
        return latencies, time.perf_counter()

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            results = list(executor.map(client, range(concurrency)))
        wall = max(end for _, end in results) - timing["start"]
        cpu_seconds = sum(process.cpu_times()[:2]) - timing["cpu"]
    finally:
        stopped.set()
        sampler.join()
        if gpu_sampler is not None:
            gpu_sampler.join()
    rss.append(process.memory_info().rss)
    latency = np.concatenate([values for values, _ in results])
    if len(latency) == 0:
        raise ValueError("Нет измеренных запросов")
    return {
        "requests": len(latency),
        "wall_seconds": wall,
        "p50_ms": float(np.percentile(latency, 50)),
        "p95_ms": float(np.percentile(latency, 95)),
        "p99_ms": float(np.percentile(latency, 99)),
        "requests_per_second": len(latency) / wall,
        "samples_per_second": len(latency) * len(pool[0]) / wall,
        "cpu_percent": cpu_seconds / wall * 100,
        "allocated_cpu_percent": cpu_seconds / wall / cpus * 100,
        "rss_peak_mib": max(rss) / 2**20,
        "gpu_util_mean_percent": (
            statistics.mean(v[0] for v in gpu_samples) if gpu_samples else None
        ),
        "gpu_device_memory_peak_mib": (
            max(v[1] for v in gpu_samples) if gpu_samples else None
        ),
        "gpu_power_mean_watts": (
            statistics.mean(v[2] for v in gpu_samples) if gpu_samples else None
        ),
    }


def worker(config):
    # Affinity задаётся до импорта вычислительных библиотек.
    available = sorted(os.sched_getaffinity(0))
    selected = available[: config["cores"]]
    os.sched_setaffinity(0, selected)
    import numpy as np

    data, scaler = load_data()
    rows = []
    cases = list(itertools.product(config["batches"], config["concurrency"]))
    for batch, concurrency in cases:
        threads = max(1, config["cores"] // concurrency)
        try:
            run, backend = make_runner(
                config["model"], config["device"], threads, scaler.n_features_in_
            )
        except Unavailable as exc:
            return {"status": "skipped", "reason": str(exc), "config": config}
        pool = [
            np.ascontiguousarray(data[(np.arange(batch) + i * batch) % len(data)])
            for i in range(16)
        ]
        for repeat in range(config["repeats"]):
            result = measure(
                run,
                scaler,
                pool,
                concurrency,
                config["seconds"],
                config["warmup"],
                config["cores"],
                config["device"],
            )
            rows.append(
                {
                    **result,
                    **backend,
                    "model": config["model"],
                    "device": config["device"],
                    "cores": config["cores"],
                    "affinity": selected,
                    "threads": threads,
                    "batch": batch,
                    "concurrency": concurrency,
                    "repeat": repeat + 1,
                }
            )
        del run
    return {"status": "ok", "rows": rows}


def metadata():
    import psutil

    versions = {}
    for name in ("torch", "onnx", "onnxruntime", "numpy", "scikit-learn", "psutil"):
        versions[name] = importlib.metadata.version(name)
    for name in ("onnxruntime-gpu", "nvidia-curand"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    cpu = platform.processor()
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("model name"):
            cpu = line.split(":", 1)[1].strip()
            break
    gpu = (
        subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,memory.total",
                "--format=csv",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if shutil.which("nvidia-smi")
        else None
    )
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "host": platform.node(),
        "os": platform.platform(),
        "python": platform.python_version(),
        "cpu": cpu,
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cpus": psutil.cpu_count(),
        "affinity": sorted(os.sched_getaffinity(0)),
        "ram_gib": psutil.virtual_memory().total / 2**30,
        "gpu_probe": (
            (gpu.stdout or gpu.stderr).strip() if gpu else "nvidia-smi отсутствует"
        ),
        "versions": versions,
        "onnxruntime_module": importlib.util.find_spec("onnxruntime").origin,
        "cpu_topology": subprocess.run(
            ["lscpu", "-p=CPU,CORE,SOCKET"], capture_output=True, text=True, check=False
        ).stdout,
        "artifacts": {
            name: {
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for name, path in {
                **ARTIFACTS,
                "scaler": BEST_NN_SCALER_PATH,
                "data": PROCESSED_DATA_PATH,
            }.items()
        },
    }


def summarize(rows):
    groups = {}
    for row in rows:
        key = tuple(
            row[k] for k in ("model", "device", "cores", "batch", "concurrency")
        )
        groups.setdefault(key, []).append(row)
    summaries = []
    for group in groups.values():
        first = group[0]
        row = {
            k: first[k]
            for k in ("model", "device", "cores", "threads", "batch", "concurrency")
        }
        for metric in (
            "p50_ms",
            "p95_ms",
            "p99_ms",
            "requests_per_second",
            "samples_per_second",
            "cpu_percent",
            "allocated_cpu_percent",
        ):
            row[metric] = statistics.median(r[metric] for r in group)
        row["rss_peak_mib"] = max(r["rss_peak_mib"] for r in group)
        for metric in (
            "gpu_util_mean_percent",
            "gpu_device_memory_peak_mib",
            "gpu_power_mean_watts",
        ):
            values = [r.get(metric) for r in group if r.get(metric) is not None]
            row[metric] = (
                (
                    max(values)
                    if metric == "gpu_device_memory_peak_mib"
                    else statistics.median(values)
                )
                if values
                else None
            )
        row["rps_min"] = min(r["requests_per_second"] for r in group)
        row["rps_max"] = max(r["requests_per_second"] for r in group)
        row["worst_p95_ms"] = max(r["p95_ms"] for r in group)
        summaries.append(row)
    return summaries


def plot_results(report, path):
    """Сравнение одиночных запросов; неподдерживаемые режимы без столбца."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    rows = [r for r in report["summary"] if r["batch"] == 1 and r["concurrency"] == 1]
    if not rows:
        return
    profiles = sorted({(r["device"], r["cores"]) for r in rows})
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5), layout="constrained")
    for offset, model in enumerate(ARTIFACTS):
        x = np.arange(len(profiles)) + (offset - 1) * 0.25
        selected = {(r["device"], r["cores"]): r for r in rows if r["model"] == model}
        for axis, metric in zip(axes, ("p95_ms", "requests_per_second")):
            values = [
                selected[p][metric] if p in selected else np.nan for p in profiles
            ]
            axis.bar(x, values, width=0.25, label=model)
    for axis in axes:
        axis.set_xticks(
            np.arange(len(profiles)),
            [f"{d.upper()} / {c} logical CPU" for d, c in profiles],
            rotation=15,
        )
        axis.grid(axis="y", alpha=0.25)
        axis.set_axisbelow(True)
    axes[0].set_ylabel("p95 latency, ms (lower is better)")
    axes[1].set_ylabel("Requests/s (higher is better)")
    axes[0].legend(fontsize=8)
    fig.suptitle("Batch=1, concurrency=1; preprocessing + inference + CPU output")
    fig.savefig(path, dpi=150)
    plt.close(fig)


def write_report(report, path):
    env, args = report["environment"], report["parameters"]
    lines = [
        "# Benchmark нейромодели",
        "",
        f"Дата UTC: {env['timestamp_utc']}. Хост: `{env['host']}`.",
        "",
        "## Оборудование и ПО",
        "",
        f"CPU: {env['cpu']}; физических ядер: {env['physical_cores']}; "
        f"логических CPU: {env['logical_cpus']}; RAM: {env['ram_gib']:.1f} GiB.",
        f"ОС: {env['os']}; Python: {env['python']}.",
        "",
        "Версии: " + ", ".join(f"{k}={v}" for k, v in env["versions"].items()),
        "",
        "GPU probe:",
        "```text",
        env["gpu_probe"],
        "```",
        "",
        "## Методика",
        "",
        "Каждый backend/CPU-профиль запускается в отдельном процессе. CPU-профили "
        "ограничены affinity и являются локальными конфигурациями одного хоста, "
        "а не замерами разных облачных инстансов. Число CPU обозначает логические CPU.",
        "Используются реальные строки test-разбиения (20%, stratify, seed=42), "
        "тот же порядок признаков и сохранённый StandardScaler. На запрос выполняются "
        "scaler.transform, преобразование float32 и инференс с возвратом результата на CPU. "
        "Загрузка артефактов и прогрев исключены. HTTP, JSON и сеть не измеряются.",
        "Нагрузка closed-loop: каждый клиент отправляет следующий запрос после ответа. "
        "Latency измеряется от начала обработки запроса; ожидание внешней очереди "
        "и coordinated omission не моделируются. Throughput считается по общему wall time, "
        "а не как обратная величина средней latency.",
        f"Прогрев: {args['warmup']} запросов; измерение: {args['seconds']} с на повтор; "
        f"повторов: {args['repeats']}. Таблица — медиана повторов, RSS — максимум.",
        "CPU% процесса: 100% = один полностью занятый CPU. RSS включает среду Python, "
        "данные и backend; опрос каждые 20 мс, короткие пики могут быть пропущены. "
        "GPU utilization, VRAM и power опрашиваются через nvidia-smi каждые 200 мс; "
        "это показатели всей видеокарты, включая графическую среду и другие процессы. "
        "GPU-вызовы возвращают результат на CPU, что учитывает синхронизацию и копирование. "
        "Для ONNX CUDA размещение узлов проверяется профилировщиком: допускаются "
        "метаданные формы на CPU, но не вычислительные операторы. CPU fallback вычислений "
        "отмечается skipped. Профилирование отключено во время замеров.",
        "",
        "## Результаты",
        "",
        "![Latency и throughput одиночных запросов](benchmark.png)",
        "",
        "| Модель | Device | CPU | Batch | Клиенты | p50, мс | p95, мс | p99, мс | req/s | объектов/с | CPU% | RSS MiB |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in report["summary"]:
        lines.append(
            f"| {r['model']} | {r['device']} | {r['cores']} | {r['batch']} | "
            f"{r['concurrency']} | {r['p50_ms']:.3f} | {r['p95_ms']:.3f} | "
            f"{r['p99_ms']:.3f} | {r['requests_per_second']:.0f} | "
            f"{r['samples_per_second']:.0f} | {r['cpu_percent']:.1f} | "
            f"{r['rss_peak_mib']:.1f} |"
        )
    lines += [
        "",
        "## Ресурсы GPU (вся видеокарта)",
        "",
        "| Модель | Batch | Клиенты | GPU util, % | VRAM peak, MiB | Power, W |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for r in report["summary"]:
        if r.get("gpu_util_mean_percent") is not None:
            lines.append(
                f"| {r['model']} | {r['batch']} | {r['concurrency']} | "
                f"{r['gpu_util_mean_percent']:.1f} | "
                f"{r['gpu_device_memory_peak_mib']:.0f} | "
                f"{r['gpu_power_mean_watts']:.1f} |"
            )
    lines += ["", "## Недоступные режимы", ""]
    for skip in report["skipped"]:
        c = skip["config"]
        lines.append(
            f"- {c['model']}, {c['device']}, CPU={c['cores']}: {skip['reason']}"
        )
    if not report["skipped"]:
        lines.append("Нет.")
    lines += [
        "",
        "## Конфигурация для production",
        "",
        f"Предварительные требования: {args['target_rps']} запросов/с, "
        f"batch=1, p95 ≤ {args['slo_ms']} мс; резерв пропускной способности 50%. "
        "Это допущения для выбора, их нужно заменить требованиями сервиса.",
    ]
    candidates = [
        r
        for r in report["summary"]
        if r["batch"] == 1
        and r["rps_min"] * 0.5 >= args["target_rps"]
        and r["worst_p95_ms"] <= args["slo_ms"]
    ]
    if candidates:
        best = min(
            candidates,
            key=lambda r: (r["device"] != "cpu", r["cores"], r["worst_p95_ms"]),
        )
        memory = max(256, math.ceil(best["rss_peak_mib"] * 2 / 128) * 128)
        lines += [
            f"Стартовая рекомендация среди измеренных конфигураций: **{best['model']}, "
            f"{best['device']}, {best['cores']} CPU, 1 процесс, "
            f"{best['threads']} inference threads, {best['concurrency']} клиентов**. "
            f"Худший p95: {best['worst_p95_ms']:.3f} мс; минимальный throughput повторов: "
            f"{best['rps_min']:.0f} req/s. Стартовый лимит памяти: {memory} MiB "
            "(не менее 2× наблюдавшегося RSS, округление вверх).",
        ]
    else:
        lines.append(
            "Ни одна измеренная конфигурация не удовлетворяет заданным требованиям."
        )
    lines += [
        "Это выбор стартовой конфигурации, а не доказанный глобальный оптимум или "
        "сравнение стоимости облачных инстансов. CPU affinity не является CPU quota. "
        "Для production нужны повтор на целевом инстансе, HTTP-тест с заданным arrival "
        "rate, длительный soak-тест, измерение очередей и полной памяти контейнера. "
        "Для отказоустойчивости предусмотрите минимум две реплики и проверьте N−1.",
        "INT8 уменьшает размер файла, но может проигрывать FP32 по времени. "
        "Качество INT8 проверяется отдельно: `make train-nn`, отчёт `reports/quantization.json`.",
        "GPU/CPU разных типов инстансов нельзя сравнить без доступа к ним. "
        "Пропущенные GPU-прогоны не являются результатами производительности.",
        "",
        "## Воспроизведение",
        "",
        "```bash",
        report["command"],
        "```",
        "",
        "Для воспроизведения с временным CUDA-окружением используйте "
        "`make benchmark-nn-gpu` с теми же `BENCHMARK_ARGS` (версии GPU-пакетов закреплены в Makefile). "
        "Для другого хоста повторите команду с отдельным `--output-dir` и `--label`. "
        "На GPU нужен совместимый NVIDIA-драйвер и ONNX Runtime с CUDA EP. "
        "Динамический INT8 может не поддерживаться CUDA без CPU fallback.",
        "",
        "Полные повторы, диапазоны throughput, SHA256 артефактов и параметры — "
        "в `benchmark.json` рядом с этим отчётом.",
        "",
        "Методика GPU: [PyTorch CUDA timing](https://docs.pytorch.org/docs/stable/notes/cuda.html); "
        "[ONNX Runtime CUDA EP](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html).",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    parser.add_argument("--cpu-cores", nargs="+", type=int, default=[1, 2, 4])
    parser.add_argument(
        "--devices", nargs="+", choices=["cpu", "cuda"], default=["cpu", "cuda"]
    )
    parser.add_argument("--batch-sizes", nargs="+", type=int, default=[1, 32, 256])
    parser.add_argument("--concurrency", nargs="+", type=int, default=[1, 4])
    parser.add_argument("--seconds", type=float, default=2)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=50)
    parser.add_argument("--target-rps", type=float, default=1000)
    parser.add_argument("--slo-ms", type=float, default=10)
    parser.add_argument("--label", default=platform.node())
    parser.add_argument("--output-dir", type=Path, default=REPORTS_DIR / "benchmark")
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(json.loads(args.worker))))
        return
    if any(n < 1 for n in args.cpu_cores + args.batch_sizes + args.concurrency):
        parser.error("CPU, batch и concurrency должны быть положительными")
    if (
        any(
            not math.isfinite(n) or n <= 0
            for n in (args.seconds, args.target_rps, args.slo_ms)
        )
        or args.repeats < 1
        or args.warmup < 1
    ):
        parser.error(
            "Длительность, повторы, прогрев, target-rps и slo-ms должны быть > 0"
        )
    if max(args.cpu_cores) > len(os.sched_getaffinity(0)):
        parser.error("Запрошено больше CPU, чем доступно по affinity")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "status": "running",
        "environment": metadata(),
        "parameters": {
            k: v for k, v in vars(args).items() if k not in ("output_dir", "worker")
        },
        "command": shlex.join(
            ["uv", "run", "python", "-m", "src.neural.benchmark", *sys.argv[1:]]
        ),
        "rows": [],
        "skipped": [],
    }
    for device in args.devices:
        cores_list = args.cpu_cores if device == "cpu" else [max(args.cpu_cores)]
        for cores, model in itertools.product(cores_list, ARTIFACTS):
            config = {
                "cores": cores,
                "model": model,
                "device": device,
                "batches": args.batch_sizes,
                "concurrency": args.concurrency,
                "seconds": args.seconds,
                "repeats": args.repeats,
                "warmup": args.warmup,
            }
            print(f"Benchmark {model} / {device} / CPU={cores} ...", flush=True)
            env = {
                **os.environ,
                "OMP_NUM_THREADS": str(cores),
                "MKL_NUM_THREADS": str(cores),
                "OPENBLAS_NUM_THREADS": str(cores),
            }
            child = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "src.neural.benchmark",
                    "--worker",
                    json.dumps(config),
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=max(
                    120,
                    args.seconds
                    * args.repeats
                    * len(args.batch_sizes)
                    * len(args.concurrency)
                    * 4
                    + 60,
                ),
            )
            if child.returncode:
                raise RuntimeError(
                    f"Benchmark failed: {config}\n{child.stderr}\n{child.stdout}"
                )
            result = json.loads(child.stdout)
            if result["status"] == "skipped":
                report["skipped"].append(result)
                print("  skipped: " + result["reason"], flush=True)
            else:
                report["rows"].extend(result["rows"])
            report["summary"] = summarize(report["rows"])
            (args.output_dir / "benchmark.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    report["status"] = "complete"
    (args.output_dir / "benchmark.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    plot_results(report, args.output_dir / "benchmark.png")
    write_report(report, args.output_dir / "benchmark.md")
    print(f"Отчёт: {args.output_dir / 'benchmark.md'}", flush=True)


if __name__ == "__main__":
    main()
