from fastapi import FastAPI, Depends, HTTPException
from sqlmodel import Session, select, create_engine
from typing import List, Optional
import pandas as pd
from models import Aluno, Localidade, QuestaoMapeamento, Gabarito
from fastapi.responses import StreamingResponse
import io

# --- CONFIGURAÇÃO DO BANCO ---
sqlite_url = "sqlite:///plataforma_educacional.db"
engine = create_engine(sqlite_url)
app = FastAPI(title="P360 Analytics API")

def get_session():
    with Session(engine) as session:
        yield session

# --- CACHE (CARREGA GABARITOS E MAPAS NA MEMÓRIA) ---
def carregar_contexto():
    try:
        with Session(engine) as session:
            gabs = session.exec(select(Gabarito)).all()
            if not gabs: 
                print("⚠️ Atenção: Gabarito vazio!")
                return {}, pd.DataFrame()
            
            gabarito_map = {g.co_caderno: list(g.respostas_gabarito) for g in gabs}
            
            mapas = session.exec(select(QuestaoMapeamento)).all()
            df_mapa = pd.DataFrame([m.model_dump() for m in mapas])
            
            return gabarito_map, df_mapa
    except Exception as e:
        print(f"❌ Erro ao carregar contexto: {e}")
        return {}, pd.DataFrame()

GABARITO_CACHE, DF_MAPA_CACHE = carregar_contexto()

# --- FUNÇÃO AUXILIAR DE CÁLCULO ---
def calcular_metricas_curso(co_curso: int, session: Session):
    alunos = session.exec(select(Aluno).where(Aluno.co_curso == co_curso)).all()
    if not alunos: return None
    
    dados_alunos = []
    for al in alunos:
        respostas = list(al.respostas)
        if len(respostas) < 100: respostas += [' ']*(100-len(respostas))
        dados_alunos.append([al.id, al.co_caderno] + respostas[:100])
    
    colunas_q = [n for n in range(1, 101)]
    df_alunos = pd.DataFrame(dados_alunos, columns=['aluno_registro_id', 'co_caderno'] + colunas_q)

    df_corr = df_alunos.copy()
    for caderno in df_alunos['co_caderno'].unique():
        gab = GABARITO_CACHE.get(caderno)
        if not gab: continue
        mask = df_alunos['co_caderno'] == caderno
        for i, col in enumerate(colunas_q):
            if i >= len(gab): break
            if gab[i] in ['X', 'Z', '*']: df_corr.loc[mask, col] = 1
            else: df_corr.loc[mask, col] = (df_alunos.loc[mask, col] == gab[i]).astype(int)

    df_long = df_corr.melt(id_vars=['aluno_registro_id'], value_vars=colunas_q, var_name='nu_questao', value_name='acerto')
    df_long['nu_questao'] = df_long['nu_questao'].astype(int)
    
    return pd.merge(df_long, DF_MAPA_CACHE, on='nu_questao', how='inner')

# ==========================================
# 1. ROTAS DE DASHBOARD E MATRIZ
# ==========================================

@app.get("/")
def home():
    return {"status": "API P360 Online 🚀"}

@app.get("/ies/{co_curso}/dashboard")
def dashboard_completo(co_curso: int, session: Session = Depends(get_session)):
    df = calcular_metricas_curso(co_curso, session)
    if df is None: raise HTTPException(404, detail="IES não encontrada")
    
    media_ies = df['acerto'].mean()
    agrupado = df.groupby(['grande_area', 'subespecialidade'])['acerto'].mean().reset_index()
    
    agrupado['gap'] = (agrupado['acerto'] - 0.50) * 100
    fortalezas = agrupado.sort_values('gap', ascending=False).head(5).to_dict(orient='records')
    atencao = agrupado.sort_values('gap', ascending=True).head(5).to_dict(orient='records')
    
    ies_nome = session.exec(select(Aluno.ies_nome).where(Aluno.co_curso == co_curso)).first()

    return {
        "ies": ies_nome,
        "media_geral": round(float(media_ies * 100), 2),
        "alunos": int(df['aluno_registro_id'].nunique()),
        "analise": {"fortalezas": fortalezas, "atencao": atencao}
    }

