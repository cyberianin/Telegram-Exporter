from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExportOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["full", "text", "media", "range", "incremental", "reconcile", "recovery"] = "text"
    strategy: Literal["direct", "compatibility", "recovery", "diagnostic"] = "direct"
    media: Literal["none", "metadata", "selected", "full"] = "metadata"
    format: Literal["jsonl", "json", "html", "txt", "csv", "sqlite", "jsonl.zst"] = "jsonl"
    min_id: int = Field(default=0, ge=0)
    max_id: int | None = Field(default=None, gt=0)
    since: str | None = None
    until: str | None = None
    page_size: int = Field(default=100, ge=1, le=100)
    media_concurrency: int = Field(default=3, ge=1, le=16)
    selected_types: list[str] = Field(default_factory=lambda: ["photo", "document"])
    segment_size: int = Field(default=10000, ge=1, le=100000)
    connection_retries: int = Field(default=2, ge=0, le=5)

    @model_validator(mode="after")
    def validate_range(self) -> ExportOptions:
        from datetime import datetime

        if self.max_id is not None and self.max_id < self.min_id:
            raise ValueError("max_id must be >= min_id")
        for value in (self.since, self.until):
            if value:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    raise ValueError("Date bounds require a timezone")
        if self.since and self.until:
            if datetime.fromisoformat(self.since) > datetime.fromisoformat(self.until):
                raise ValueError("since must be <= until")
        return self
