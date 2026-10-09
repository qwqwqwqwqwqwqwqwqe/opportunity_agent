"""MinerU's authenticated file-upload API; called only after per-job consent."""
from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .config import _setting
from .resume_models import MAX_EXPANDED_BYTES, MAX_PAGES, MAX_TEXT_CHARS, ParsedResumeDocument, ResumeBlock
from .resume_parsers import redact_contacts


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MinerUParser:
    api = "https://mineru.net/api/v4"

    @property
    def enabled(self):
        return bool(_setting("MINERU_API_TOKEN"))

    def parse(self, path: Path, cancelled=lambda: False, timeout=120) -> ParsedResumeDocument:
        token = _setting("MINERU_API_TOKEN")
        if not token:
            raise ValueError("增强识别需要在项目 .env 配置 MINERU_API_TOKEN")
        deadline = time.monotonic() + timeout
        opener = build_opener(NoRedirect())

        def request(url, method="GET", payload=None, auth=False, limit=MAX_EXPANDED_BYTES):
            if cancelled():
                raise ValueError("解析已取消")
            host = urlsplit(url)
            allowed = (host.hostname == "mineru.net" or
                       (host.hostname or "").endswith((".openxlab.org.cn", ".shlab.tech", ".mineru.net")) or
                       ("mineru" in (host.hostname or "") and (host.hostname or "").endswith(".aliyuncs.com")))
            if host.scheme != "https" or host.username or host.password or host.port not in (None, 443) or not allowed:
                raise ValueError("解析服务返回的资源地址不在许可域名中")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("文档解析超时")
            headers = {"Authorization": f"Bearer {token}"} if auth else {}
            if isinstance(payload, dict):
                payload = json.dumps(payload).encode()
                headers["Content-Type"] = "application/json"
            with opener.open(Request(url, data=payload, method=method, headers=headers),
                             timeout=min(30, remaining)) as response:
                data = response.read(limit + 1)
            if len(data) > limit:
                raise ValueError("解析结果超过安全大小限制")
            return data

        def api(url, payload=None):
            result = json.loads(request(url, "POST" if payload else "GET", payload, True, 1024 * 1024))
            if result.get("code") != 0:
                raise ValueError("MinerU 请求未成功，请检查 Token、额度或稍后重试")
            return result["data"]

        batch = api(self.api + "/file-urls/batch", {"files": [{"name": "resume" + path.suffix,
            "data_id": path.stem}], "model_version": "vlm", "language": "ch", "enable_formula": False})
        urls = batch.get("file_urls", [])
        if len(urls) != 1:
            raise ValueError("解析服务未返回有效上传地址")
        request(urls[0], "PUT", path.read_bytes())
        while time.monotonic() < deadline:
            result = api(self.api + "/extract-results/batch/" + str(batch["batch_id"]))
            items = result.get("extract_result", [])
            item = items[0] if items else {}
            progress = item.get("extract_progress") or {}
            if progress.get("total_pages", 0) > MAX_PAGES:
                raise ValueError("简历不能超过 20 页")
            if item.get("state") == "failed":
                raise ValueError("MinerU 未能解析文件，请检查格式或更换文件")
            if item.get("state") == "done":
                data = request(item["full_zip_url"])
                return self._decode_zip(data)
            for _ in range(10):
                if cancelled():
                    raise ValueError("解析已取消")
                time.sleep(.2)
        raise TimeoutError("文档解析超时")

    @staticmethod
    def _decode_zip(data: bytes) -> ParsedResumeDocument:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 3000 or sum(i.file_size for i in infos) > MAX_EXPANDED_BYTES:
                raise ValueError("云端解压结果超出限制")
            # Read selected members in memory; never extract archive paths.
            member = next((i.filename for i in infos if i.filename.endswith("_content_list.json")), None)
            blocks = []
            pages = 0
            if member:
                for i, item in enumerate(json.loads(archive.read(member))):
                    page = int(item.get("page_idx", 0)) + 1
                    pages = max(pages, page)
                    text = str(item.get("text") or item.get("table_body") or "").strip()
                    if text:
                        blocks.append(ResumeBlock(block_id=f"cloud{i}", text=redact_contacts(text),
                            page=page, locator=f"第 {page} 页 · 块 {i+1}"))
            else:
                raise ValueError("云端缺少带页码的内容列表，请重试或手工补充")
            if pages > MAX_PAGES or sum(len(b.text) for b in blocks) > MAX_TEXT_CHARS:
                raise ValueError("简历超过页数或文本上限")
            if not blocks:
                raise ValueError("云端未识别到有效文本")
            return ParsedResumeDocument(parser="mineru", blocks=blocks, page_count=pages)

