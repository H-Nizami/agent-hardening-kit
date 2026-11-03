#!/usr/bin/env python3
"""Policy-check rendered Helm manifests for the local lab chart.

Reads a multi-document YAML stream (from `helm template`) and fails on any
violation. Schema validation against a real API server is done separately by
the owned-cluster driver with a strict server-side dry run.
"""

import argparse
import re
import sys
from pathlib import Path

import yaml

WORKLOADS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "Pod"}
ALLOWED_KINDS = {"ConfigMap", "Deployment", "NetworkPolicy", "PersistentVolumeClaim", "Service"}
PINNED = re.compile(r"^[^:@\s]+(:[A-Za-z0-9_.-]+)?@sha256:[0-9a-f]{64}$")


def pod_spec(doc):
    spec = doc.get("spec", {})
    return spec if doc["kind"] == "Pod" else spec.get("template", {}).get("spec", {})


def check(docs, local_images):
    errors = []
    names = set()
    for doc in docs:
        kind, name = doc.get("kind"), doc.get("metadata", {}).get("name", "?")
        where = f"{kind}/{name}"
        names.add(where)
        if kind not in ALLOWED_KINDS:
            errors.append(f"{where}: kind not allowed in the local lab chart")
        if not doc.get("metadata", {}).get("namespace"):
            errors.append(f"{where}: namespace must be explicit")
        if kind == "Service":
            if doc["spec"].get("type", "ClusterIP") != "ClusterIP":
                errors.append(f"{where}: only ClusterIP Services are allowed")
            if doc["spec"].get("externalIPs") or any("nodePort" in port for port in doc["spec"].get("ports", [])):
                errors.append(f"{where}: external IPs/node ports are not allowed")
        if kind not in WORKLOADS:
            continue
        spec = pod_spec(doc)
        if spec.get("automountServiceAccountToken") is not False:
            errors.append(f"{where}: service-account token must not be mounted")
        if spec.get("hostNetwork") or spec.get("hostPID") or spec.get("hostIPC"):
            errors.append(f"{where}: host namespaces are not allowed")
        pod_security = spec.get("securityContext", {})
        if pod_security.get("runAsNonRoot") is not True:
            errors.append(f"{where}: pod must set runAsNonRoot")
        if pod_security.get("seccompProfile", {}).get("type") != "RuntimeDefault":
            errors.append(f"{where}: pod must use the RuntimeDefault seccomp profile")
        for volume in spec.get("volumes", []):
            if "hostPath" in volume:
                errors.append(f"{where}: hostPath volume {volume['name']} is not allowed")
        for container in spec.get("initContainers", []) + spec.get("containers", []):
            label = f"{where}/{container['name']}"
            image = container.get("image", "")
            if not (PINNED.match(image) or image in local_images):
                errors.append(f"{label}: image must be pinned by digest ({image})")
            if image.endswith(":latest") or ":" not in image.split("/")[-1]:
                errors.append(f"{label}: floating image tag")
            security = container.get("securityContext", {})
            if security.get("allowPrivilegeEscalation") is not False:
                errors.append(f"{label}: allowPrivilegeEscalation must be false")
            if security.get("privileged"):
                errors.append(f"{label}: privileged containers are not allowed")
            if security.get("readOnlyRootFilesystem") is not True:
                errors.append(f"{label}: root filesystem must be read-only")
            if "ALL" not in security.get("capabilities", {}).get("drop", []):
                errors.append(f"{label}: capabilities must drop ALL")
            if security.get("capabilities", {}).get("add"):
                errors.append(f"{label}: added capabilities are not allowed")
            resources = container.get("resources", {})
            for section in ("requests", "limits"):
                for resource in ("cpu", "memory"):
                    if resource not in resources.get(section, {}):
                        errors.append(f"{label}: missing {section}.{resource}")
    return errors, names


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rendered", type=Path)
    parser.add_argument("--local-image", action="append", default=[],
                        help="locally built image reference allowed without a registry digest")
    args = parser.parse_args()
    docs = [doc for doc in yaml.safe_load_all(args.rendered.read_text()) if doc]
    errors, names = check(docs, set(args.local_image))
    for required in ("Deployment/", "PersistentVolumeClaim/"):
        if not any(name.startswith(required) and name.endswith("-worker") for name in names):
            errors.append(f"{required}: expected resource is missing")
    for error in errors:
        print("policy:", error, file=sys.stderr)
    print(f"Manifest policy: {len(docs)} objects, {len(errors)} violations")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
