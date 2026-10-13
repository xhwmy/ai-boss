"""
KIMI Webbridge WebSocket 客户端

通过 KIMI Webbridge 扩展的 chrome.debugger API 控制浏览器。
扩展每 30 秒自动连接 ws://127.0.0.1:10086/ws，我们只需运行服务器即可。

协议：
  [连接]  扩展 → 服务器: {"type":"hello","payload":{"extensionVersion":"1.11.3"}}
  [调用]  服务器 → 扩展: {"type":"tool_call","requestId":"uuid","payload":{"name":"tool","args":{}}}
  [响应]  扩展 → 服务器: {"type":"tool_result","responseToRequestId":"uuid","payload":{"data":{}}}
  [心跳]  服务器 → 扩展: {"type":"ping"}  → 扩展回复 {"type":"pong"}
"""

import asyncio
import json
import logging
import threading
import uuid
from typing import Any, Optional

import websockets

logger = logging.getLogger("webbridge")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 10086
WS_PATH = "/ws"


class WebbridgeError(Exception):
    """Webbridge 通信错误"""
    pass


class WebbridgeClient:
    """与 KIMI Webbridge 扩展通信的客户端"""

    def __init__(self):
        self._server: Optional[websockets.WebSocketServer] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ws: Optional[websockets.WebSocketServerProtocol] = None

        # 同步事件
        self._server_ready = threading.Event()
        self._connected = threading.Event()

        # 异步响应管理（在 event loop 中使用 asyncio.Event）
        self._responses: dict[str, Any] = {}
        self._response_events: dict[str, asyncio.Event] = {}
        self._async_lock: Optional[asyncio.Lock] = None
        self._lock = threading.Lock()

        self._ext_version: Optional[str] = None
        self._tab_id: Optional[int] = None

    # ══════════ 生命周期 ══════════

    def start(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        """启动 WebSocket 服务器（后台线程，不阻塞）"""
        self._thread = threading.Thread(
            target=self._run_event_loop,
            args=(host, port),
            daemon=True,
            name="webbridge-ws",
        )
        self._thread.start()
        if not self._server_ready.wait(timeout=10):
            raise WebbridgeError("WebSocket 服务器启动超时")
        logger.info(f"✅ WebSocket 服务器已启动 (ws://{host}:{port}{WS_PATH})")

    def wait_ready(self, timeout: float = 60) -> bool:
        """等待扩展连接就绪"""
        if self._connected.is_set():
            return True
        logger.info("⏳ 等待 KIMI Webbridge 扩展连接（请确保 Edge 浏览器已打开）...")
        ok = self._connected.wait(timeout=timeout)
        if ok:
            logger.info(f"✅ KIMI Webbridge 扩展已连接！版本: {self._ext_version or '未知'}")
        else:
            logger.warning(f"⏰ 等待扩展连接超时（{int(timeout)}秒）")
        return ok

    def close(self):
        """关闭服务器并清理"""
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        self._connected.clear()
        self._ws = None

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def _run_event_loop(self, host: str, port: int):
        """在后台线程中运行 asyncio 事件循环"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve(host, port))
        except Exception as e:
            logger.error(f"WebSocket 服务器异常退出: {e}")

    async def _serve(self, host: str, port: int):
        """启动 WebSocket 服务"""
        self._async_lock = asyncio.Lock()
        self._server = await websockets.serve(
            self._handle_ws,
            host, port,
            ping_interval=None,  # 我们自己控制心跳
        )
        self._server_ready.set()
        logger.info(f"  📡 监听 {host}:{port} ...")
        # 启动心跳任务（每 15 秒发送 ping，防止扩展断开连接）
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        # 保持运行直到关闭
        await asyncio.get_running_loop().create_future()

    async def _heartbeat_loop(self):
        """每 15 秒发送一次心跳，保持扩展连接活跃"""
        while True:
            await asyncio.sleep(15)
            if self._ws and self._ws.close_code is None:
                try:
                    await self._ws.send(json.dumps({"type": "ping"}))
                except Exception:
                    pass

    async def _shutdown(self):
        """关闭服务器"""
        if hasattr(self, '_heartbeat_task'):
            self._heartbeat_task.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ══════════ WebSocket 连接处理 ══════════

    async def _handle_ws(self, ws):
        """处理 WebSocket 连接"""
        # 如果有旧连接，先关掉
        if self._ws and self._ws != ws:
            try:
                await self._ws.close()
            except Exception:
                pass

        self._ws = ws
        self._connected.set()
        logger.info("🔌 KIMI Webbridge 扩展已连接")

        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                    await self._handle_message(data)
                except json.JSONDecodeError:
                    logger.warning(f"收到无效消息: {raw[:100]}")
        except websockets.ConnectionClosed:
            logger.info("🔌 KIMI Webbridge 扩展断开连接（浏览器可能已关闭）")
        except Exception as e:
            logger.warning(f"WebSocket 连接异常: {e}")
        finally:
            if self._ws == ws:
                self._ws = None
                self._connected.clear()

    async def _handle_message(self, data: dict):
        """处理来自扩展的消息"""
        msg_type = data.get("type")

        if msg_type == "hello":
            self._ext_version = data.get("payload", {}).get("extensionVersion", "未知")
            logger.info(f"  📦 扩展版本: {self._ext_version}")
            # 连接后自动获取当前标签页信息
            asyncio.create_task(self._auto_inspect())

        elif msg_type == "tool_result":
            req_id = data.get("responseToRequestId")
            payload = data.get("payload", {})
            if req_id:
                async with self._async_lock:
                    self._responses[req_id] = payload
                    evt = self._response_events.pop(req_id, None)
                if evt:
                    evt.set()

        elif msg_type == "pong":
            pass  # 心跳正常

        else:
            logger.debug(f"未处理消息类型: {msg_type}")

    async def _auto_inspect(self):
        """连接后自动检查浏览器状态"""
        try:
            tabs_result = await self._send_command("list_tabs", {})
            tabs = tabs_result.get("tabs", [])
            active_tabs = [t for t in tabs if t.get("active")]
            if active_tabs:
                tab = active_tabs[0]
                title = tab.get("title", "?")[:40]
                url = tab.get("url", "?")[:50]
                logger.info(f"  📄 当前标签页: {title} | {url}")
        except Exception:
            pass

    # ══════════ 命令发送（异步） ══════════

    async def _send_command(self, tool: str, args: dict,
                            timeout: float = 30) -> Any:
        """发送命令到扩展并等待响应（异步，在 event loop 中调用）"""
        if not self._ws:
            raise WebbridgeError("WebSocket 未连接")
        if self._ws.close_code is not None:
            raise WebbridgeError("WebSocket 已关闭")

        req_id = str(uuid.uuid4())
        event = asyncio.Event()

        async with self._async_lock:
            self._response_events[req_id] = event

        if self._tab_id is not None and tool not in ('list_browser_tabs', 'list_tabs') and not (tool == 'navigate' and args.get('newTab')):
            args = {**args, '_tabId': self._tab_id}
        msg = json.dumps({
            "type": "tool_call",
            "requestId": req_id,
            "payload": {"name": tool, "args": args},
        })
        await self._ws.send(msg)

        # 等待响应
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            async with self._async_lock:
                self._response_events.pop(req_id, None)
            raise WebbridgeError(f"命令超时 ({tool}, {timeout}秒)")

        async with self._async_lock:
            result = self._responses.pop(req_id, {})

        if "error" in result:
            raise WebbridgeError(f"扩展返回错误: {result['error']}")
        return result.get("data", {})

    # ══════════ 同步 API（线程安全，供其他模块调用） ══════════

    def call(self, tool: str, args: dict = None,
             timeout: float = 30) -> Any:
        """调用扩展工具（同步，线程安全）"""
        if not self._loop or self._loop.is_closed():
            raise WebbridgeError("WebSocket 服务器未运行")
        if not self._connected.is_set():
            # 如果暂时断开，等待重连（最多 5 秒）
            self._connected.wait(timeout=5)
            if not self._connected.is_set():
                raise WebbridgeError("KIMI Webbridge 扩展未连接")

        future = asyncio.run_coroutine_threadsafe(
            self._send_command(tool, args or {}, timeout),
            self._loop,
        )
        try:
            return future.result(timeout=timeout + 5)
        except asyncio.TimeoutError:
            raise WebbridgeError(f"调用超时: {tool}")

    # ══════════ 便捷方法 ══════════

    def navigate(self, url: str, new_tab: bool = False) -> dict:
        """导航到 URL"""
        result = self.call("navigate", {"url": url, "newTab": new_tab}, timeout=45)
        if new_tab and isinstance(result.get("tabId"), int):
            self._tab_id = result["tabId"]
        return result

    def navigate_fast(self, url: str) -> dict:
        """在当前 BOSS 标签页启动导航，不等待所有网络请求结束。"""
        result = self.call("cdp", {
            "method": "Page.navigate",
            "params": {"url": url},
        })
        if result.get("errorText"):
            raise WebbridgeError(f"页面跳转失败: {result['errorText']}")
        return result

    def find_tab(self, url_pattern: str, active: bool = False) -> dict:
        """按 URL 查找标签页（hostname 匹配）"""
        return self.call("find_tab", {"url": url_pattern, "active": active})

    def switch_tab(self, url_pattern: str) -> bool:
        """将后续命令固定到匹配的标签页，不抢占用户前台焦点。"""
        tabs = self.call("list_browser_tabs", {}).get("tabs", [])
        matches = [tab for tab in tabs if url_pattern in tab.get("url", "")]
        if not matches:
            return False
        matches.sort(
            key=lambda tab: ("/web/geek/jobs" in tab.get("url", ""), tab.get("active", False)),
            reverse=True,
        )
        self._tab_id = matches[0]["tabId"]
        return True

    def evaluate(self, code: str) -> Any:
        """执行 JavaScript，返回值"""
        result = self.call("evaluate", {"code": code})
        return result.get("value")

    def click(self, selector: str) -> dict:
        """DOM 点击（通过 JS 触发，更快）"""
        return self.call("click", {"selector": selector.replace("'", '"')})

    def mouse_click(self, selector: str) -> dict:
        """真实鼠标点击（CDP Input.dispatchMouseEvent，更逼真）"""
        return self.call("mouse_click", {"selector": selector.replace("'", '"')})

    def mouse_wheel(self, delta_y: int = 500, x: int = 300, y: int = 400,
                    delta_x: int = 0) -> dict:
        """真实鼠标滚轮滚动（CDP Input.dispatchMouseEvent mouseWheel）

        通过扩展的通用 `cdp` 工具发送浏览器级输入事件，与真实滚轮完全等效
        （事件 isTrusted=True）。BOSS 直聘的懒加载 / 下拉刷新依赖真实的
        wheel 输入，JS 合成的 scroll 事件（isTrusted=False）不会触发。
        delta_y > 0 向下滚动；x/y 为视口内 CSS 像素坐标（滚轮落点）。
        """
        return self.call("cdp", {
            "method": "Input.dispatchMouseEvent",
            "params": {
                "type": "mouseWheel",
                "x": x,
                "y": y,
                "deltaX": delta_x,
                "deltaY": delta_y,
                "modifiers": 0,
            },
        })

    def fill(self, selector: str, value: str) -> dict:
        """填写输入框（通过 JS 设置 value）"""
        return self.call("fill", {"selector": selector.replace("'", '"'), "value": value})

    def type_text(self, text: str) -> dict:
        """键盘输入文本（CDP Input.insertText，真实键盘事件）"""
        return self.call("key_type", {"text": text})

    def send_keys(self, keys: str, repeat: int = 1) -> dict:
        """键盘快捷键（'Enter', 'Mod+A', 'Escape' 等）"""
        return self.call("send_keys", {"keys": keys, "repeat": repeat})

    def snapshot(self) -> dict:
        """获取页面无障碍树"""
        return self.call("snapshot", {})

    def list_tabs(self) -> list:
        """列出所有标签页"""
        result = self.call("list_tabs", {})
        return result.get("tabs", [])

    def get_page_text(self) -> str:
        """获取页面所有可见文本"""
        try:
            val = self.evaluate("document.body.innerText")
            return str(val) if val is not None else ""
        except Exception:
            return ""

    def get_page_url(self) -> str:
        """获取当前页面 URL"""
        try:
            val = self.evaluate("window.location.href")
            return str(val) if val is not None else ""
        except Exception:
            return ""

    def close_tab(self) -> bool:
        """关闭当前标签页"""
        return self.call("close_tab", {}).get("closed", False)

    def page_has_text(self, text: str) -> bool:
        """检测页面是否包含特定文本"""
        try:
            code = f"document.body.innerText.includes({json.dumps(text)})"
            val = self.evaluate(code)
            return bool(val)
        except Exception:
            return False

    def scroll_into_view(self, selector: str):
        """滚动元素到可视区域"""
        self.evaluate(f"""
            (() => {{
                const el = document.querySelector({json.dumps(selector)});
                if (el) el.scrollIntoView({{ block: 'center', behavior: 'instant' }});
            }})()
        """)
