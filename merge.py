"""
merge.py — Auditoria de Descontos Manuais · O Boticário
========================================================
Lê todos os CSVs de descontos de uma pasta (OneDrive),
aplica deduplicação e regras de negócio, gera dados.json
e faz deploy automático para o GitHub Pages.

Uso:
    python merge.py
    python merge.py --pasta "C:/Users/Voce/OneDrive/Descontos"
    python merge.py --alerta 35
    python merge.py --sem-deploy        (só gera o JSON, não publica)

Dependências: apenas biblioteca padrão do Python (sem pip necessário)
"""

import csv, json, os, argparse, glob, subprocess
from collections import defaultdict
from datetime import datetime


# ─────────────────────────────────────────────
# CONFIGURAÇÕES — edite aqui uma única vez
# ─────────────────────────────────────────────

PASTA_PADRAO = r"C:/Users/fabio.silva/OneDrive - Gentil Negócios/Documentos/Claude/Projects/Analista Auditor de Descontos/Dados Dashboard descontos varejo/Descontos RGB"
REPO_PADRAO      = os.path.dirname(__file__)   # pasta onde está o merge.py = raiz do repo
SAIDA_PADRAO     = os.path.join(REPO_PADRAO, "dados.json")
THRESHOLD_ALERTA = 40   # % de desconto Manual que dispara alerta


# ─────────────────────────────────────────────
# ARGUMENTOS
# ─────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--pasta",       default=PASTA_PADRAO)
    p.add_argument("--saida",       default=SAIDA_PADRAO)
    p.add_argument("--repo",        default=REPO_PADRAO, help="Pasta raiz do repositório Git")
    p.add_argument("--alerta",      default=THRESHOLD_ALERTA, type=float)
    p.add_argument("--sem-deploy",  action="store_true", help="Não faz git push")
    return p.parse_args()


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def to_num(v):
    try:
        return float((v or "0").strip().replace(".", "").replace(",", "."))
    except ValueError:
        return 0.0

def extrair_tag_manual(motivo):
    partes, tags, vistas = motivo.split(" / "), [], set()
    for p in partes:
        p = p.strip()
        if p in ("Motivo Desconto (Subtotal)", "") : continue
        if "(DF) Desconto fidelidade" in p         : continue
        if p.startswith("1 -")                     : continue
        if p not in vistas:
            tags.append(p)
            vistas.add(p)
    return "; ".join(tags) if tags else "Sem tag"


# ─────────────────────────────────────────────
# LEITURA
# ─────────────────────────────────────────────

def ler_csv(caminho):
    linhas = []
    try:
        with open(caminho, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f, delimiter=";"):
                row = {k.strip(): v.strip() for k, v in row.items()}
                boleto = row.get("N. Boleto", "").strip()
                if not boleto:
                    continue
                cl   = row.get("Codigo Loja", "").strip()
                data = row.get("Data Desconto", "").strip()
                org  = row.get("Origem Desconto", "").strip().upper()
                sku  = row.get("Codigo SKU", "").strip()
                linhas.append({
                    "codigo_loja":   cl,
                    "loja":          row.get("Loja", "").strip(),
                    "origem":        org,
                    "desc_campanha": row.get("Descricao Campanha", "").strip(),
                    "id_campanha":   row.get("ID Campanha", "").strip(),
                    "data":          data,
                    "linha_produto": row.get("Linha", "").strip(),
                    "sku":           sku,
                    "produto":       row.get("Descricao Produto", "").strip(),
                    "qtd":           to_num(row.get("Qtd.", "0")),
                    "bruto":         to_num(row.get("Valor Bruto", "0")),
                    "desconto":      to_num(row.get("Valor Desconto", "0")),
                    "liquido":       to_num(row.get("Valor Liquido", "0")),
                    "motivo":        row.get("Motivo Desconto", "").strip(),
                    "boleto":        boleto,
                    "chave_cupom":   f"{cl}-{data}-{boleto}",
                    "chave_item":    f"{cl}-{data}-{boleto}-{sku}-{org}",
                    "arquivo":       os.path.basename(caminho),
                })
    except Exception as e:
        print(f"  [AVISO] Erro ao ler {caminho}: {e}")
    return linhas


# ─────────────────────────────────────────────
# DEDUPLICAÇÃO
# ─────────────────────────────────────────────

def deduplicar(linhas):
    mapa = {}
    for l in linhas:
        k = l["chave_item"]
        if k in mapa:
            mapa[k]["qtd"]      += l["qtd"]
            mapa[k]["bruto"]    += l["bruto"]
            mapa[k]["desconto"] += l["desconto"]
            mapa[k]["liquido"]  += l["liquido"]
            mapa[k]["_dups"]    += 1
        else:
            l["_dups"] = 1
            mapa[k] = dict(l)
    return list(mapa.values())


# ─────────────────────────────────────────────
# VALIDAÇÃO
# ─────────────────────────────────────────────

def validar(linhas):
    return [
        {"chave_cupom": l["chave_cupom"], "sku": l["sku"],
         "liquido_informado": l["liquido"],
         "liquido_calculado": round(l["bruto"] - l["desconto"], 2)}
        for l in linhas
        if abs(round(l["bruto"] - l["desconto"], 2) - round(l["liquido"], 2)) > 0.02
    ]


