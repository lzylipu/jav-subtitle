"""运行设备自动识别：有 GPU 就用 GPU，没有才回退 CPU。

为什么不能写死 device="cpu"：
- 同一份镜像要能跑在纯 CPU 的机器（比如 E3-1231v3）和带 N 卡的机器上；
- 判断依据必须是**运行时探测**，不是镜像种类、也不是代码常量；
- 探测不到 / 驱动不匹配时**必须**安静回退 CPU 并说明原因，不能让任务崩。

探测分两层，任何一层失败都只降级不报错：
1. `ctranslate2.get_cuda_device_count()` —— 是否有可用 CUDA 设备（PyPI 的
   ctranslate2 wheel 是带 CUDA 支持的，缺驱动时这里返回 0 或抛错）；
2. `nvidia-smi` —— 只用来拿显卡名字/显存，用于界面展示和 compute_type 微调。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field

DEVICE_CHOICES = ("auto", "cuda", "cpu")
COMPUTE_CHOICES = ("auto", "float16", "int8_float16", "int8", "int8_float32", "float32")

# 显存低于这个值就把 compute_type 降到 int8_float16，避免 OOM
GPU_LOW_VRAM_MB = 4096


@dataclass
class DeviceInfo:
    """这台机器/这个容器的硬件探测结果。"""

    cuda_device_count: int = 0
    cuda_compute_types: list[str] = field(default_factory=list)
    cuda_error: str = ""
    gpu_names: list[str] = field(default_factory=list)
    gpu_memory_mb: list[int] = field(default_factory=list)
    cpu_count: int = 1

    @property
    def has_cuda(self) -> bool:
        return self.cuda_device_count > 0

    def to_dict(self) -> dict:
        data = asdict(self)
        data["has_cuda"] = self.has_cuda
        return data


@dataclass
class DevicePlan:
    """最终决定给 whisper 用的参数。"""

    device: str                    # cuda | cpu
    compute_type: str              # float16 / int8_float16 / int8 / ...
    cpu_threads: int
    requested_device: str
    requested_compute: str
    reason: str
    info: DeviceInfo

    def to_dict(self) -> dict:
        return {
            "device": self.device,
            "compute_type": self.compute_type,
            "cpu_threads": self.cpu_threads,
            "requested_device": self.requested_device,
            "requested_compute": self.requested_compute,
            "reason": self.reason,
            "hardware": self.info.to_dict(),
        }


def _nvidia_smi() -> tuple[list[str], list[int]]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return [], []
    try:
        done = subprocess.run(
            [exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return [], []
    names: list[str] = []
    mems: list[int] = []
    for line in (done.stdout or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if not parts or not parts[0]:
            continue
        names.append(parts[0])
        try:
            mems.append(int(float(parts[1])))
        except (IndexError, ValueError):
            mems.append(0)
    return names, mems


def _container_sees_nvidia_nodes() -> bool:
    """容器里有没有被交给显卡设备节点。

    docker 默认不把 /dev/nvidia* 给容器，必须启动时加 `--gpus all`。
    区分这两种情况是为了给出人能看懂的提示：
    - 没有节点 → 大概率是没加 --gpus，不是没显卡
    - 有节点但探测到 0 → 才是真的没有可用显卡/驱动版本不匹配
    """
    return any(os.path.exists(p) for p in ("/dev/nvidiactl", "/dev/nvidia0"))


def probe() -> DeviceInfo:
    """探测一次硬件。结果不缓存太久（页面刷新要能看到 GPU 插拔变化）。"""
    info = DeviceInfo(cpu_count=os.cpu_count() or 1)
    try:
        import ctranslate2
    except Exception as exc:  # pragma: no cover - 依赖缺失
        info.cuda_error = f"ctranslate2 不可用：{exc}"
        return info

    try:
        info.cuda_device_count = int(ctranslate2.get_cuda_device_count())
    except Exception as exc:
        info.cuda_error = str(exc)
        return info

    if info.cuda_device_count <= 0:
        # 没有设备就没必要再问支持哪些类型（缺驱动时这句会抛错）
        if not info.cuda_error:
            if _container_sees_nvidia_nodes():
                info.cuda_error = "未检测到可用 CUDA 设备（驱动版本不匹配或显卡不被支持）"
            else:
                info.cuda_error = ("容器里看不到显卡设备。若这台机器装有 NVIDIA 显卡，"
                                   "启动命令需要加 --gpus all；没有显卡则本来就用 CPU")
        return info

    try:
        info.cuda_compute_types = sorted(ctranslate2.get_supported_compute_types("cuda"))
    except Exception as exc:
        info.cuda_error = str(exc)

    names, mems = _nvidia_smi()
    info.gpu_names = names or [f"CUDA 设备 {i}" for i in range(info.cuda_device_count)]
    info.gpu_memory_mb = mems or [0] * info.cuda_device_count
    return info


def resolve(requested_device: str = "auto",
            requested_compute: str = "auto",
            requested_threads: int | None = None) -> DevicePlan:
    """把用户设置解析成实际可用的 (device, compute_type, cpu_threads)。

    requested_* 来自 Web UI / 环境变量；非法值按 auto 处理，不报错。
    """
    requested_device = (requested_device or "auto").strip().lower()
    if requested_device not in DEVICE_CHOICES:
        requested_device = "auto"
    requested_compute = (requested_compute or "auto").strip().lower()
    if requested_compute not in COMPUTE_CHOICES:
        requested_compute = "auto"

    info = probe()

    if requested_device == "cuda":
        use_cuda = info.has_cuda
        reason = ("按设置强制使用 CUDA（检测到 %d 个设备）" % info.cuda_device_count
                  if use_cuda else
                  "设置要求 CUDA，但没有可用设备，已回退 CPU：%s" % (info.cuda_error or "未知原因"))
    elif requested_device == "cpu":
        use_cuda = False
        reason = "按设置强制使用 CPU"
    else:
        use_cuda = info.has_cuda
        reason = ("自动识别到 %d 个 CUDA 设备，使用 GPU（%s）"
                  % (info.cuda_device_count, "、".join(info.gpu_names) or "未命名")
                  if use_cuda else
                  "自动识别未发现可用 CUDA 设备，使用 CPU")

    device = "cuda" if use_cuda else "cpu"

    if requested_compute != "auto":
        compute_type = requested_compute
    elif use_cuda:
        low_vram = bool(info.gpu_memory_mb) and 0 < max(info.gpu_memory_mb) < GPU_LOW_VRAM_MB
        compute_type = "int8_float16" if low_vram else "float16"
        supported = info.cuda_compute_types
        if supported and compute_type not in supported:
            compute_type = supported[0]
    else:
        compute_type = "int8"

    if requested_threads is not None and requested_threads > 0:
        cpu_threads = int(requested_threads)
    else:
        # GPU 时 CPU 只负责喂数据；纯 CPU 时留 4 线程，再多收益递减且抢内存带宽
        cpu_threads = 1 if use_cuda else max(1, min(4, info.cpu_count))

    return DevicePlan(
        device=device,
        compute_type=compute_type,
        cpu_threads=cpu_threads,
        requested_device=requested_device,
        requested_compute=requested_compute,
        reason=reason,
        info=info,
    )
