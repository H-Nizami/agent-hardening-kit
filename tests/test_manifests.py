"""Policy checks applied to rendered Helm manifests."""

import copy
import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "check_manifests", Path(__file__).resolve().parents[1] / "scripts" / "check_manifests.py")
check_manifests = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_manifests)

DIGEST = "sha256:" + "a" * 64
CONTAINER = {
    "name": "app",
    "image": "example/app:1.0@" + DIGEST,
    "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]}},
    "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"cpu": "1", "memory": "64Mi"}},
}
DEPLOYMENT = {
    "kind": "Deployment", "metadata": {"name": "ahk-worker", "namespace": "ahk"},
    "spec": {"template": {"spec": {
        "automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [CONTAINER],
    }}},
}
SERVICE = {"kind": "Service", "metadata": {"name": "ahk-worker", "namespace": "ahk"},
           "spec": {"type": "ClusterIP", "ports": [{"port": 80}]}}


def errors(*docs, local=()):
    return check_manifests.check(list(docs), set(local))[0]


def mutated(path, value):
    doc = copy.deepcopy(DEPLOYMENT)
    target = doc["spec"]["template"]["spec"]
    *parents, last = path
    for key in parents:
        target = target[key]
    target[last] = value
    return doc


def test_compliant_manifests_pass():
    assert errors(DEPLOYMENT, SERVICE) == []


def test_local_image_needs_explicit_allowance():
    doc = mutated(["containers", 0, "image"], "ahk-worker:local")
    assert errors(doc)
    assert errors(doc, local=["ahk-worker:local"]) == []


def test_violations_are_reported():
    cases = [
        mutated(["automountServiceAccountToken"], True),
        mutated(["securityContext", "runAsNonRoot"], False),
        mutated(["containers", 0, "image"], "example/app:latest"),
        mutated(["containers", 0, "securityContext", "readOnlyRootFilesystem"], False),
        mutated(["containers", 0, "securityContext", "capabilities", "drop"], []),
        mutated(["containers", 0, "resources", "limits"], {"cpu": "1"}),
        mutated(["volumes"], [{"name": "host", "hostPath": {"path": "/"}}]),
        mutated(["hostNetwork"], True),
    ]
    for doc in cases:
        assert errors(doc), doc


def test_only_cluster_local_services():
    for kind in ("NodePort", "LoadBalancer"):
        service = copy.deepcopy(SERVICE)
        service["spec"]["type"] = kind
        assert errors(service)
    service = copy.deepcopy(SERVICE)
    service["spec"]["ports"][0]["nodePort"] = 30000
    assert errors(service)


def test_unexpected_kinds_and_missing_namespace():
    assert errors({"kind": "ClusterRoleBinding", "metadata": {"name": "x", "namespace": "ahk"}})
    service = copy.deepcopy(SERVICE)
    del service["metadata"]["namespace"]
    assert errors(service)
