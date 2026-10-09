/* Offline, deliberately limited Markdown viewer. Source HTML is always escaped. */
(function (root) {
  'use strict';
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function inline(value) {
    // Tokenize before escaping: links/code cannot introduce active HTML or protocols.
    const tokens = [];
    let text = String(value).replace(/`([^`]+)`|\[([^\]]+)\]\(([^\s)]+)\)/g, (match, code, label, url) => {
      let html;
      if (code !== undefined) html = '<code>' + escape(code) + '</code>';
      else if (/^https?:\/\//i.test(url)) html = '<a href="' + escape(url) + '" target="_blank" rel="noopener noreferrer">' + escape(label) + '</a>';
      else html = escape(label) + ' <span class="md-muted">（链接已禁用）</span>';
      const key = '\u0000' + tokens.length + '\u0000'; tokens.push(html); return key;
    });
    text = escape(text).replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
      .replace(/__([^_\n]+)__/g, '<strong>$1</strong>')
      .replace(/(^|\s)\*([^*\n]+)\*(?=\s|[.,，。]|$)/g, '$1<em>$2</em>');
    return text.replace(/\u0000(\d+)\u0000/g, (_, index) => tokens[Number(index)] || '');
  }
  function cells(line) {
    return line.trim().replace(/^\|/, '').replace(/\|$/, '').split(/(?<!\\)\|/).map(x => x.trim().replace(/\\\|/g, '|'));
  }
  const pipe = line => /^\s*\|.*\|\s*$/.test(line);
  const separator = line => (cells(line).every(x => /^:?-{3,}:?$/.test(x)) && cells(line).length > 0)
    // Extracted tables sometimes lose pipes only in the separator row.
    || /^\s*\|?\s*:?-{3,}:?(?:\s+:?-{3,}:?)+\s*\|?\s*$/.test(line);
  function render(source) {
    // Normalize only the display; the raw text and all evidence offsets stay untouched.
    const lines = String(source ?? '').replace(/\r\n?/g, '\n').split('\n');
    const result = []; let index = 0;
    while (index < lines.length) {
      let line = lines[index];
      if (!line.trim()) { index++; continue; }
      if (/^\s*```/.test(line)) {
        const body = []; index++;
        while (index < lines.length && !/^\s*```/.test(lines[index])) body.push(lines[index++]);
        if (index < lines.length) index++;
        result.push('<pre class="md-code"><code>' + escape(body.join('\n')) + '</code></pre>'); continue;
      }
      const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+)/);
      if (heading) {
        const level = Math.min(heading[1].length + 1, 6);
        result.push('<h' + level + '>' + inline(heading[2]) + '</h' + level + '>'); index++; continue;
      }
      if (pipe(line) || (line.includes('|') && index + 1 < lines.length && separator(lines[index + 1]))) {
        const header = index + 1 < lines.length && separator(lines[index + 1]);
        const rows = [cells(line)]; index++;
        if (header) index++;
        while (index < lines.length && (pipe(lines[index]) || (header && lines[index].includes('|')))) {
          if (!separator(lines[index])) rows.push(cells(lines[index])); index++;
        }
        let html = '<div class="md-table-scroll"><table>';
        if (header) html += '<thead><tr>' + rows.shift().map(x => '<th>' + inline(x) + '</th>').join('') + '</tr></thead>';
        html += '<tbody>' + rows.map(row => '<tr>' + row.map(x => '<td>' + inline(x) + '</td>').join('') + '</tr>').join('') + '</tbody></table></div>';
        if (!header) html += '<p class="md-muted md-caption">片段未包含完整表头；按原始行展示，不推断列含义。</p>';
        result.push(html); continue;
      }
      if (/^\s*>\s?/.test(line)) {
        const body = [];
        while (index < lines.length && /^\s*>/.test(lines[index])) body.push(inline(lines[index++].replace(/^\s*>\s?/, '')));
        result.push('<blockquote>' + body.join('<br>') + '</blockquote>'); continue;
      }
      if (/^\s*(?:[-+*]|\d+[.)])\s+/.test(line)) {
        const ordered = /^\s*\d+[.)]/.test(line), tag = ordered ? 'ol' : 'ul', body = [];
        const pattern = ordered ? /^\s*\d+[.)]\s+(.+)/ : /^\s*[-+*]\s+(.+)/;
        while (index < lines.length && pattern.test(lines[index])) body.push('<li>' + inline(lines[index++].match(pattern)[1]) + '</li>');
        result.push('<' + tag + '>' + body.join('') + '</' + tag + '>'); continue;
      }
      if (/^\s*(?:---+|\*\*\*+)\s*$/.test(line)) { result.push('<hr>'); index++; continue; }
      const body = [inline(line.trim())]; index++;
      while (index < lines.length && lines[index].trim() && !/^\s*(?:#|>|```|[-+*]\s|\d+[.)]\s)/.test(lines[index]) && !pipe(lines[index])
             && !(lines[index].includes('|') && index + 1 < lines.length && separator(lines[index + 1]))) body.push(inline(lines[index++].trim()));
      result.push('<p>' + body.join('<br>') + '</p>');
    }
    return result.join('\n');
  }
  function card(text) {
    return '<div class="chunk-reader"><div class="chunk-toolbar"><strong>证据正文</strong><span>' + String(text ?? '').length.toLocaleString() + ' 字符 · Markdown 预览</span></div>'
      + '<article class="chunk-markdown">' + render(text) + '</article>'
      + '<details class="chunk-original"><summary>查看原始文本（逐字引用请以此为准）</summary><pre>' + escape(text) + '</pre></details></div>';
  }
  root.ChunkMarkdown = {render, card};
})(globalThis);
