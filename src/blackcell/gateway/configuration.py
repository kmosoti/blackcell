from dataclasses import dataclass

from blackcell.gateway.profiles import GatewayProfile


@dataclass(frozen=True, slots=True)
class GatewayConfiguration:
    schema_version: str
    profiles: tuple[GatewayProfile, ...]

    def __post_init__(self) -> None:
        if self.schema_version != "gateway-config/v1":
            raise ValueError(f"unsupported gateway configuration {self.schema_version!r}")
        if not self.profiles:
            raise ValueError("gateway configuration requires at least one profile")
        profile_ids = tuple(profile.profile_id for profile in self.profiles)
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("gateway profile ids must be unique")
