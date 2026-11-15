"""The semantic module's one public face (#486 §2 item 1): the implementations of the interface
contracts its `module.toml` provides, and nothing else."""

from orvex.semantic.result_cache import ResultCache

__all__ = ["ResultCache"]
