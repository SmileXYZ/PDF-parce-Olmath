#!/usr/bin/env python3
"""
service.py — HTTP-микросервис вокруг olymp_parse.parse. Без нейросетей.

Эндпоинты:
  GET  /health                     -> {"ok": true}
  POST /parse                      -> парсит PDF, возвращает JSON + картинки base64
       принимает ЛИБО:
         - multipart/form-data, поле file=<pdf>            (загрузка с фронта)
         - application/pdf, тело = сырые байты PDF          (бинарь в вебхук)
         - application/json {"url": "...", "headers": {...}} (скачать с Supabase/S3)
       query ?images=base64|none   (none — только JSON с bbox, без картинок)

Ответ /parse:
{
  "source": "9-sol.pdf",
  "tasks": [ { number, author, statement_md, answer_md, solutions[],
              comments[], figures[{id,page,bbox,file,mime,b64?}], meta{}, verified } ]
}
Плейсхолдеры {{FIG:id}} остаются в *_md как есть — подстановку URL делает n8n
после заливки картинок в Supabase Storage.

Запуск:
  uvicorn service:app --host 0.0.0.0 --port 8080
Docker: см. Dockerfile рядом.
"""
import os, io, base64, tempfile, shutil, urllib.request, json
from fastapi import FastAPI, Request, UploadFile, File, HTTPException, Query
from fastapi.responses import JSONResponse
import olymp_parse

app = FastAPI(title="olympiad-pdf-parser", version="1.0")

MAX_BYTES = int(os.environ.get("MAX_PDF_BYTES", 40 * 1024 * 1024))  # 40 MB


@app.get("/health")
def health():
    return {"ok": True, "pymupdf": olymp_parse.fitz.__doc__.splitlines()[0]}


async def _read_pdf_bytes(request: Request, file: UploadFile | None) -> bytes:
    ct = (request.headers.get("content-type") or "").lower()
    # 1) multipart form (фронт-форма)
    if file is not None:
        data = await file.read()
    # 2) сырой бинарь application/pdf или octet-stream
    elif "application/pdf" in ct or "octet-stream" in ct:
        data = await request.body()
    # 3) JSON со ссылкой (скачать с Supabase Storage / S3 / любого URL)
    elif "application/json" in ct:
        payload = await request.json()
        url = payload.get("url")
        if not url:
            raise HTTPException(400, "json body requires 'url'")
        req = urllib.request.Request(url, headers=payload.get("headers") or {})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
    else:
        # последний шанс — вдруг тело всё же есть
        data = await request.body()
    if not data:
        raise HTTPException(400, "empty body: send multipart file, raw pdf, or json{url}")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, f"pdf too large (> {MAX_BYTES} bytes)")
    if data[:5] != b"%PDF-":
        raise HTTPException(415, "not a PDF (missing %PDF- signature)")
    return data


@app.post("/parse")
async def parse_endpoint(
    request: Request,
    file: UploadFile | None = File(default=None),
    images: str = Query(default="base64", pattern="^(base64|none)$"),
):
    data = await _read_pdf_bytes(request, file)
    workdir = tempfile.mkdtemp(prefix="olymp_")
    try:
        pdf_path = os.path.join(workdir, "in.pdf")
        with open(pdf_path, "wb") as f:
            f.write(data)
        result = olymp_parse.parse(pdf_path, workdir)

        figdir = os.path.join(workdir, "figures")
        for t in result["tasks"]:
            for fig in t["figures"]:
                p = os.path.join(workdir, fig["file"])
                fig["mime"] = "image/png"
                if images == "base64" and os.path.exists(p):
                    with open(p, "rb") as fp:
                        fig["b64"] = base64.b64encode(fp.read()).decode()
                fig.pop("file", None)  # локальный путь наружу не отдаём
        return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"parse failed: {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
