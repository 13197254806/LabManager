"""成员在网页上浏览 GPU 服务器文件时，平台和 Agent 之间的请求转发。

平台访问不到 GPU 服务器（只有 Agent 主动连出来），所以用“长轮询”：
Agent 一直挂着一个 /agent/browse/poll 请求；成员发起浏览时，请求放进队列、立刻唤醒这个轮询，
Agent 读完目录后把结果回传，平台再回复给成员。请求只存在内存里（web 进程是单进程），重启后丢失即可。
"""
import threading
import uuid


class Broker:
    def __init__(self):
        self._cv = threading.Condition()
        self._queue: dict[int, list[dict]] = {}  # server_id -> 等待 Agent 领取的请求
        self._waiting: dict[str, dict] = {}  # req_id -> 请求（含 server_id、event、result）

    def ask(self, server_id: int, op: str, path: str, timeout: float = 25.0) -> dict | None:
        """发起请求并等待 Agent 回复；超时返回 None。"""
        req = {"id": uuid.uuid4().hex, "server_id": server_id, "op": op, "path": path,
               "event": threading.Event(), "result": None}
        with self._cv:
            self._waiting[req["id"]] = req
            self._queue.setdefault(server_id, []).append(req)
            self._cv.notify_all()
        req["event"].wait(timeout)
        with self._cv:
            self._waiting.pop(req["id"], None)
            q = self._queue.get(server_id, [])
            if req in q:
                q.remove(req)
        return req["result"]

    def poll(self, server_id: int, timeout: float = 20.0) -> list[dict]:
        """Agent 领取请求；没有就最多等 timeout 秒。"""
        with self._cv:
            if not self._queue.get(server_id):
                self._cv.wait(timeout)
            reqs = self._queue.pop(server_id, [])
        return [{"id": r["id"], "op": r["op"], "path": r["path"]} for r in reqs]

    def answer(self, req_id: str, server_id: int, result: dict) -> bool:
        with self._cv:
            req = self._waiting.get(req_id)
            if not req or req["server_id"] != server_id:
                return False
            req["result"] = result
            req["event"].set()
        return True


broker = Broker()
