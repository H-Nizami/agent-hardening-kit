"""Public exports load lazily so Temporal replay imports no HTTP/model stack."""

from importlib import import_module

__all__ = ["Answer", "BudgetStore", "Document", "FixtureBackend", "Quote", "Request", "TenantPolicy", "Usage", "run_request"]


def __getattr__(name):
    module = {"run_request": "agent", "FixtureBackend": "backend", "BudgetStore": "budget"}.get(name)
    if module is None and name in __all__:
        module = "models"
    if module is None:
        raise AttributeError(name)
    return getattr(import_module("." + module, __name__), name)
