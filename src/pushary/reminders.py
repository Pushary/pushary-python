from __future__ import annotations
from typing import Callable, Dict, Optional
from uuid import uuid4

RequestFn = Callable[..., Dict[str, object]]


class RemindersResource:
    def __init__(self, request: RequestFn) -> None:
        self._request = request

    def schedule(
        self, body: str, *, in_minutes: Optional[int] = None, at: Optional[str] = None,
        title: Optional[str] = None, agent_name: Optional[str] = None,
        session_id: Optional[str] = None, machine_id: Optional[str] = None,
        env: Optional[str] = None, request_id: Optional[str] = None,
    ) -> Dict[str, object]:
        values = {"body": body, "inMinutes": in_minutes, "at": at, "title": title,
                  "agentName": agent_name, "sessionId": session_id, "machineId": machine_id, "env": env,
                  "requestId": str(uuid4()) if request_id is None else request_id}
        return self._request("POST", "/reminders", body={key: value for key, value in values.items() if value is not None})

    def list(self) -> Dict[str, object]:
        return self._request("GET", "/reminders")

    def cancel(self, reminder_id: str) -> Dict[str, object]:
        return self._request("POST", "/reminders", body={"cancelReminderId": reminder_id})

    def get(self, reminder_id: str) -> Dict[str, object]:
        return self._request("POST", "/reminders", body={"reminderId": reminder_id})
