#!/usr/bin/env python3
"""
Atualiza o histórico de "velas" de contribuições do GitHub e redesenha
commit-candles.svg no estilo MT5 (verde = mais contribuições que o dia
anterior, vermelho = menos).

Cada vela representa um dia. A "abertura" de uma vela é o "fechamento"
(nível acumulado suavizado) da vela anterior, para manter o efeito de
gráfico contínuo. A altura do corpo reflete o número de contribuições
daquele dia especificamente.

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
MAX_DAYS = 90  # mantém só os últimos 90 dias no gráfico

GRAPHQL_URL = "https://api.github.com/graphql"


def fetch_contributions(username, token):
    """Busca o calendário de contribuições dos últimos ~100 dias via GraphQL."""
    query = """
    query($login: String!) {
      user(login: $login) {
        contributionsCollection {
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
    body = json.dumps({"query": query, "variables": {"login": username}}).encode()
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
    days = []
    for week in weeks:
        for day in week["contributionDays"]:
            days.append({"date": day["date"], "count": day["contributionCount"]})
    days.sort(key=lambda d: d["date"])
    return days


def load_existing(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return []


def merge_history(existing, fresh):
    """Funde o histórico salvo com os dados frescos da API (a API é a fonte
    da verdade para qualquer dia que ela cubra; dias mais antigos que já
    saíram da janela da API são preservados do arquivo salvo)."""
    by_date = {d["date"]: d["count"] for d in existing}
    for d in fresh:
        by_date[d["date"]] = d["count"]
    merged = [{"date": k, "count": v} for k, v in sorted(by_date.items())]
    # só ontem para trás (dia de hoje ainda está em andamento, não fecha a vela)
    today = datetime.date.today().isoformat()
    merged = [d for d in merged if d["date"] < today]
    return merged[-MAX_DAYS:]


def render_svg(days, path):
    if not days:
        print("Nenhum dado de contribuição para desenhar.", file=sys.stderr)
        return

    counts = [d["count"] for d in days]
    n = len(days)

    # "Preço" acumulado suavizado: cada dia soma sua contagem normalizada,
    # dando o efeito de tendência contínua (open do dia = close do anterior).
    # Normaliza para uma escala tipo "pontos" para não explodir com dias de
    # muita atividade.
    scale = 3.0
    level = 100.0
    levels = []  # (open, close) por dia
    for c in counts:
        o = level
        c_norm = min(c, 40)  # cap para não distorcer o gráfico com outliers
        close = o + (c_norm - 5) * scale * 0.15  # ~5 contribs/dia = neutro
        levels.append((o, close))
        level = close

    highs = []
    lows = []
    for i, (o, c) in enumerate(levels):
        count = counts[i]
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
    body_w = round(max(candle_w * 0.6, 2.0), 1)

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
    fresh = fetch_contributions(GITHUB_USER, GITHUB_TOKEN)
    existing = load_existing(DATA_FILE)
    merged = merge_history(existing, fresh)

    with open(DATA_FILE, "w") as f:
        json.dump(merged, f, indent=2)

    render_svg(merged, SVG_FILE)
    print(f"OK: {len(merged)} dias, SVG atualizado.")


if __name__ == "__main__":
    main()
