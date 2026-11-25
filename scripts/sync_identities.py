#!/usr/bin/env python3
"""신규 인격 자동 수집기.

limbuscompany.wiki.gg 의 렌더링된 위키 페이지(robots.txt가 허용하는 /wiki/ 경로만)를 하루 한 번 읽어
아직 모르는 인격을 찾고, 이미지를 image_<수감자>/ 폴더에 내려받은 뒤 data/auto_identities.json 에 추가합니다.
워크플로가 변경분으로 PR을 열면, 관리자가 한국어 인격명을 확인하고 머지하면 사이트에 반영됩니다.

- data/wiki_seen.json : 이미 처리한 위키 인격명(영문) 목록. 파일이 없으면 첫 실행에서 현재 목록을 전부 기록만 하고
  아무것도 추가하지 않습니다(기존 인격은 이미 사이트에 있으므로).
- 위키에 아직 임시 이미지(Placeholder)만 있는 인격은 건너뛰고 다음 실행에서 다시 확인합니다.

사용법: python scripts/sync_identities.py [--dry-run] [--report PATH]
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

WIKI = "https://limbuscompany.wiki.gg"
DATA_PAGE = WIKI + "/wiki/List_of_Identities/Data"
GALLERY_PAGE = WIKI + "/wiki/List_of_Identities"
USER_AGENT = "MirrorRouletteSync/1.0 (daily identity sync for a fan roulette site)"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEEN_PATH = os.path.join(ROOT, "data", "wiki_seen.json")
AUTO_PATH = os.path.join(ROOT, "data", "auto_identities.json")
PV_PATH = os.path.join(ROOT, "data", "pv_names.json")

# 공식 유튜브 PV: 제목 예) "LimbusCompany [000] 오트쿠튀르:: 르루주 B2F 타래둥지 그레고르 / [000] 오트쿠튀르:: 르누아르 B3F 건조장 이상"
# 유튜브가 공개로 제공하는 채널 RSS 피드만 읽습니다(웹페이지는 읽지 않음).
YOUTUBE_CHANNEL_ID = "UCpqyr6h4RCXCEswHlkSjykA"

# 위키 영문 수감자명 -> (사이트 id, 이미지 폴더, 파일명 접미사, 한국어 이름)
SINNERS = [
    ("Yi Sang", "yisang", "image_yisang", "yisang", "이상"),
    ("Faust", "faust", "image_paust", "paust", "파우스트"),
    ("Don Quixote", "donquixote", "image_donquixote", "donquixote", "돈키호테"),
    ("Ryoshu", "ryoshu", "images_ryoshyu", "ryoshu", "료슈"),
    ("Meursault", "meursault", "image_meursault", "meursault", "뫼르소"),
    ("Hong Lu", "honglu", "image_honglu", "honglu", "홍루"),
    ("Heathcliff", "heathcliff", "image_heathcliff", "heathcliff", "히스클리프"),
    ("Ishmael", "ishmael", "images_ismael", "ismael", "이스마엘"),
    ("Rodion", "rodion", "image_rodion", "rodion", "로쟈"),
    ("Sinclair", "sinclair", "image_sinclair", "sinclair", "싱클레어"),
    ("Outis", "outis", "image_outis", "outis", "오티스"),
    ("Gregor", "gregor", "image_gregor", "gregor", "그레고르"),
]

# 한국어 이름 추정용 단어표. 맞히지 못한 부분은 영문 그대로 남기고 PR에서 확인을 요청합니다.
KO_PHRASES = [
    ("Haute Couture::", "오트쿠튀르::"),
    ("LCB Sinner", "LCB 수감자"),
    ("Lobotomy E.G.O::", "로보토미 E.G.O::"),
    ("LCE E.G.O::", "LCE E.G.O::"),
    ("The House of Spiders", "거미집"),
    ("Kurokumo Clan", "흑운회"),
    ("Blade Lineage", "검계"),
    ("Heishou Pack", "흑수"),
    ("Wuthering Heights", "워더링하이츠"),
    ("La Manchaland", "라만차랜드"),
    ("Middle", "중지"),
    ("Index", "인덱스"),
    ("Le Rouge", "르루주"),
    ("Le Noir", "르누아르"),
    ("W Corp.", "W사"),
    ("R Corp.", "R사"),
    ("N Corp.", "N사"),
    ("G Corp.", "G사"),
    ("K Corp.", "K사"),
    ("T Corp.", "T사"),
]


def norm(s):
    s = s.replace("ō", "o").replace("ū", "u").replace("Ō", "O").replace("Ū", "U")
    return re.sub(r"\s+", " ", s).strip()


def get(url):
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
    r.raise_for_status()
    return r


def sinner_of(name):
    n = norm(name)
    for en, sid, folder, suffix, ko in SINNERS:
        if n.endswith(" " + en) or n == en:
            return en, sid, folder, suffix, ko
    return None


def original_from_thumb(src):
    """/images/thumb/a/ab/File.png/120px-File.png?x -> /images/a/ab/File.png"""
    src = src.split("?")[0]
    if src.startswith("//"):
        src = "https:" + src
    elif src.startswith("/"):
        src = WIKI + src
    if "/thumb/" in src:
        src = src.replace("/thumb/", "/", 1).rsplit("/", 1)[0]
    return src


def img_src(tag):
    return tag.get("data-src") or tag.get("src") or ""


def file_name(src):
    return urllib.parse.unquote(original_from_thumb(src).rsplit("/", 1)[-1])


DATE_RE = re.compile(r"(20\d\d)[.\-/](\d{1,2})[.\-/](\d{1,2})")


def parse_data_page(html):
    """Data 표에서 {name, rarity, date} 목록을 뽑습니다.

    표 머리글 구성에 기대지 않고, 출시일(YYYY.MM.DD)이 들어 있는 행마다
    첫 번째 문서 링크를 인격명으로, IDNumberN 아이콘을 등급으로 읽습니다.
    """
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for tr in soup.find_all("tr"):
        text = tr.get_text(" ")
        m = DATE_RE.search(text)
        if not m:
            continue
        name = ""
        for a in tr.find_all("a"):
            t = norm(a.get_text(" ") or a.get("title") or "")
            href = a.get("href") or ""
            if t and "File:" not in href and "Special:" not in href and not DATE_RE.search(t):
                name = t
                break
        if not name or name in seen:
            continue
        rarity = 0
        for im in tr.find_all("img"):
            mm = re.search(r"IDNumber(\d)", img_src(im) + " " + (im.get("alt") or ""))
            if mm:
                rarity = int(mm.group(1))
                break
        seen.add(name)
        out.append({"name": name, "rarity": rarity,
                    "date": f"{m.group(1)}.{int(m.group(2)):02d}.{int(m.group(3)):02d}", "season": ""})
    return out


def describe_page(html):
    """인격을 못 찾았을 때 로그로 남길 페이지 구조 요약."""
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    lines = [f"HTML {len(html)}자, title={soup.title.string if soup.title else ''!r}, 표 {len(tables)}개, tr {len(soup.find_all('tr'))}개"]
    for t in tables[:5]:
        first = t.find("tr")
        lines.append(f"  표 class={t.get('class')} 첫 행: {norm(first.get_text(' '))[:200] if first else ''}")
    body = soup.find(id="mw-content-text")
    lines.append("  본문 앞부분: " + norm((body or soup).get_text(" "))[:500])
    return "\n".join(lines)


def img_key(name):
    """파일명과 인격명 비교용 키: '::'·밑줄·문장부호·대소문자 차이를 무시."""
    return re.sub(r"[^a-z0-9]", "", norm(name).lower())


def parse_gallery_page(html):
    """갤러리 페이지에서 인격명(파일명 기준) -> 원본 이미지 URL. _Uptied(동기화 후) 그림은 제외."""
    soup = BeautifulSoup(html, "html.parser")
    images = {}
    for im in soup.find_all("img"):
        src = img_src(im)
        if not src:
            continue
        fname = file_name(src)
        if not fname.lower().endswith((".png", ".webp", ".jpg")):
            continue
        stem = fname.rsplit(".", 1)[0]
        if stem.endswith("_Uptied") or stem.endswith("_Icon") or "Placeholder" in stem:
            continue
        images.setdefault(img_key(stem), original_from_thumb(src))
    return images


def parse_pv_title(title):
    """PV 제목 -> [(등급 문자열, 한국어 인격명)]. 인격 PV가 아니면 빈 목록."""
    out = []
    for part in re.split(r"\s+/\s+", title):
        m = re.search(r"\[(0{1,3})\]\s*(.+)$", part.strip())
        if m:
            name = re.sub(r"::\s+", "::", m.group(2)).strip()
            out.append((m.group(1), norm(name)))
    return out


def fetch_pv_names(pv):
    """채널 RSS(최근 영상 15개)에서 PV 인격명을 모아 pv["names"]에 누적합니다."""
    cid = YOUTUBE_CHANNEL_ID
    xml = get("https://www.youtube.com/feeds/videos.xml?channel_id=" + cid).content
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    known = {(n["sinner"], n["name"]) for n in pv.setdefault("names", [])}
    ko2sid = {ko: sid for _, sid, _, _, ko in SINNERS}
    for entry in ET.fromstring(xml).findall("a:entry", ns):
        title = entry.findtext("a:title", "", ns)
        vid = entry.findtext("yt:videoId", "", ns)
        published = entry.findtext("a:published", "", ns)[:10]
        for stars, name in parse_pv_title(title):
            sid = ko2sid.get(name.split(" ")[-1])
            if not sid or (sid, name) in known:
                continue
            pv["names"].append({"sinner": sid, "stars": stars, "name": name, "video": vid,
                                "published": published, "used_by": ""})
            known.add((sid, name))


def match_pv(pv, sid, wiki_name):
    """같은 수감자의 아직 안 쓴 PV 이름 중 위키 이름과 가장 잘 맞는 것을 고릅니다."""
    # 최근 35일 안에 올라온 PV만 후보로 삼아, 오래전 PV 이름이 엉뚱하게 붙지 않게 함
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 35 * 86400))
    cands = [n for n in pv.get("names", [])
             if n["sinner"] == sid and not n["used_by"] and n["published"] >= cutoff]
    if not cands:
        return None
    en = norm(wiki_name)
    def score(n):
        sc = 0
        for tok in re.findall(r"[A-Z0-9]*\d[A-Z0-9]*", en):  # B3F 같은 공통 표기
            if tok in n["name"]:
                sc += 3
        for e, k in KO_PHRASES:
            if e in en and k.rstrip(":") in n["name"]:
                sc += 2
        return (sc, n["published"])
    return max(cands, key=score)


def identity_page_image(name):
    """갤러리에 그림이 없을 때(오트쿠튀르 등) 인격 개별 문서에서 같은 이름의 그림을 찾습니다."""
    time.sleep(1)
    try:
        html = get(WIKI + "/wiki/" + urllib.parse.quote(name.replace(" ", "_"))).text
    except Exception as e:
        print(f"인격 문서 접속 실패({name}): {e}")
        return None
    found = parse_gallery_page(html)
    hit = found.get(img_key(name))
    if not hit and DEBUG:
        sinner = sinner_of(name)
        cands = [u.rsplit("/", 1)[-1] for k, u in found.items() if sinner and img_key(sinner[0]) in k]
        print(f"    ({name} 문서의 그림 후보: {', '.join(cands[:8]) or '없음'})")
    return hit


def guess_ko(name, sinner_en, sinner_ko):
    base = norm(name)
    if base.endswith(" " + sinner_en):
        base = base[: -len(sinner_en) - 1]
    ko = base
    for en, k in KO_PHRASES:
        ko = ko.replace(en, k)
    ko = norm(ko + " " + sinner_ko)
    confident = not re.search(r"[A-Za-z]{3,}", re.sub(r"(LCB|LCE|LCA|LCCB|E\.G\.O)", "", ko))
    return ko, confident


def slug(name, sinner_en, suffix):
    base = norm(name)
    if base.endswith(" " + sinner_en):
        base = base[: -len(sinner_en) - 1]
    base = base.split("::")[-1]
    s = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_")
    return (s[:40].rstrip("_") or "new") + "_" + suffix


def detect_ext(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


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


DEBUG = False
PIPE = "\\|"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="파일을 쓰지 않고 찾은 내용만 출력")
    ap.add_argument("--report", help="PR 본문으로 쓸 마크다운 보고서 경로")
    args = ap.parse_args()
    global DEBUG
    DEBUG = args.dry_run

    data_html = get(DATA_PAGE).text
    rows = parse_data_page(data_html)
    time.sleep(2)
    gallery = parse_gallery_page(get(GALLERY_PAGE).text)
    print(f"위키 인격 {len(rows)}개, 갤러리 이미지 {len(gallery)}개를 읽었습니다.")
    if args.dry_run:
        for r in sorted(rows, key=lambda r: r["date"])[-6:]:
            src = gallery.get(img_key(r["name"])) or identity_page_image(r["name"])
            print(f"  최근 인격: {r['date']} [{r['rarity']}] {r['name']} / 이미지 {src or '없음'}")
    if len(rows) < 100:
        print(describe_page(data_html))
        sys.exit(f"인격이 {len(rows)}개뿐입니다. 위키 페이지 구조가 바뀐 것 같아 중단합니다.")

    pv = load_json(PV_PATH, {})
    try:
        time.sleep(1)
        fetch_pv_names(pv)
        print(f"유튜브 PV 인격명 {len(pv.get('names', []))}개를 알고 있습니다.")
        if args.dry_run:
            for n in pv.get("names", [])[-6:]:
                print(f"  PV 인격명: {n['published']} [{n['stars']}] {n['name']}")
    except Exception as e:  # 유튜브 실패는 치명적이지 않음: 추정 이름으로 진행
        print("유튜브 PV 확인 실패(추정 이름으로 진행): " + str(e))
    if not args.dry_run:
        write_json(PV_PATH, pv)

    seen_exists = os.path.exists(SEEN_PATH)
    seen = set(load_json(SEEN_PATH, []))
    auto = load_json(AUTO_PATH, [])

    if not seen_exists:
        # 오늘 이전에 출시된 인격만 기준선으로 기록. 출시 예정(오늘 이후)인 인격은 남겨 두어 출시 후 자동 추가되게 함
        today = time.strftime("%Y%m%d", time.gmtime())
        def released(r):
            d = re.sub(r"\D", "", r["date"])
            return not (len(d) == 8 and d >= today)
        seen = {r["name"] for r in rows if released(r)}
        print(f"첫 실행: 이미 출시된 위키 인격 {len(seen)}개를 기준선으로 기록합니다(추가 없음).")
        if not args.dry_run:
            write_json(SEEN_PATH, sorted(seen))
        if args.report:
            with open(args.report, "w", encoding="utf-8") as f:
                f.write(f"자동 수집 첫 실행입니다. 이미 출시된 위키 인격 {len(seen)}개를 기준선(`data/wiki_seen.json`)으로 기록했습니다.\n"
                        "이 PR을 머지하면 다음 실행부터 새로 출시되는 인격만 자동으로 추가됩니다.\n")
        return

    added, waiting, unknown = [], [], []
    for r in rows:
        if r["name"] in seen:
            continue
        sin = sinner_of(r["name"])
        if not sin:
            unknown.append(r["name"])
            continue
        en, sid, folder, suffix, ko_sinner = sin
        d = re.sub(r"\D", "", r["date"])
        if len(d) == 8 and d > time.strftime("%Y%m%d", time.gmtime(time.time() + 9 * 3600)):
            waiting.append(r["name"])  # 출시 전(한국 시간 기준) → 출시 후 추가
            continue
        url = gallery.get(img_key(r["name"])) or identity_page_image(r["name"])
        if not url:
            waiting.append(r["name"])  # 아직 임시 이미지 → 다음 실행에서 재시도
            continue
        time.sleep(1)
        data = get(url).content
        ext = detect_ext(data)
        if not ext:
            waiting.append(r["name"])
            continue
        rel = f"{folder}/{slug(r['name'], en, suffix)}{ext}"
        stars = "0" * max(1, min(3, r["rarity"] or 1))
        hit = match_pv(pv, sid, r["name"])
        if hit:
            ko, confident, stars = hit["name"], True, hit["stars"]
            if not args.dry_run:
                hit["used_by"] = r["name"]
        else:
            ko, confident = guess_ko(r["name"], en, ko_sinner)
        entry = {
            "sinner": sid,
            "text": f"[{stars}] | {ko}",
            "img": rel,
            "en": r["name"],
            "release": r["date"],
            "ko_source": "youtube" if hit else "guess",
        }
        added.append((entry, confident))
        if not args.dry_run:
            with open(os.path.join(ROOT, rel), "wb") as f:
                f.write(data)
            auto.append(entry)
            seen.add(r["name"])

    for e, ok in added:
        print(("추가: " if ok else "추가(이름 확인 필요): ") + e["text"] + "  <- " + e["en"])
    for n in waiting:
        print("이미지 대기: " + n)
    for n in unknown:
        print("수감자 판별 실패: " + n)

    if not args.dry_run and added:
        write_json(PV_PATH, pv)
        write_json(AUTO_PATH, auto)
        write_json(SEEN_PATH, sorted(seen))

    if args.report:
        lines = ["위키에서 새 인격을 찾아 이미지를 내려받았습니다. **한국어 인격명이 공식 표기와 맞는지 확인**한 뒤 머지해 주세요.",
                 "이름이 다르면 `data/auto_identities.json`의 `text`를 고치면 됩니다.", "",
                 "| 수감자 | 한국어 이름 (출처) | 위키 이름 | 출시일 | 이미지 |", "|---|---|---|---|---|"]
        for e, ok in added:
            lines.append(f"| {e['sinner']} | {e['text'].replace('|', PIPE)} ({'공식 PV 제목' if e['ko_source'] == 'youtube' else '번역 추정 ⚠️' if not ok else '번역 추정'}) | {e['en']} | {e['release']} | `{e['img']}` |")
        if waiting:
            lines += ["", "아직 위키에 이미지가 없어 다음 실행에서 다시 확인할 인격: " + ", ".join(waiting)]
        if unknown:
            lines += ["", "수감자를 판별하지 못한 인격(직접 확인 필요): " + ", ".join(unknown)]
        lines += ["", "출처: " + DATA_PAGE]
        with open(args.report, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"added={len(added)}\n")


if __name__ == "__main__":
    main()
