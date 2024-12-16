from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True)
class Service:
    name: str
    address: Optional[str] = field(default=None, compare=False)
    port: Optional[int] = field(default=None, compare=False)

    def __str__(self):
        return f"{self.name} ({self.address}:{self.port})" if self.address and self.port else self.name
