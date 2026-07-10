"""Configuration adapters."""

from blackcell.adapters.config.gateway_yaml import GatewayConfigurationError, load_gateway_config

__all__ = ["GatewayConfigurationError", "load_gateway_config"]
