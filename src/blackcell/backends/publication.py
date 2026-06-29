"""Read-only local publication backend."""

from typing import Protocol

from blackcell.contracts.publication import PublicationSnapshot, PublicationStage


class PublicationBackend(Protocol):
    def snapshot(self, stage: PublicationStage) -> PublicationSnapshot: ...
