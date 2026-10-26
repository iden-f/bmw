"""The private API and the small web app that uses it.

Bound to localhost; Tailscale Serve puts HTTPS and the tailnet's own access
control in front of it. The bearer token is a second lock: a request without
it, or from another origin, is refused. Nothing here is public.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from autotrader import clock

from . import VERSION, media
from .db import Database, now, row_dict
from .ingest import collections as list_collections

COOKIE = "macwatch_token"
WEB = Path(__file__).resolve().parent / "web" / "index.html"


def create_app(db: Database, settings, service=None) -> FastAPI:
    app = FastAPI(title="AutoTrader Watch on this Mac", version=VERSION,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db, app.state.settings, app.state.service = db, settings, service

    # ---------------------------------------------------------- auth

    def token_ok(request: Request) -> bool:
        wanted = str(settings.api_token or "")
        if not wanted:
            return False
        header = request.headers.get("authorization", "")
        given = header[7:].strip() if header.lower().startswith("bearer ") else \
            request.cookies.get(COOKIE, "")
        return bool(given) and secrets.compare_digest(given, wanted)

    def same_origin(request: Request) -> bool:
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if not origin:
            return True          # a non-browser client; the token decides
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        return (urlsplit(origin).hostname or "") == host.split(":")[0]

    def authed(request: Request) -> None:
        if not token_ok(request):
            raise HTTPException(401, "sign in with the token (open /login?token=...)")
        if request.method not in ("GET", "HEAD") and not same_origin(request):
            raise HTTPException(403, "cross-origin writes are refused")

    # --------------------------------------------------------- pages

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        if not token_ok(request):
            return HTMLResponse(_LOGIN_PAGE, status_code=401)
        html = WEB.read_text(encoding="utf-8") if WEB.is_file() else "<p>web app missing</p>"
        return HTMLResponse(html, headers={
            "Content-Security-Policy": "default-src 'self'; img-src 'self' https: data:; "
                                       "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                                       "connect-src 'self'",
            "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store"})

    @app.get("/login")
    def login(token: str = "", request: Request = None) -> Response:
        if not settings.api_token or not secrets.compare_digest(token, settings.api_token):
            return HTMLResponse(_LOGIN_PAGE, status_code=401)
        response = RedirectResponse("/", status_code=303)
        secure = bool(request and request.headers.get("x-forwarded-proto") == "https")
        response.set_cookie(COOKIE, token, max_age=365 * 86400, httponly=True,
                            samesite="lax", secure=secure, path="/")
        return response

    @app.post("/logout", dependencies=[Depends(authed)])
    def logout() -> Response:
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    # -------------------------------------------------------- health

    @app.get("/api/health", dependencies=[Depends(authed)])
    def health() -> dict[str, Any]:
        out = {"version": VERSION, "now": now(), "ownership": settings.ownership,
               "ntfy_topic": settings.ntfy_topic, "label": settings.ntfy_label}
        if service is not None:
            out.update(service.status())
        else:
            out["counts"] = db.counts()
        out["outbox"] = [row_dict(r) for r in db.q(
            "SELECT * FROM outbox ORDER BY id DESC LIMIT 20")]
        out["runs"] = [dict(r) for r in db.q(
            "SELECT * FROM query_runs ORDER BY started_at DESC LIMIT 20")]
        return out

    # --------------------------------------------------- collections

    @app.get("/api/collections", dependencies=[Depends(authed)])
    def get_collections() -> list[dict[str, Any]]:
        counts = {r["collection_id"]: r for r in db.q(
            """SELECT collection_id, COUNT(*) AS n,
                      SUM(CASE WHEN l.status='active' THEN 1 ELSE 0 END) AS active,
                      MIN(matched_at) AS since
               FROM collection_listings m JOIN listings l ON l.id=m.listing_id
               GROUP BY collection_id""")}
        out = []
        for c in list_collections(db):
            row = counts.get(c.id)
            out.append({"id": c.id, "name": c.name, "notify_mode": c.notify_mode,
                        "enabled": c.enabled, "site": c.site, "search_url": c.search_url,
                        "rules": c.rules, "listings": int(row["n"]) if row else 0,
                        "active": int(row["active"] or 0) if row else 0,
                        "tracking_since": row["since"] if row else None,
                        "baseline_done_at": c.baseline_done_at,
                        "baseline_marketplace_at": c.baseline_marketplace_at})
        return out

    # ------------------------------------------------------ listings

    @app.get("/api/listings", dependencies=[Depends(authed)])
    def get_listings(collection: str = "", status: str = "", site: str = "", q: str = "",
                     qualified: str = "", sort: str = "last_seen", limit: int = Query(60, le=500),
                     offset: int = 0) -> dict[str, Any]:
        where, params = [], []
        if collection:
            where.append("l.id IN (SELECT listing_id FROM collection_listings WHERE collection_id=?)")
            params.append(collection)
        if status == "active":
            where.append("l.status='active'")
        elif status == "gone":
            where.append("l.status<>'active'")
        if site in ("autotrader", "marketplace"):
            where.append("l.site=?")
            params.append(site)
        if qualified == "yes":
            where.append("l.qualified=1")
        elif qualified == "no":
            where.append("l.qualified=0")
        if q.strip():
            where.append("(l.title LIKE ? OR l.make LIKE ? OR l.model LIKE ? OR l.location LIKE ?)")
            params += [f"%{q.strip()}%"] * 4
        order = {"last_seen": "l.last_seen DESC", "first_seen": "l.first_seen DESC",
                 "price": "l.price IS NULL, l.price ASC", "price_desc": "l.price DESC",
                 "mileage": "l.mileage_km IS NULL, l.mileage_km ASC"}.get(sort, "l.last_seen DESC")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = db.one(f"SELECT COUNT(*) AS n FROM listings l{clause}", params)["n"]
        rows = db.q(f"""SELECT l.*, k.shortlisted, k.muted, k.dismissed, k.note,
                               (SELECT COUNT(*) FROM price_events p WHERE p.listing_id=l.id) AS price_points,
                               (SELECT MIN(price) FROM price_events p WHERE p.listing_id=l.id AND price IS NOT NULL) AS lowest,
                               (SELECT MAX(price) FROM price_events p WHERE p.listing_id=l.id AND price IS NOT NULL AND p.price_source=l.price_source) AS highest,
                               (SELECT asset_id FROM listing_media m WHERE m.listing_id=l.id AND m.status='saved' ORDER BY position LIMIT 1) AS photo_asset,
                               (SELECT COUNT(*) FROM listing_media m WHERE m.listing_id=l.id AND m.status='saved') AS photos_saved,
                               (SELECT COUNT(*) FROM listing_media m WHERE m.listing_id=l.id) AS photos_wanted
                        FROM listings l LEFT JOIN marks k ON k.listing_id=l.id{clause}
                        ORDER BY {order} LIMIT ? OFFSET ?""", params + [limit, offset])
        return {"total": int(total), "offset": offset, "items": [_listing(r) for r in rows]}

    @app.get("/api/listings/{listing_id}", dependencies=[Depends(authed)])
    def get_listing(listing_id: str) -> dict[str, Any]:
        row = db.one("""SELECT l.*, k.shortlisted, k.muted, k.dismissed, k.note
                        FROM listings l LEFT JOIN marks k ON k.listing_id=l.id WHERE l.id=?""",
                     (listing_id,))
        if row is None:
            raise HTTPException(404, "no such listing")
        out = _listing(row)
        out["collections"] = [dict(r) for r in db.q(
            """SELECT c.id, c.name, c.notify_mode, m.matched_at, m.qualified, m.reason
               FROM collection_listings m JOIN collections c ON c.id=m.collection_id
               WHERE m.listing_id=?""", (listing_id,))]
        out["photos"] = media.photos_of(db, listing_id)
        out["notifications"] = [row_dict(r) for r in db.q(
            "SELECT * FROM outbox WHERE listing_id=? ORDER BY id DESC LIMIT 30", (listing_id,))]
        return out

    @app.get("/api/listings/{listing_id}/history", dependencies=[Depends(authed)])
    def get_history(listing_id: str) -> dict[str, Any]:
        if db.one("SELECT 1 FROM listings WHERE id=?", (listing_id,)) is None:
            raise HTTPException(404, "no such listing")
        return {
            "prices": [dict(r) for r in db.q(
                "SELECT at, price, currency, price_source, kind, provenance FROM price_events "
                "WHERE listing_id=? ORDER BY at, id", (listing_id,))],
            "revisions": [row_dict(r) for r in db.q(
                "SELECT * FROM listing_revisions WHERE listing_id=? ORDER BY at, id", (listing_id,))],
            "sightings": [dict(r) for r in db.q(
                "SELECT collection_id, run_id, at, price FROM sightings WHERE listing_id=? "
                "ORDER BY at DESC LIMIT 200", (listing_id,))],
        }

    @app.patch("/api/listings/{listing_id}/marks", dependencies=[Depends(authed)])
    async def patch_marks(listing_id: str, request: Request) -> dict[str, Any]:
        if db.one("SELECT 1 FROM listings WHERE id=?", (listing_id,)) is None:
            raise HTTPException(404, "no such listing")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(422, "a JSON object is needed")
        fields = {}
        for key in ("shortlisted", "muted", "dismissed"):
            if key in body:
                if not isinstance(body[key], bool):
                    raise HTTPException(422, f"{key} must be true or false")
                fields[key] = int(body[key])
        if "note" in body:
            if not isinstance(body["note"], str) or len(body["note"]) > 4000:
                raise HTTPException(422, "note must be text under 4000 characters")
            fields["note"] = body["note"]
        if not fields:
            raise HTTPException(422, "nothing to change")
        with db.tx() as conn:
            conn.execute("INSERT INTO marks(listing_id, updated_at) VALUES(?, ?) "
                         "ON CONFLICT(listing_id) DO NOTHING", (listing_id, now()))
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE marks SET {sets}, updated_at=? WHERE listing_id=?",
                         [*fields.values(), now(), listing_id])
            if fields.get("muted"):
                conn.execute("UPDATE outbox SET status='suppressed', reason='muted by you' "
                             "WHERE listing_id=? AND status='pending'", (listing_id,))
        return dict(db.one("SELECT * FROM marks WHERE listing_id=?", (listing_id,)))

    # --------------------------------------------------------- media

    @app.get("/api/media/{asset_id}", dependencies=[Depends(authed)])
    def get_media(asset_id: str) -> Response:
        if not asset_id.isalnum() or len(asset_id) != 64:
            raise HTTPException(404, "no such photo")
        found = media.asset_path(db, asset_id)
        if found is None:
            raise HTTPException(404, "no such photo")
        path, mime = found
        return FileResponse(str(path), media_type=mime,
                            headers={"Cache-Control": "private, max-age=31536000, immutable"})

    # ------------------------------------------------------ settings

    @app.get("/api/settings", dependencies=[Depends(authed)])
    def get_settings() -> dict[str, Any]:
        return {"settings": settings.public(), "editable": sorted(settings.EDITABLE),
                "version": settings.path.stat().st_mtime_ns if settings.path.is_file() else 0}

    @app.patch("/api/settings", dependencies=[Depends(authed)])
    async def patch_settings(request: Request) -> Response:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(422, "a JSON object is needed")
        problems = settings.apply(body)
        if problems:
            return JSONResponse({"problems": problems}, status_code=422)
        settings.save()
        return JSONResponse({"settings": settings.public()})

    # ---------------------------------------------------------- jobs

    @app.post("/api/checks", dependencies=[Depends(authed)])
    async def post_check(request: Request) -> dict[str, Any]:
        body = await request.json() if int(request.headers.get("content-length") or 0) else {}
        site = str((body or {}).get("site") or "all")
        if site not in ("all", "autotrader", "marketplace"):
            raise HTTPException(422, "site must be all, autotrader or marketplace")
        if service is None:
            raise HTTPException(503, "no scheduler in this process")
        return {"job": service.request_check(site)}

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(authed)])
    def get_job(job_id: str) -> dict[str, Any]:
        found = service.job(job_id) if service is not None else None
        if found is None:
            raise HTTPException(404, "no such job")
        return found

    @app.post("/api/test-alert", dependencies=[Depends(authed)])
    def test_alert() -> dict[str, Any]:
        from . import outbox
        if not settings.ntfy_topic:
            raise HTTPException(422, "no ntfy topic configured")
        try:
            pid = outbox.send_text(settings, "Test from the Mac watcher",
                                   f"Sent {clock.stamp()} from the trial service.",
                                   tags="white_check_mark", priority="default")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, str(exc)[:300])
        return {"ok": True, "id": pid, "topic": settings.ntfy_topic}

    @app.get("/api/outbox", dependencies=[Depends(authed)])
    def get_outbox(status: str = "", limit: int = Query(100, le=1000)) -> list[dict[str, Any]]:
        if status:
            rows = db.q("SELECT * FROM outbox WHERE status=? ORDER BY id DESC LIMIT ?",
                        (status, limit))
        else:
            rows = db.q("SELECT * FROM outbox ORDER BY id DESC LIMIT ?", (limit,))
        return [row_dict(r) for r in rows]

    @app.get("/api/export/{collection_id}.json", dependencies=[Depends(authed)])
    def export_collection(collection_id: str) -> Response:
        rows = db.q("""SELECT l.* FROM listings l JOIN collection_listings m ON m.listing_id=l.id
                       WHERE m.collection_id=? ORDER BY l.first_seen""", (collection_id,))
        out = []
        for r in rows:
            item = _listing(r)
            item["prices"] = [dict(p) for p in db.q(
                "SELECT at, price, currency, price_source, kind, provenance FROM price_events "
                "WHERE listing_id=? ORDER BY at, id", (r["id"],))]
            item["photos"] = media.photos_of(db, r["id"])
            out.append(item)
        return Response(json.dumps({"collection": collection_id, "exported_at": now(),
                                    "listings": out}, indent=1, default=str),
                        media_type="application/json",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{collection_id}.json"'})

    return app


def _listing(row) -> dict[str, Any]:
    out = row_dict(row) or {}
    for key in ("shortlisted", "muted", "dismissed", "qualified"):
        if key in out:
            out[key] = bool(out[key])
    out.setdefault("note", "") or out.update(note=out.get("note") or "")
    return out


_LOGIN_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AutoTrader Watch on this Mac</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;padding:40px 20px;background:#f2f2f7;color:#111}
.card{max-width:420px;margin:0 auto;background:#fff;border-radius:20px;padding:24px}
input{width:100%;box-sizing:border-box;font-size:16px;padding:12px;border-radius:12px;border:1px solid #ccd}
button{margin-top:12px;width:100%;font-size:16px;padding:12px;border-radius:12px;border:0;background:#0a84ff;color:#fff}</style>
<div class="card"><h1 style="font-size:24px;margin:0 0 8px">Sign in</h1>
<p style="color:#555">Paste the token from <code>macwatch/run status</code> on the Mac. It is remembered on this device.</p>
<form method="get" action="/login"><input name="token" placeholder="token" autocomplete="off"><button>Open</button></form></div>"""
