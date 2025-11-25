import re
from collections import defaultdict

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, render_template_string

app = Flask(__name__)

BASE_TOURNAMENT_URL = "https://labs.limitlesstcg.com/{tid}/decks"


# ---------- UTILIDADES BÁSICAS ----------

def parse_percentage_str(s: str):
    s = s.strip()
    if s.endswith("%"):
        s = s[:-1]
    s = s.replace(",", ".")
    return float(s)


# ---------- PARSER DE OVERALL / DAY2 / DAY1 ----------

def extract_overall_or_day(html):
    """
    Lê o texto inteiro da página e casa linhas do tipo:
    307 Gholdengo Lunatone 14.52% 1295 - 1054 - 456 51.59%

    Retorna lista de dicts:
    {
        'deck': str,
        'players': int,
        'share': float,
        'wins': int,
        'losses': int,
        'ties': int,
        'win_pct': float,
    }
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    pattern = re.compile(
        r"(\d+)\s+"                    # players
        r"([A-Z0-9][^%]+?)\s+"         # deck name (preguiçoso)
        r"(\d+[.,]?\d*)%\s+"           # share %
        r"(\d+)\s*-\s*(\d+)\s*-\s*(\d+)\s+"  # record W - L - T
        r"(\d+[.,]?\d*)%",             # win %
    )

    decks = []
    for m in pattern.finditer(text):
        players = int(m.group(1))
        deck = m.group(2).strip()
        share = parse_percentage_str(m.group(3))
        wins = int(m.group(4))
        losses = int(m.group(5))
        ties = int(m.group(6))
        win_pct = parse_percentage_str(m.group(7))
        decks.append(
            {
                "deck": deck,
                "players": players,
                "share": share,
                "wins": wins,
                "losses": losses,
                "ties": ties,
                "win_pct": win_pct,
            }
        )

    return decks


def extract_conversion(html):
    """
    Lê o texto inteiro e casa linhas do tipo:
    Gholdengo Lunatone 307 77 25.08%

    Retorna lista de dicts:
    {
        'deck': str,
        'day1_players': int,
        'day2_players': int,
        'conv_pct': float,
    }
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    pattern = re.compile(
        r"([A-Z0-9][^%]+?)\s+"       # deck name
        r"(\d+)\s+"                  # day1
        r"(\d+)\s+"                  # day2
        r"(\d+[.,]?\d*)%",           # conv %
    )

    results = []
    for m in pattern.finditer(text):
        deck = m.group(1).strip()
        try:
            day1 = int(m.group(2))
            day2 = int(m.group(3))
        except ValueError:
            continue
        conv_pct = parse_percentage_str(m.group(4))
        results.append(
            {
                "deck": deck,
                "day1_players": day1,
                "day2_players": day2,
                "conv_pct": conv_pct,
            }
        )

    return results


def extract_deck_urls(html, tid):
    """
    Retorna:
      { 'Deck Name': 'https://labs.limitlesstcg.com/XXXX/decks/DECKID', ... }
    a partir da página principal de decks do torneio.
    """
    soup = BeautifulSoup(html, "html.parser")
    deck_urls = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if f"/{tid}/decks/" not in href:
            continue

        name = a.get_text(strip=True)
        if not name:
            continue

        # evita links genéricos de navegação
        if "Decks" in name or "Players" in name or "Conversion" in name:
            continue

        if href.startswith("http"):
            url = href
        else:
            url = f"https://labs.limitlesstcg.com{href}"

        deck_urls.setdefault(name, url)

    return deck_urls


# ---------- PARSER DE MATCHUPS ----------

def extract_matchups_from_html(html_text, this_deck, meta_decks):
    """
    Extrai matchups no formato real do Labs, por exemplo:

    Dragapult Dusknoir 361 182 - 125 - 54 55.40%
    Gardevoir 310 154 - 91 - 65 56.67%

    Formato:
      <NOME> <N> <W> - <L> - <T> <WIN%>%
    """
    result = {}

    for opp in meta_decks:
        if opp == this_deck:
            continue

        pattern = re.compile(
            re.escape(opp) +
            r"\s+(\d+)\s+(\d+)\s*-\s*(\d+)\s*-\s*(\d+)\s+(\d+[.,]?\d*)%",
            re.IGNORECASE,
        )

        m = pattern.search(html_text)
        if not m:
            continue

        # groups: 1 = N (ignorado), 2 = W, 3 = L, 4 = T, 5 = Win%
        w = int(m.group(2))
        l = int(m.group(3))
        t = int(m.group(4))
        win_pct = parse_percentage_str(m.group(5))

        result[opp] = {
            "wins": w,
            "losses": l,
            "ties": t,
            "win_pct": win_pct,
        }

    return result


