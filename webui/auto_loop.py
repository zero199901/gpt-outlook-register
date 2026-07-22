"""auto-loop 控制器：多 worker 并发，每个 worker 用独立代理。

设计：
  - 主控线程 manage_loop：监听 stop/pause、根据 concurrency 启停 worker
  - 多个 worker 线程：claim_next() → 注册 → 完成 → 继续
  - 代理池：每个 worker 按 worker index 取一个代理（round-robin），避免同 IP 多号
  - 状态机：stopped → running → paused → running / stopped
  - 优雅暂停/停止：当前 worker 跑完才退出，不强杀
  - 复用 registrar.start_registration：每个号开一个 run，由 worker 等其结束
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Optional

from . import db, registrar
from .proxy_subscription import dedupe_preserve_order, load_proxy_subscription, mask_url

logger = logging.getLogger("auto_loop")


class AutoLoopState:
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"


def _parse_proxy_pool(text: str) -> list[str]:
    """把多行代理字符串拆成列表。空行 / # 开头注释跳过。"""
    out: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


class AutoLoopController:
    """多 worker auto-loop 控制器。

    options 关键字段：
      proxy:                单代理（兼容旧版，concurrency=1 时用）
      proxy_pool:           多代理字符串（每行一个；多 worker 会按 worker index 轮流取）
      concurrency:          并发 worker 数（1-20）
      cool_down_seconds:    每个 worker 跑完后冷却时间（默认 3）
      其余参数透传给 registrar.start_registration
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._state = AutoLoopState.STOPPED
        self._manage_thread: Optional[threading.Thread] = None
        self._workers: list[threading.Thread] = []
        self._options: dict = {}
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set = 暂停
        # 进度统计
        self._started_at: float = 0.0
        self._registered_ok = 0
        self._registered_fail = 0
        # 当前每个 worker 在跑啥（worker_id → email）
        self._worker_status: dict[int, dict] = {}
        self._last_message = ""
        # 熔断状态
        self._consecutive_network_fails = 0
        self._circuit_break_threshold = 3
        self._last_break_reason = ""
        # SSE 订阅
        self._subscribers: list[queue.Queue] = []
        # 代理池 / 并发数
        self._proxy_pool: list[str] = []
        self._proxy_pool_manual: list[str] = []
        self._proxy_pool_subscription: list[str] = []
        self._concurrency: int = 1
        self._proxy_subscription_url: str = ""
        self._proxy_subscription_refresh_seconds: int = 0
        self._last_proxy_subscription_fetch: float = 0.0
        self._proxy_subscription_status: str = ""
        self._proxy_subscription_source: str = ""
        self._proxy_subscription_limit: int = 0
        self._proxy_subscription_fetch_lock = threading.Lock()
        self._max_runs: int = 0
        self._runs_started: int = 0

    # ──────────────────────── 公共 API ────────────────────────

    def start(self, options: dict) -> dict:
        with self._lock:
            if self._state in (AutoLoopState.RUNNING, AutoLoopState.PAUSED):
                return {"ok": False, "error": f"已经在跑了 (state={self._state})"}
            # 重置
            self._stop_event.clear()
            self._pause_event.clear()
            self._options = dict(options or {})
            self._state = AutoLoopState.RUNNING
            self._started_at = time.time()
            self._registered_ok = 0
            self._registered_fail = 0
            self._runs_started = 0
            self._worker_status.clear()
            self._consecutive_network_fails = 0
            self._last_message = "auto-loop 启动"
            # 解析并发参数
            self._concurrency = max(1, min(20, int(self._options.get("concurrency") or 1)))
            pool_text = self._options.get("proxy_pool") or ""
            self._proxy_pool_manual = _parse_proxy_pool(pool_text)
            self._proxy_pool_subscription = []
            self._proxy_pool = list(self._proxy_pool_manual)
            self._proxy_subscription_url = (self._options.get("proxy_subscription_url") or "").strip()
            self._proxy_subscription_refresh_seconds = max(0, int(self._options.get("proxy_subscription_refresh_seconds") or 0))
            self._proxy_subscription_limit = max(0, int(self._options.get("proxy_subscription_limit") or 0))
            self._max_runs = max(0, int(self._options.get("max_runs") or 0))
            self._last_proxy_subscription_fetch = 0.0
            self._proxy_subscription_source = mask_url(self._proxy_subscription_url)
            self._proxy_subscription_status = ""

        # 启动前先拉一次订阅，让 worker 第一轮就能用到。
        if self._proxy_subscription_url:
            self._refresh_proxy_subscription(force=True)

        with self._lock:
            # 启 manage 线程
            self._manage_thread = threading.Thread(
                target=self._manage_loop, daemon=True, name="auto-loop-manage"
            )
            self._manage_thread.start()
        self._broadcast("state", self._snapshot())
        return {
            "ok": True,
            "state": self._state,
            "concurrency": self._concurrency,
            "proxy_pool_size": len(self._proxy_pool),
            "proxy_subscription_count": len(self._proxy_pool_subscription),
            "proxy_subscription_limit": self._proxy_subscription_limit,
            "max_runs": self._max_runs,
        }

    def pause(self) -> dict:
        with self._lock:
            if self._state != AutoLoopState.RUNNING:
                return {"ok": False, "error": f"当前 state={self._state}，不可暂停"}
            self._pause_event.set()
            self._state = AutoLoopState.PAUSED
            self._last_message = "已请求暂停（当前 worker 跑完才生效）"
        self._broadcast("state", self._snapshot())
        return {"ok": True, "state": self._state}

    def resume(self) -> dict:
        with self._lock:
            if self._state != AutoLoopState.PAUSED:
                return {"ok": False, "error": f"当前 state={self._state}，不可恢复"}
            self._pause_event.clear()
            self._state = AutoLoopState.RUNNING
            self._last_message = "已恢复"
        self._broadcast("state", self._snapshot())
        return {"ok": True, "state": self._state}

    def stop(self) -> dict:
        with self._lock:
            if self._state == AutoLoopState.STOPPED:
                return {"ok": False, "error": "没在跑"}
            self._stop_event.set()
            self._pause_event.clear()
            self._last_message = "已请求停止（当前 worker 跑完才生效）"
        self._broadcast("state", self._snapshot())
        return {"ok": True}

    def status(self) -> dict:
        return self._snapshot()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.append(q)
        try:
            q.put_nowait({"kind": "state", "data": self._snapshot()})
        except queue.Full:
            pass
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            try: self._subscribers.remove(q)
            except ValueError: pass

    # ──────────────────────── 内部 ────────────────────────

    def _snapshot(self) -> dict:
        with self._lock:
            stats = db.stats()
            workers_info = [
                {
                    "id": wid,
                    "email": info.get("email", ""),
                    "run_id": info.get("run_id", ""),
                    "proxy": info.get("proxy", ""),
                    "started_at": info.get("started_at", 0),
                }
                for wid, info in sorted(self._worker_status.items())
            ]
            return {
                "state": self._state,
                "started_at": self._started_at,
                "elapsed": (time.time() - self._started_at) if self._started_at else 0,
                "registered_ok": self._registered_ok,
                "registered_fail": self._registered_fail,
                "runs_started": self._runs_started,
                "max_runs": self._max_runs,
                "concurrency": self._concurrency,
                "proxy_pool_size": len(self._proxy_pool),
                "proxy_pool_manual_size": len(self._proxy_pool_manual),
                "proxy_subscription_count": len(self._proxy_pool_subscription),
                "proxy_subscription_url": self._proxy_subscription_source,
                "proxy_subscription_refresh_seconds": self._proxy_subscription_refresh_seconds,
                "proxy_subscription_limit": self._proxy_subscription_limit,
                "proxy_subscription_last_fetch": self._last_proxy_subscription_fetch,
                "proxy_subscription_status": self._proxy_subscription_status,
                "workers": workers_info,
                "last_message": self._last_message,
                "pool_stats": stats,
            }

    def _broadcast(self, kind: str, data):
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait({"kind": kind, "data": data})
            except queue.Full:
                pass

    def _set_message(self, msg: str):
        with self._lock:
            self._last_message = msg
        self._broadcast("state", self._snapshot())

    def _refresh_proxy_subscription(self, force: bool = False):
        """按需刷新订阅；成功后把订阅代理 + 手填代理合并成当前代理池。"""
        with self._lock:
            url = self._proxy_subscription_url
            refresh_seconds = self._proxy_subscription_refresh_seconds
            last_fetch = self._last_proxy_subscription_fetch
        if not url:
            return
        now = time.time()
        if not force and refresh_seconds <= 0:
            return
        if not force and last_fetch and (now - last_fetch) < refresh_seconds:
            return

        acquired = self._proxy_subscription_fetch_lock.acquire(blocking=force)
        if not acquired:
            return
        try:
            with self._lock:
                # 多 worker 同时进来时，抢到锁的 worker 再检查一次间隔。
                last_fetch = self._last_proxy_subscription_fetch
                refresh_seconds = self._proxy_subscription_refresh_seconds
            now = time.time()
            if not force and refresh_seconds > 0 and last_fetch and (now - last_fetch) < refresh_seconds:
                return

            result = load_proxy_subscription(url)
            msg_tail = ""
            if result.warnings:
                msg_tail = "；" + "；".join(result.warnings[:2])
            limit = max(0, int(self._proxy_subscription_limit or 0))
            selected_proxies = result.proxies[:limit] if limit > 0 else result.proxies
            limit_tail = f"，取前 {limit} 个" if limit > 0 and result.proxies else ""
            with self._lock:
                self._last_proxy_subscription_fetch = time.time()
                if selected_proxies:
                    self._proxy_pool_subscription = selected_proxies
                    self._proxy_pool = dedupe_preserve_order(
                        self._proxy_pool_subscription + self._proxy_pool_manual
                    )
                    self._proxy_subscription_status = (
                        f"订阅代理 {len(result.proxies)} 个{limit_tail}，总代理池 {len(self._proxy_pool)} 个{msg_tail}"
                    )
                else:
                    extra = result.error or "订阅内暂无 HTTP/SOCKS 直连代理"
                    self._proxy_subscription_status = f"订阅刷新完成：{extra}{msg_tail}"
                    self._proxy_pool = dedupe_preserve_order(
                        self._proxy_pool_subscription + self._proxy_pool_manual
                    )
                self._last_message = self._proxy_subscription_status
            self._broadcast("state", self._snapshot())
        except Exception as e:
            with self._lock:
                self._last_proxy_subscription_fetch = time.time()
                self._proxy_subscription_status = f"订阅刷新异常：{e}"
                self._last_message = self._proxy_subscription_status
            self._broadcast("state", self._snapshot())
        finally:
            self._proxy_subscription_fetch_lock.release()

    def _proxy_for_worker(self, worker_id: int) -> str:
        """按 worker_id 从代理池里挑一个代理。空池时回退到 options.proxy。"""
        self._refresh_proxy_subscription(force=False)
        with self._lock:
            pool = list(self._proxy_pool)
            fallback = self._options.get("proxy", "") or ""
        if pool:
            return pool[worker_id % len(pool)]
        return fallback

    def _record_finish(self, ok: bool, category: str):
        """worker 结束一个 run 后调，更新计数 + 熔断。"""
        with self._lock:
            if ok:
                self._registered_ok += 1
                self._consecutive_network_fails = 0
            else:
                self._registered_fail += 1
                if category == "network":
                    self._consecutive_network_fails += 1
                else:
                    self._consecutive_network_fails = 0
            self._last_message = (
                f"累计 ok={self._registered_ok} fail={self._registered_fail}"
            )
            trigger_break = (
                self._consecutive_network_fails >= self._circuit_break_threshold
                and self._state == AutoLoopState.RUNNING
            )

        if trigger_break:
            with self._lock:
                self._pause_event.set()
                self._state = AutoLoopState.PAUSED
                self._last_break_reason = (
                    f"连续 {self._consecutive_network_fails} 次网络/环境错误，"
                    f"自动暂停（号已自动 release，请检查代理后点恢复）"
                )
                self._last_message = self._last_break_reason
                self._consecutive_network_fails = 0
            logger.warning(self._last_break_reason)
            self._broadcast("circuit_break", {"reason": self._last_break_reason})

    def _manage_loop(self):
        """主控线程：启动 worker，等所有 worker 结束，更新最终状态。"""
        try:
            workers = []
            for wid in range(self._concurrency):
                t = threading.Thread(
                    target=self._worker_loop, args=(wid,),
                    daemon=True, name=f"auto-loop-worker-{wid}",
                )
                t.start()
                workers.append(t)
                # 每个 worker 之间错开 1s 启动，避免同时打 OpenAI
                time.sleep(1.0)
            self._workers = workers
            # 等所有 worker 退出
            for t in workers:
                t.join()
        except Exception as e:
            logger.exception(f"manage_loop 异常: {e}")
        finally:
            with self._lock:
                self._state = AutoLoopState.STOPPED
                self._worker_status.clear()
                self._last_message = (
                    f"已停止（成功 {self._registered_ok} / 失败 {self._registered_fail}）"
                )
            self._broadcast("state", self._snapshot())

    def _reserve_run_slot(self, worker_id: int) -> bool:
        """限制本次自动跑号最多启动多少个 run；0 表示不限。"""
        with self._lock:
            if self._max_runs > 0 and self._runs_started >= self._max_runs:
                self._last_message = f"已达到本次最多 {self._max_runs} 个 run，worker-{worker_id} 停止"
                return False
            self._runs_started += 1
            return True

    def _worker_loop(self, worker_id: int):
        """单 worker 循环：claim → 跑 → 等结束 → 继续。"""
        idle_round = 0
        logger.info(f"[worker-{worker_id}] 启动")

        while True:
            # 检查停止
            if self._stop_event.is_set():
                logger.info(f"[worker-{worker_id}] 已停止")
                return

            # 检查暂停
            if self._pause_event.is_set():
                while self._pause_event.is_set() and not self._stop_event.is_set():
                    time.sleep(0.5)
                if self._stop_event.is_set():
                    return

            # 本次最多启动 N 个 run；到数后停止整个 auto-loop。
            if not self._reserve_run_slot(worker_id):
                self._broadcast("state", self._snapshot())
                return

            # claim 下一个号（CF 模式用虚拟占位，无需 outlook 号池）
            mail_source = db.get_setting("mail_source", "outlook")
            if mail_source == "cf_temp":
                account = {
                    "email": f"cf_placeholder_{int(time.time())}_{worker_id}@cf.local",
                    "password": "", "client_id": "", "refresh_token": "",
                }
            else:
                account = db.claim_next()
            if not account:
                with self._lock:
                    self._runs_started = max(0, self._runs_started - 1)
                idle_round += 1
                if idle_round == 1:
                    self._set_message(
                        f"worker-{worker_id} 号池空，等待新号..."
                    )
                # 空 10 轮（约 30s）就停掉这个 worker
                if idle_round >= 10:
                    logger.info(f"[worker-{worker_id}] 号池空 30s，停止")
                    return
                # 等 3s 再试
                for _ in range(30):
                    if self._stop_event.is_set() or self._pause_event.is_set():
                        break
                    time.sleep(0.1)
                continue
            idle_round = 0

            # 给这个 run 注入 worker 自己的代理；每轮重新取，便于订阅刷新后动态切换。
            proxy = self._proxy_for_worker(worker_id)
            logger.info(f"[worker-{worker_id}] 本轮代理: {proxy or '直连'}")
            run_options = dict(self._options)
            if proxy:
                run_options["proxy"] = proxy

            # 启一个 run
            try:
                run_id = registrar.start_registration(account, run_options)
            except Exception as e:
                with self._lock:
                    self._runs_started = max(0, self._runs_started - 1)
                logger.exception(f"[worker-{worker_id}] 启动注册失败: {e}")
                if mail_source != "cf_temp":
                    db.release_unused(account["email"])
                time.sleep(2)
                continue

            with self._lock:
                self._worker_status[worker_id] = {
                    "email": account["email"],
                    "run_id": run_id,
                    "proxy": proxy,
                    "started_at": time.time(),
                }
            self._broadcast("state", self._snapshot())
            self._broadcast("run_started", {
                "worker_id": worker_id,
                "email": account["email"],
                "run_id": run_id,
                "proxy": proxy,
            })

            # 等当前 run 跑完
            ok, category = self._wait_run_finish(run_id)

            with self._lock:
                self._worker_status.pop(worker_id, None)
            self._record_finish(ok, category)
            self._broadcast("state", self._snapshot())
            self._broadcast("run_finished", {
                "worker_id": worker_id,
                "email": account["email"],
                "run_id": run_id,
                "ok": ok,
                "category": category,
            })

            # 冷却（每个 worker 自己的节奏）
            cool_down = float(self._options.get("cool_down_seconds") or 3)
            if cool_down > 0:
                for _ in range(int(cool_down * 10)):
                    if self._stop_event.is_set() or self._pause_event.is_set():
                        break
                    time.sleep(0.1)

    def _wait_run_finish(self, run_id: str, timeout: int = 1800) -> tuple[bool, str]:
        """轮询 runs 表，等 run 跑完。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._stop_event.is_set():
                return False, ""
            con = db._conn()
            cur = con.execute(
                "SELECT status, error_category FROM runs WHERE run_id=?", (run_id,)
            )
            row = cur.fetchone()
            if row:
                st = row["status"]
                if st == "done":
                    return True, ""
                if st == "failed":
                    return False, (row["error_category"] or "")
            time.sleep(1)
        logger.warning(f"run {run_id} 等了 {timeout}s 没结束，超时放弃")
        return False, ""


# 全局单例
CONTROLLER = AutoLoopController()
