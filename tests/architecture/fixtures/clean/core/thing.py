from pydantic import BaseModel

from core.domain.money import Money


class Thing(BaseModel):
    amount: Money