def fetch_matchups_for_deck(deck_url, this_deck, meta_decks):
    """
    Baixa a página de MATCHUPS do deck e retorna:
      { 'Opponent Deck Name': { wins, losses, ties, win_pct }, ... }
    apenas para opponents que estejam em meta_decks.
    """
    # garante que estamos na aba /matchups
    if not deck_url.endswith("/matchups"):
        deck_url = deck_url.rstrip("/") + "/matchups"

    r = requests.get(deck_url, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(" ", strip=True)

    matchups = extract_matchups_from_html(text, this_deck, meta_decks)
    print(f"[DEBUG] Matchups de {this_deck} em {deck_url}: {list(matchups.keys())}")
    return matchups


# ---------- AGREGAÇÃO ENTRE TORNEIOS (BASE + MATCHUPS) ----------

def fetch_tournament_data_with_links(tid):
    """
    Busca Overall, Day2, Conversion e URLs de decks para um torneio.
    """
    base_url = BASE_TOURNAMENT_URL.format(tid=tid)

    # Página principal (Overall)
    r_main = requests.get(base_url, timeout=20)
    r_main.raise_for_status()
    html_main = r_main.text

    overall = extract_overall_or_day(html_main)
    deck_urls = extract_deck_urls(html_main, tid)

    # Day 2
    r_day2 = requests.get(base_url + "?day=2", timeout=20)
    r_day2.raise_for_status()
    day2 = extract_overall_or_day(r_day2.text)

    # Conversion
    r_conv = requests.get(base_url + "?conversion=", timeout=20)
    r_conv.raise_for_status()
    conv = extract_conversion(r_conv.text)

    print(f"[{tid}] overall={len(overall)} decks, day2={len(day2)} decks, conv={len(conv)} decks, links={len(deck_urls)}")

    return {
        "overall": overall,
        "day2": day2,
        "conversion": conv,
        "deck_urls": deck_urls,
    }


def aggregate_tournaments_with_matchups(
    tournament_ids,
    min_players=20,
    meta_pool_size=20,
    matchup_weight=0.5,
):
    """
    1) Agrega Overall/Day2/Conversion.
    2) Calcula base_score.
    3) Define meta_decks = top 'meta_pool_size' em players_overall.
    4) Para cada meta deck, busca matchups contra outros meta decks.
    5) Calcula matchup_score e final_score.

    Retorna:
      base_rows (lista de decks com métricas e scores),
      matchups_agg[deck][opp] = {wins, losses, ties}
    """

    overall_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "ties": 0, "players": 0})
    day2_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "ties": 0, "players": 0})
    conv_stats = defaultdict(lambda: {"day1": 0, "day2": 0})
    deck_urls_global = defaultdict(set)

    # 1) Agregação básica + URLs
    for tid in tournament_ids:
        data = fetch_tournament_data_with_links(tid)

        for d in data["overall"]:
            name = d["deck"]
            overall_stats[name]["wins"] += d["wins"]
            overall_stats[name]["losses"] += d["losses"]
            overall_stats[name]["ties"] += d["ties"]
            overall_stats[name]["players"] += d["players"]
            if name in data["deck_urls"]:
                deck_urls_global[name].add(data["deck_urls"][name])

        for d in data["day2"]:
            name = d["deck"]
            day2_stats[name]["wins"] += d["wins"]
            day2_stats[name]["losses"] += d["losses"]
            day2_stats[name]["ties"] += d["ties"]
            day2_stats[name]["players"] += d["players"]

        for c in data["conversion"]:
            name = c["deck"]
            conv_stats[name]["day1"] += c["day1_players"]
            conv_stats[name]["day2"] += c["day2_players"]

    # 2) Calcula métricas base por deck
    base_rows = []
    for deck, o in overall_stats.items():
        players_overall = o["players"]
        if players_overall < min_players:
            continue

        total_games_overall = o["wins"] + o["losses"] + o["ties"]
        if total_games_overall <= 0:
            continue

        overall_win_pct = (o["wins"] + 0.5 * o["ties"]) / total_games_overall * 100

        d2 = day2_stats.get(deck)
        if d2:
            games_d2 = d2["wins"] + d2["losses"] + d2["ties"]
            if games_d2 > 0:
                day2_win_pct = (d2["wins"] + 0.5 * d2["ties"]) / games_d2 * 100
            else:
                day2_win_pct = None
            players_day2 = d2["players"]
        else:
            day2_win_pct = None
            players_day2 = 0

        c = conv_stats.get(deck)
        if c and c["day1"] > 0:
            conv_pct = c["day2"] / c["day1"] * 100
        else:
            conv_pct = None

        d2_win_for_score = day2_win_pct if day2_win_pct is not None else overall_win_pct
        conv_for_score = conv_pct if conv_pct is not None else 50.0

        w_overall = 0.4
        w_day2 = 0.4
        w_conv = 0.2

        base_score = (
            w_overall * overall_win_pct +
            w_day2 * d2_win_for_score +
            w_conv * conv_for_score
        )

        base_rows.append(
            {
                "deck": deck,
                "players_overall": players_overall,
                "overall_win_pct": overall_win_pct,
                "players_day2": players_day2,
                "day2_win_pct": day2_win_pct,
                "conv_pct": conv_pct,
                "base_score": base_score,
            }
        )

    # Ordena por players_overall para definir meta
    base_rows.sort(key=lambda x: x["players_overall"], reverse=True)

    # 3) Define meta_decks
    meta_decks = {row["deck"] for row in base_rows[:meta_pool_size]}
    total_players_meta = sum(
        row["players_overall"] for row in base_rows if row["deck"] in meta_decks
    )
    meta_weight = {}
    for row in base_rows:
        if row["deck"] in meta_decks and total_players_meta > 0:
            meta_weight[row["deck"]] = row["players_overall"] / total_players_meta

    # 4) Agrega matchups entre meta_decks (simétrico)
    # matchups_agg[deck][opp] = {wins, losses, ties}
    matchups_agg = defaultdict(lambda: defaultdict(lambda: {"wins": 0, "losses": 0, "ties": 0}))

    for deck in meta_decks:
        urls = list(deck_urls_global.get(deck, []))
        if not urls:
            continue

        for url in urls:
            try:
                per_tournament_matchups = fetch_matchups_for_deck(url, deck, meta_decks)
            except Exception as e:
                print(f"[WARN] Falha ao buscar matchups de {deck} em {url}: {e}")
                continue

            for opp, stats in per_tournament_matchups.items():
                w = stats["wins"]
                l = stats["losses"]
                t = stats["ties"]

                # Direção "deck da página" → oponente
                matchups_agg[deck][opp]["wins"] += w
                matchups_agg[deck][opp]["losses"] += l
                matchups_agg[deck][opp]["ties"] += t

                # Direção espelhada: oponente → deck da página
                matchups_agg[opp][deck]["wins"] += l
                matchups_agg[opp][deck]["losses"] += w
                matchups_agg[opp][deck]["ties"] += t

    # 5) Calcula matchup_score e final_score
    for row in base_rows:
        deck = row["deck"]
        m_vs = matchups_agg.get(deck, {})
        matchup_score = 0.0

        for opp, s in m_vs.items():
            games = s["wins"] + s["losses"] + s["ties"]
            if games <= 0:
                continue
            win_pct_vs_opp = (s["wins"] + 0.5 * s["ties"]) / games * 100
            delta = win_pct_vs_opp - 50.0
            weight_opp = meta_weight.get(opp, 0.0)
            matchup_score += weight_opp * delta

        row["matchup_score"] = matchup_score
        row["final_score"] = row["base_score"] + matchup_weight * matchup_score

    # Ordena por final_score
    base_rows.sort(key=lambda x: x["final_score"], reverse=True)
    return base_rows, matchups_agg


