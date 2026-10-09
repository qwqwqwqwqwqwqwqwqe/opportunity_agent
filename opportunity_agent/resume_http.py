"""Resume HTTP adapter. Optional parsing packages load only on upload."""
from __future__ import annotations
import io
import json
import threading
from urllib.parse import parse_qs, urlsplit

from .resume_models import MAX_FILE_BYTES
from .resume_service import ResumeImportService

_service_lock = threading.Lock()


def service_for(store):
    with _service_lock:
        if not hasattr(store, "_resume_service"):
            store._resume_service = ResumeImportService(store)
        return store._resume_service


def _json_body(handler):
    length = int(handler.headers.get("Content-Length", 0))
    if not 0 < length <= 1024 * 1024:
        raise ValueError("简历草稿请求大小无效")
    value = json.loads(handler.rfile.read(length))
    if not isinstance(value, dict):
        raise ValueError("请求必须是 JSON 对象")
    return value


def _session(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError("session_id 无效")
    return value


def resume_http(handler, method):
    parsed = urlsplit(handler.path)
    prefix = "/api/resume/imports"
    if not (parsed.path == prefix or parsed.path.startswith(prefix + "/")):
        return False
    service = service_for(handler.store)
    parts = parsed.path[len(prefix):].strip("/").split("/") if parsed.path != prefix else []
    if method in {"GET", "DELETE"}:
        sid = _session(parse_qs(parsed.query).get("session_id", [None])[0])
        if len(parts) > 1:
            raise ValueError("未知的简历地址")
        if method == "GET":
            result = service.get(sid, parts[0]) if parts else {"imports": service.list(sid)}
        elif len(parts) == 1:
            result = service.delete(sid, parts[0])
        else:
            raise ValueError("请选择要删除的简历")
        handler._send_json(200, result)
        return True
    if not parts:
        length = int(handler.headers.get("Content-Length", 0))
        if not 0 < length <= MAX_FILE_BYTES + 65536:
            raise ValueError("上传不能超过 10 MB")
        try:
            from python_multipart import create_form_parser
        except ImportError as exc:
            raise ValueError('请先运行 python -m pip install -e ".[resume]"') from exc
        fields, uploads, opened_files = {}, [], []
        def on_field(field):
            name = field.field_name.decode("utf-8")
            if name in fields or len(fields) >= 4:
                raise ValueError("上传字段重复或过多")
            fields[name] = field.value.decode("utf-8")
        def on_file(file):
            opened_files.append(file)
            if uploads:
                raise ValueError("一次只能上传一份简历")
            file.file_object.seek(0)
            data = file.file_object.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise ValueError("简历不能超过 10 MB")
            uploads.append((file.file_name.decode("utf-8"), data))
        body = handler.rfile.read(length)
        if not handler.headers.get("Content-Type", "").lower().startswith("multipart/form-data"):
            raise ValueError("请使用 multipart/form-data 上传")
        # Keep the capped multipart body in memory. Only our service writes an
        # original to disk, with a tracked random name and guaranteed cleanup.
        parser = create_form_parser({"Content-Type": handler.headers.get("Content-Type", "").encode(),
                    "Content-Length": str(length).encode()}, on_field, on_file,
                    config={"MAX_MEMORY_FILE_SIZE": MAX_FILE_BYTES + 65536,
                            "MAX_BODY_SIZE": MAX_FILE_BYTES + 65536})
        try:
            parser.write(body)
            parser.finalize()
        except Exception as exc:
            raise ValueError("上传表单格式无效") from exc
        finally:
            parser.close()
            for file in opened_files:
                file.close()
        if len(uploads) != 1:
            raise ValueError("请选择一个 PDF/DOC/DOCX 文件")
        result = service.upload(_session(fields.get("session_id")), fields.get("request_id"),
                                uploads[0][0], uploads[0][1], fields.get("enhanced") == "true")
        handler._send_json(202, result)
        return True
    payload = _json_body(handler)
    sid = _session(payload.get("session_id"))
    import_id = parts[0]
    action = parts[1] if len(parts) == 2 else ""
    if action == "cloud-consent":
        if not isinstance(payload.get("accept"), bool):
            raise ValueError("请明确同意或拒绝云端解析")
        service.get(sid, import_id)  # existence and expiry check before dispatch
        result = service.consent(sid, import_id, payload["accept"])
    elif action == "draft":
        result = service.save_draft(sid, import_id, payload)
    elif action == "confirm":
        result = service.confirm(sid, import_id, payload)
    elif action == "retry":
        service.get(sid, import_id)
        result = service.retry(sid, import_id, payload.get("text"))
    elif action == "reextract":
        service.get(sid, import_id)
        result = service.reextract(sid, import_id)
    else:
        raise ValueError("未知的简历操作")
    handler._send_json(200, result)
    return True
