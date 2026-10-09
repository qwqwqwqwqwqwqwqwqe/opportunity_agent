"""Viewer rendering must be offline, safe, and never mutate evidence text."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_markdown_tables_lists_raw_source_and_untrusted_html():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for JavaScript renderer tests")
    renderer = Path(__file__).resolve().parents[1] / "opportunity_agent/v2/evaluation/research_markdown.js"
    script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
const source='# Admissions\n\n| Programme | Deadline |\n| --- | --- |\n| **MSCS** | January 15 |\n\n- GRE optional\n- English required';
const output=ChunkMarkdown.render(source);
assert(output.includes('<h2>Admissions</h2>'));
assert(output.includes('<thead>'));
assert(output.includes('<th>Programme</th>'));
assert(output.includes('<td><strong>MSCS</strong></td>'));
assert(output.includes('<ul><li>GRE optional</li>'));
const card=ChunkMarkdown.card(source);
assert(card.includes('查看原始文本'));
assert(card.includes('| Programme | Deadline |'));
const bad=ChunkMarkdown.render('<script>alert(1)</script>\n<img src=x onerror=alert(1)>\n[x](javascript:alert(1))');
assert(!bad.includes('<script>'));assert(!bad.includes('<img'));
assert(!bad.includes('href="javascript:'));assert(bad.includes('&lt;script&gt;'));
assert(ChunkMarkdown.render('[Official](https://school.edu)').includes('rel="noopener noreferrer"'));
assert(ChunkMarkdown.render('| MS | Jan 15 |').includes('片段未包含完整表头'));
assert(ChunkMarkdown.render('| Programme | Deadline |\n ---  --- |\n| MS | January 15 |').includes('<thead>'));
assert(ChunkMarkdown.render('```\n<b>raw</b>\n```').includes('&lt;b&gt;raw&lt;/b&gt;'));
assert(source==='# Admissions\n\n| Programme | Deadline |\n| --- | --- |\n| **MSCS** | January 15 |\n\n- GRE optional\n- English required');
'''
    subprocess.run([node, "-e", script, str(renderer)], check=True, capture_output=True, text=True)


def test_local_markdown_assets_are_served_and_both_pages_use_viewer():
    from fastapi.testclient import TestClient
    from opportunity_agent.v2.evaluation.research_annotation import create_app
    with TestClient(create_app(Path.cwd() / "deliverables/research/real150")) as client:
        for page in ('/', '/llm'):
            body = client.get(page).text
            assert '/annotation-assets/markdown.js' in body
            assert 'ChunkMarkdown.card(d.text)' in body
        js = client.get('/annotation-assets/markdown.js')
        assert js.status_code == 200
        assert js.headers['content-type'].startswith('application/javascript')
        css = client.get('/annotation-assets/markdown.css')
        assert css.status_code == 200
        assert '.md-table-scroll' in css.text
        assert client.get('/annotation-assets/unknown.js').status_code == 404
