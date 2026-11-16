"""Provider-independent sandbox interfaces and implementations."""

from app.sandbox.base import ExecResult, SandboxHandle, SandboxProvider, SandboxSpec

__all__ = ["ExecResult", "SandboxHandle", "SandboxProvider", "SandboxSpec"]
