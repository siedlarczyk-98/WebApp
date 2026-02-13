from fastapi import FastAPI, Depends, HTTPException
from sqlmodel import Session, select, create_engine
from typing import List, Optional
import pandas as pd
from models import Aluno, Localidade, QuestaoMapeamento, Gabarito
from fastapi.responses import StreamingResponse
from fpdf import FPDF
import tempfile
import os
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
            if not gabs: return {}, pd.DataFrame()
            
            gabarito_map = {g.co_caderno: list(g.respostas_gabarito) for g in gabs}
            mapas = session.exec(select(QuestaoMapeamento)).all()
            df_mapa = pd.DataFrame([m.model_dump() for m in mapas])
            
            return gabarito_map, df_mapa
    except Exception as e:
        print(f"❌ Erro ao carregar contexto: {e}")
        return {}, pd.DataFrame()

GABARITO_CACHE, DF_MAPA_CACHE = carregar_contexto()

# --- FUNÇÕES DE SUPORTE E RANKING ---

def obter_referencial_nacional(session: Session):
    """Calcula a média Brasil por diagnóstico para base de Gap Real"""
    todos_alunos = session.exec(select(Aluno)).all()
    lista_acertos = []
    for al in todos_alunos:
        gab = GABARITO_CACHE.get(al.co_caderno)
        if not gab: continue
        res = list(al.respostas)
        for i in range(min(len(res), 100)):
            resp_al = str(res[i]).strip().upper()
            resp_gab = str(gab[i]).strip().upper()
            acerto = 1 if resp_gab in ['X', 'Z', '*'] else (1 if resp_al == resp_gab else 0)
            lista_acertos.append({"nu_questao": i + 1, "co_caderno": al.co_caderno, "acerto": acerto})
    
    df_nacional = pd.DataFrame(lista_acertos)
    df_nacional = pd.merge(df_nacional, DF_MAPA_CACHE, on=['nu_questao', 'co_caderno'])
    return df_nacional.groupby(['grande_area', 'subespecialidade', 'diagnostico'])['acerto'].mean().reset_index()

def obter_ranking_ies(session: Session, co_curso: int, uf: Optional[str] = None):
    """Gera o ranking para o posicionamento competitivo no PDF"""
    statement = select(Aluno.co_curso, Aluno.ies_nome, Aluno.respostas, Aluno.co_caderno)
    if uf:
        sub = select(Localidade.co_curso).where(Localidade.sigla_estado == uf)
        statement = statement.where(Aluno.co_curso.in_(sub))
    
    todos = session.exec(statement).all()
    resultados = {}
    for r in todos:
        if r.co_curso not in resultados: 
            resultados[r.co_curso] = {"nome": r.ies_nome, "acertos": 0, "total": 0, "co_curso": r.co_curso}
        gab = GABARITO_CACHE.get(r.co_caderno)
        if not gab: continue
        res = list(r.respostas)
        for i in range(min(len(res), 100)):
            resultados[r.co_curso]["total"] += 1
            if gab[i] in ['X','Z','*'] or res[i] == gab[i]: resultados[r.co_curso]["acertos"] += 1
    
    ranking = []
    for cid, dados in resultados.items():
        media = (dados["acertos"] / dados["total"] * 100) if dados["total"] > 0 else 0
        ranking.append({"co_curso": cid, "nome": dados["nome"], "media": round(media, 1)})
    
    ranking = sorted(ranking, key=lambda x: x['media'], reverse=True)
    posicao = next((i for i, item in enumerate(ranking) if item["co_curso"] == co_curso), 0) + 1
    return ranking, posicao, len(ranking)

def calcular_metricas_curso(co_curso: int, session: Session):
    alunos = session.exec(select(Aluno).where(Aluno.co_curso == co_curso)).all()
    if not alunos: return None
    
    dados_alunos = []
    for al in alunos:
        respostas = list(al.respostas)
        if len(respostas) < 100: respostas += [' ']*(100-len(respostas))
        dados_alunos.append([al.id, int(al.co_caderno)] + respostas[:100])
    
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

    df_long = df_corr.melt(id_vars=['aluno_registro_id', 'co_caderno'], value_vars=colunas_q, var_name='nu_questao', value_name='acerto')
    df_long['nu_questao'] = pd.to_numeric(df_long['nu_questao']).astype(int)
    df_long['co_caderno'] = pd.to_numeric(df_long['co_caderno']).astype(int)
    
    return pd.merge(df_long, DF_MAPA_CACHE, on=['nu_questao', 'co_caderno'], how='inner')

