#!/usr/bin/env python3
# olymp_parse.py - deterministic parser of digital olympiad LaTeX PDFs.
# No neural nets. PyMuPDF only. Display formulas are rebuilt in 2D from
# geometry (fraction bars -> \frac, recursive).
import os, re, json, statistics
import fitz

DPI = 200
SYM = {
    '\u00b7': r'\cdot ', '\u00d7': r'\times ', '\u2212': '-', '\u2013': '-',
    '\u2a7e': r'\geqslant ', '\u2a7d': r'\leqslant ', '\u2265': r'\ge ', '\u2264': r'\le ',
    '\u2220': r'\angle ', '\u25e6': r'\circ ', '\u2208': r'\in ', '\u2260': r'\ne ',
    '\u21d4': r'\Leftrightarrow ', '\u21d2': r'\Rightarrow ', '\u2192': r'\to ',
    '\u221a': r'\sqrt', '\u221e': r'\infty ', '\u25b3': r'\triangle ', '\u00b1': r'\pm ',
    '\u03c0': r'\pi ', '\u03c6': r'\varphi ', '\u03c8': r'\psi ', '\u03b1': r'\alpha ',
    '\u03b2': r'\beta ', '\u03b3': r'\gamma ', '\u03b4': r'\delta ', '\u03c9': r'\omega ',
    '\u03bb': r'\lambda ', '\u03bc': r'\mu ', '\u03c1': r'\rho ', '\u03c3': r'\sigma ',
    '\u03c4': r'\tau ', '\u03b8': r'\theta ', '\u03be': r'\xi ',
    '\u2211': r'\sum ', '\u220f': r'\prod ', '\u222b': r'\int ', '\u2202': r'\partial ',
    '\u2032': "'", '\u2033': "''", '\u2026': r'\ldots ', '\u25a1': '', '\u223c': r'\sim ',
    '\u2248': r'\approx ', '\u2261': r'\equiv ', '\u222a': r'\cup ', '\u2229': r'\cap ',
    '\u2282': r'\subset ', '\u2286': r'\subseteq ', '\u2200': r'\forall ', '\u2203': r'\exists ',
    '\u00ac': r'\neg ', '\u2227': r'\wedge ', '\u2228': r'\vee ', '\u22a5': r'\perp ',
    '\u2225': r'\parallel ', '\u2205': r'\varnothing ', '\u2213': r'\mp ', '\u2207': r'\nabla ',
    '\x12': r'\left(', '\x13': r'\right)', '\x0e': r'\left(', '\x0f': r'\right)',
    '\x10': r'\left[', '\x11': r'\right]', '\uf8f6': r'\right)', '\uf8f5': r'\left(',
}
NAMED = {'sin', 'cos', 'tg', 'ctg', 'cot', 'arctg', 'arcctg', 'arcsin', 'arccos',
         'tan', 'log', 'ln', 'lg', 'min', 'max', 'gcd', 'lcm', 'lim', 'exp', 'deg'}
BUILTIN = {'sin', 'cos', 'tan', 'cot', 'log', 'ln', 'lg', 'min', 'max', 'gcd', 'lim',
           'exp', 'deg', 'arcsin', 'arccos'}

SEC_RE = re.compile(
    r'^(\u041e\u0442\u0432\u0435\u0442|\u0420\u0435\u0448\u0435\u043d\u0438\u0435(?:\s+\d+)?|'
    r'\u0421\u043f\u043e\u0441\u043e\u0431(?:\s+\d+)?|\u041a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0439(?:\s+\d+)?|'
    r'\u041e\u0446\u0435\u043d\u043a\u0430|\u041f\u0440\u0438\u043c\u0435\u0440|'
    r'\u0417\u0430\u043c\u0435\u0447\u0430\u043d\u0438\u0435[^.:]*|'
    r'\u0414\u043e\u043a\u0430\u0437\u0430\u0442\u0435\u043b\u044c\u0441\u0442\u0432\u043e)\s*[.:]')