# ---------- INTERFACE WEB (FLASK) ----------

HTML_TEMPLATE = """
<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8">
  <title>Meta Aggregator V2 (Matchups) - Limitless Labs</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 20px; background: #111; color: #eee; }
    h1 { color: #ffcc00; }
    textarea, input { width: 100%; margin: 5px 0; padding: 6px; background: #222; color: #eee; border: 1px solid #444; }
    table { border-collapse: collapse; width: 100%; margin-top: 20px; }
    th, td { border: 1px solid #444; padding: 6px 8px; text-align: center; }
    th { background: #333; }
    tr:nth-child(even) { background: #1a1a1a; }
    tr:nth-child(odd) { background: #141414; }
    .small { font-size: 0.9em; color: #aaa; }
    .btn { background: #ffcc00; color: #000; border: none; padding: 8px 16px; cursor: pointer; margin-top: 10px; font-weight: bold; }
    .btn:hover { background: #ffdd33; }
    .container { max-width: 1200px; margin: auto; }
    td.cell-good  { background: #1b5e20; color: #fff; }  /* verde */
    td.cell-bad   { background: #7f1d1d; color: #fff; }  /* vermelho */
    td.cell-neutral { background: #424242; color: #fff; }/* cinza médio */
    td.cell-empty { background: #222; color: #666; }      /* vazio */
    td.cell-diag  { background: #303030; color: #ccc; font-weight: bold; } /* diagonal */
  </style>
</head>
<body>
<div class="container">
  <h1>Meta Aggregator V2 (com Matchups)</h1>
  <p>Coloque abaixo os IDs ou URLs dos torneios do <strong>Limitless Labs</strong> (um por linha):</p>
  <form method="post">
    <textarea name="tournaments" rows="6" placeholder="Exemplos:
0046
https://labs.limitlesstcg.com/0046/decks
0044
0042"></textarea>
    <label class="small">
      Mínimo de jogadores agregados por deck (Overall):
      <input type="number" name="min_players" value="20">
    </label>
    <label class="small">
      Tamanho do meta (número de decks considerados &quot;meta&quot; para matchups):
      <input type="number" name="meta_pool_size" value="20">
    </label>
    <label class="small">
      Peso dos matchups no Score final (β):
      <input type="number" step="0.1" name="matchup_weight" value="0.5">
    </label>
    <button type="submit" class="btn">Analisar com Matchups</button>
  </form>

  {% if error %}
    <p style="color: #ff6666;"><strong>Erro:</strong> {{ error }}</p>
  {% endif %}

  {% if results %}
    <h2>Top 10 decks (Score final com matchups)</h2>
    <table>
      <tr>
        <th>#</th>
        <th>Deck</th>
        <th>Players Overall</th>
        <th>Overall Win %</th>
        <th>Day 2 Win %</th>
        <th>Conv %</th>
        <th>Base Score</th>
        <th>Matchup Score</th>
        <th>Final Score</th>
      </tr>
      {% for row in results %}
      <tr>
        <td>{{ loop.index }}</td>
        <td style="text-align:left">{{ row.deck }}</td>
        <td>{{ row.players_overall }}</td>
        <td>{{ "%.2f"|format(row.overall_win_pct) }}</td>
        <td>
          {% if row.day2_win_pct is not none %}
            {{ "%.2f"|format(row.day2_win_pct) }}
          {% else %}
            -
          {% endif %}
        </td>
        <td>
          {% if row.conv_pct is not none %}
            {{ "%.2f"|format(row.conv_pct) }}
          {% else %}
            -
          {% endif %}
        </td>
        <td>{{ "%.2f"|format(row.base_score) }}</td>
        <td>{{ "%.2f"|format(row.matchup_score) }}</td>
        <td>{{ "%.2f"|format(row.final_score) }}</td>
      </tr>
      {% endfor %}
    </table>

    {% if matrix %}
      <h2>Matriz de matchups entre os Top 10</h2>
      <table>
        <tr>
          <th>Deck vs Deck</th>
          {% for name in matrix_headers %}
            <th style="max-width: 140px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{{ name }}</th>
          {% endfor %}
        </tr>
        {% for i in range(matrix_headers|length) %}
          <tr>
            <th style="text-align:left">{{ matrix_headers[i] }}</th>
            {% for j in range(matrix_headers|length) %}
              {% set cell = matrix[i][j] %}
              <td class="
                {% if cell.type == 'diag' %}
                  cell-diag
                {% elif cell.type == 'empty' %}
                  cell-empty
                {% elif cell.wr is not none %}
                  {% if cell.wr >= 55 %}
                    cell-good
                  {% elif cell.wr <= 45 %}
                    cell-bad
                  {% else %}
                    cell-neutral
                  {% endif %}
                {% else %}
                  cell-empty
                {% endif %}
              ">
                {{ cell.text }}
              </td>
            {% endfor %}
          </tr>
        {% endfor %}
      </table>
      <p class="small">
        Cada célula mostra o desempenho da <strong>linha</strong> contra a <strong>coluna</strong>:<br>
        <code>W-L-T (Win%)</code>, por exemplo <code>6-4-0 (60.0%)</code> = 6 vitórias, 4 derrotas, 0 empates, 60% WR.
      </p>
    {% endif %}
  {% endif %}
</div>
</body>
</html>
"""


