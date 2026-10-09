"""Bounded file parsing; no macros, embedded links or PDF JavaScript are run."""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .resume_models import (MAX_EXPANDED_BYTES, MAX_FILE_BYTES, MAX_PAGES, MAX_TEXT_CHARS,
                            ParsedResumeDocument, ResumeBlock)


def validate_file(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError("简历不能为空，且不能超过 10 MB")
    if suffix == ".pdf" and data.startswith(b"%PDF-"):
        return suffix
    if suffix == ".doc" and data.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
        # Check the compound document structurally before sending it elsewhere.
        try:
            import olefile
        except ImportError as exc:
            raise ValueError('DOC 校验依赖缺失，请安装项目的 [resume] 可选依赖') from exc
        try:
            with olefile.OleFileIO(io.BytesIO(data)) as ole:
                names = ole.listdir()
                if any(any(p.casefold() in {"vba", "macros", "_vba_project_cur"} for p in n) for n in names):
                    raise ValueError("不支持包含宏的 Word 文件")
                if ole.exists("EncryptedPackage") or ole.exists("EncryptionInfo"):
                    raise ValueError("请先解除 Word 文件密码")
                if not ole.exists("WordDocument"):
                    raise ValueError("文件不是有效的 Word DOC")
                header = ole.openstream("WordDocument").read(12)
                if header[:2] != b"\xec\xa5":
                    raise ValueError("文件缺少有效的 Word DOC 标识")
                if len(header) < 12 or int.from_bytes(header[10:12], "little") & 0x0100:
                    raise ValueError("请先解除 Word 文件密码")
        except (OSError, IOError) as exc:
            raise ValueError("DOC 文件损坏") from exc
        return suffix
    if suffix == ".docx" and data.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                infos = archive.infolist()
                if len(infos) > 3000 or sum(x.file_size for x in infos) > MAX_EXPANDED_BYTES:
                    raise ValueError("Word 解压内容超出安全限制")
                names = {i.filename for i in infos}
                if "word/document.xml" not in names or "[Content_Types].xml" not in names:
                    raise ValueError("文件不是有效的 DOCX")
                if any("vbaproject" in n.lower() for n in names):
                    raise ValueError("不支持包含宏的 Word 文件")
                types = archive.read("[Content_Types].xml")
                if b"macroEnabled" in types or any(i.flag_bits & 1 for i in infos):
                    raise ValueError("不支持宏或加密 Word 文件")
                for info in infos:
                    if info.filename.lower().endswith(".xml"):
                        content = archive.read(info)
                        if b"<!DOCTYPE" in content or b"<!ENTITY" in content:
                            raise ValueError("Word 含不安全的 XML 声明")
        except (zipfile.BadZipFile, RuntimeError) as exc:
            raise ValueError("DOCX 文件损坏") from exc
        return suffix
    raise ValueError("仅接受真实的 PDF、DOC、DOCX 文件；文件内容与扩展名必须一致")


def redact_contacts(text: str) -> str:
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[邮箱已过滤]", text)
    text = re.sub(r"(?<!\d)\d{17}[\dXx](?!\d)", "[证件号码已过滤]", text)
    text = re.sub(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)", "[电话已过滤]", text)
    return re.sub(r"(?im)((?:phone|tel|mobile|电话|手机)\s*[:：]?\s*)[+\d() -]{7,}", r"\1[电话已过滤]", text)


def parse_local(path: Path) -> ParsedResumeDocument:
    result = ParsedResumeDocument()
    if path.suffix == ".doc":
        result.needs_cloud = True
        result.warnings.append("旧 DOC 需要云端格式转换；也可自行另存为 PDF/DOCX 后上传")
        return result
    if path.suffix == ".docx":
        try:
            from docx import Document
            from docx.table import Table
            from docx.text.paragraph import Paragraph
        except ImportError as exc:
            raise ValueError('请先运行 python -m pip install -e ".[resume]"') from exc
        document = Document(path)
        # Word's stored page count, if present, is advisory but can reject an
        # obviously excessive document without requiring a desktop renderer.
        with zipfile.ZipFile(path) as archive:
            if "docProps/app.xml" in archive.namelist():
                root = ElementTree.fromstring(archive.read("docProps/app.xml"))
                pages = next((int(e.text) for e in root.iter() if e.tag.endswith("}Pages") and (e.text or "").isdigit()), None)
                if pages and pages > MAX_PAGES:
                    raise ValueError("简历不能超过 20 页")
                result.page_count = pages
        for index, child in enumerate(document.element.body):
            if child.tag.endswith("}p"):
                text = Paragraph(child, document).text
            elif child.tag.endswith("}tbl"):
                text = "\n".join(" | ".join(dict.fromkeys(c.text for c in row.cells))
                                 for row in Table(child, document).rows)
            else:
                continue
            if text.strip():
                result.blocks.append(ResumeBlock(block_id=f"p{index+1}", text=redact_contacts(text.strip()),
                                                  locator=f"段落/表格 {index+1}"))
        if document.element.xpath(".//w:txbxContent"):
            result.needs_cloud = True
            result.warnings.append("Word 含文本框，本地段落读取可能不完整，建议增强识别")
    else:
        try:
            import pdfplumber
        except ImportError as exc:
            raise ValueError('请先运行 python -m pip install -e ".[resume]"') from exc
        try:
            with pdfplumber.open(path) as pdf:
                result.page_count = len(pdf.pages)
                if result.page_count > MAX_PAGES:
                    raise ValueError("简历不能超过 20 页")
                if pdf.doc.encryption:
                    raise ValueError("请先解除 PDF 密码")
                for page in pdf.pages:
                    text = (page.extract_text(layout=True) or "").strip()
                    if len(re.sub(r"\s", "", text)) < 25:
                        result.needs_cloud = True
                        result.warnings.append(f"第 {page.page_number} 页文字过少，可能是扫描页")
                    if text:
                        result.blocks.append(ResumeBlock(block_id=f"page{page.page_number}",
                            text=redact_contacts(text), page=page.page_number, locator=f"第 {page.page_number} 页"))
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("PDF 无法读取，请检查损坏或密码保护") from exc
    if len(result.text) > MAX_TEXT_CHARS:
        raise ValueError("简历文本过长，请精简后上传；不会截断后伪装完整识别")
    if (len(result.text.strip()) < 25 or result.text.count("\ufffd") > max(3, len(result.text) * .01)
            or len(re.findall(r"\(cid:\d+\)", result.text)) > 3):
        result.needs_cloud = True
        result.warnings.append("本地文字不足或存在乱码，请增强识别或粘贴正确文本")
    return result
