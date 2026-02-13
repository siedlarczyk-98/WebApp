from fastapi import FastAPI, Depends, HTTPException
from sqlmodel import Session, select, create_engine, func
from typing import List, Optional
import pandas as pd
from models import Aluno, Localidade, QuestaoMapeamento, Gabarito
from fastapi.responses import StreamingResponse
from fpdf import FPDF
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
    for col in colunas_q:
        df_corr[col] = 0  # Inicializa tudo como 0 (inteiro)

    for caderno in df_alunos['co_caderno'].unique():
        gab = GABARITO_CACHE.get(caderno)
        if not gab: continue
        mask = df_alunos['co_caderno'] == caderno
        
        for i, col in enumerate(colunas_q):
            if i >= len(gab): break
            if gab[i] in ['X', 'Z', '*']: 
                df_corr.loc[mask, col] = 1
            else: 
                # Comparamos o df_alunos (que tem as letras) mas salvamos no df_corr (que agora é int)
                df_corr.loc[mask, col] = (df_alunos.loc[mask, col] == gab[i]).astype(int)

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
    pdf.cell(0, 10, sanitizar_texto("1. Performance Comparativa"), new_x="LMARGIN", new_y="NEXT")    
    
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
        pdf.cell(w_card, 5, sanitizar_texto(label), align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.set_x(x_pos); pdf.set_font('Helvetica', 'B', 18); pdf.set_text_color(*cor_texto)
        pdf.cell(w_card, 12, valor, align="C", new_x="LMARGIN", new_y="NEXT")

    pdf.set_xy(15, y_topo_cards + h_card + 3)
    pdf.set_font('Helvetica', 'I', 8); pdf.set_text_color(120)
    pdf.cell(0, 8, sanitizar_texto(f"Gap: {bench['gaps']['vs_elite']:+.1f} pp em relação à elite."), new_x="LMARGIN", new_y="NEXT")

    # --- RANKING ---
    pdf.ln(5); pdf.set_font('Helvetica', 'B', 14); pdf.set_text_color(30, 58, 95)
    pdf.cell(0, 10, "2. Posicionamento Competitivo", new_x="LMARGIN", new_y="NEXT")

    def draw_rank(titulo, r_list, pos_atual, total_ies):
        pdf.set_font('Helvetica', 'B', 10); pdf.set_text_color(30, 58, 95)
        pdf.cell(0, 8, sanitizar_texto(f"{titulo} - {pos_atual} de {total_ies}"), new_x="LMARGIN", new_y="NEXT")
        indices = [0, 1, 2]; vizinhança = [pos_atual-2, pos_atual-1, pos_atual]
        for v in vizinhança: 
            if v not in indices and 0 <= v < len(r_list): indices.append(v)
        
        y_bar = pdf.get_y() + 2
        for idx in sorted(list(set(indices))):
            item = r_list[idx]; eh_user = (item['co_curso'] == co_curso)
            pdf.set_xy(15, y_bar); pdf.set_font('Helvetica', 'B' if eh_user else '', 8); pdf.set_text_color(30, 58, 95)
            pdf.cell(25, 6, sanitizar_texto("Sua IES" if eh_user else f"{idx+1} Lugar"), new_x="RIGHT", new_y="TOP", align='R')
            pdf.set_fill_color(*(253, 94, 17) if eh_user else (220, 220, 220))
            largura = (item['media'] / 100) * 120; pdf.rect(45, y_bar, largura, 6, 'F')
            pdf.set_xy(45 + largura + 2, y_bar); pdf.set_font('Helvetica', '', 8); pdf.set_text_color(100)
            pdf.cell(15, 6, f"{item['media']}%"); y_bar += 8
        pdf.set_y(y_bar + 2)

    draw_rank("2.1. Cenário Nacional", ranking_nac, pos_nac, total_nac)
    draw_rank(f"2.2. Cenário Regional ({uf_atual})", ranking_reg, pos_reg, total_reg)

# --- PÁGINA 2: DADOS + ENCERRAMENTO COMERCIAL ---
    pdf.add_page()
    pdf.set_y(55) 

    # 1. CONFIGURAÇÃO DE LAYOUT COMPACTO
    col_area, col_sub, col_diag, col_med, col_gap = 32, 38, 70, 20, 20
    h_linha, w_box, h_box = 7, 16, 5 

    def print_tabela_compacta(titulo, lista, modo_teaser=False):
        pdf.set_font('Helvetica', 'B', 12); pdf.set_text_color(30, 58, 95)
        pdf.cell(0, 8, sanitizar_texto(titulo), new_x="LMARGIN", new_y="NEXT")
        
        pdf.set_fill_color(30, 58, 95); pdf.set_text_color(255, 255, 255); pdf.set_font('Helvetica', 'B', 8)
        pdf.cell(col_area, h_linha, " Área", 0, 0, 'L', True)
        pdf.cell(col_sub, h_linha, " Subespecialidade", 0, 0, 'L', True)
        pdf.cell(col_diag, h_linha, " Diagnóstico", 0, 0, 'L', True)
        pdf.cell(col_med, h_linha, " Média", 0, 0, 'C', True)
        pdf.cell(col_gap, h_linha, " Gap", 0, 1, 'C', True)

        pdf.set_font('Helvetica', '', 7); pdf.set_text_color(60, 60, 60)
        y_inicial_dados = pdf.get_y()

        for i, item in enumerate(lista[:5]):
            fill = (i % 2 == 0); y_at, x_at = pdf.get_y(), pdf.get_x()
            pdf.set_fill_color(245, 245, 245) if fill else pdf.set_fill_color(255, 255, 255)
            
            bloquear = modo_teaser and i > 0 
            
            if bloquear:
                pdf.cell(col_area + col_sub + col_diag + col_med + col_gap, h_linha, "", 0, 1, 'L', fill)
                pdf.set_fill_color(210, 210, 210) 
                pdf.rect(x_at + 2, y_at + 2, col_area - 4, 2.5, 'F')
                pdf.rect(x_at + col_area + 2, y_at + 2, col_sub - 4, 2.5, 'F')
                pdf.rect(x_at + col_area + col_sub + 2, y_at + 2, col_diag - 15, 2.5, 'F')
            else:
                pdf.cell(col_area, h_linha, sanitizar_texto(f" {item['grande_area']}"), 0, 0, 'L', fill)
                pdf.cell(col_sub, h_linha, sanitizar_texto(f" {item['subespecialidade']}"), 0, 0, 'L', fill)
                diag = item.get('diagnostico', 'N/A')
                pdf.cell(col_diag, h_linha, sanitizar_texto(f" {diag[:45]}..."), 0, 0, 'L', fill)
                pdf.cell(col_med, h_linha, f"{item['acerto']*100:.1f}%", 0, 0, 'C', fill)
                
                gap_val = item['gap']
                pdf.set_fill_color(*(200, 0, 0) if gap_val < 0 else (0, 150, 0))
                bx = x_at + col_area + col_sub + col_diag + col_med + ((col_gap - w_box)/2)
                by = y_at + ((h_linha - h_box)/2)
                pdf.rect(bx, by, w_box, h_box, 'F')
                
                pdf.set_xy(x_at + col_area + col_sub + col_diag + col_med, y_at)
                pdf.set_font('Helvetica', 'B', 8); pdf.set_text_color(255, 255, 255)
                pdf.cell(col_gap, h_linha, f"{gap_val:+.1f}", new_x="LMARGIN", new_y="NEXT", align='C')
                pdf.set_font('Helvetica', '', 7); pdf.set_text_color(60, 60, 60)

        if modo_teaser:
            largura_box, altura_box = 130, 22
            pos_x_central = (210 - largura_box) / 2
            y_box = y_inicial_dados + h_linha + 2 
            
            pdf.set_fill_color(255, 255, 255); pdf.set_draw_color(253, 94, 17); pdf.set_line_width(0.6)
            pdf.rect(pos_x_central, y_box, largura_box, altura_box, 'FD')
            
            pdf.set_xy(pos_x_central, y_box + 5)
            pdf.set_font('Helvetica', 'B', 10); pdf.set_text_color(30, 58, 95)
            pdf.cell(largura_box, 6, sanitizar_texto("CONTEÚDO BLOQUEADO NO TEASER"), new_x="LMARGIN", new_y="NEXT", align='C')
            
            pdf.set_x(pos_x_central)
            pdf.set_font('Helvetica', 'B', 11); pdf.set_text_color(253, 94, 17)
            pdf.cell(largura_box, 7, sanitizar_texto("Solicite a versão completa com seu consultor"), new_x="LMARGIN", new_y="NEXT", align='C')

    # --- EXECUÇÃO DAS TABELAS ---
    # Tabela 3 - Mantendo o detalhe verde para pontos críticos
    y_3 = pdf.get_y()
    pdf.set_x(15)
    print_tabela_compacta("3. Pontos Críticos (Gap vs Nacional)", dash['analise']['atencao'], modo_teaser=False)
    
    pdf.ln(4)
    
    # Tabela 4 - SIMPLIFICADO: Sem a caixinha verde conforme solicitado
    pdf.set_x(15) 
    print_tabela_compacta("4. Destaques Institucionais (Top 5)", dash['analise']['fortalezas'], modo_teaser=True)
    
    # --- AJUSTE DE RESPIRAÇÃO: 1cm de respiro ---
    pdf.set_y(pdf.get_y() + 10) 
    y_bloco_comercial = pdf.get_y()

    # Linha divisória dinâmica
    pdf.set_draw_color(220); pdf.set_line_width(0.3)
    pdf.line(15, y_bloco_comercial, 195, y_bloco_comercial)
    pdf.ln(6)

    # DEFINIÇÃO DOS TÓPICOS
    topicos = [
        "Feedback imediato para o estudante",
        "Raciocínio Clínico estruturado e guiado",
        "Matriz Curricular alinhada aos casos",
        "Correção por IA individual"
    ]

    y_inicio_conteudo = pdf.get_y()
    
    # Coluna da Esquerda
    pdf.set_font('Helvetica', 'B', 14); pdf.set_text_color(30, 58, 95)
    pdf.cell(80, 10, sanitizar_texto("Inteligência para o dia a dia"), new_x="LMARGIN", new_y="NEXT")
    
    pdf.set_font('Helvetica', '', 9); pdf.set_text_color(60, 60, 60)
    pdf.multi_cell(80, 4.5, sanitizar_texto("Não basta identificar os erros: é preciso corrigi-los na prática. O Paciente 360 conecta aprendizagem, prática e avaliação."))
    pdf.ln(2)

    for topico in topicos:
        # Detalhe laranja/vermelho na frente do tópico
        pdf.set_fill_color(253, 94, 17)
        pdf.rect(15, pdf.get_y() + 1.2, 2.5, 2.5, 'F') 
        pdf.set_x(20); pdf.set_font('Helvetica', 'B', 9)
        pdf.cell(70, 5, sanitizar_texto(topico), new_x="LMARGIN", new_y="NEXT")

    # --- COLUNA DA DIREITA: IMAGEM AMPLIADA ---
    img_w, img_x, img_y = 92, 103, y_inicio_conteudo

    try:
        pdf.image('cenario_paciente.png', x=img_x, y=img_y, w=img_w)
        pdf.set_xy(img_x + 2, img_y + 53) 
        pdf.set_font('Helvetica', 'I', 8); pdf.set_text_color(100)
        pdf.set_draw_color(253, 94, 17); pdf.set_line_width(0.8)
        pdf.line(img_x, pdf.get_y(), img_x, pdf.get_y() + 8) 
        pdf.set_x(img_x + 3)
        pdf.multi_cell(img_w - 5, 3.5, sanitizar_texto("Da análise de dados à prática: pacientes padronizados para correção imediata dos gaps identificados."))
    except:
        pdf.set_fill_color(245, 245, 245); pdf.rect(img_x, img_y, img_w, 40, 'F')

    # --- BIG NUMBERS E FECHAMENTO ---
    pdf.set_y(242) 
    pdf.set_draw_color(253, 94, 17); pdf.set_line_width(0.5)
    pdf.line(15, pdf.get_y() - 2, 195, pdf.get_y() - 2)
    pdf.ln(5)

    y_final_nums = pdf.get_y()
    
    # Bloco 1 (85%)
    pdf.set_x(15); pdf.set_font('Helvetica', 'B', 32); pdf.set_text_color(30, 58, 95)
    pdf.cell(30, 12, "85%", new_x="RIGHT", new_y="TOP")
    pdf.set_xy(15, y_final_nums + 11); pdf.set_font('Helvetica', 'B', 8); pdf.set_text_color(253, 94, 17)
    pdf.cell(30, 5, "DAS QUESTÕES", new_x="LMARGIN", new_y="NEXT")
    pdf.set_xy(50, y_final_nums + 1); pdf.set_font('Helvetica', '', 8.5); pdf.set_text_color(60, 60, 60)
    pdf.multi_cell(50, 4, sanitizar_texto("Do ENAMED 2025 exigem raciocínio clínico e não memorização."))

    # Bloco 2 (80%)
    pdf.set_xy(105, y_final_nums)
    pdf.set_font('Helvetica', 'B', 32); pdf.set_text_color(30, 58, 95)
    pdf.cell(30, 12, "80%", new_x="RIGHT", new_y="TOP")
    pdf.set_xy(105, y_final_nums + 11); pdf.set_font('Helvetica', 'B', 8); pdf.set_text_color(253, 94, 17)
    pdf.cell(30, 5, "DOS CASOS", new_x="LMARGIN", new_y="NEXT")
    pdf.set_xy(140, y_final_nums + 1); pdf.set_font('Helvetica', '', 8.5); pdf.set_text_color(60, 60, 60)
    pdf.multi_cell(55, 4, sanitizar_texto("Dos casos cobrados no exame já estão prontos na plataforma Paciente 360."))

    # LINHA LARANJA DE FECHAMENTO
    pdf.ln(12)
    pdf.set_draw_color(253, 94, 17); pdf.set_line_width(0.5)
    pdf.line(15, pdf.get_y(), 195, pdf.get_y())
    
    # --- SAÍDA FINAL ---
    pdf_out = pdf.output(dest='S')
    return StreamingResponse(
        io.BytesIO(pdf_out), 
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=Teaser_P360_{co_curso}.pdf"}
    )

# ==========================================
# 3. FILTROS E INICIALIZAÇÃO
# ==========================================

@app.get("/filtros/ufs")
def listar_ufs(session: Session = Depends(get_session)):
    res = session.exec(select(Localidade.sigla_estado).distinct()).all()
    return sorted([str(uf).strip().upper() for uf in res if uf])

@app.get("/filtros/ies")
def listar_ies(
    uf: Optional[str] = None, 
    municipio: Optional[str] = None, 
    session: Session = Depends(get_session)
):
    stmt = select(Aluno.co_curso, Aluno.ies_nome).distinct()
    
    if uf or municipio:
        sub_stmt = select(Localidade.co_curso)
        if uf:
            sub_stmt = sub_stmt.where(Localidade.sigla_estado == uf.upper())
        if municipio:
            sub_stmt = sub_stmt.where(func.upper(Localidade.ies_munic) == municipio.upper())
        stmt = stmt.where(Aluno.co_curso.in_(sub_stmt))

    resultados = session.exec(stmt.order_by(Aluno.ies_nome)).all()

    # --- É AQUI QUE O AJUSTE ENTRA ---
    return [
        {
            "co_curso": r[0], 
            # O .encode('latin-1').decode('utf-8') reconstrói os acentos
            "nome": r[1].encode('latin-1').decode('utf-8') if r[1] else ""
        } 
        for r in resultados
    ]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)