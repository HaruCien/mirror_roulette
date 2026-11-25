#!/usr/bin/env python3
"""덱 빌더용 데이터 수집기: 인격 스킬 속성(data/identity_skills.json)과 E.G.O 목록(data/ego.json).

출처
- 한국어 이름: LocalizeLimbusCompany 저장소의 KR/EN 게임 텍스트(Personalities*.json, Egos*.json).
  같은 id로 영문·한국어 이름을 짝지어 위키의 영문 이름을 사이트의 한국어 이름으로 바꿉니다.
  (CC BY-NC-SA 4.0, https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany)
  아직 번역 파일에 없는 신규 인격은 data/auto_identities.json(공식 PV 제목 기반 이름)으로 보완하고,
  data/name_overrides.json 에 직접 적은 이름({"identities": {위키 영문: 한국어}, "ego": {...}})이 가장 우선합니다.
- 스킬 1·2·3의 죄악 속성과 공격 유형: limbuscompany.wiki.gg 인격 문서(robots.txt가 허용하는 /wiki/ 경로만).
  주 1회(목요일 인게임 패치 다음 날) 실행하며, 패치로 바뀐 스킬도 반영되도록 인격 문서를 매번 다시 읽습니다.
- E.G.O 등급(ZAYIN~ALEPH)과 죄악 속성: 위키 List_of_E.G.O/Data 표.

출력
- data/identity_skills.json : {수감자id: {"한국어 인격명": {"en": 위키 이름, "skills": [{"type": "slash|pierce|blunt", "sin": "wrath|…"} ×3]}}}
- data/ego.json             : {수감자id: [{"name": 한국어 이름, "en": 위키 이름, "tier": "zayin|teth|he|waw|aleph", "sin": "wrath|…"}]}

사용법: python scripts/sync_deck_data.py [--dry-run] [--report PATH] [--max-pages N]
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse

import requests
from bs4 import BeautifulSoup

WIKI = "https://limbuscompany.wiki.gg"
ID_DATA_PAGE = WIKI + "/wiki/List_of_Identities/Data"
EGO_DATA_PAGE = WIKI + "/wiki/List_of_E.G.O/Data"
LOC_REPO = "LocalizeLimbusCompany/LocalizeLimbusCompany"
LOC_RAW = f"https://raw.githubusercontent.com/{LOC_REPO}/main/"
LOC_TREE = f"https://api.github.com/repos/{LOC_REPO}/git/trees/main?recursive=1"
USER_AGENT = "MirrorRouletteSync/1.0 (daily deck data sync for a fan planner site)"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_PATH = os.path.join(ROOT, "data", "identity_skills.json")
EGO_PATH = os.path.join(ROOT, "data", "ego.json")
AUTO_PATH = os.path.join(ROOT, "data", "auto_identities.json")
OVERRIDES_PATH = os.path.join(ROOT, "data", "name_overrides.json")

# 게임 id의 수감자 번호(1SSNN / 2SSNN의 SS) 순서 = 사이트 수감자 순서
SINNERS = [
    ("yisang", "Yi Sang"), ("faust", "Faust"), ("donquixote", "Don Quixote"), ("ryoshu", "Ryoshu"),
    ("meursault", "Meursault"), ("honglu", "Hong Lu"), ("heathcliff", "Heathcliff"), ("ishmael", "Ishmael"),
    ("rodion", "Rodion"), ("sinclair", "Sinclair"), ("outis", "Outis"), ("gregor", "Gregor"),
]
SINS = ["wrath", "lust", "sloth", "gluttony", "gloom", "pride", "envy"]
TIERS = ["zayin", "teth", "he", "waw", "aleph"]
SKILL_SIN_RE = re.compile(r"^(Wrath|Lust|Sloth|Gluttony|Gloom|Pride|Envy)([123])\.png$")
ATK_RE = re.compile(r"^(Slash|Pierce|Blunt)\.png$")
LCB_SIN_RE = re.compile(r"^LcbSin(Wrath|Lust|Sloth|Gluttony|Gloom|Pride|Envy)\.png$")
TIER_RE = re.compile(r"^(ZAYIN|TETH|HE|WAW|ALEPH) Label\.png$")


def norm(s):
    s = s.replace("ō", "o").replace("ū", "u").replace("Ō", "O").replace("Ū", "U")
    return re.sub(r"\s+", " ", s).strip()


def key(s):
    """이름 비교용 키: 공백·문장부호·대소문자·따옴표 차이를 무시."""
    return re.sub(r"[^0-9a-z가-힣]", "", norm(s).lower())


def get(url):
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
    r.raise_for_status()
    return r


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def sinner_of_id(i):
    n = (i // 100) % 100
    return SINNERS[n - 1] if 1 <= n <= len(SINNERS) else None


def sinner_of_name(name):
    n = norm(name)
    for sid, en in SINNERS:
        if n.endswith(" " + en) or n == en:
            return sid, en
    return None


def strip_sinner(name, en):
    n = norm(name)
    return n[: -len(en) - 1] if n.endswith(" " + en) else n


# ---------- 게임 텍스트(한국어 이름) ----------

def loc_files(prefix):
    """KR/<prefix>*.json 목록. 트리 API가 막히면 기본 파일만."""
    try:
        tree = get(LOC_TREE).json().get("tree", [])
        names = sorted({t["path"].split("/", 1)[1] for t in tree
                        if re.match(rf"^KR/{prefix}[^/]*\.json$", t["path"])})
        if names:
            return names
    except Exception as e:
        print(f"번역 저장소 파일 목록 확인 실패({e}), 기본 파일만 읽습니다.")
    return [prefix + ".json"]


def load_loc(lang, fname):
    try:
        return json.loads(get(LOC_RAW + lang + "/" + fname).content.decode("utf-8-sig")).get("dataList") or []
    except Exception as e:
        print(f"번역 파일 읽기 실패({lang}/{fname}): {e}")
        return []


def build_name_maps():
    """(인격 영문키 -> (수감자id, 한국어 인격명), E.G.O 영문키 -> (수감자id, 한국어 이름))"""
    id_map, ego_map = {}, {}
    for fname in loc_files("Personalities"):
        en = {d.get("id"): d for d in load_loc("EN", fname)}
        for d in load_loc("KR", fname):
            i, e = d.get("id"), en.get(d.get("id"))
            if not isinstance(i, int) or not 10101 <= i <= 11299 or not e:
                continue
            sin = sinner_of_id(i)
            if not sin:
                continue
            en_name = norm(f"{e.get('title', '')} {e.get('name') or sin[1]}")
            ko_name = re.sub(r"::\s+", "::", norm(f"{d.get('title', '')} {d.get('name', '')}"))
            if ko_name:
                id_map[key(en_name)] = (sin[0], ko_name)
    for fname in loc_files("Egos"):
        en = {d.get("id"): d for d in load_loc("EN", fname)}
        for d in load_loc("KR", fname):
            i, e = d.get("id"), en.get(d.get("id"))
            if not isinstance(i, int) or not 20101 <= i <= 21299 or not e:
                continue
            sin = sinner_of_id(i)
            if sin and d.get("name"):
                ego_map[key(f"{e.get('name', '')} {sin[1]}")] = (sin[0], norm(d["name"]))
    # 번역 파일에 아직 없는 신규 인격: 인격 자동 수집기가 PV 제목으로 붙인 이름
    for a in load_json(AUTO_PATH, []):
        if a.get("en") and a.get("sinner") and a.get("text"):
            k = key(a["en"])
            if k not in id_map:
                id_map[k] = (a["sinner"], norm(re.sub(r"^\s*\[0*\]\s*\|\s*", "", a["text"])))
    # 직접 적어 둔 이름(위키 영문 이름 -> 한국어 이름)이 가장 우선
    ov = load_json(OVERRIDES_PATH, {})
    for target, field in ((id_map, "identities"), (ego_map, "ego")):
        for en, ko in (ov.get(field) or {}).items():
            sin = sinner_of_name(en)
            if sin and isinstance(ko, str) and ko.strip():
                target[key(en)] = (sin[0], norm(ko))
    return id_map, ego_map


# ---------- 위키 ----------

def row_cells(tr):
    return tr.find_all(["td", "th"], recursive=False)


def alts(tag):
    return [im.get("alt") or "" for im in tag.find_all("img")]


def header_index(table):
    first = table.find("tr")
    return {norm(c.get_text(" ")): i for i, c in enumerate(row_cells(first))} if first else {}


def main_table(html):
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find(id="mw-content-text") or soup
    for t in body.find_all("table"):
        h = header_index(t)
        if "Name" in h:
            return t, h
    return None, {}


def parse_identity_data(html):
    """List_of_Identities/Data -> [{en, href, sins:[S1,S2,S3]}] (위키 표의 S1~S3 죄악 속성)."""
    table, h = main_table(html)
    if not table:
        return []
    cols = [h.get("S1"), h.get("S2"), h.get("S3")]
    out = []
    for tr in table.find_all("tr")[1:]:
        cells = row_cells(tr)
        if not cells:
            continue
        a = cells[0].find("a")
        if not a:
            continue
        sins = []
        for c in cols:
            m = None
            if c is not None and c < len(cells):
                m = next((LCB_SIN_RE.match(x) for x in alts(cells[c]) if LCB_SIN_RE.match(x)), None)
            sins.append(m.group(1).lower() if m else "")
        out.append({"en": norm(a.get_text(" ")), "href": a.get("href") or "", "sins": sins})
    return out


def parse_ego_data(html):
    """List_of_E.G.O/Data -> [{en, sin, tier}]"""
    table, h = main_table(html)
    if not table:
        return []
    ci, ct = h.get("Sin"), h.get("Risk Level")
    out = []
    for tr in table.find_all("tr")[1:]:
        cells = row_cells(tr)
        if not cells or ci is None or ct is None or max(ci, ct) >= len(cells):
            continue
        a = cells[0].find("a")
        name = norm((a or cells[0]).get_text(" "))
        sm = next((LCB_SIN_RE.match(x) for x in alts(cells[ci]) if LCB_SIN_RE.match(x)), None)
        tm = next((TIER_RE.match(x) for x in alts(cells[ct]) if TIER_RE.match(x)), None)
        if name and tm:
            out.append({"en": name, "sin": sm.group(1).lower() if sm else "", "tier": tm.group(1).lower()})
    return out


def parse_identity_skills(html):
    """인격 문서 -> 스킬 1·2·3의 [{type, sin}]. 표마다 '죄악N.png' 아이콘이 스킬 번호, 처음 나온 표(동기화 4)를 씁니다."""
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find(id="mw-content-text") or soup
    found = {}
    for t in body.find_all("table"):
        if t.find("table") or not t.find("img", alt="SkillAttack.png"):
            continue
        td = t.find("td")
        if not td:
            continue
        a = alts(td)
        sm = next((SKILL_SIN_RE.match(x) for x in a if SKILL_SIN_RE.match(x)), None)
        tm = next((ATK_RE.match(x) for x in a if ATK_RE.match(x)), None)
        if sm and tm and sm.group(2) not in found:
            found[sm.group(2)] = {"type": tm.group(1).lower(), "sin": sm.group(1).lower()}
    return [found.get(n) for n in "123"]


def complete(skills):
    return isinstance(skills, list) and len(skills) == 3 and all(
        isinstance(s, dict) and s.get("type") and s.get("sin") for s in skills)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="파일을 쓰지 않고 결과만 출력")
    ap.add_argument("--report", help="PR 본문으로 쓸 마크다운 보고서 경로")
    ap.add_argument("--max-pages", type=int, default=260, help="한 번에 읽을 인격 문서 수 상한")
    args = ap.parse_args()

    id_map, ego_map = build_name_maps()
    print(f"게임 텍스트: 인격 {len(id_map)}개, E.G.O {len(ego_map)}개의 한국어 이름")
    if len(id_map) < 100:
        sys.exit("한국어 인격 이름이 너무 적습니다. 번역 저장소 구조가 바뀐 것 같아 중단합니다.")

    rows = parse_identity_data(get(ID_DATA_PAGE).text)
    print(f"위키 인격 {len(rows)}개")
    if len(rows) < 100:
        sys.exit("위키 인격 표를 읽지 못했습니다. 페이지 구조가 바뀐 것 같아 중단합니다.")

    old = load_json(SKILLS_PATH, {})
    known = {}  # 영문 이름 -> 이전에 수집한 스킬
    for sid, ents in old.items():
        if isinstance(ents, dict):
            for ko, e in ents.items():
                if isinstance(e, dict) and e.get("en"):
                    known[e["en"]] = e.get("skills")

    skills_out = {sid: {} for sid, _ in SINNERS}
    added, changed, partial, unnamed = [], [], [], []
    pages = 0
    for r in rows:
        hit = id_map.get(key(r["en"]))
        if not hit:
            unnamed.append(r["en"])
            continue
        sid, ko = hit
        prev = known.get(r["en"])
        skills = prev
        # 주 1회 실행: 패치로 바뀐 스킬도 반영되도록 인격 문서를 매번 다시 읽고, 읽기에 실패하면 이전 값을 유지
        if pages < args.max_pages and r["href"].startswith("/wiki/"):
            pages += 1
            time.sleep(1)
            try:
                read = parse_identity_skills(get(WIKI + r["href"]).text)
            except Exception as e:
                print(f"인격 문서 읽기 실패({r['en']}): {e}")
                read = [None, None, None]
            # 문서에서 못 읽은 스킬은 이전 값, 그것도 없으면 Data 표의 S1~S3 속성(공격 유형은 비워 둠)
            old = prev if isinstance(prev, list) and len(prev) == 3 else [None, None, None]
            skills = [read[i] or (old[i] if old[i] and old[i].get("type") else None)
                      or ({"type": "", "sin": r["sins"][i]} if r["sins"][i] else None) for i in range(3)]
            if r["en"] not in known:
                added.append(f"{ko} ({r['en']})")
            elif complete(prev) and skills != prev:
                changed.append(f"{ko} ({r['en']})")
        if not skills or not any(skills):
            partial.append(r["en"])
            continue
        if not complete(skills):
            partial.append(r["en"])
        skills_out[sid][ko] = {"en": r["en"], "skills": [s or {"type": "", "sin": ""} for s in skills]}

    egos = parse_ego_data(get(EGO_DATA_PAGE).text)
    print(f"위키 E.G.O {len(egos)}개")
    ego_out = {sid: [] for sid, _ in SINNERS}
    ego_unnamed = []
    for e in egos:
        sin = sinner_of_name(e["en"])
        if not sin:
            ego_unnamed.append(e["en"])
            continue
        hit = ego_map.get(key(e["en"]))
        name = hit[1] if hit else strip_sinner(e["en"], sin[1])
        if not hit:
            ego_unnamed.append(e["en"])
        ego_out[sin[0]].append({"name": name, "en": e["en"], "tier": e["tier"], "sin": e["sin"]})
    for sid in ego_out:
        ego_out[sid].sort(key=lambda x: (TIERS.index(x["tier"]), x["en"]))

    n_id = sum(len(v) for v in skills_out.values())
    n_ego = sum(len(v) for v in ego_out.values())
    print(f"결과: 인격 {n_id}개(새로 읽음 {len(added)}개, 정보 부족 {len(partial)}개), E.G.O {n_ego}개")
    for n in unnamed:
        print("한국어 이름 없음(다음에 재시도): " + n)
    for n in partial:
        print("스킬 정보 일부 없음: " + n)
    for n in ego_unnamed:
        print("E.G.O 한국어 이름 없음(영문 표시): " + n)
    if args.dry_run:
        for sid in ("yisang", "gregor"):
            for ko, e in list(skills_out[sid].items())[:3]:
                print(f"  {ko}: {e['skills']}")
            print(f"  {sid} E.G.O: {[(x['tier'], x['name'], x['sin']) for x in ego_out[sid][:4]]}")
        return
    if n_id < 100 or n_ego < 50:
        sys.exit("수집 결과가 너무 적어 파일을 바꾸지 않습니다.")

    write_json(SKILLS_PATH, skills_out)
    write_json(EGO_PATH, ego_out)

    if args.report:
        lines = ["덱 빌더용 인격 스킬 속성과 E.G.O 목록을 갱신했습니다.", "",
                 f"- 인격 {n_id}개, E.G.O {n_ego}개"]
        if added:
            lines.append(f"- 새로 읽은 인격 {len(added)}개: " + ", ".join(added[:40]) + (" 외" if len(added) > 40 else ""))
        if changed:
            lines.append("- 스킬 정보가 바뀐 인격(패치 반영): " + ", ".join(changed))
        if partial:
            lines.append("- 스킬 정보가 일부 비어 있는 인격(위키 갱신 후 다시 채움): " + ", ".join(partial))
        if unnamed:
            lines.append("- 한국어 이름을 아직 몰라 제외한 인격(다음 실행에서 재시도): " + ", ".join(unnamed))
        if ego_unnamed:
            lines.append("- 한국어 이름을 몰라 영문으로 넣은 E.G.O: " + ", ".join(ego_unnamed))
        lines += ["", f"출처: {ID_DATA_PAGE}, {EGO_DATA_PAGE}, 한국어 이름은 https://github.com/{LOC_REPO} (CC BY-NC-SA 4.0)"]
        with open(args.report, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