TASK_RE = re.compile(r'^\u0417\u0430\u0434\u0430\u0447\u0430\s+(\d+)\s*\.')
AUTHOR_RE = re.compile(r'^\(([\u0410-\u042f\u0401][^)]{2,60})\)$')


def is_math_font(f):
    return f[:2] in ('CM', 'MS', 'LA', 'RS', 'EU', 'BB')


def page_geometry(page):
    h_bars, curves, draw_rects = [], [], []
    for d in page.get_drawings():
        draw_rects.append(fitz.Rect(d['rect']))
        for it in d['items']:
            if it[0] == 'l':
                (x0, y0), (x1, y1) = it[1], it[2]
                if abs(y0 - y1) < 0.6 and abs(x1 - x0) > 2:
                    h_bars.append((min(x0, x1), max(x0, x1), (y0 + y1) / 2))
            elif it[0] == 're':
                rr = it[1]
                h_bars += [(rr.x0, rr.x1, rr.y0), (rr.x0, rr.x1, rr.y1)]
            elif it[0] == 'c':
                curves.append(fitz.Rect(d['rect']))
    img_rects = [fitz.Rect(im['bbox']) for im in page.get_image_info()]
    return h_bars, curves, draw_rects, img_rects


def cluster_rects(rects, gap=20):
    boxes = [[r.x0, r.y0, r.x1, r.y1] for r in map(fitz.Rect, rects)]
    changed = True
    while changed:
        changed = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if (a[0]-gap <= b[2]+gap and b[0]-gap <= a[2]+gap and
                        a[1]-gap <= b[3]+gap and b[1]-gap <= a[3]+gap):
                    boxes[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    boxes.pop(j)
                    changed = True
                    break
            if changed:
                break
    return [fitz.Rect(b) for b in boxes]


def detect_graphics(page, h_bars, curves, draw_rects, img_rects):
    segs = []
    for d in page.get_drawings():
        for it in d['items']:
            if it[0] == 'l':
                segs.append((it[1], it[2]))
            elif it[0] == 're':
                rr = it[1]
                segs += [((rr.x0, rr.y0), (rr.x1, rr.y0)), ((rr.x0, rr.y1), (rr.x1, rr.y1)),
                         ((rr.x0, rr.y0), (rr.x0, rr.y1)), ((rr.x1, rr.y0), (rr.x1, rr.y1))]
    clusters = cluster_rects(draw_rects + img_rects, gap=20)
    figures, tables = [], []
    for c in clusters:
        if c.get_area() < 1500 or c.width < 25 or c.height < 12:
            continue
        inside = [s for s in segs
                  if c.x0-2 <= min(s[0][0], s[1][0]) and max(s[0][0], s[1][0]) <= c.x1+2
                  and c.y0-2 <= min(s[0][1], s[1][1]) and max(s[0][1], s[1][1]) <= c.y1+2]
        horiz = [s for s in inside if abs(s[0][1]-s[1][1]) < .5]
        ortho = [s for s in inside if abs(s[0][0]-s[1][0]) < .5 or abs(s[0][1]-s[1][1]) < .5]
        has_img = any(c.intersects(ir) for ir in img_rects)
        has_curve = any(c.x0-2 <= cr.x0 and cr.x1 <= c.x1+2 and c.y0-2 <= cr.y0 and cr.y1 <= c.y1+2
                        for cr in curves)
        n_text = sum(len(tl['spans']) for tb in page.get_text('dict', clip=c)['blocks']
                     if tb['type'] == 0 for tl in tb['lines'])
        if inside and len(horiz) == len(inside) and not has_img and not has_curve:
            continue
        if (inside and len(ortho) == len(inside) and not has_img and not has_curve
                and len(inside) >= 4 and n_text >= 2):
            v_xs = sorted({round(s[0][0], 1) for s in ortho if abs(s[0][0]-s[1][0]) < .5})
            h_ys = sorted({round(s[0][1], 1) for s in ortho if abs(s[0][1]-s[1][1]) < .5})
            if len(v_xs) >= 3 and len(h_ys) >= 3:
                tables.append((c, v_xs, h_ys))
                continue
        figures.append(c)
    return figures, tables


def _named(tex):
    for f in sorted(NAMED, key=len, reverse=True):
        repl = ('\\' + f) if f in BUILTIN else ('\\operatorname{%s}' % f)
        tex = re.sub(r'(?<![A-Za-z\\])' + f + r'(?![A-Za-z])',
                     lambda _m, r=repl: r, tex)
    return tex


def units_to_tex(units, base_size):
    out, script, base_y = [], 0, None
    for u in units:
        if 'tex' in u:
            if script:
                out.append('}'); script = 0
            out.append(u['tex'])
            base_y = u.get('cy', base_y)
            continue
        s = u['span']
        txt = ''.join(SYM.get(c, c) for c in s['text'])
        size = s['size']
        cy = (s['bbox'][1] + s['bbox'][3]) / 2
        is_script = (size < base_size * 0.82)
        if is_script and base_y is not None:
            want = 1 if cy < base_y - 0.4 else -1
            if script != want:
                if script:
                    out.append('}')
                out.append('^{' if want == 1 else '_{')
                script = want
            out.append(txt)
        else:
            if script:
                out.append('}'); script = 0
            out.append(txt)
            if txt.strip():
                base_y = cy
    if script:
        out.append('}')
    tex = ''.join(out)
    # схлопываем пробелы внутри индексов: _{HP R} -> _{HPR}
    tex = re.sub(r'([\^_])\{([^{}]*)\}',
                 lambda m: m.group(1) + '{' + re.sub(r'\s+', '', m.group(2)) + '}', tex)
    tex = re.sub(r'\s+', ' ', tex).strip()
    # \sqrt требует радиканд в скобках: \sqrt AH... -> \sqrt{AH \cdot HA_1}
    tex = re.sub(r'\\sqrt\s*(?!\{)([A-Za-z0-9]+(?:\s*\\cdot\s*[A-Za-z0-9_{}\^]+)*)',
                 lambda m: r'\sqrt{' + m.group(1).strip() + '}', tex)
    return _named(tex)


def render_region(spans, bars, base_size):
    # узкие черты первыми (вложенные дроби), тесная вертикальная привязка
    bars = sorted([b for b in bars if (b[1] - b[0]) > 3], key=lambda b: (b[1] - b[0]))
    units = [{'span': s, 'x0': s['bbox'][0], 'x1': s['bbox'][2],
              'cy': (s['bbox'][1] + s['bbox'][3]) / 2,
              'h': s['bbox'][3] - s['bbox'][1]} for s in spans]
    band = base_size * 0.95
    for x0, x1, y in bars:
        num, den, rest = [], [], []
        for u in units:
            ucx = (u['x0'] + u['x1']) / 2
            gap = u['cy'] - y
            if x0 - 2 <= ucx <= x1 + 2 and abs(gap) < band + u.get('h', base_size) * 0.5:
                (num if gap < 0 else den).append(u)
            else:
                rest.append(u)
        if not num and not den:
            continue
        numT = units_to_tex(sorted(num, key=lambda u: u['x0']), base_size) or ' '
        denT = units_to_tex(sorted(den, key=lambda u: u['x0']), base_size) or ' '
        rest.append({'tex': r'\dfrac{%s}{%s}' % (numT, denT), 'x0': x0, 'x1': x1, 'cy': y})
        units = rest
    # порядок чтения: по строкам сверху вниз, внутри строки слева направо
    return rows_to_tex(units, base_size)


def rows_to_tex(units, base_size):
    if not units:
        return ''
    us = sorted(units, key=lambda u: u['cy'])
    rows, cur = [], [us[0]]
    for u in us[1:]:
        if u['cy'] - cur[-1]['cy'] > base_size * 0.8:
            rows.append(cur); cur = [u]
        else:
            cur.append(u)
    rows.append(cur)
    parts = [units_to_tex(sorted(r, key=lambda u: u['x0']), base_size) for r in rows]
    tex = ' '.join(p for p in parts if p)
    # схлопываем дублированный оператор на переносе строки (… = ) (= …) -> … = …
    tex = re.sub(r'([=<>+\-]|\\[lg]e(?:qslant)?|\\Leftrightarrow)\s+\1(?![\w])', r'\1', tex)
    return re.sub(r'\s{2,}', ' ', tex).strip()


def inline_line_tex(spans, base_size):
    out, buf = [], []

    def flush():
        if not buf:
            return
        tex = render_region(buf, [], base_size)
        if tex:
            out.append('$' + tex + '$')
        buf.clear()

    for s in spans:
        if is_math_font(s['font']):
            buf.append(s)
        elif not s['text'].strip():
            if buf:
                buf.append(s)
            else:
                out.append(s['text'])
        else:
            flush()
            t = s['text']
            if s['flags'] & 16 and t.strip():
                t = '**' + t.strip() + '** '
            out.append(t)
    flush()
    line = ''.join(out)
    line = re.sub(r'([^\s(\[{$\u00b7-])\$', r'\1 $', line)
    line = re.sub(r'\$\s+', '$', line)
    line = re.sub(r'\s+\$', '$', line)
    line = re.sub(r'\$\s*\$', ' ', line)
    line = line.replace('. . .', ' \\ldots ')
    return re.sub(r'  +', ' ', line).strip()


def build_table_md(page, rect, v_xs, h_ys, base_size):
    d = page.get_text('dict', clip=fitz.Rect(rect.x0-2, rect.y0-2, rect.x1+2, rect.y1+2))
    cells = {}
    for b in d['blocks']:
        if b['type'] != 0:
            continue
        for l in b['lines']:
            for s in l['spans']:
                cx = (s['bbox'][0] + s['bbox'][2]) / 2
                cy = (s['bbox'][1] + s['bbox'][3]) / 2
                ci = sum(1 for x in v_xs[1:-1] if cx > x)
                ri = sum(1 for y in h_ys[1:-1] if cy > y)
                cells.setdefault((ri, ci), []).append(s)
    nrows, ncols = len(h_ys) - 1, len(v_xs) - 1
    rows = []
    for r in range(nrows):
        row = []
        for c in range(ncols):
            sp = cells.get((r, c), [])
            tex = render_region(sp, [], base_size)
            row.append(('$' + tex + '$') if tex else ' ')
        rows.append('| ' + ' | '.join(row) + ' |')
    if not rows:
        return ''
    sep = '|' + '---|' * ncols
    return '\n'.join([rows[0], sep] + rows[1:])


def parse(pdf_path, outdir):
    doc = fitz.open(pdf_path)
    os.makedirs(os.path.join(outdir, 'figures'), exist_ok=True)

    sizes = [s['size'] for p in doc for b in p.get_text('dict')['blocks']
             if b['type'] == 0 for l in b['lines'] for s in l['spans']]
    base_size = statistics.mode([round(s) for s in sizes])

    stream, fig_no = [], 0
    for pno, page in enumerate(doc):
        h_bars, curves, draw_rects, img_rects = page_geometry(page)
        figures, tables = detect_graphics(page, h_bars, curves, draw_rects, img_rects)
        fig_zones = list(figures) + [t[0] for t in tables]

        for f in figures:
            grown = fitz.Rect(f)
            for b in page.get_text('dict')['blocks']:
                if b['type'] != 0:
                    continue
                for l in b['lines']:
                    for s in l['spans']:
                        sb = fitz.Rect(s['bbox'])
                        if fitz.Rect(f.x0-10, f.y0-10, f.x1+10, f.y1+10).intersects(sb) and sb.width < 60:
                            grown |= sb
            stream.append((pno, (f.y0 + f.y1) / 2, 'figure', grown))
        for t in tables:
            stream.append((pno, (t[0].y0 + t[0].y1) / 2, 'table', t))

        lines = []
        for b in page.get_text('dict')['blocks']:
            if b['type'] != 0:
                continue
            for l in b['lines']:
                lb = fitz.Rect(l['bbox'])
                if lb.width < 250 and any(z.intersects(lb) and (z & lb).get_area() > lb.get_area() * .5
                                          for z in fig_zones):
                    continue
                spans = l['spans']
                if not spans:
                    continue
                centered = (lb.x0 - page.rect.x0 > 110) and (page.rect.x1 - lb.x1 > 110)
                only_math = all(is_math_font(s['font']) or not s['text'].strip() for s in spans)
                lines.append({'y': lb.y0, 'x0': lb.x0, 'x1': lb.x1, 'y0': lb.y0, 'y1': lb.y1,
                              'spans': spans, 'math': only_math, 'centered': centered,
                              'bold': bool(spans[0]['flags'] & 16),
                              'italic': bool(spans[0]['flags'] & 2) and not is_math_font(spans[0]['font'])})
        lines.sort(key=lambda L: (round(L['y']), L['x0']))

        i = 0
        while i < len(lines):
            L = lines[i]
            # прогон подряд идущих чисто-математических строк = выключной блок
            if L['math']:
                block = [L]
                j = i + 1
                while j < len(lines) and lines[j]['math'] and \
                        lines[j]['y0'] - max(b['y1'] for b in block) < base_size * 1.5:
                    block.append(lines[j]); j += 1
                x0 = min(b['x0'] for b in block); x1 = max(b['x1'] for b in block)
                y0 = min(b['y0'] for b in block); y1 = max(b['y1'] for b in block)
                spans = [s for b in block for s in b['spans']]
                bars = [hb for hb in h_bars if y0-2 <= hb[2] <= y1+2 and hb[0] >= x0-6 and hb[1] <= x1+6]
                is_display = len(block) >= 2 or any(b['centered'] for b in block) or bool(bars)
                tex = render_region(spans, bars, base_size)
                stripped = re.sub(r'[\s{}^_]', '', tex)
                # мусорные метки чертежа: только заглавные буквы, нет операторов/дробей/цифр
                is_labels = (not bars and 'dfrac' not in tex
                             and re.fullmatch(r'[A-Za-z]{1,4}', stripped or 'x')
                             and not re.search(r'\\(?!,)', tex))
                if is_labels:
                    i = j; continue
                if is_display:
                    stream.append((pno, y0, 'display', tex))
                else:
                    stream.append((pno, y0, 'line', L))
                i = j
            else:
                stream.append((pno, L['y'], 'line', L))
                i += 1

    stream.sort(key=lambda t: (t[0], t[1]))

    tasks, cur, section = [], None, None

    def new_section(kind, title):
        nonlocal section
        section = {'kind': kind, 'title': title, 'parts': []}
        cur['sections'].append(section)

    for pno, y, kind, payload in stream:
        if kind == 'display':
            if cur is None:
                continue
            if section is None:
                new_section('statement', '\u0423\u0441\u043b\u043e\u0432\u0438\u0435')
            section['parts'].append(('display', payload, None))
        elif kind == 'line':
            text = inline_line_tex(payload['spans'], base_size)
            if not text:
                continue
            plain = re.sub(r'\*\*', '', text)
            m = TASK_RE.match(plain)
            if m and payload['bold']:
                cur = {'number': int(m.group(1)), 'author': None, 'sections': []}
                tasks.append(cur)
                new_section('statement', '\u0423\u0441\u043b\u043e\u0432\u0438\u0435')
                rest = plain[m.end():].strip()
                if rest:
                    section['parts'].append(('text', rest, None))
                continue
            if cur is None:
                continue
            am = AUTHOR_RE.match(plain.strip())
            if am and payload['italic']:
                cur['author'] = am.group(1)
                continue
            sm = SEC_RE.match(plain)
            if sm and payload['italic']:
                title = sm.group(1)
                body = plain[sm.end():].strip()
                base = title.split()[0]
                k2 = ('answer' if base == '\u041e\u0442\u0432\u0435\u0442' else
                      'solution' if base in ('\u0420\u0435\u0448\u0435\u043d\u0438\u0435', '\u0421\u043f\u043e\u0441\u043e\u0431') else
                      'comment' if base == '\u041a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0439' else 'sub')
                if k2 == 'sub' and section:
                    section['parts'].append(('text', '**%s.** %s' % (title, body), None))
                    continue
                new_section(k2, title)
                if body:
                    section['parts'].append(('text', body, None))
                continue
            if section is None:
                new_section('statement', '\u0423\u0441\u043b\u043e\u0432\u0438\u0435')
            section['parts'].append(('text', text, None))
        elif kind in ('figure', 'table') and cur is not None:
            if section is None:
                new_section('statement', '\u0423\u0441\u043b\u043e\u0432\u0438\u0435')
            if kind == 'figure':
                fig_no += 1
                fid = 't%d_f%d' % (cur['number'], fig_no)
                rect = fitz.Rect(payload.x0-4, payload.y0-4, payload.x1+4, payload.y1+4) & doc[pno].rect
                doc[pno].get_pixmap(clip=rect, dpi=DPI).save(os.path.join(outdir, 'figures', fid + '.png'))
                section['parts'].append(('figure', fid, {'page': pno + 1, 'bbox': [round(v, 1) for v in payload]}))
            else:
                rect, vxs, hys = payload
                md = build_table_md(doc[pno], rect, vxs, hys, base_size)
                section['parts'].append(('text', '\n' + md + '\n', None))

    def assemble(parts):
        md, figs = [], []
        for typ, a, b in parts:
            if typ == 'figure':
                md.append('\n{{FIG:%s}}\n' % a)
                figs.append({'id': a, **b, 'file': 'figures/%s.png' % a})
            elif typ == 'display':
                md.append('\n$$\n%s\n$$\n' % a)
            else:
                md.append(a)
        text = ' '.join(md)
        # выкидываем выключные блоки из одних заглавных меток чертежа
        text = re.sub(r'\n?\$\$\n[A-Za-z](?:\s*[A-Za-z]){0,3}\n\$\$\n?', '\n', text)
        text = re.sub(r'(\w)-\s+([\u0430-\u044f\u0451])', r'\1\2', text)   # переносы слов
        # пробел между инлайн-формулой и кириллицей с обеих сторон
        text = re.sub(r'\$([\u0410-\u044f\u0451])', r'$ \1', text)
        text = re.sub(r'([\u0410-\u044f\u0451])\$(?!\$)', r'\1 $', text)
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r' ?\n ?', '\n', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip(), figs

    result = {'source': os.path.basename(pdf_path), 'base_size': base_size, 'tasks': []}
    for t in tasks:
        obj = {'number': t['number'], 'author': t['author'], 'statement_md': '',
               'answer_md': None, 'solutions': [], 'comments': [], 'figures': [],
               'meta': {}, 'verified': False}
        for sec in t['sections']:
            body, figs = assemble(sec['parts'])
            obj['figures'] += figs
            if sec['kind'] == 'statement':
                obj['statement_md'] = (obj['statement_md'] + '\n' + body).strip()
            elif sec['kind'] == 'answer':
                obj['answer_md'] = body
            elif sec['kind'] == 'solution':
                obj['solutions'].append({'title': sec['title'], 'body_md': body})
            else:
                obj['comments'].append({'title': sec['title'], 'body_md': body})
        result['tasks'].append(obj)

    with open(os.path.join(outdir, 'result.json'), 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


if __name__ == '__main__':
    import sys
    r = parse(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 'out')
    print('tasks:', len(r['tasks']), '| figures:', sum(len(t['figures']) for t in r['tasks']))