# ─────────────────────────────────────────────
# CONSTRUÇÃO DO JSON
# ─────────────────────────────────────────────

def construir_dados(linhas, threshold):
    manuais = [l for l in linhas if l["origem"] == "MANUAL"]
    for m in manuais:
        m["tag"]    = extrair_tag_manual(m["motivo"])
        m["pct"]    = round(m["desconto"] / m["bruto"] * 100, 2) if m["bruto"] > 0 else 0.0
        m["alerta"] = m["pct"] > threshold

    total_cupons        = len(set(l["chave_cupom"] for l in linhas))
    cupons_manuais      = len(set(m["chave_cupom"] for m in manuais))
    total_bruto         = sum(l["bruto"]    for l in linhas if l["origem"] != "SEM DESCONTOS")
    total_manual        = sum(m["desconto"] for m in manuais)
    total_fidelidade    = sum(l["desconto"] for l in linhas if l["origem"] == "FIDELIDADE")
    total_promo         = sum(l["desconto"] for l in linhas if l["origem"] == "PROMOCIONAL")
    custo_franqueada    = total_manual + total_fidelidade
    n_itens             = len(manuais)
    alertas_count       = sum(1 for m in manuais if m["alerta"])
    campanhas_distintas = len(set(m["tag"] for m in manuais if m["tag"] != "Sem tag"))
    medio_abs           = round(total_manual / n_itens, 2) if n_itens else 0
    medio_pct           = round(sum(m["pct"] for m in manuais) / n_itens, 2) if n_itens else 0
    pct_manual_bruto    = round(total_manual / total_bruto * 100, 2) if total_bruto else 0

    datas   = sorted(set(l["data"] for l in linhas))
    periodo = {"inicio": datas[0], "fim": datas[-1], "dias": len(datas)} if datas else {}

    # Por campanha
    pc = defaultdict(lambda: {"total_desconto":0,"total_bruto":0,"n_cupons":set(),"n_itens":0,"skus":set(),"alertas":0})
    for m in manuais:
        c = pc[m["tag"]]
        c["total_desconto"] += m["desconto"]
        c["total_bruto"]    += m["bruto"]
        c["n_cupons"].add(m["chave_cupom"])
        c["n_itens"]        += 1
        c["skus"].add(m["sku"])
        if m["alerta"]: c["alertas"] += 1
    por_campanha = [
        {"tag": t, "total_desconto": round(c["total_desconto"],2),
         "total_bruto": round(c["total_bruto"],2),
         "pct_medio": round(c["total_desconto"]/c["total_bruto"]*100,2) if c["total_bruto"] else 0,
         "n_cupons": len(c["n_cupons"]), "n_itens": c["n_itens"],
         "n_skus_distintos": len(c["skus"]), "alertas": c["alertas"]}
        for t, c in sorted(pc.items(), key=lambda x: -x[1]["total_desconto"])
    ]

    # Por loja
    pl = defaultdict(lambda: {"total_manual":0,"total_fidelidade":0,"total_promo":0,"total_bruto":0,"n_cupons":set(),"alertas":0,"codigo_loja":""})
    for l in linhas:
        lj = pl[l["loja"]]
        lj["codigo_loja"] = l["codigo_loja"]
        lj["n_cupons"].add(l["chave_cupom"])
        if l["origem"] == "MANUAL":
            lj["total_manual"]     += l["desconto"]
            lj["total_bruto"]      += l["bruto"]
            if l.get("alerta"): lj["alertas"] += 1
        elif l["origem"] == "FIDELIDADE": lj["total_fidelidade"] += l["desconto"]
        elif l["origem"] == "PROMOCIONAL": lj["total_promo"]     += l["desconto"]
    por_loja = [
        {"loja": n, "codigo_loja": lj["codigo_loja"],
         "total_desconto_manual": round(lj["total_manual"],2),
         "total_desconto_fidelidade": round(lj["total_fidelidade"],2),
         "total_desconto_promo": round(lj["total_promo"],2),
         "custo_franqueada": round(lj["total_manual"]+lj["total_fidelidade"],2),
         "pct_manual_bruto": round(lj["total_manual"]/lj["total_bruto"]*100,2) if lj["total_bruto"] else 0,
         "n_cupons": len(lj["n_cupons"]), "alertas": lj["alertas"]}
        for n, lj in sorted(pl.items(), key=lambda x: -x[1]["total_manual"])
    ]

    # Itens para tabela
    itens = [
        {"chave_cupom": m["chave_cupom"], "boleto": m["boleto"], "loja": m["loja"],
         "data": m["data"], "sku": m["sku"], "produto": m["produto"],
         "linha": m["linha_produto"], "tag": m["tag"], "qtd": m["qtd"],
         "bruto": round(m["bruto"],2), "desconto": round(m["desconto"],2),
         "liquido": round(m["liquido"],2), "pct": m["pct"],
         "alerta": m["alerta"], "arquivo": m["arquivo"]}
        for m in sorted(manuais, key=lambda x: -x["pct"])
    ]

    return {
        "gerado_em":        datetime.now().strftime("%d/%m/%Y %H:%M"),
        "threshold_alerta": threshold,
        "periodo":          periodo,
        "kpis": {
            "total_linhas_brutas":       len(linhas),
            "total_cupons":              total_cupons,
            "cupons_com_manual":         cupons_manuais,
            "itens_manuais":             n_itens,
            "campanhas_distintas":       campanhas_distintas,
            "alertas_count":             alertas_count,
            "total_desconto_manual":     round(total_manual,2),
            "total_desconto_fidelidade": round(total_fidelidade,2),
            "total_desconto_promo":      round(total_promo,2),
            "custo_franqueada":          round(custo_franqueada,2),
            "pct_manual_sobre_bruto":    pct_manual_bruto,
            "desconto_medio_abs":        medio_abs,
            "desconto_medio_pct":        medio_pct,
        },
        "por_campanha": por_campanha,
        "por_loja":     por_loja,
        "itens":        itens,
        "alertas":      [i for i in itens if i["alerta"]],
    }