@app.get("/ies/{co_curso}/matriz")
def matriz_priorizacao(co_curso: int, session: Session = Depends(get_session)):
    df = calcular_metricas_curso(co_curso, session)
    if df is None: raise HTTPException(404)
    
    matriz = df.groupby(['grande_area', 'subespecialidade']).agg(
        acerto_medio=('acerto', 'mean'),
        volume_questoes=('nu_questao', 'nunique')
    ).reset_index()
    
    matriz['acerto_medio'] = matriz['acerto_medio'] * 100
    return matriz.to_dict(orient='records')

# ==========================================
# 2. ROTA DE BENCHMARK (ENAMED 5)
# ==========================================

@app.get("/ies/{co_curso}/benchmark")
def obter_benchmark(co_curso: int, session: Session = Depends(get_session)):
    """Compara a IES com a Média Nacional e Elite (ENAMED 5)."""
    todos_alunos = session.exec(select(Aluno)).all()
    if not todos_alunos: raise HTTPException(404, detail="Banco vazio")

    def calcular_media_lista(lista):
        acertos, total = 0, 0
        for al in lista:
            gab = GABARITO_CACHE.get(al.co_caderno)
            if not gab: continue
            res = list(al.respostas)
            for i in range(min(len(res), 100)):
                total += 1
                if gab[i] in ['X','Z','*'] or res[i] == gab[i]: acertos += 1
        return (acertos / total * 100) if total > 0 else 0

    media_ies = calcular_media_lista([a for a in todos_alunos if a.co_curso == co_curso])
    media_nac = calcular_media_lista(todos_alunos)
    media_elite = calcular_media_lista([a for a in todos_alunos if str(a.enamed_ies).strip() == '5'])

    return {
        "performance": {
            "ies_atual": round(media_ies, 2),
            "media_nacional": round(media_nac, 2),
            "media_elite_enamed_5": round(media_elite, 2)
        },
        "gaps": {
            "vs_nacional": round(media_ies - media_nac, 2),
            "vs_elite": round(media_ies - media_elite, 2)
        }
    }

