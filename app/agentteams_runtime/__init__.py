"""Small, explicit AgentTeams integration surface for FinFlux.

The package intentionally contains transport and lifecycle code only. Financial
truth is produced by versioned deterministic Skills and validated by
``LiveIntakeRepository``; this package never invents a PASS/BLOCK decision.
"""

from .config import AgentTeamsConfigurationError, AgentTeamsUnavailable
from .service import AgentTeamsService

__all__ = [
    "AgentTeamsConfigurationError",
    "AgentTeamsUnavailable",
    "AgentTeamsService",
]
