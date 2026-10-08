"""Small common report contract shared by CLI and terminal tools."""

from dataclasses import dataclass, field, asdict
import math
import unicodedata
from typing import Any, Tuple


def clean_text(value: Any, limit: int = 512) -> str:
    """Remove terminal controls at every new display boundary."""
    return "".join(char for char in str(value)
                   if not unicodedata.category(char).startswith("C"))[:limit]


def finite_number(value: Any):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if math.isfinite(value):
                return value
        except OverflowError:
            pass
    return None


@dataclass(frozen=True)
class ToolReport:
    tool: str
    status: str
    summary: str
    columns: Tuple[str, ...] = ()
    rows: Tuple[Tuple[str, ...], ...] = ()
    details: Tuple[str, ...] = ()
    complete: bool = False
    truncated: bool = False
    data: dict = field(default_factory=dict)

    def payload(self) -> dict:
        return {"schema_version": 1, "kind": "tool_report", **asdict(self)}

    def lines(self) -> Tuple[str, ...]:
        result = [clean_text(self.summary), ""]
        if self.columns:
            result.append("  |  ".join(clean_text(v) for v in self.columns))
        result.extend("  |  ".join(clean_text(v) for v in row) for row in self.rows)
        if self.details:
            result.extend(("", *(clean_text(v) for v in self.details)))
        if self.truncated:
            result.append("TRUNCATED · limit reached")
        return tuple(result)
