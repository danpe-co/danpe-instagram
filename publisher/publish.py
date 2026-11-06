#!/usr/bin/env python3
"""Publicador DANPE: publica a fila do Instagram pela API oficial (Instagram Login).

Uso:
  python publisher/publish.py            publica o que já passou do horário
  python publisher/publish.py --check    só valida a fila e mostra o que está devido

Variáveis de ambiente:
  IG_TOKEN         token de acesso do Instagram (secret do repositório)
  MEDIA_BASE_URL   (opcional) base pública da pasta do repo; padrão = raw.githubusercontent do repo
  IG_API_VERSION   (opcional) padrão v23.0
  MAX_PER_RUN      (opcional) máximo de itens por execução; padrão 30
  GAP_SECONDS      (opcional) pausa entre publicações; padrão 20
"""
import argparse
import datetime as dt
import io
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
QUEUE = ROOT / "queue"
PUBLISHED = ROOT / "published"
FAILED = ROOT / "failed"
MEDIA = ROOT / "media"

TOKEN = "".join(os.environ.get("IG_TOKEN", "").split())
# Token do login do Instagram começa com "IG"; token do Facebook/login unificado começa com "EAA".
TOKEN_KIND = "facebook" if TOKEN.startswith("EAA") else "instagram"
API_HOST = "https://graph.facebook.com/" if TOKEN_KIND == "facebook" else "https://graph.instagram.com/"
API = API_HOST + os.environ.get("IG_API_VERSION", "v23.0")
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "30"))
GAP_SECONDS = int(os.environ.get("GAP_SECONDS", "20"))
MAX_ATTEMPTS = 3

TYPES = {"image", "carousel", "reel", "story"}
JPEG_EXT = {".jpg", ".jpeg"}
VIDEO_EXT = {".mp4", ".mov"}
IMAGE_EXT = JPEG_EXT | {".png", ".webp"}


def log(*args):
    print(*args, flush=True)


def now():
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value):
    t = dt.datetime.fromisoformat(value)
    if t.tzinfo is None:
        raise ValueError(f"publish_at sem fuso horário: {value} (use -03:00)")
    return t


def is_url(s):
    return s.startswith("http://") or s.startswith("https://")


def ext(s):
    return pathlib.PurePosixPath(urllib.parse.urlparse(s).path).suffix.lower()


def is_video(s):
    return ext(s) in VIDEO_EXT


# ---------------------------------------------------------------- HTTP / API

def http(method, url, params=None, timeout=90):
    data = None
    if params:
        encoded = urllib.parse.urlencode(params)
        if method == "GET":
            url += ("&" if "?" in url else "?") + encoded
        else:
            data = encoded.encode()
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:800]
        # nunca imprime a query string (é onde vai o token nos GETs)
        raise RuntimeError(f"{method} {url.split('?')[0]} -> HTTP {e.code}: {detail}") from None


def api(method, path, **params):
    params["access_token"] = TOKEN
    return http(method, f"{API}/{path}", params)


