"""
工具函数模块
包含延时、日志、重试、结果保存等功能
"""

import json
import logging
import os
import random
import time
from datetime import datetime
from functools import wraps
from pathlib import Path

from config import ROOT_DIR


# ===== 日志设置 =====

def setup_logger(name: str = "boss_app") -> logging.Logger:
    """配置并返回日志记录器"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    # 控制台输出
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    console_handler.setFormatter(console_fmt)
    logger.addHandler(console_handler)

    # 文件输出
    log_dir = ROOT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"boss_{datetime.now().strftime('%Y%m%d')}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler.setFormatter(file_fmt)
    logger.addHandler(file_handler)

    return logger


# ===== 延时工具 =====

def random_delay(min_sec: float = 2.0, max_sec: float = 6.0):
    """随机等待一段时间，模拟人类操作"""
    delay = random.uniform(min_sec, max_sec)
    time.sleep(delay)
    return delay


def human_pause(seconds: int, logger: logging.Logger, reason: str = ""):
    """暂停并倒计时显示"""
    if reason:
        logger.info(f"⏸️ {reason}，等待 {seconds} 秒...")
    for i in range(seconds, 0, -1):
        if i % 5 == 0 or i <= 3:
            print(f"  ⏳ 继续倒计时: {i} 秒...", end="\r")
        time.sleep(1)
    print(" " * 40, end="\r")


# ===== 重试装饰器 =====

def retry_on_error(max_retries: int = 3, delay_base: float = 2.0, logger: logging.Logger = None):
    """重试装饰器：异常时自动重试（指数退避）"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_error = e
                    if logger:
                        logger.warning(f"  ⚠️ 第 {attempt}/{max_retries} 次失败: {e}")
                    if attempt < max_retries:
                        wait = delay_base * (2 ** (attempt - 1)) + random.uniform(0, 1)
                        time.sleep(wait)
            if logger:
                logger.error(f"  ❌ 重试 {max_retries} 次后仍然失败: {last_error}")
            raise last_error
        return wrapper
    return decorator


# ===== 结果保存 =====

def save_result(category: str, data: dict):
    """保存投递结果到 results/ 目录"""
    results_dir = ROOT_DIR / "results"
    results_dir.mkdir(exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    file_path = results_dir / f"{category}_{today}.json"

    # 如果文件已存在，追加
    records = []
    if file_path.exists():
        with open(file_path, "r", encoding="utf-8") as f:
            try:
                records = json.load(f)
            except json.JSONDecodeError:
                records = []

    # 添加时间戳
    data["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    records.append(data)

    # Replace the completed file atomically so an interrupted write keeps the
    # previous day's records readable after a restart.
    temp_path = file_path.with_name(file_path.name + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, file_path)


# ===== 计时器 =====

class Timer:
    """简单的性能计时器"""

    def __init__(self):
        self.start_time = None

    def start(self):
        self.start_time = time.time()

    def elapsed(self) -> str:
        if self.start_time is None:
            return "0秒"
        elapsed = time.time() - self.start_time
        if elapsed < 60:
            return f"{elapsed:.0f}秒"
        else:
            return f"{elapsed / 60:.1f}分钟"
