"""OS-authenticated local approval bridge; Temporal dev access remains trusted."""

import asyncio
from contextlib import asynccontextmanager
import ctypes
import json
import os
from pathlib import Path
import socket
import stat
import struct
from typing import Literal

from pydantic import Field

from .models import Identifier, StrictModel


class ApprovalMessage(StrictModel):
    workflow_id: Identifier
    tenant_id: Literal["acme", "beta"]
    action_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["approve", "reject"]


def peer_uid(connection):
    """Obtain identity from the kernel, never from an approval JSON field."""
    if hasattr(socket, "SO_PEERCRED"):
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", credentials)[1]
    # Python does not expose getpeereid() on every supported macOS build.
    library = ctypes.CDLL(None, use_errno=True)
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    function = library.getpeereid
    function.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
    function.restype = ctypes.c_int
    if function(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        raise OSError(ctypes.get_errno(), "unable to obtain local peer identity")
    return uid.value


class ApprovalController:
    def __init__(self, client, *, allowed_uid=None, tenants=("acme", "beta")):
        self.client = client
        self.allowed_uid = os.getuid() if allowed_uid is None else allowed_uid
        self.tenants = frozenset(tenants)

    async def decide(self, uid, message):
        if uid != self.allowed_uid:
            raise PermissionError("local peer is not authorized")
        request = ApprovalMessage.model_validate(message)
        if request.tenant_id not in self.tenants:
            raise PermissionError("tenant is not authorized")
        handle = self.client.get_workflow_handle(request.workflow_id)
        state = await handle.query("status")
        if (state["tenant_id"] != request.tenant_id or state["action_id"] != request.action_id
                or state["approval_state"] != "pending"):
            raise PermissionError("approval does not match the pending action")
        return await handle.execute_update("decide", {
            "tenant_id": request.tenant_id, "actor": "local-operator",
            "action_id": request.action_id, "decision": request.decision,
        })

    async def _connection(self, reader, writer):
        try:
            connection = writer.get_extra_info("socket")
            uid = peer_uid(connection)
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not line or len(line) > 4096:
                raise ValueError("invalid approval frame")
            result = await asyncio.wait_for(self.decide(uid, json.loads(line)), timeout=15)
            response = {"ok": True, "result": result}
        except Exception:
            # No input or raw exception data crosses into logs or diagnostics.
            response = {"ok": False, "error": "approval_denied"}
        try:
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    @asynccontextmanager
    async def listen(self, path):
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = path.parent.stat()
        if directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) & 0o077:
            raise PermissionError("approval socket requires an owned private directory")
        if path.exists() or path.is_symlink():
            raise FileExistsError("approval socket path is already in use")
        server = await asyncio.start_unix_server(self._connection, path=str(path), limit=4097, start_serving=False)
        path.chmod(0o600)
        inode = path.stat().st_ino
        await server.start_serving()
        try:
            yield server
        finally:
            server.close()
            await server.wait_closed()
            if path.exists() and path.lstat().st_ino == inode:
                path.unlink()


async def send_decision(path, message):
    reader, writer = await asyncio.open_unix_connection(str(path), limit=4097)
    try:
        writer.write((json.dumps(message) + "\n").encode())
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), timeout=20))
        if not response.get("ok"):
            raise PermissionError("approval denied by local controller")
        return response["result"]
    finally:
        writer.close()
        await writer.wait_closed()
