from sqlmodel import Session, select, create_engine
from models import Aluno
import pandas as pd

sqlite_url = "sqlite:///plataforma_educacional.db"
engine = create_engine(sqlite_url)

with Session(engine) as session:
    # 1. Conta quantos alunos temos
    total = session.query(Aluno).count()
    print(f"📊 Total de Alunos no banco: {total}")
    
    # 2. Pega os primeiros 5 alunos para ver a cara dos dados
    alunos = session.exec(select(Aluno).limit(5)).all()
    
    print("\n🔍 Amostra dos dados (Primeiros 5):")
    for aluno in alunos:
        print(f"ID: {aluno.id} | CO_CURSO: {aluno.co_curso} | IES: {aluno.ies_nome}")

    # 3. Lista os códigos de curso ÚNICOS disponíveis (Para você saber qual testar)
    print("\n✅ Códigos de Curso (CO_CURSO) disponíveis para teste:")
    statement = select(Aluno.co_curso, Aluno.ies_nome).distinct().limit(10)
    cursos = session.exec(statement).all()
    
    for c in cursos:
        print(f"👉 Código: {c[0]}  |  Nome: {c[1]}")