@app.get("/ies/{co_curso}/exportar")
def exportar_excel(co_curso: int, session: Session = Depends(get_session)):
    """
    Gera um relatório detalhado em Excel tratando erros de tipos não numéricos.
    """
    df_detalhado = calcular_metricas_curso(co_curso, session)
    if df_detalhado is None:
        raise HTTPException(404, detail="Não há dados para exportar.")

    relatorio = df_detalhado.groupby(['aluno_registro_id', 'grande_area'])['acerto'].mean().unstack()
    relatorio = relatorio.apply(pd.to_numeric, errors='coerce')
    relatorio = relatorio * 100
    
    relatorio['Média Geral (%)'] = relatorio.mean(axis=1, skipna=True).round(2)
    relatorio = relatorio.round(2)
    
    # 3. Criar o arquivo Excel em memória
    output = io.BytesIO()
    try:
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            # Aba 1: Desempenho por Aluno
            relatorio.to_excel(writer, sheet_name='Desempenho por Aluno')
            
            # Aba 2: Análise por Tema
            matriz = df_detalhado.groupby(['grande_area', 'subespecialidade'])['acerto'].mean().reset_index()
            matriz['Acerto (%)'] = (pd.to_numeric(matriz['acerto'], errors='coerce') * 100).round(2)
            matriz.drop(columns=['acerto']).to_excel(writer, sheet_name='Analise por Tema', index=False)
    except Exception as e:
        print(f"Erro ao gerar Excel: {e}")
        raise HTTPException(500, detail="Erro interno ao gerar o arquivo Excel.")

    output.seek(0)

    filename = f"Relatorio_IES_{co_curso}.xlsx"
    return StreamingResponse(
        output, 
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@app.get("/ies/{co_curso}/pdf")
def gerar_pdf_relatorio(co_curso: int, session: Session = Depends(get_session)):
    # 1. Coleta os dados necessários (reutilizando os endpoints que já criamos)
    dash = dashboard_completo(co_curso, session)
    bench = obter_benchmark(co_curso, session)
    
    # 2. Configuração do PDF
    pdf = PDF_Relatorio()
    pdf.add_page()
    pdf.set_font("Arial", size=10)

    # --- CABEÇALHO E RESUMO ---
    pdf.set_font("Arial", "B", 14)
    pdf.cell(0, 10, f"Instituicao: {dash['ies']}", ln=True)
    pdf.set_font("Arial", "", 12)
    pdf.cell(0, 10, f"Aproveitamento Geral: {dash['media_geral']}%", ln=True)
    pdf.cell(0, 10, f"Total de Alunos Avaliados: {dash['alunos']}", ln=True)
    pdf.ln(10)

    # --- TABELA DE BENCHMARK ---
    pdf.set_fill_color(200, 220, 255)
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 10, "Comparativo de Mercado (Benchmark)", ln=True, fill=True)
    pdf.set_font("Arial", "", 11)
    
    pdf.cell(90, 10, "Grupo de Comparacao", border=1)
    pdf.cell(90, 10, "Media de Acerto", border=1, ln=True)
    
    pdf.cell(90, 10, "Sua Instituicao", border=1)
    pdf.cell(90, 10, f"{bench['performance']['ies_atual']}%", border=1, ln=True)
    
    pdf.cell(90, 10, "Media Nacional", border=1)
    pdf.cell(90, 10, f"{bench['performance']['media_nacional']}%", border=1, ln=True)
    
    pdf.cell(90, 10, "Elite (ENAMED 5)", border=1)
    pdf.cell(90, 10, f"{bench['performance']['media_elite_enamed_5']}%", border=1, ln=True)
    pdf.ln(10)

    pdf.set_fill_color(255, 200, 200)
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 10, "Principais Gaps Pedagogicos (Prioridade de Intervencao)", ln=True, fill=True)
    pdf.set_font("Arial", "", 10)
    
    for item in dash['analise']['atencao']:
        area = item['grande_area']
        sub = item['subespecialidade']
        gap = round(item['gap'], 2)
        pdf.cell(0, 8, f"* {area} ({sub}): Gap de {gap}%", ln=True)

    pdf_output = pdf.output(dest='S') # Gera string binária
    
    filename = f"Relatorio_P360_IES_{co_curso}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_output),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# ==========================================
# 3. ROTAS DE FILTROS
# ==========================================

@app.get("/filtros/ufs")
def listar_ufs(session: Session = Depends(get_session)):
    res = session.exec(select(Localidade.sigla_estado).distinct()).all()
    return sorted([str(uf).strip().upper() for uf in res if uf])

@app.get("/filtros/municipios/{uf}")
def listar_municipios(uf: str, session: Session = Depends(get_session)):
    res = session.exec(select(Localidade.ies_munic).where(Localidade.sigla_estado == uf.upper()).distinct()).all()
    return sorted([str(m).strip().upper() for m in res if m])

@app.get("/filtros/ies")
def listar_ies(uf: Optional[str] = None, municipio: Optional[str] = None, session: Session = Depends(get_session)):
    stmt = select(Aluno.co_curso, Aluno.ies_nome).distinct()
    if uf or municipio:
        sub = select(Localidade.co_curso)
        if uf: sub = sub.where(Localidade.sigla_estado == uf.upper())
        if municipio: sub = sub.where(Localidade.ies_munic == municipio.upper())
        stmt = stmt.where(Aluno.co_curso.in_(sub))
    
    resultados = session.exec(stmt.order_by(Aluno.ies_nome)).all()
    return [{"co_curso": r[0], "nome": r[1]} for r in resultados]

# --- INICIALIZAÇÃO ---
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)