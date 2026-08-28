#!/usr/bin/env python3
"""
Atualiza o histórico de "velas" de contribuições do GitHub e redesenha
commit-candles.svg no estilo MT5 (verde = mais contribuições que o dia
anterior, vermelho = menos).

Cada vela representa um dia, contando a partir de START_DATE (fixo, não
uma janela móvel). A altura do corpo é proporcional ao número de
contribuições daquele dia especificamente (relativo ao máximo do
período), com uma altura mínima sempre visível — para que dias de baixa
atividade não colapsem em um pontinho ilegível.

Histórico persistido em candles-data.json (lista de {date, count}).
"""
import json
import os
import sys
import urllib.request
import datetime

GITHUB_TOKEN = os.environ["GH_TOKEN"]
GITHUB_USER = os.environ["GH_USERNAME"]
DATA_FILE = "candles-data.json"
SVG_FILE = "commit-candles.svg"

# Data fixa de início do histórico — as velas contam a partir daqui.
# Depois de acumular MAX_DAYS velas, o gráfico vira uma janela móvel
# dos últimos MAX_DAYS dias (nunca antes de START_DATE).
START_DATE = datetime.date(2026, 6, 26)
MAX_DAYS = 90

GRAPHQL_URL = "https://api.github.com/graphql"


def fetch_contributions(username, token, since, until):
    """Busca o calendário de contribuições entre `since` e `until`
    (inclusive) via GraphQL. A API limita cada consulta a 1 ano, então
    isso é feito em janelas de até 365 dias."""
    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          contributionCalendar {
            weeks {
              contributionDays {
                date
                contributionCount
              }
            }
          }
        }
      }
    }
    """
    all_days = []
    window_start = since
    while window_start <= until:
        window_end = min(window_start + datetime.timedelta(days=364), until)
        variables = {
            "login": username,
            "from": f"{window_start.isoformat()}T00:00:00Z",
            "to": f"{window_end.isoformat()}T23:59:59Z",
        }
        body = json.dumps({"query": query, "variables": variables}).encode()
        req = urllib.request.Request(
            GRAPHQL_URL,
            data=body,
            headers={
                "Authorization": f"bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": username,
            },
        )
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            print(f"Falha HTTP {e.code} ao consultar a API GraphQL: {detail}", file=sys.stderr)
            sys.exit(1)

        if "errors" in data:
            print("GraphQL errors:", data["errors"], file=sys.stderr)
            sys.exit(1)

        weeks = data["data"]["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]
        for week in weeks:
            for day in week["contributionDays"]:
                all_days.append({"date": day["date"], "count": day["contributionCount"]})

        window_start = window_end + datetime.timedelta(days=1)

    all_days.sort(key=lambda d: d["date"])
    return all_days


def load_existing(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return []


def merge_history(existing, fresh, start_date, max_days):
    """Funde o histórico salvo com os dados frescos da API (a API é a
    fonte da verdade). Cresce a partir de start_date até acumular
    max_days velas; depois disso, vira uma janela móvel dos últimos
    max_days dias (nunca voltando para antes de start_date, que já não
    acontece pois a lista nunca tem menos de max_days itens quando isso
    passaria a importar)."""
    by_date = {d["date"]: d["count"] for d in existing}
    for d in fresh:
        by_date[d["date"]] = d["count"]
    merged = [{"date": k, "count": v} for k, v in sorted(by_date.items())]
    # só ontem para trás (dia de hoje ainda está em andamento) e nunca
    # antes da data fixa de início
    today = datetime.date.today().isoformat()
    start_str = start_date.isoformat()
    merged = [d for d in merged if start_str <= d["date"] < today]
    # Enquanto tiver <= max_days desde o início, isto não corta nada
    # (crescimento fixo a partir de start_date). Depois de max_days,
    # vira janela móvel dos mais recentes.
    return merged[-max_days:]


def render_svg(days, path):
    # Corta a sequência de dias com 0 contribuições que vem ANTES do
    # primeiro dia com atividade real — evita um trecho inicial de velas
    # mínimas repetidas (candles-data.json continua guardando tudo, isso
    # só afeta o desenho). Se não houver nenhum dia com atividade ainda,
    # mantém a lista como está (não teria o que cortar).
    first_active = next((i for i, d in enumerate(days) if d["count"] > 0), None)
    if first_active is not None:
        days = days[first_active:]

    if not days:
        print("Nenhum dado de contribuição para desenhar (ainda).", file=sys.stderr)
        # escreve um SVG vazio em vez de deixar o arquivo antigo/inexistente
        empty = (
            '<svg width="900" height="140" viewBox="0 0 900 140" '
            'xmlns="http://www.w3.org/2000/svg">'
            '<rect width="900" height="140" fill="#0b0e14"/></svg>'
        )
        with open(path, "w") as f:
            f.write(empty)
        return

    counts = [d["count"] for d in days]
    n = len(days)

    # "Preço" acumulado suavizado: cada dia soma sua contagem normalizada,
    # dando o efeito de tendência contínua (open do dia = close do
    # anterior) — o mesmo estilo do mockup original aprovado. A "linha
    # neutra" é a MÉDIA real do período (não um valor fixo), então o
    # nível oscila em torno da média em vez de derivar para um lado só
    # quando a atividade típica do período é bem diferente de ~5/dia.
    #
    # Movimentos de queda (dia abaixo da média) pesam metade do que os
    # de alta (dia acima da média) — DOWN_WEIGHT abaixo — para que o
    # gráfico tenda mais facilmente para uma tendência positiva.
    avg_count = sum(counts) / len(counts) if counts else 0.0
    cap = max(avg_count * 4, 10)  # outliers não distorcem o gráfico
    scale = 3.0
    DOWN_WEIGHT = 0.5  # quedas valem metade do peso das altas
    level = 100.0
    levels = []  # (open, close) por dia
    for c in counts:
        o = level
        c_norm = min(c, cap)
        delta = (c_norm - avg_count) * scale * 0.15
        if delta < 0:
            delta *= DOWN_WEIGHT
        close = o + delta
        levels.append((o, close))
        level = close

    highs = []
    lows = []
    for o, c in levels:
        body_range = abs(c - o)
        wick_extra = max(body_range * 0.4, scale * 0.5)
        highs.append(max(o, c) + wick_extra * 0.6)
        lows.append(min(o, c) - wick_extra * 0.6)

    all_vals = [v for pair in levels for v in pair] + highs + lows
    lo, hi = min(all_vals), max(all_vals)
    rng = max(hi - lo, 1e-6)

    W, H = 900, 140
    pad_top, pad_bottom = 8, 8
    plot_h = H - pad_top - pad_bottom

    def y(v):
        return round(pad_top + (hi - v) / rng * plot_h, 1)

    candle_w = W / n
    body_w = round(max(candle_w * 0.6, 1.2), 1)

    body_parts = []
    for i, ((o, c), h_, l_) in enumerate(zip(levels, highs, lows)):
        x_center = round(i * candle_w + candle_w / 2, 1)
        up = c >= o
        color = "#26a69a" if up else "#ef5350"
        y_open, y_close = y(o), y(c)
        y_high, y_low = y(h_), y(l_)
        body_top = min(y_open, y_close)
        body_h = round(max(abs(y_close - y_open), 1.0), 1)
        x_left = round(x_center - body_w / 2, 1)
        body_parts.append(
            f'<line x1="{x_center}" y1="{y_high}" x2="{x_center}" y2="{y_low}" stroke="{color}"/>'
            f'<rect x="{x_left}" y="{body_top}" width="{body_w}" height="{body_h}" fill="{color}">'
            f'<title>{days[i]["date"]}: {counts[i]} contribuições</title></rect>'
        )
    body_svg = "".join(body_parts)

    grid_parts = []
    for frac in (0.2, 0.5, 0.8):
        gy = round(pad_top + frac * plot_h, 1)
        grid_parts.append(f'<line x1="0" y1="{gy}" x2="{W}" y2="{gy}" stroke="#232837"/>')
    grid_svg = "".join(grid_parts)

    svg = (
        f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">'
        f'<rect width="{W}" height="{H}" fill="#0b0e14"/>'
        f"{grid_svg}{body_svg}</svg>"
    )

    with open(path, "w") as f:
        f.write(svg)


def main():
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)

    if yesterday < START_DATE:
        print(f"Ainda não chegou em {START_DATE.isoformat()} — nada para desenhar ainda.")
        render_svg([], SVG_FILE)
        with open(DATA_FILE, "w") as f:
            json.dump([], f, indent=2)
        return

    fresh = fetch_contributions(GITHUB_USER, GITHUB_TOKEN, START_DATE, yesterday)
    existing = load_existing(DATA_FILE)
    merged = merge_history(existing, fresh, START_DATE, MAX_DAYS)

    with open(DATA_FILE, "w") as f:
        json.dump(merged, f, indent=2)

    render_svg(merged, SVG_FILE)
    print(f"OK: {len(merged)} dias desde {START_DATE.isoformat()}, SVG atualizado.")


if __name__ == "__main__":
    main()
