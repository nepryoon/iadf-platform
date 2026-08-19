"""
SandboxRunner: ephemeral rootless gVisor containers with capability restrictions.

ADD §25.3: baseline agent sandboxes are single-task, rootless OCI containers
using gVisor `runsc`, read-only base image, tmpfs work area with quota, dropped
Linux capabilities, no host mount and separate network namespace.

§21.2 tool-loop guard: the sandbox kills the process at a limit; the result is
TIMEOUT, never a request for more permission.

SEC-IADF-002: ephemeral, rootless, resource-limited, deny egress.
"""
import os
import subprocess
from dataclasses import dataclass
from typing import FrozenSet, List, Optional, Tuple, Callable, Any


ALLOWED_CAPABILITIES: FrozenSet[str] = frozenset({"CHOWN", "NET_BIND_SERVICE"})


class CapabilityDeniedError(Exception):
    """Raised when a capability grant is requested that is not in ALLOWED_CAPABILITIES."""
    pass


@dataclass(frozen=True)
class CapabilityGrant:
    """
    A capability grant for a sandbox.
    
    Only capabilities in ALLOWED_CAPABILITIES are permitted (§25.2: intersection, never union).
    """
    name: str
    
    def __post_init__(self) -> None:
        if self.name not in ALLOWED_CAPABILITIES:
            raise CapabilityDeniedError(
                f"Capability '{self.name}' is not in ALLOWED_CAPABILITIES"
            )


@dataclass(frozen=True)
class SandboxSpec:
    """
    Specification for an ephemeral sandbox container.
    
    All numeric resource limits must be positive. Command must be non-empty.
    """
    image: str
    command: Tuple[str, ...]
    workdir: str = "/work"
    memory_mb: int = 512
    cpus: float = 1.0
    pids_limit: int = 256
    tmpfs_mb: int = 256
    timeout_s: int = 120
    grants: Tuple[CapabilityGrant, ...] = ()
    network: bool = False
    use_gvisor: bool = True
    
    def __post_init__(self) -> None:
        if self.memory_mb <= 0:
            raise ValueError("memory_mb must be positive")
        if self.pids_limit <= 0:
            raise ValueError("pids_limit must be positive")
        if self.tmpfs_mb <= 0:
            raise ValueError("tmpfs_mb must be positive")
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if self.cpus <= 0:
            raise ValueError("cpus must be positive")
        if not self.command:
            raise ValueError("command must be non-empty")


@dataclass(frozen=True)
class SandboxResult:
    """
    Result of a sandbox execution.
    
    exit_code: Process exit code (124 for timeout per §21.2).
    stdout: Standard output captured from the process.
    stderr: Standard error captured from the process.
    timed_out: True if the process was killed due to timeout.
    """
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


class SandboxRunner:
    """
    Lifecycle manager for ephemeral rootless containers.
    
    Hermetic-testable via executor injection. Tests inject a fake executor
    and assert on the exact argv; tests never launch a real container.
    """
    
    def __init__(self, runtime: str = "podman", executor: Optional[Callable] = None) -> None:
        """
        Initialize the sandbox runner.
        
        Args:
            runtime: Container runtime binary (default: "podman").
            executor: Callable for process execution (default: subprocess.run).
        """
        self.runtime = runtime
        self.executor = executor if executor is not None else subprocess.run
    
    def build_argv(self, spec: SandboxSpec) -> List[str]:
        """
        Build the exact argv for the container runtime invocation.
        
        Returns the command-line arguments in the exact order specified by ADD §25.3.
        """
        argv = [self.runtime, "run", "--rm", "--pull=never"]
        
        # gVisor runtime if requested
        if spec.use_gvisor:
            argv.extend(["--runtime", "runsc"])
        
        # Network configuration
        network_mode = "slirp4netns" if spec.network else "none"
        argv.extend(["--network", network_mode])
        
        # User and capability restrictions
        argv.extend(["--user", "1000:1000", "--cap-drop", "ALL"])
        
        # Add granted capabilities in order
        for grant in spec.grants:
            argv.extend(["--cap-add", grant.name])
        
        # Security and resource limits
        argv.extend([
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(spec.pids_limit),
            "--memory", f"{spec.memory_mb}m",
            "--cpus", str(spec.cpus),
            "--read-only",
            "--tmpfs", f"{spec.workdir}:rw,size={spec.tmpfs_mb}m",
            "--workdir", spec.workdir,
        ])
        
        # Image and command
        argv.append(spec.image)
        argv.extend(spec.command)
        
        return argv
    
    def run(self, spec: SandboxSpec) -> SandboxResult:
        """
        Execute the sandbox container.
        
        §21.2 kill-at-limit: timeout results in exit_code=124 and timed_out=True,
        never raises an exception to the caller.
        
        Args:
            spec: Sandbox specification.
            
        Returns:
            SandboxResult with execution outcome.
        """
        argv = self.build_argv(spec)
        
        try:
            result = self.executor(
                argv,
                timeout=spec.timeout_s,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=os.environ.copy(),
            )
            return SandboxResult(
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                timed_out=False,
            )
        except subprocess.TimeoutExpired as e:
            # §21.2: timeout is mapped to exit_code=124, never re-raised
            stdout = ""
            stderr = ""
            
            # Decode partial output if present
            if e.output is not None:
                if isinstance(e.output, bytes):
                    stdout = e.output.decode("utf-8", errors="replace")
                else:
                    stdout = e.output
            
            if e.stderr is not None:
                if isinstance(e.stderr, bytes):
                    stderr = e.stderr.decode("utf-8", errors="replace")
                else:
                    stderr = e.stderr
            
            return SandboxResult(
                exit_code=124,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
            )