# ==========================================
# 1. ENDPOINTS DE DADOS
# ==========================================

@app.get("/")
def home():
    return {"status": "API P360 Online 🚀"}

@app.get("/ies/{co_curso}/dashboard")
def dashboard_completo(co_curso: int, session: Session = Depends(get_session)):
    df_ies = calcular_metricas_curso(co_curso, session)
    if df_ies is None or df_ies.empty: raise HTTPException(404, detail="IES sem dados")
    
    # Média Nacional de Referência
    df_referencial = obter_referencial_nacional(session).rename(columns={'acerto': 'media_nacional'})
    
    # Média IES por tema
    agrupado_ies = df_ies.groupby(['grande_area', 'subespecialidade', 'diagnostico'])['acerto'].mean().reset_index()
    
    # Merge para cálculo de Gap Real (IES - Nacional)
    df_comparativo = pd.merge(agrupado_ies, df_referencial, on=['grande_area', 'subespecialidade', 'diagnostico'])
    df_comparativo['gap'] = (df_comparativo['acerto'] - df_comparativo['media_nacional']) * 100
    
    fortalezas = df_comparativo.sort_values('gap', ascending=False).to_dict(orient='records')
    atencao = df_comparativo.sort_values('gap', ascending=True).to_dict(orient='records')
    
    media_geral_ies = df_ies['acerto'].mean()
    ies_nome = session.exec(select(Aluno.ies_nome).where(Aluno.co_curso == co_curso)).first()

    return {
        "ies": ies_nome,
        "media_geral": round(float(media_geral_ies * 100), 2),
        "alunos": int(df_ies['aluno_registro_id'].nunique()),
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

@app.get("/ies/{co_curso}/benchmark")
def obter_benchmark(co_curso: int, session: Session = Depends(get_session)):
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
    df_detalhado = calcular_metricas_curso(co_curso, session)
    if df_detalhado is None: raise HTTPException(404, detail="Não há dados.")
    
    relatorio = df_detalhado.groupby(['aluno_registro_id', 'grande_area'])['acerto'].mean().unstack()
    relatorio = relatorio.apply(pd.to_numeric, errors='coerce') * 100
    relatorio['Média Geral (%)'] = relatorio.mean(axis=1, skipna=True).round(2)
    
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        relatorio.to_excel(writer, sheet_name='Desempenho por Aluno')
    output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": f"attachment; filename=Relatorio_IES_{co_curso}.xlsx"})

# ==========================================
# 2. RELATÓRIO PDF (P360 ANALYTICS - VERSÃO TEASER)
# ==========================================

def sanitizar_texto(txt):
    if not isinstance(txt, str): return str(txt)
    mapa = {'\u201c': '"', '\u201d': '"', '\u2018': "'", '\u2019': "'", '\u2013': '-', '–': '-'}
    for o, d in mapa.items(): txt = txt.replace(o, d)
    return txt.encode('latin-1', 'replace').decode('latin-1')

class RelatorioP360(FPDF):
    def header(self):
        # 1. Fundo azul do cabeçalho
        self.set_fill_color(30, 58, 95)
        self.rect(0, 0, 210, 45, 'F')
        
        try:
            self.image('logo_branca.png', x=165, y=10, w=30)
        except:
            pass # Se não achar a imagem, o código não trava
            
        self.set_xy(15, 12)
        self.set_font('Helvetica', 'B', 18)
        self.set_text_color(255, 255, 255)
        self.cell(0, 8, sanitizar_texto("Diagnóstico Microdados ENAMED 2025"), ln=True)
        if hasattr(self, 'ies_info'):
            self.set_font('Helvetica', 'B', 11)
            self.set_text_color(253, 94, 17)
            self.cell(0, 7, sanitizar_texto(f"IES: {self.ies_info['nome']}"), ln=True)
            self.set_font('Helvetica', '', 10)
            self.set_text_color(220, 220, 220)
            texto_sub = f"{self.ies_info['municipio']} - {self.ies_info['uf']} | Conceito ENAMED: {self.ies_info['conceito']}"
            self.cell(0, 6, sanitizar_texto(texto_sub), ln=True)

    def footer(self):
        self.set_y(-15)
        self.set_font('Helvetica', 'I', 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, f"P360 Analytics - Pagina {self.page_no()}", 0, 0, 'C')

@app.get("/ies/{co_curso}/pdf", responses={200: {"content": {"application/pdf": {}}}})
def gerar_pdf_visual(co_curso: int, session: Session = Depends(get_session)):

    dash = dashboard_completo(co_curso, session)
    bench = obter_benchmark(co_curso, session)
    loc = session.exec(select(Localidade).where(Localidade.co_curso == co_curso)).first()
    uf_atual = loc.sigla_estado if loc else None
    conceito = session.exec(select(Aluno.enamed_ies).where(Aluno.co_curso == co_curso)).first()
    
    ranking_nac, pos_nac, total_nac = obter_ranking_ies(session, co_curso)
    ranking_reg, pos_reg, total_reg = obter_ranking_ies(session, co_curso, uf=uf_atual)

    pdf = RelatorioP360()
    pdf.ies_info = {
        'nome': dash['ies'],
        'uf': uf_atual if uf_atual else "-",
        'municipio': loc.ies_munic if loc else "-",
        'conceito': conceito if conceito else "N/A"
    }
    
    pdf.set_margins(15, 15, 15)
    pdf.add_page()
    
    # --- PÁGINA 1: PERFORMANCE ---
    pdf.set_y(55)
    pdf.set_font('Helvetica', 'B', 14); pdf.set_text_color(30, 58, 95)
    pdf.cell(0, 10, sanitizar_texto("1. Performance Comparativa"), ln=True)    
    
    y_topo_cards = pdf.get_y() + 2
    w_card, h_card, gap_card = 58, 28, 3

    for i, (label, valor, cor_fundo, cor_texto) in enumerate([
        ("Sua Média Geral", f"{bench['performance']['ies_atual']}%", (245, 245, 245), (30, 58, 95)),
        ("Média Nacional", f"{bench['performance']['media_nacional']}%", (245, 245, 245), (30, 58, 95)),
        ("Referências Conceito 5", f"{bench['performance']['media_elite_enamed_5']}%", (253, 94, 17), (255, 255, 255))
    ]):
        x_pos = 15 + (i * (w_card + gap_card))
        pdf.set_fill_color(*cor_fundo); pdf.rect(x_pos, y_topo_cards, w_card, h_card, 'F')
        pdf.set_xy(x_pos, y_topo_cards + 5)
        pdf.set_font('Helvetica', '', 8); pdf.set_text_color(100 if i < 2 else 255)
        pdf.cell(w_card, 5, sanitizar_texto(label), align="C", ln=True)
        pdf.set_x(x_pos); pdf.set_font('Helvetica', 'B', 18); pdf.set_text_color(*cor_texto)
        pdf.cell(w_card, 12, valor, align="C", ln=True)

    pdf.set_xy(15, y_topo_cards + h_card + 3)
    pdf.set_font('Helvetica', 'I', 8); pdf.set_text_color(120)
    pdf.cell(0, 8, sanitizar_texto(f"Gap: {bench['gaps']['vs_elite']:+.1f} pp em relação à elite."), ln=True)

    # --- SEÇÃO 2: POSICIONAMENTO ---
    pdf.ln(5); pdf.set_font('Helvetica', 'B', 14); pdf.set_text_color(30, 58, 95)
    pdf.cell(0, 10, "2. Posicionamento Competitivo", ln=True)

    def draw_rank(titulo, r_list, pos_atual, total_ies):
        pdf.set_font('Helvetica', 'B', 10); pdf.set_text_color(30, 58, 95)
        pdf.cell(0, 8, sanitizar_texto(f"{titulo} - {pos_atual} de {total_ies}"), ln=True)
        indices = [0, 1, 2]; vizinhança = [pos_atual-2, pos_atual-1, pos_atual]
        for v in vizinhança: 
            if v not in indices and 0 <= v < len(r_list): indices.append(v)
        
        y_bar = pdf.get_y() + 2
        for idx in sorted(list(set(indices))):
            item = r_list[idx]; eh_user = (item['co_curso'] == co_curso)
            pdf.set_xy(15, y_bar); pdf.set_font('Helvetica', 'B' if eh_user else '', 8); pdf.set_text_color(30, 58, 95)
            pdf.cell(25, 6, sanitizar_texto("Sua IES" if eh_user else f"{idx+1} Lugar"), 0, 0, 'R')
            pdf.set_fill_color(*(253, 94, 17) if eh_user else (220, 220, 220))
            largura = (item['media'] / 100) * 120; pdf.rect(45, y_bar, largura, 6, 'F')
            pdf.set_xy(45 + largura + 2, y_bar); pdf.set_font('Helvetica', '', 8); pdf.set_text_color(100)
            pdf.cell(15, 6, f"{item['media']}%"); y_bar += 8
        pdf.set_y(y_bar + 2)

    draw_rank("2.1. Cenário Nacional", ranking_nac, pos_nac, total_nac)
    draw_rank(f"2.2. Cenário Regional ({uf_atual})", ranking_reg, pos_reg, total_reg)

    pdf.add_page()
    pdf.set_y(55)

    # 1. CONFIGURAÇÃO DE LAYOUT UNIFORME
    col_area, col_sub, col_diag, col_med, col_gap = 32, 38, 70, 20, 20
    h_linha, w_box, h_box = 8, 16, 5

    def print_tabela(titulo, lista, modo_teaser=False):
        pdf.set_fill_color(30, 58, 95); pdf.set_text_color(255, 255, 255); pdf.set_font('Helvetica', 'B', 8)
        pdf.cell(col_area, h_linha, " Grande Área", 0, 0, 'L', True)
        pdf.cell(col_sub, h_linha, " Subespecialidade", 0, 0, 'L', True)
        pdf.cell(col_diag, h_linha, " Diagnóstico", 0, 0, 'L', True)
        pdf.cell(col_med, h_linha, " Média", 0, 0, 'C', True)
        pdf.cell(col_gap, h_linha, " Gap", 0, 1, 'C', True)

        pdf.set_font('Helvetica', '', 7); pdf.set_text_color(60, 60, 60)
        y_inicial_dados = pdf.get_y()

        for i, item in enumerate(lista[:5]):
            fill = (i % 2 == 0)
            y_at, x_at = pdf.get_y(), pdf.get_x()
            pdf.set_fill_color(245, 245, 245) if fill else pdf.set_fill_color(255, 255, 255)
            
            bloquear = modo_teaser and i > 0 
            
            if bloquear:
                pdf.cell(col_area + col_sub + col_diag + col_med + col_gap, h_linha, "", 0, 1, 'L', fill)
                pdf.set_fill_color(220, 220, 220) 
                pdf.rect(x_at + 2, y_at + 2.5, col_area - 4, 3, 'F')
                pdf.rect(x_at + col_area + 2, y_at + 2.5, col_sub - 4, 3, 'F')
                pdf.rect(x_at + col_area + col_sub + 2, y_at + 2.5, col_diag - 10, 3, 'F')
                pdf.rect(x_at + col_area + col_sub + col_diag + 4, y_at + 2.5, col_med - 8, 3, 'F')
                pdf.rect(x_at + col_area + col_sub + col_diag + col_med + 2, y_at + 2.5, col_gap - 4, 3, 'F')
            else:
                pdf.cell(col_area, h_linha, sanitizar_texto(f" {item['grande_area']}"), 0, 0, 'L', fill)
                pdf.cell(col_sub, h_linha, sanitizar_texto(f" {item['subespecialidade']}"), 0, 0, 'L', fill)
                diag = item.get('diagnostico', 'N/A')
                pdf.cell(col_diag, h_linha, sanitizar_texto(f" {diag[:45]}..."), 0, 0, 'L', fill)
                pdf.cell(col_med, h_linha, f"{item['acerto']*100:.1f}%", 0, 0, 'C', fill)
                
                pdf.cell(col_gap, h_linha, "", 0, 0, 'C', fill)
                gap_val = item['gap']
                pdf.set_fill_color(*(200, 0, 0) if gap_val < 0 else (0, 150, 0))
                
                box_x = x_at + col_area + col_sub + col_diag + col_med + ((col_gap - w_box)/2)
                box_y = y_at + ((h_linha - h_box)/2)
                pdf.rect(box_x, box_y, w_box, h_box, 'F')
                
                pdf.set_xy(x_at + col_area + col_sub + col_diag + col_med, y_at)
                pdf.set_font('Helvetica', 'B', 8); pdf.set_text_color(255, 255, 255)
                pdf.cell(col_gap, h_linha, f"{gap_val:+.1f}", 0, 1, 'C')
                pdf.set_font('Helvetica', '', 7); pdf.set_text_color(60, 60, 60)

        # SOBREPOSIÇÃO DO TEASER - CENTRALIZAÇÃO MATEMÁTICA
        if modo_teaser:
            largura_box = 120
            altura_box = 20
            pos_x_central = (210 - largura_box) / 2
            y_centro_bloqueio = y_inicial_dados + h_linha + ((4 * h_linha) / 2) - 10
            
            pdf.set_fill_color(255, 255, 255); pdf.set_draw_color(253, 94, 17); pdf.set_line_width(0.5)
            pdf.rect(pos_x_central, y_centro_bloqueio, largura_box, altura_box, 'FD')
            
            pdf.set_xy(pos_x_central, y_centro_bloqueio + 4)
            pdf.set_font('Helvetica', 'B', 9); pdf.set_text_color(30, 58, 95)
            pdf.cell(largura_box, 5, sanitizar_texto("CONTEÚDO BLOQUEADO NO TEASER"), 0, 1, 'C')
            
            pdf.set_x(pos_x_central) 
            pdf.set_font('Helvetica', 'B', 10); pdf.set_text_color(253, 94, 17)
            pdf.cell(largura_box, 6, sanitizar_texto("Solicite a versão completa com seu consultor"), 0, 1, 'C')

    pdf.set_font('Helvetica', 'B', 14); pdf.set_text_color(30, 58, 95)
    pdf.cell(0, 10, sanitizar_texto("3. Pontos Críticos: Temas com maior defasagem"), ln=True); pdf.ln(2)
    print_tabela("Pontos Críticos", dash['analise']['atencao'], modo_teaser=False)

    pdf.ln(10); pdf.set_font('Helvetica', 'B', 14); pdf.set_text_color(30, 58, 95)
    y_titulo = pdf.get_y()
    pdf.set_fill_color(0, 150, 0); pdf.rect(15, y_titulo + 2, 2, 6, 'F')
    pdf.set_x(20)
    pdf.cell(0, 10, sanitizar_texto("4. Destaques Institucionais (Top 5 Desempenhos)"), ln=True); pdf.ln(2)
    print_tabela("Fortalezas", dash['analise']['fortalezas'], modo_teaser=True)

    pdf_out = pdf.output(dest='S')
    return StreamingResponse(
        io.BytesIO(pdf_out), 
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=Relatorio_Teaser_{co_curso}.pdf"}
    )

# ==========================================
# 3. FILTROS E INICIALIZAÇÃO
# ==========================================

@app.get("/filtros/ufs")
def listar_ufs(session: Session = Depends(get_session)):
    res = session.exec(select(Localidade.sigla_estado).distinct()).all()
    return sorted([str(uf).strip().upper() for uf in res if uf])

@app.get("/filtros/ies")
def listar_ies(uf: Optional[str] = None, session: Session = Depends(get_session)):
    stmt = select(Aluno.co_curso, Aluno.ies_nome).distinct()
    if uf:
        sub = select(Localidade.co_curso).where(Localidade.sigla_estado == uf.upper())
        stmt = stmt.where(Aluno.co_curso.in_(sub))
    resultados = session.exec(stmt.order_by(Aluno.ies_nome)).all()
    return [{"co_curso": r[0], "nome": r[1]} for r in resultados]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)