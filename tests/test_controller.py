import asyncio
import os
import socket
from pathlib import Path
import tempfile

import pytest

from agent_hardening_kit.controller import ApprovalController, peer_uid, send_decision


ACTION = "a" * 64


class Handle:
    def __init__(self):
        self.decisions = []

    async def query(self, name):
        assert name == "status"
        return {"tenant_id": "acme", "action_id": ACTION, "approval_state": "pending"}

    async def execute_update(self, name, decision):
        assert name == "decide"
        self.decisions.append(decision)
        return "approved"


class Client:
    def __init__(self):
        self.handle = Handle()

    def get_workflow_handle(self, workflow_id):
        assert workflow_id == "test-workflow"
        return self.handle


def message(**changes):
    return {"workflow_id": "test-workflow", "tenant_id": "acme", "action_id": ACTION,
            "decision": "approve", **changes}


def test_kernel_peer_identity():
    left, right = socket.socketpair()
    try:
        assert peer_uid(left) == os.getuid()
        assert peer_uid(right) == os.getuid()
    finally:
        left.close()
        right.close()


def test_real_socket_derives_actor_and_removes_owned_socket():
    async def check(directory):
        client = Client()
        path = Path(directory) / "private" / "approve.sock"
        async with ApprovalController(client).listen(path):
            assert path.stat().st_mode & 0o777 == 0o600
            assert await send_decision(path, message()) == "approved"
        assert not path.exists()
        assert client.handle.decisions[0]["actor"] == "local-operator"
    with tempfile.TemporaryDirectory(prefix="ahk-", dir="/tmp") as directory:
        asyncio.run(check(directory))


@pytest.mark.parametrize("change", ({"actor": "local-operator"}, {"uid": 0},
                                    {"tenant_id": "beta"}, {"action_id": "b" * 64}))
def test_untrusted_identity_or_binding_does_not_reach_temporal(change):
    async def check():
        client = Client()
        with pytest.raises((ValueError, PermissionError)):
            await ApprovalController(client).decide(os.getuid(), message(**change))
        assert not client.handle.decisions
    asyncio.run(check())


def test_wrong_kernel_uid_is_denied():
    async def check():
        client = Client()
        with pytest.raises(PermissionError):
            await ApprovalController(client, allowed_uid=os.getuid() + 1).decide(os.getuid(), message())
        assert not client.handle.decisions
    asyncio.run(check())


def test_public_directory_is_rejected(tmp_path):
    async def check():
        directory = tmp_path / "public"
        directory.mkdir(mode=0o755)
        with pytest.raises(PermissionError):
            async with ApprovalController(Client()).listen(directory / "approve.sock"):
                pytest.fail("public socket directory accepted")
    asyncio.run(check())