def download(url):
    req = urllib.request.Request(url, headers={"User-Agent": "danpe-publicador/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def wait_reachable(url, limit=300):
    """Espera o arquivo ficar acessível no link público (ex.: logo após o push)."""
    deadline = time.time() + limit
    while True:
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "danpe-publicador/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status == 200:
                    return
        except Exception:
            pass
        if time.time() > deadline:
            raise RuntimeError(f"mídia não ficou acessível: {url}")
        time.sleep(15)


# ---------------------------------------------------------------- fila

def load_items():
    items = []
    for path in sorted(QUEUE.glob("*.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        item["_path"] = path
        items.append(item)
    return items


def save(item, folder=None):
    path = item["_path"]
    target = (folder / path.name) if folder else path
    target.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in item.items() if not k.startswith("_")}
    target.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if folder and path.exists() and path != target:
        path.unlink()
    item["_path"] = target


def validate(item):
    errors = []
    for key in ("id", "publish_at", "type", "media"):
        if key not in item:
            errors.append(f"falta '{key}'")
    if errors:
        return errors
    if item["type"] not in TYPES:
        errors.append(f"type inválido: {item['type']} (use {sorted(TYPES)})")
    try:
        parse_time(item["publish_at"])
    except ValueError as e:
        errors.append(str(e))
    media = item["media"]
    if not isinstance(media, list) or not media:
        errors.append("media deve ser uma lista com pelo menos 1 arquivo")
        return errors
    for m in media:
        if ext(m) not in IMAGE_EXT | VIDEO_EXT:
            errors.append(f"formato não suportado: {m}")
        if not is_url(m) and not (ROOT / m).exists():
            errors.append(f"arquivo não encontrado no repo: {m}")
    t = item["type"]
    if t == "carousel" and not 2 <= len(media) <= 10:
        errors.append("carrossel precisa de 2 a 10 arquivos")
    if t in {"image", "reel", "story"} and len(media) != 1:
        errors.append(f"{t} leva exatamente 1 arquivo")
    if t == "reel" and not is_video(media[0]):
        errors.append("reel precisa de vídeo (.mp4/.mov)")
    if t == "image" and is_video(media[0]):
        errors.append("image não aceita vídeo (use reel)")
    return errors


# ---------------------------------------------------------------- mídia

def to_jpeg(raw, dest):
    from PIL import Image  # importado só quando precisa

    img = Image.open(io.BytesIO(raw))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (12, 14, 18))  # fundo grafite DANPE, caso haja transparência
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "JPEG", quality=92, optimize=True, progressive=False)


def prepare(item):
    """A API só aceita JPEG. Converte PNG/WebP (locais ou remotos) para media/<id>/NN.jpg."""
    changed = False
    out = []
    for i, m in enumerate(item["media"], 1):
        if is_video(m) or (not is_url(m) and ext(m) in JPEG_EXT):
            out.append(m)
            continue
        raw = download(m) if is_url(m) else (ROOT / m).read_bytes()
        dest = MEDIA / item["id"] / f"{i:02d}.jpg"
        to_jpeg(raw, dest)
        out.append(dest.relative_to(ROOT).as_posix())
        changed = True
    cover = item.get("cover")
    if cover and not (not is_url(cover) and ext(cover) in JPEG_EXT):
        raw = download(cover) if is_url(cover) else (ROOT / cover).read_bytes()
        dest = MEDIA / item["id"] / "capa.jpg"
        to_jpeg(raw, dest)
        item["cover"] = dest.relative_to(ROOT).as_posix()
        changed = True
    if changed:
        item.setdefault("source_media", item["media"])
        item["media"] = out
    return changed


def public_url(m):
    if is_url(m):
        return m
    base = os.environ.get("MEDIA_BASE_URL", "").strip()
    if not base:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        branch = os.environ.get("GITHUB_REF_NAME", "main")
        if not repo:
            raise RuntimeError("defina MEDIA_BASE_URL ou rode dentro do GitHub Actions")
        base = f"https://raw.githubusercontent.com/{repo}/{branch}"
    return base.rstrip("/") + "/" + urllib.parse.quote(m)


# ---------------------------------------------------------------- publicação

def create_container(ig, **params):
    return api("POST", f"{ig}/media", **params)["id"]


def wait_ready(container_id, video):
    deadline = time.time() + (900 if video else 240)
    while True:
        status = api("GET", container_id, fields="status_code,status")
        code = status.get("status_code")
        if code in ("FINISHED", "PUBLISHED"):
            return
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"container {container_id} {code}: {status.get('status')}")
        if time.time() > deadline:
            raise RuntimeError(f"container {container_id} não ficou pronto a tempo ({code})")
        time.sleep(15 if video else 4)


def publish_item(ig, item):
    t = item["type"]
    urls = [public_url(m) for m in item["media"]]
    for u in urls:
        wait_reachable(u)
    caption = item.get("caption", "")
    video = any(is_video(u) for u in urls)

    if t == "image":
        cid = create_container(ig, image_url=urls[0], caption=caption)
    elif t == "story":
        key = "video_url" if video else "image_url"
        cid = create_container(ig, media_type="STORIES", **{key: urls[0]})
    elif t == "reel":
        params = {
            "media_type": "REELS",
            "video_url": urls[0],
            "caption": caption,
            "share_to_feed": "true" if item.get("share_to_feed", True) else "false",
        }
        if item.get("cover"):
            cover_url = public_url(item["cover"])
            wait_reachable(cover_url)
            params["cover_url"] = cover_url
        cid = create_container(ig, **params)
    else:  # carousel
        children = []
        for u in urls:
            if is_video(u):
                child = create_container(ig, media_type="VIDEO", video_url=u, is_carousel_item="true")
            else:
                child = create_container(ig, image_url=u, is_carousel_item="true")
            wait_ready(child, is_video(u))
            children.append(child)
        cid = create_container(ig, media_type="CAROUSEL", children=",".join(children), caption=caption)

    wait_ready(cid, video)
    media_id = api("POST", f"{ig}/media_publish", creation_id=cid)["id"]
    permalink = None
    try:
        permalink = api("GET", media_id, fields="permalink").get("permalink")
    except Exception:
        pass
    return media_id, permalink


