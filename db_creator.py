import pandas as pd
from sqlmodel import SQLModel, create_engine, Session, text
import os

# Importando do seu arquivo models.py
from models import Aluno, Localidade, QuestaoMapeamento, Gabarito

# --- CONFIGURAÇÃO ---
SQLITE_FILE = "plataforma_educacional.db"
CONN_STR = f"sqlite:///{SQLITE_FILE}"
engine = create_engine(CONN_STR)

# --- UTILITÁRIOS ---
def safe_int(val):
    try:
        if pd.isna(val) or val == '': return 0
        return int(float(val))
    except:
        return 0

def limpar_colunas(df):
    df.columns = [str(c).strip().upper() for c in df.columns]
    return df

# --- FUNÇÕES DE IMPORTAÇÃO ---

def importar_localidades(session):
    print("📍 Processando Localidades...")
    try:
        df = pd.read_excel("mapeamento_localidade.xlsx")
        df = limpar_colunas(df).dropna(subset=['CO_CURSO', 'IES_ESTADO'])
        
        session.exec(text("DELETE FROM localidade"))
        
        objs = []
        for _, row in df.iterrows():
            # A LIMPEZA DEVE OCORRER AQUI DENTRO:
            objs.append(Localidade(
                co_curso=safe_int(row['CO_CURSO']),
                ies_estado=str(row['IES_ESTADO']).strip(),
                ies_munic=str(row.get('IES_MUNIC', '')).upper().strip(),
                sigla_estado=str(row.get('SIGLA_ESTADO', '')).upper().strip()
            ))
        session.add_all(objs)
        session.commit()
        print(f"✅ {len(objs)} Localidades importadas.")
    except Exception as e:
        print(f"⚠️ Erro em Localidades: {e}")

def importar_mapeamento(session):
    print("🗺️ Processando Mapeamento...")
    try:
        df = pd.read_excel("Base_mapeamento.xlsx")
        df = df.dropna(subset=['CO_CADERNO', 'NU_QUESTAO'])
        
        session.exec(text("DELETE FROM questaomapeamento"))
        
        objs = []
        for _, row in df.iterrows():
            r = {k.lower().strip(): v for k, v in row.to_dict().items()}
            objs.append(QuestaoMapeamento(
                co_caderno=safe_int(r.get('co_caderno')),
                nu_questao=safe_int(r.get('nu_questao')),
                grande_area=str(r.get('grande_area', '')).strip(),
                subespecialidade=str(r.get('subespecialidade', '')).strip(),
                diagnostico=str(r.get('diagnostico', r.get('diagnóstico', ''))).strip()
            ))
        session.add_all(objs)
        session.commit()
        print(f"✅ {len(objs)} Questões mapeadas.")
    except Exception as e:
        print(f"⚠️ Erro em Mapeamento: {e}")

def importar_gabarito(session):
    print("🔑 Importando Gabarito...")
    try:
        df = pd.read_csv("base_gabarito.csv", sep=";", engine='python')
        df = limpar_colunas(df)
        session.exec(text("DELETE FROM gabarito"))
        
        objs = []
        for _, row in df.iterrows():
            gabarito_list = [str(row.get(f"DS_VT_GAB_OBJ.{n}", "X")) for n in range(1, 101)]
            objs.append(Gabarito(
                co_caderno=safe_int(row.get("CO_CADERNO")),
                respostas_gabarito="".join(gabarito_list)
            ))
        session.add_all(objs)
        session.commit()
        print(f"✅ {len(objs)} Gabaritos importados.")
    except Exception as e:
        print(f"❌ Erro no Gabarito: {e}")

def importar_alunos(session):
    print("🎓 Processando Alunos (Lote de 2000)...")
    try:
        df = pd.read_csv("base_alunos.csv", sep=";", engine='python', on_bad_lines='skip', encoding='latin1')
        df.columns = [str(c).encode('ascii', 'ignore').decode('ascii').strip().upper() for c in df.columns]
        
        if "NU_ANO" not in df.columns:
            print(f"❌ Coluna NU_ANO não encontrada! Colunas: {list(df.columns[:5])}")
            return

        df = df[df["NU_ANO"].notna()]
        session.exec(text("DELETE FROM aluno"))
        
        total = len(df)
        batch = []
        for i, (idx, row) in enumerate(df.iterrows()):
            respostas_list = [str(row.get(f"DS_VT_ESC_OBJ.{n}", " ")) for n in range(1, 101)]
            
            batch.append(Aluno(
                nu_ano=safe_int(row.get("NU_ANO")),
                co_curso=safe_int(row.get("CO_CURSO")),
                co_caderno=safe_int(row.get("CO_CADERNO")),
                ies_nome=str(row.get("IES_NOME", "Desconhecido")).strip(),
                p360=str(row.get("P360", "N")).strip(),
                enamed_ies=str(row.get("ENAMED_IES", "N")).strip(),
                respostas="".join(respostas_list)
            ))
            
            if len(batch) >= 2000:
                session.add_all(batch)
                session.commit()
                batch = []
                print(f"   Progresso: {i + 1}/{total}...", end="\r")
        
        if batch:
            session.add_all(batch)
            session.commit()
        print(f"\n✅ SUCESSO! {total} alunos importados.")
    except Exception as e:
        print(f"\n❌ Erro crítico em Alunos: {e}")

def main():
    print("🚀 Iniciando migração de dados...")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        importar_localidades(session)
        importar_mapeamento(session)
        importar_gabarito(session)
        importar_alunos(session)
    print(f"\n✨ Banco de dados atualizado com sucesso!")

if __name__ == "__main__":
    main()