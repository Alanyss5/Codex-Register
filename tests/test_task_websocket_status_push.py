import asyncio
import threading

from fastapi import WebSocketDisconnect

from src.web import task_manager as task_manager_module
from src.web.routes import websocket as websocket_routes
from src.web.task_manager import TaskManager


class DummyWebSocket:
    def __init__(self):
        self.sent = []
        self.accepted = False
        self._incoming: asyncio.Queue = asyncio.Queue()

    async def accept(self):
        self.accepted = True

    async def send_json(self, message):
        self.sent.append(message)

    async def receive_json(self):
        item = await self._incoming.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def disconnect(self):
        await self._incoming.put(WebSocketDisconnect())


def _reset_task_manager_state():
    task_manager_module._log_queues.clear()
    task_manager_module._log_locks.clear()
    task_manager_module._ws_connections.clear()
    task_manager_module._ws_sent_index.clear()
    task_manager_module._task_status.clear()
    task_manager_module._task_cancelled.clear()
    task_manager_module._batch_status.clear()
    task_manager_module._batch_logs.clear()
    task_manager_module._batch_locks.clear()


def test_update_status_broadcasts_terminal_failed_with_error_when_loop_available():
    async def scenario():
        _reset_task_manager_state()
        manager = TaskManager()
        manager.set_loop(asyncio.get_running_loop())
        ws = DummyWebSocket()
        manager.register_websocket("task-failed", ws)

        manager.update_status("task-failed", "failed")
        await asyncio.sleep(0.05)

        assert ws.sent, "expected websocket status push for failed status"
        failed_messages = [m for m in ws.sent if m.get("type") == "status" and m.get("status") == "failed"]
        assert failed_messages, "expected failed status message"
        assert failed_messages[-1].get("error"), "failed status must include error"
        assert manager.get_status("task-failed").get("error")

    asyncio.run(scenario())


def test_update_status_broadcasts_terminal_completed_payload_when_loop_available():
    async def scenario():
        _reset_task_manager_state()
        manager = TaskManager()
        manager.set_loop(asyncio.get_running_loop())
        ws = DummyWebSocket()
        manager.register_websocket("task-completed", ws)

        manager.update_status("task-completed", "completed", email="done@example.com")
        await asyncio.sleep(0.05)

        completed_messages = [m for m in ws.sent if m.get("type") == "status" and m.get("status") == "completed"]
        assert completed_messages, "expected completed status message"
        assert completed_messages[-1].get("email") == "done@example.com"
        assert completed_messages[-1].get("updated_at")

    asyncio.run(scenario())


def test_task_websocket_sends_initial_status_snapshot():
    async def scenario():
        _reset_task_manager_state()
        manager = TaskManager()
        manager.update_status("task-initial", "running", message="booting")
        ws = DummyWebSocket()

        original = websocket_routes.task_manager
        websocket_routes.task_manager = manager
        try:
            task = asyncio.create_task(websocket_routes.task_websocket(ws, "task-initial"))
            await asyncio.sleep(0.01)
            await ws.disconnect()
            await task
        finally:
            websocket_routes.task_manager = original

        assert ws.accepted
        status_messages = [m for m in ws.sent if m.get("type") == "status"]
        assert status_messages, "expected initial status snapshot on connect"
        assert status_messages[0].get("status") == "running"
        assert status_messages[0].get("message") == "booting"

    asyncio.run(scenario())


def test_task_websocket_receives_terminal_update_after_connect():
    async def scenario():
        _reset_task_manager_state()
        manager = TaskManager()
        manager.set_loop(asyncio.get_running_loop())
        ws = DummyWebSocket()

        original = websocket_routes.task_manager
        websocket_routes.task_manager = manager
        try:
            task = asyncio.create_task(websocket_routes.task_websocket(ws, "task-live"))
            await asyncio.sleep(0.01)
            manager.update_status("task-live", "failed", error="boom")
            await asyncio.sleep(0.05)
            await ws.disconnect()
            await task
        finally:
            websocket_routes.task_manager = original

        terminal_messages = [m for m in ws.sent if m.get("type") == "status" and m.get("status") == "failed"]
        assert terminal_messages, "expected live failed status over websocket"
        assert terminal_messages[-1].get("error") == "boom"

    asyncio.run(scenario())


def test_update_status_broadcasts_from_worker_thread_when_loop_available():
    async def scenario():
        _reset_task_manager_state()
        manager = TaskManager()
        manager.set_loop(asyncio.get_running_loop())
        ws = DummyWebSocket()
        manager.register_websocket("task-threaded", ws)

        worker = threading.Thread(
            target=lambda: manager.update_status(
                "task-threaded",
                "completed",
                email="thread@example.com"
            )
        )
        worker.start()
        worker.join()
        await asyncio.sleep(0.05)

        completed_messages = [
            m for m in ws.sent
            if m.get("type") == "status" and m.get("status") == "completed"
        ]
        assert completed_messages, "expected threaded status push over websocket"
        assert completed_messages[-1].get("email") == "thread@example.com"
        assert completed_messages[-1].get("message")

    asyncio.run(scenario())


def test_update_status_terminal_defaults_include_useful_fields():
    _reset_task_manager_state()
    manager = TaskManager()

    manager.update_status("task-term", "completed")
    completed = manager.get_status("task-term")
    assert completed["message"]

    manager.update_status("task-term", "failed")
    failed = manager.get_status("task-term")
    assert failed["message"]
    assert failed["error"]

    manager.update_status("task-term", "cancelled")
    cancelled = manager.get_status("task-term")
    assert cancelled["message"]

    manager.update_status("task-term", "cancelling")
    cancelling = manager.get_status("task-term")
    assert cancelling["message"]
