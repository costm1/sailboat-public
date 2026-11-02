"""Optional assembly package. Importing it loads no domain or example."""
from .contracts import AssemblySpec, FeedbackPolicy, Piece, Decision, Matcher, SelectionPolicy
from .ledger import Assembly

__all__ = ["Assembly", "AssemblySpec", "FeedbackPolicy", "Piece", "Decision", "Matcher", "SelectionPolicy"]
