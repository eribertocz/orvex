"""The correctness engine (P2-11): whether a bound query has exactly one correct answer."""

from orvex.semantic.correctness.analysis import IMPLEMENTED, RULES, SHAPES, Shape, analyze
from orvex.semantic.correctness.report import CorrectnessReport, Obligation

__all__ = [
    "IMPLEMENTED",
    "RULES",
    "SHAPES",
    "CorrectnessReport",
    "Obligation",
    "Shape",
    "analyze",
]
