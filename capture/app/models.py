from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, computed_field


class Plate(BaseModel):
    id: str
    condition_id: str
    name: str
    condition_name: Optional[str] = None  # user-given name; None on legacy plates
    plate_number: int
    created_at: str

    @computed_field
    @property
    def folder_name(self) -> str:
        return f"{self.condition_id}_{self.name}_plate{self.plate_number:02d}"


class Session(BaseModel):
    id: str
    name: str
    assay_mode: Literal["motility", "survival"]
    assay_config: Dict[str, Any] = Field(default_factory=dict)
    created_at: str
    plates: List[Plate] = Field(default_factory=list)
    schema_version: int = 1


class CreateSessionRequest(BaseModel):
    name: str
    assay_mode: Literal["motility", "survival"]
    assay_config: Dict[str, Any] = Field(default_factory=dict)


class CreatePlateRequest(BaseModel):
    condition_id: str
    name: str
    condition_name: Optional[str] = None
    plate_number: int
    replicates: int = 1