# ─────────────────────────────────────────────
# DEPLOY — git add / commit / push
# ─────────────────────────────────────────────

def git_push(repo, mensagem):
    """Faz commit do dados.json e push para o GitHub Pages."""
    def run(cmd):
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"Erro em '{' '.join(cmd)}':\n{r.stderr.strip()}")
        return r.stdout.strip()

    print("\n  Publicando no GitHub Pages...")
    run(["git", "add", "dados.json", "index.html"])

    # Verifica se há algo para commitar
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()

    if not status:
        print("  Nenhuma alteração nos dados — GitHub Pages já está atualizado.")
        return

    run(["git", "commit", "-m", mensagem])
    run(["git", "push"])
    print("  ✓ Deploy concluído — dashboard atualizado em instantes.")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    args = parse_args()
    SEP  = "=" * 55

    print(SEP)
    print("  Auditoria de Descontos Manuais · O Boticário")
    print(SEP)
    print(f"  Pasta CSVs  : {args.pasta}")
    print(f"  Saída JSON  : {args.saida}")
    print(f"  Repositório : {args.repo}")
    print(f"  Alerta      : >{args.alerta}%")
    print(f"  Deploy      : {'NÃO' if args.sem_deploy else 'SIM (GitHub Pages)'}")
    print()

    # 1. Localiza CSVs
    arquivos = sorted(glob.glob(os.path.join(args.pasta, "*.csv")))
    if not arquivos:
        print(f"[ERRO] Nenhum .csv encontrado em: {args.pasta}")
        return

    print(f"  {len(arquivos)} arquivo(s) encontrado(s):")
    for a in arquivos:
        print(f"    · {os.path.basename(a)}")
    print()

    # 2. Lê e combina
    linhas = []
    for arq in arquivos:
        lidas = ler_csv(arq)
        print(f"  [{os.path.basename(arq)}] → {len(lidas)} linhas")
        linhas.extend(lidas)

    # 3. Deduplica
    linhas = deduplicar(linhas)
    print(f"\n  Total após deduplicação: {len(linhas)} linhas")

    # 4. Valida
    inc = validar(linhas)
    if inc:
        print(f"  [AVISO] {len(inc)} linha(s) com inconsistência Bruto−Desconto≠Líquido")
        for i in inc[:3]:
            print(f"    · {i['chave_cupom']} SKU {i['sku']}: informado={i['liquido_informado']} calculado={i['liquido_calculado']}")
    else:
        print("  Validação: OK")

    # 5. Constrói JSON
    dados = construir_dados(linhas, args.alerta)

    # 6. Salva JSON
    os.makedirs(os.path.dirname(os.path.abspath(args.saida)), exist_ok=True)
    with open(args.saida, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)

    k = dados["kpis"]
    per = dados["periodo"]
    print(f"\n  ✓ dados.json salvo")
    print(f"\n  Período: {per.get('inicio','')} → {per.get('fim','')} ({per.get('dias',0)} dia(s))")
    print(f"  Cupons totais          : {k['total_cupons']}")
    print(f"  Cupons com Manual      : {k['cupons_com_manual']}")
    print(f"  Total desconto Manual  : R$ {k['total_desconto_manual']:>10,.2f}".replace(",","X").replace(".",",").replace("X","."))
    print(f"  Custo franqueada total : R$ {k['custo_franqueada']:>10,.2f}".replace(",","X").replace(".",",").replace("X","."))
    print(f"  Alertas (>{args.alerta}%)       : {k['alertas_count']}")

    # 7. Deploy
    if not args.sem_deploy:
        try:
            msg = f"dados: {per.get('inicio','')} → {per.get('fim','')} | {datetime.now().strftime('%d/%m/%Y %H:%M')}"
            git_push(args.repo, msg)
        except RuntimeError as e:
            print(f"\n  [ERRO no deploy] {e}")
            print("  O dados.json foi gerado corretamente — verifique o Git e tente novamente.")

    print(f"\n{SEP}")


if __name__ == "__main__":
    main()