def normalize_tournament_id(line):
    line = line.strip()
    if not line:
        return None
    m = re.search(r"/(\d{4})", line)
    if m:
        return m.group(1)
    if line.isdigit():
        return line.zfill(4)
    return None


@app.route("/", methods=["GET", "POST"])
def index():
    results = None
    error = None
    matrix = None
    matrix_headers = None

    if request.method == "POST":
        raw = request.form.get("tournaments", "")
        min_players_raw = request.form.get("min_players", "20")
        meta_pool_size_raw = request.form.get("meta_pool_size", "20")
        matchup_weight_raw = request.form.get("matchup_weight", "0.5")

        try:
            min_players = int(min_players_raw)
        except ValueError:
            min_players = 20

        try:
            meta_pool_size = int(meta_pool_size_raw)
        except ValueError:
            meta_pool_size = 20

        try:
            matchup_weight = float(matchup_weight_raw)
        except ValueError:
            matchup_weight = 0.5

        tids = []
        for line in raw.splitlines():
            tid = normalize_tournament_id(line)
            if tid:
                tids.append(tid)

        if not tids:
            error = "Nenhum ID de torneio válido encontrado."
        else:
            try:
                all_results, matchups_agg = aggregate_tournaments_with_matchups(
                    tids,
                    min_players=min_players,
                    meta_pool_size=meta_pool_size,
                    matchup_weight=matchup_weight,
                )
                if not all_results:
                    error = "Nenhum deck passou os filtros (talvez min_players esteja alto demais?)."
                else:
                    # Top 10
                    results = all_results[:10]

                    # Monta matriz de matchups entre o Top 10
                    matrix_headers = [row["deck"] for row in results]
                    matrix = []
                    
                    for deck_a in matrix_headers:
                        row = []
                        for deck_b in matrix_headers:
                            if deck_a == deck_b:
                                # diagonal (deck vs ele mesmo)
                                row.append({"text": "-", "wr": None, "type": "diag"})
                            else:
                                stats = matchups_agg.get(deck_a, {}).get(deck_b)
                                if not stats:
                                    row.append({"text": "", "wr": None, "type": "empty"})
                                else:
                                    g = stats["wins"] + stats["losses"] + stats["ties"]
                                    if g > 0:
                                        wr = (stats["wins"] + 0.5 * stats["ties"]) / g * 100
                                        text = f'{stats["wins"]}-{stats["losses"]}-{stats["ties"]} ({wr:.1f}%)'
                                        row.append({"text": text, "wr": wr, "type": "data"})
                                    else:
                                        row.append({"text": "", "wr": None, "type": "empty"})
                        matrix.append(row)
            except Exception as e:
                error = f"Falha ao buscar/parsear dados: {e}"

    return render_template_string(
        HTML_TEMPLATE,
        results=results,
        error=error,
        matrix=matrix,
        matrix_headers=matrix_headers,
    )


if __name__ == "__main__":
    app.run(debug=True)
