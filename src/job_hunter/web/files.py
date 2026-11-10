"""My files: edit the YAML settings, keep personal documents, import a CV from a PDF, and
move a whole data folder in or out as a zip."""

from __future__ import annotations

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response

from job_hunter import cvimport
from job_hunter.userfiles import (
    MAX_ZIP_BYTES,
    YAML_FILES,
    FileError,
    doc_path,
    export_zip,
    history,
    import_zip,
    list_docs,
    read_yaml,
    save_doc,
    save_yaml,
    undo_yaml,
)
from job_hunter.users import DOC_FOLDERS
from job_hunter.web.workspace import Workspace, flash, render, workspace

WS = Depends(workspace)
UPLOAD = File(...)
LABELS = {
    "config.yaml": "Profile & search",
    "companies.yaml": "Companies",
    "master_cv.yaml": "Master CV",
}


def _yaml_name(name: str) -> str:
    if name not in YAML_FILES:
        raise HTTPException(404, "not an editable file")
    return name


def _to_files(edit: str = "") -> RedirectResponse:
    return RedirectResponse(f"/files?edit={edit}" if edit else "/files", status_code=303)


def _page(request: Request, ws: Workspace, edit: str, text=None, error="", status=200):
    edit = edit if edit in YAML_FILES else YAML_FILES[0]
    ctx = {
        "tabs": [(n, LABELS[n]) for n in YAML_FILES],
        "edit": edit,
        "text": read_yaml(ws.data_dir, edit) if text is None else text,
        "error": error,
        "versions": len(history(ws.data_dir, edit)),
        "docs": list_docs(ws.data_dir),
        "folders": DOC_FOLDERS,
        "draft": cvimport.read_draft(ws.data_dir),
    }
    return render(request, "files.html", ctx, status_code=status)


def _yaml_routes(app: FastAPI) -> None:
    @app.get("/files")
    def files(request: Request, edit: str = "", ws: Workspace = WS):
        return _page(request, ws, edit)

    @app.post("/files/yaml/{name}")
    def save(request: Request, name: str, text: str = Form(...), ws: Workspace = WS):
        name = _yaml_name(name)
        try:
            save_yaml(ws.data_dir, name, text)
        except FileError as exc:
            return _page(request, ws, name, text, str(exc), status=400)
        flash(request, f"Saved {LABELS[name]}. The next run uses it.")
        return _to_files(name)

    @app.post("/files/yaml/{name}/undo")
    def undo(request: Request, name: str, ws: Workspace = WS):
        name = _yaml_name(name)
        if undo_yaml(ws.data_dir, name):
            flash(request, f"Restored the previous {LABELS[name]}.")
        else:
            flash(request, "No earlier version to restore.", "warn")
        return _to_files(name)


def _doc_routes(app: FastAPI) -> None:
    @app.post("/files/docs")
    async def upload(
        request: Request,
        folder: str = Form(...),
        upload: UploadFile = UPLOAD,
        ws: Workspace = WS,
    ):
        try:
            saved = save_doc(ws.data_dir, folder, upload.filename or "", await upload.read())
        except FileError as exc:
            flash(request, str(exc), "error")
        else:
            flash(request, f"Uploaded {saved.name} to {folder}.")
        return _to_files()

    @app.get("/files/docs/{folder}/{filename}")
    def download(folder: str, filename: str, ws: Workspace = WS):
        try:
            path = doc_path(ws.data_dir, folder, filename)
        except FileError as exc:
            raise HTTPException(404, str(exc)) from exc
        if not path.is_file():
            raise HTTPException(404, "no such file")
        return FileResponse(path, filename=path.name)

    @app.post("/files/docs/{folder}/{filename}/delete")
    def delete(request: Request, folder: str, filename: str, ws: Workspace = WS):
        try:
            path = doc_path(ws.data_dir, folder, filename)
        except FileError as exc:
            raise HTTPException(404, str(exc)) from exc
        path.unlink(missing_ok=True)
        flash(request, f"Deleted {path.name}.")
        return _to_files()


def _cv_routes(app: FastAPI) -> None:
    @app.post("/files/cv-import")
    async def cv_import(request: Request, upload: UploadFile = UPLOAD, ws: Workspace = WS):
        content = await upload.read()
        try:
            save_doc(ws.data_dir, "cv", upload.filename or "cv.pdf", content)
            llm = request.app.state.llm_factory()
            cvimport.make_draft(ws.data_dir, content, llm, ws.settings.tailor_model)
        except FileError as exc:
            flash(request, str(exc), "error")
            return _to_files()
        flash(request, "Draft master CV ready: check it below, then apply it.")
        return _to_files("master_cv.yaml")

    @app.post("/files/cv-import/apply")
    def cv_apply(request: Request, text: str = Form(...), ws: Workspace = WS):
        try:
            cvimport.apply_draft(ws.data_dir, text)
        except FileError as exc:
            flash(request, f"Draft not applied: {exc}", "error")
            return _to_files("master_cv.yaml")
        flash(request, "Master CV replaced by the draft. Undo restores the previous one.")
        return _to_files("master_cv.yaml")

    @app.post("/files/cv-import/discard")
    def cv_discard(request: Request, ws: Workspace = WS):
        cvimport.discard_draft(ws.data_dir)
        flash(request, "Draft discarded.")
        return _to_files("master_cv.yaml")


def _zip_routes(app: FastAPI) -> None:
    @app.get("/files/export.zip")
    def export(ws: Workspace = WS):
        headers = {"Content-Disposition": f'attachment; filename="job-hunter-{ws.user.slug}.zip"'}
        return Response(export_zip(ws.data_dir), media_type="application/zip", headers=headers)

    @app.post("/files/import")
    async def import_(request: Request, upload: UploadFile = UPLOAD, ws: Workspace = WS):
        content = await upload.read(MAX_ZIP_BYTES + 1)
        request.app.state.stores.drop(ws.data_dir)
        try:
            count = import_zip(ws.data_dir, content)
        except FileError as exc:
            flash(request, f"Import failed: {exc}", "error")
        else:
            flash(request, f"Imported {count} files.")
        return _to_files()


def install_files(app: FastAPI) -> None:
    _yaml_routes(app)
    _doc_routes(app)
    _cv_routes(app)
    _zip_routes(app)