def find_ig_account():
    """Descobre o id da conta do Instagram conforme o tipo de token."""
    forced = os.environ.get("IG_USER_ID", "").strip()
    if forced:
        log(f"Conta: id {forced} (IG_USER_ID)")
        return forced
    if TOKEN_KIND == "instagram":
        me = api("GET", "me", fields="user_id,username")
        log(f"Conta: @{me.get('username')}")
        return me.get("user_id") or me["id"]
    # token do Facebook: procura a conta do Instagram nas Páginas e no portfólio
    tries = [
        ("me/accounts", {"fields": "instagram_business_account{id,username}"}),
        ("me/businesses", {"fields": "owned_instagram_accounts{id,username},instagram_accounts{id,username}"}),
    ]
    found = []
    for path, params in tries:
        try:
            data = api("GET", path, **params).get("data", [])
        except Exception as e:
            log(f"  ({path}: {e})")
            continue
        for row in data:
            iba = row.get("instagram_business_account")
            if iba:
                found.append(iba)
            for key in ("owned_instagram_accounts", "instagram_accounts"):
                found.extend(row.get(key, {}).get("data", []))
    # sem Página do Facebook: o próprio token diz a qual conta do Instagram ele dá acesso
    try:
        dbg = api("GET", "debug_token", input_token=TOKEN).get("data", {})
        ids = set()
        for g in dbg.get("granular_scopes", []):
            if g.get("scope", "").startswith("instagram"):
                ids.update(g.get("target_ids", []))
        log(f"  token: tipo {dbg.get('type')}, escopos {[g.get('scope') for g in dbg.get('granular_scopes', [])]}")
        for igid in ids:
            try:
                acc = api("GET", igid, fields="id,username")
                found.append(acc)
            except Exception as e:
                log(f"  ({igid}: {e})")
    except Exception as e:
        log(f"  (debug_token: {e})")
    want = os.environ.get("IG_USERNAME", "somosdanpe").lower()
    for acc in found:
        if (acc.get("username") or "").lower() == want:
            log(f"Conta: @{acc['username']} (id {acc['id']})")
            return acc["id"]
    names = ", ".join(a.get("username") or a.get("id") for a in found) or "nenhuma"
    raise RuntimeError(f"não achei @{want} com esse token (contas visíveis: {names})")


# ---------------------------------------------------------------- git (só no Actions)

def git_sync(message):
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    run = lambda *a: subprocess.run(["git", *a], cwd=ROOT, check=True)
    run("add", "-A", "queue", "published", "failed", "media")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
        return
    run("commit", "-m", message)
    for _ in range(3):
        if subprocess.run(["git", "pull", "--rebase", "-q"], cwd=ROOT).returncode == 0 and \
           subprocess.run(["git", "push", "-q"], cwd=ROOT).returncode == 0:
            return
        time.sleep(5)
    raise RuntimeError("não consegui enviar o commit para o GitHub")


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="só valida a fila")
    args = parser.parse_args()

    items = load_items()
    problems = {it["_path"].name: validate(it) for it in items}
    problems = {k: v for k, v in problems.items() if v}
    for name, errs in problems.items():
        log(f"ERRO em queue/{name}: " + "; ".join(errs))

    valid = [it for it in items if it["_path"].name not in problems]
    due = sorted(
        (it for it in valid if parse_time(it["publish_at"]) <= now()),
        key=lambda it: (parse_time(it["publish_at"]), it["id"]),
    )
    log(f"Fila: {len(items)} itens, {len(due)} no horário, {len(problems)} com erro.")

    if args.check:
        for it in due:
            log(f"  devido: {it['publish_at']}  {it['type']:<8} {it['id']}")
        sys.exit(1 if problems else 0)

    if not due:
        return
    if not TOKEN:
        sys.exit("IG_TOKEN não configurado (Settings → Secrets → Actions).")

    due = due[:MAX_PER_RUN]

    converted = False
    for it in due:
        if prepare(it):
            save(it)
            converted = True
    if converted:
        git_sync("mídia: converte para JPEG")

    log(f"Token: tipo {TOKEN_KIND} ({len(TOKEN)} caracteres)")
    ig = find_ig_account()
    try:
        limit = api("GET", f"{ig}/content_publishing_limit", fields="quota_usage,config")
        log(f"Cota de publicação (24h): {json.dumps(limit.get('data', limit), ensure_ascii=False)}")
    except Exception as e:
        log(f"(não consegui ler a cota: {e})")

    done = 0
    for it in due:
        log(f"→ {it['type']} {it['id']}")
        try:
            media_id, permalink = publish_item(ig, it)
        except Exception as e:
            it["attempts"] = it.get("attempts", 0) + 1
            it["last_error"] = str(e)[:1000]
            log(f"  FALHOU (tentativa {it['attempts']}): {e}")
            save(it, FAILED if it["attempts"] >= MAX_ATTEMPTS else None)
            # para aqui para não embaralhar a ordem (stories e destaques dependem dela)
            break
        it.pop("attempts", None)
        it.pop("last_error", None)
        it["published_at"] = now().isoformat(timespec="seconds")
        it["media_id"] = media_id
        if permalink:
            it["permalink"] = permalink
        save(it, PUBLISHED)
        done += 1
        log(f"  OK {permalink or media_id}")
        if done < len(due):
            time.sleep(GAP_SECONDS)

    git_sync(f"publica {done} item(ns)")
    if done < len(due):
        sys.exit(1)


if __name__ == "__main__":
    main()
