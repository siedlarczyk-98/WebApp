from sqlmodel import Field, SQLModel
from typing import Optional

class Aluno(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    nu_ano: int
    co_curso: int
    co_caderno: int
    ies_nome: str
    p360: str
    enamed_ies: str
    respostas: str 

class QuestaoMapeamento(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    co_caderno: int
    nu_questao: int
    grande_area: str
    subespecialidade: str
    diagnostico: str

class Localidade(SQLModel, table=True):
    co_curso: int = Field(primary_key=True)
    ies_estado: str
    ies_munic: str
    sigla_estado: str

# --- NOVO: A CLASSE QUE FALTAVA ---
class Gabarito(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    co_caderno: int
    respostas_gabarito: str