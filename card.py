import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Card:
    exam_tag: str
    number: int
    question_html: str
    options: list[str]
    correct: Optional[int] = None
    explanation_html: str = ""
    question_image: Optional[str] = None
    explanation_image: Optional[str] = None
    source: str = ""
    include: bool = True
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def correct_text(self) -> str:
        if self.correct and 1 <= self.correct <= len(self.options):
            return self.options[self.correct - 1]
        return ""


@dataclass
class ParseResult:
    cards: list[Card] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    media_dir: Optional[str] = None
    exam_tags: list[str] = field(default_factory=list)
