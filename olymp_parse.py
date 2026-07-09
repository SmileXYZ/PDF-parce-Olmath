#!/usr/bin/env python3
# olymp_parse.py - deterministic olympiad-PDF parser. No neural nets. PyMuPDF.
# Core engine + pluggable format profiles + auto-detection.
#   parse(pdf_path, outdir) -> writes outdir/result.json (+ figures), returns dict.
import os, re, json, statistics, unicodedata
import fitz

DPI = 200

# ---- shared symbol table (Unicode glyph -> TeX), used after NFKC normalize ----
SYM = {
    '\u00b7': r'\cdot ', '\u00d7': r'\times ', '\u2212': '-', '\u2013': '-',
    '\u2a7e': r'\geqslant ', '\u2a7d': r'\leqslant ', '\u2265': r'\ge ', '\u2264': r'\le ',
    '\u2220': r'\angle ', '\u25e6': r'\circ ', '\u2208': r'\in ', '\u2260': r'\ne ',
    '\u21d4': r'\Leftrightarrow ', '\u21d2': r'\Rightarrow ', '\u2192': r'\to ',
    '\u221a': r'\sqrt', '\u221e': r'\infty ', '\u25b3': r'\triangle ', '\u00b1': r'\pm ',
    '\u03c0': r'\pi ', '\u03c6': r'\varphi ', '\u03c8': r'\psi ', '\u03b1': r'\alpha ',
    '\u03b2': r'\beta ', '\u03b3': r'\gamma ', '\u03b4': r'\delta ', '\u03c9': r'\omega ',
    '\u03bb': r'\lambda ', '\u03bc': r'\mu ', '\u03c1': r'\rho ', '\u03c3': r'\sigma ',
    '\u03c4': r'\tau ', '\u03b8': r'\theta ', '\u03be': r'\xi ', '\u0394': r'\Delta ',
    '\u2211': r'\sum ', '\u220f': r'\prod ', '\u222b': r'\int ', '\u2202': r'\partial ',
    '\u2032': "'", '\u2033': "''", '\u2026': r'\ldots ', '\u25a1': '', '\u223c': r'\sim ',
    '\u2248': r'\approx ', '\u2261': r'\equiv ', '\u222a': r'\cup ', '\u2229': r'\cap ',
    '\u2282': r'\subset ', '\u2286': r'\subseteq ', '\u2200': r'\forall ', '\u2203': r'\exists ',
    '\u00ac': r'\neg ', '\u2227': r'\wedge ', '\u2228': r'\vee ', '\u22a5': r'\perp ',
    '\u2225': r'\parallel ', '\u2205': r'\varnothing ', '\u2213': r'\mp ', '\u2207': r'\nabla ',
    '\u2260': r'\ne ', '\u2013': '-', '\u2014': '\u2014',
    '{': r'\{', '}': r'\}',
}
NAMED = {'sin', 'cos', 'tg', 'ctg', 'cot', 'arctg', 'arcctg', 'arcsin', 'arccos',
         'tan', 'log', 'ln', 'lg', 'min', 'max', 'gcd', 'lcm', 'lim', 'exp', 'deg'}
BUILTIN = {'sin', 'cos', 'tan', 'cot', 'log', 'ln', 'lg', 'min', 'max', 'gcd', 'lim',
           'exp', 'deg', 'arcsin', 'arccos'}


def apply_named(tex):
    for f in sorted(NAMED, key=len, reverse=True):
        repl = ('\\' + f) if f in BUILTIN else ('\\operatorname{%s}' % f)
        tex = re.sub(r'(?<![A-Za-z\\])' + f + r'(?![A-Za-z])', lambda _m, r=repl: r, tex)
    return tex


# ===================================================================== PROFILES
class Profile:
    """Base. Subclasses tune math detection, glyph mapping and structure grammar."""
    name = "base"
    variants = False
    VARIANT_RE = None

    # --- math ---
    def is_math_font(self, font):
        return font[:2] in ('CM', 'MS', 'LA', 'RS', 'EU', 'BB')

    def is_math_span(self, s):
        return self.is_math_font(s['font'])

    GLYPH = {}                      # profile-specific glyph overrides (raw char -> TeX)

    def to_tex(self, text):         # math text -> TeX fragment (no sup/sub, that's geometry)
        text = unicodedata.normalize('NFKC', text)
        out = []
        for c in text:
            if c in self.GLYPH:
                out.append(self.GLYPH[c])
            else:
                out.append(SYM.get(c, c))
        return ''.join(out)

    def clean_text(self, text):     # regular (non-math) text cleanup
        return text.replace('\uffff', '')

    # --- structure ---
    TASK_RE = re.compile(r'^\u0417\u0430\u0434\u0430\u0447\u0430\s+(\d+)\s*\.')
    task_needs_bold = True
    SECTION_RE = re.compile(
        r'^(\u041e\u0442\u0432\u0435\u0442|\u0420\u0435\u0448\u0435\u043d\u0438\u0435(?:\s+\d+)?|'
        r'\u0421\u043f\u043e\u0441\u043e\u0431(?:\s+\d+)?|\u041a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0439(?:\s+\d+)?|'
        r'\u041e\u0446\u0435\u043d\u043a\u0430|\u041f\u0440\u0438\u043c\u0435\u0440|'
        r'\u0417\u0430\u043c\u0435\u0447\u0430\u043d\u0438\u0435[^.:]*|'
        r'\u0414\u043e\u043a\u0430\u0437\u0430\u0442\u0435\u043b\u044c\u0441\u0442\u0432\u043e(?:\s+\d+)?|'
        r'\u041a\u0440\u0438\u0442\u0435\u0440\u0438\u0438[^.:]*)\s*[.:]')
    AUTHOR_RE = re.compile(r'^\(([\u0410-\u042f\u0401][^)]{2,60})\)$')

    def classify_section(self, base):
        A = '\u041e\u0442\u0432\u0435\u0442'                      # Ответ
        R = ('\u0420\u0435\u0448\u0435\u043d\u0438\u0435', '\u0421\u043f\u043e\u0441\u043e\u0431',
             '\u0414\u043e\u043a\u0430\u0437\u0430\u0442\u0435\u043b\u044c\u0441\u0442\u0432\u043e')  # Решение/Способ/Доказательство
        K = '\u041a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0439'   # Комментарий
        KR = '\u041a\u0440\u0438\u0442\u0435\u0440\u0438\u0438'                    # Критерии
        if base == A: return 'answer'
        if base in R: return 'solution'
        if base in (K, KR): return 'comment'
        return 'sub'

    def is_chrome(self, text, pno, page):   # repeated header/footer lines to drop
        return False


class LatexNative(Profile):     # pdfTeX ММО (9-sol) — эталон
    name = "latex-native"
    GLYPH = {'\x12': r'\left(', '\x13': r'\right)', '\x0e': r'\left(', '\x0f': r'\right)',
             '\x10': r'\left[', '\x11': r'\right]'}


class LatexQuartz(Profile):     # macOS Quartz LaTeX (Ломоносов testPDF) + варианты
    name = "latex-quartz"
    variants = True
    VARIANT_RE = re.compile(r'^\u0412-(\d+)\b')          # В-1, В-2, ...
    TASK_RE = re.compile(r'^\u0417\u0430\u0434\u0430\u0447\u0430\s+(\d+)\s*$')  # «Задача N» без точки
    # Quartz переназначил ToUnicode у CMEX больших скобок -> плоские скобки (безопасно для баланса)
    GLYPH = {'\u2713': '(', '\u25c6': ')', '\u21e2': ')', '\u21e4': '[', '\u21e5': ']',
             '\uffff': '', '\\': r'\angle '}

    _HEADER = ('\u041c\u043e\u0441\u043a\u043e\u0432\u0441\u043a\u0438\u0439 \u0433\u043e\u0441',   # Московский гос
               '\u041e\u043b\u0438\u043c\u043f\u0438\u0430\u0434\u0430 \u0448\u043a\u043e\u043b',   # Олимпиада школ
               '\u0417\u0430\u043a\u043b\u044e\u0447\u0438\u0442\u0435\u043b\u044c\u043d\u044b\u0439 \u044d\u0442\u0430\u043f')  # Заключительный этап

    def is_chrome(self, text, pno, page):
        t = text.strip().replace('\uffff', '')
        if any(t.startswith(h) for h in self._HEADER):
            return True
        # финальная таблица баллов
        if t.startswith('\u0411\u0430\u043b\u043b\u044b \u0437\u0430') or '\u041d\u043e\u043c\u0435\u0440 \u0437\u0430\u0434\u0430\u043d\u0438\u044f' in t:
            return True
        return False


class WordOMML(Profile):        # Word/OMML (resh_mat) — CambriaMath + Unicode math-alnum
    name = "word-omml"
    task_needs_bold = True
    TASK_RE = re.compile(r'^(\d+)\.(\d+)\s*\.')          # 11.1.

    def is_math_font(self, font):
        return font == 'CambriaMath'

    def is_math_span(self, s):
        if s['font'] == 'CambriaMath':
            return True
        # одиночные math-alphanumeric символы, если вдруг в другом шрифте
        return any(0x1D400 <= ord(c) <= 0x1D7FF for c in s['text'])

    # NFKC в to_tex снимает 𝑎->a; символы через SYM
    def clean_text(self, text):
        return unicodedata.normalize('NFKC', text).replace('\uffff', '')


def detect(doc):
    prod = (doc.metadata.get('producer') or '').lower()
    fonts = set()
    cmex_codes = set()
    has_variant = False
    vr = re.compile(r'^\u0412-\d')
    for pi, p in enumerate(doc):
        for b in p.get_text('dict')['blocks']:
            if b['type'] != 0:
                continue
            for l in b['lines']:
                txt = ''.join(s['text'] for s in l['spans']).strip()
                if vr.match(txt):
                    has_variant = True
                for s in l['spans']:
                    fonts.add(s['font'])
                    if s['font'] == 'CMEX10':
                        for ch in s['text']:
                            cmex_codes.add(ord(ch))
        if pi > 3 and (fonts or has_variant):
            pass
    if any('cambriamath' in f.lower() for f in fonts):
        return WordOMML()
    latex = any(f[:2] in ('CM', 'MS') for f in fonts)
    if latex:
        quartz = 'quartz' in prod or bool(cmex_codes & {0x2713, 0x25c6, 0x21e2})
        if quartz or has_variant:
            return LatexQuartz()
        return LatexNative()
    # незнакомый формат — пробуем как Word (NFKC + generic), иначе latex
    return WordOMML() if any('cambria' in f.lower() or 'times' in f.lower() for f in fonts) else LatexNative()


# ====================================================================== GEOMETRY
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
                if rr.width > 2 and rr.height < 1.4:          # тонкий прямоуг. = дробная черта (Word)
                    h_bars.append((rr.x0, rr.x1, (rr.y0 + rr.y1) / 2))
                else:
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


def detect_graphics(page, curves, draw_rects, img_rects):
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
        if c.get_area() < 1800 or c.width < 28 or c.height < 16:
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
            continue                                     # только горизонтальные = дробные черты
        if (inside and len(ortho) == len(inside) and not has_img and not has_curve
                and len(inside) >= 4 and n_text >= 2):
            v_xs = sorted({round(s[0][0], 1) for s in ortho if abs(s[0][0]-s[1][0]) < .5})
            h_ys = sorted({round(s[0][1], 1) for s in ortho if abs(s[0][1]-s[1][1]) < .5})
            if len(v_xs) >= 3 and len(h_ys) >= 3:
                tables.append((c, v_xs, h_ys))
                continue
        figures.append(c)
    return figures, tables


# ======================================================================= MATH 2D
def units_to_tex(units, base_size, prof):
    out, script, base_y = [], 0, None
    for u in units:
        if 'tex' in u:
            if script:
                out.append('}'); script = 0
            out.append(u['tex']); base_y = u.get('cy', base_y)
            continue
        s = u['span']
        txt = prof.to_tex(s['text'])
        size = s['size']
        cy = (s['bbox'][1] + s['bbox'][3]) / 2
        if size < base_size * 0.82 and base_y is not None:
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
    tex = re.sub(r'([\^_])\{([^{}]*)\}', lambda m: m.group(1) + '{' + re.sub(r'\s+', '', m.group(2)) + '}', tex)
    tex = re.sub(r'\s+', ' ', tex).strip()
    tex = re.sub(r'\\sqrt\s*(?!\{)([A-Za-z0-9]+(?:\s*\\cdot\s*[A-Za-z0-9_{}\^]+)*)',
                 lambda m: r'\sqrt{' + m.group(1).strip() + '}', tex)
    tex = re.sub(r'\\sqrt\s*\^', r'\\sqrt{}^', tex)                       # sqrt перед степенью
    tex = re.sub(r'\\sqrt(?![\s{A-Za-z0-9\\])', r'\\sqrt{}', tex)         # sqrt без радиканда
    tex = re.sub(r'_\{([^{}]*)\}(\^\{[^{}]*\})_\{([^{}]*)\}', r'_{\1\3}\2', tex)  # двойной _
    tex = re.sub(r'\^\{([^{}]*)\}(_\{[^{}]*\})\^\{([^{}]*)\}', r'^{\1\3}\2', tex)  # двойной ^
    tex = re.sub(r'\^\{([^{}]*)\}\s*\^\{([^{}]*)\}', r'^{\1\2}', tex)  # соседние ^
    tex = re.sub(r'_\{([^{}]*)\}\s*_\{([^{}]*)\}', r'_{\1\2}', tex)      # соседние _
    tex = apply_named(tex)
    tex = re.sub(r'[\u0410-\u044f\u0401\u0451][\u0410-\u044f\u0401\u0451 ]*',
                 lambda m: r'\text{' + m.group(0).rstrip() + '}', tex)   # кириллица -> \text{}
    return tex


def rows_to_tex(units, base_size, prof):
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
    parts = [units_to_tex(sorted(r, key=lambda u: u['x0']), base_size, prof) for r in rows]
    tex = ' '.join(p for p in parts if p)
    tex = re.sub(r'([=<>+\-]|\\[lg]e(?:qslant)?|\\Leftrightarrow)\s+\1(?![\w])', r'\1', tex)
    return re.sub(r'\s{2,}', ' ', tex).strip()


def render_region(spans, bars, base_size, prof):
    bars = sorted([b for b in bars if (b[1] - b[0]) > 3], key=lambda b: (b[1] - b[0]))
    units = [{'span': s, 'x0': s['bbox'][0], 'x1': s['bbox'][2],
              'cy': (s['bbox'][1] + s['bbox'][3]) / 2, 'h': s['bbox'][3] - s['bbox'][1]} for s in spans]
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
        if not num or not den:
            units = rest + num + den            # спурьёзная черта -> спаны обратно на строку
            continue
        numT = units_to_tex(sorted(num, key=lambda u: u['x0']), base_size, prof)
        denT = units_to_tex(sorted(den, key=lambda u: u['x0']), base_size, prof)
        if not numT.strip() or not denT.strip():
            units = rest + num + den
            continue
        rest.append({'tex': r'\dfrac{%s}{%s}' % (numT, denT), 'x0': x0, 'x1': x1, 'cy': y})
        units = rest
    return rows_to_tex(units, base_size, prof)


def inline_line_tex(spans, base_size, prof, bars=()):
    out, buf = [], []

    def flush():
        if not buf:
            return
        tex = render_region(list(buf), list(bars), base_size, prof)
        if tex:
            out.append('$' + tex + '$')
        buf.clear()

    for s in spans:
        if prof.is_math_span(s):
            buf.append(s)
        elif not s['text'].strip():
            if buf:
                buf.append(s)
            else:
                out.append(s['text'])
        else:
            flush()
            t = prof.clean_text(s['text'])
            if s['flags'] & 16 and t.strip():
                t = '**' + t.strip() + '** '
            out.append(t)
    flush()
    line = ''.join(out)
    line = re.sub(r'([^\s(\[{$\u00b7-])\$', r'\1 $', line)
    line = re.sub(r'\$\s+', '$', line)
    line = re.sub(r'\s+\$', '$', line)
    line = re.sub(r'\$\s*\$', ' ', line)
    return re.sub(r'  +', ' ', line).strip()


def build_table_md(page, rect, v_xs, h_ys, base_size, prof):
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
            tex = render_region(cells.get((r, c), []), [], base_size, prof)
            row.append(('$' + tex + '$') if tex else ' ')
        rows.append('| ' + ' | '.join(row) + ' |')
    if not rows:
        return ''
    return '\n'.join([rows[0], '|' + '---|' * ncols] + rows[1:])


# ========================================================================= PARSE
def parse(pdf_path, outdir):
    doc = fitz.open(pdf_path)
    prof = detect(doc)
    os.makedirs(os.path.join(outdir, 'figures'), exist_ok=True)

    sizes = [s['size'] for p in doc for b in p.get_text('dict')['blocks']
             if b['type'] == 0 for l in b['lines'] for s in l['spans']
             if not prof.is_math_span(s) and s['text'].strip()]
    base_size = statistics.mode([round(s) for s in sizes]) if sizes else 10

    stream, fig_no = [], 0
    for pno, page in enumerate(doc):
        h_bars, curves, draw_rects, img_rects = page_geometry(page)
        figures, tables = detect_graphics(page, curves, draw_rects, img_rects)
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
                raw = ''.join(s['text'] for s in spans).strip()
                if prof.is_chrome(raw, pno, page):
                    continue
                only_math = all(prof.is_math_span(s) or not s['text'].strip() for s in spans)
                lines.append({'y': lb.y0, 'x0': lb.x0, 'x1': lb.x1, 'y0': lb.y0, 'y1': lb.y1,
                              'spans': spans, 'math': only_math,
                              'centered': (lb.x0 - page.rect.x0 > 110) and (page.rect.x1 - lb.x1 > 110),
                              'bold': bool(spans[0]['flags'] & 16),
                              'italic': bool(spans[0]['flags'] & 2) and not prof.is_math_span(spans[0])})
        lines.sort(key=lambda L: (round(L['y']), L['x0']))

        i = 0
        while i < len(lines):
            L = lines[i]
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
                tex = render_region(spans, bars, base_size, prof)
                stripped = re.sub(r'[\s{}^_]', '', tex)
                if not bars and 'dfrac' not in tex and re.fullmatch(r'[A-Za-z]{1,4}', stripped or 'x') \
                        and not re.search(r'\\(?!,)', tex):
                    i = j; continue
                stream.append((pno, y0, 'display' if is_display else 'line', tex if is_display else L))
                i = j
            else:
                stream.append((pno, L['y'], 'line', L))
                i += 1

    stream.sort(key=lambda t: (t[0], t[1]))

    # ---- segmentation
    tasks, cur, variant, section = [], None, None, None

    def new_section(kind, title, into):
        nonlocal section
        section = {'kind': kind, 'title': title, 'parts': []}
        into['sections'].append(section)

    def target():                       # куда писать секции: в вариант или в задачу
        return variant if variant is not None else cur

    for pno, y, kind, payload in stream:
        if kind == 'display':
            if cur is None:
                continue
            if section is None:
                new_section('statement', '\u0423\u0441\u043b\u043e\u0432\u0438\u0435', target())
            section['parts'].append(('display', payload, None))
            continue
        if kind in ('figure', 'table'):
            if cur is None:
                continue
            if section is None:
                new_section('statement', '\u0423\u0441\u043b\u043e\u0432\u0438\u0435', target())
            if kind == 'figure':
                fig_no += 1
                fid = 't%d_f%d' % (cur['number'], fig_no)
                rect = fitz.Rect(payload.x0-4, payload.y0-4, payload.x1+4, payload.y1+4) & doc[pno].rect
                doc[pno].get_pixmap(clip=rect, dpi=DPI).save(os.path.join(outdir, 'figures', fid + '.png'))
                section['parts'].append(('figure', fid, {'page': pno + 1, 'bbox': [round(v, 1) for v in payload]}))
            else:
                rect, vxs, hys = payload
                section['parts'].append(('text', '\n' + build_table_md(doc[pno], rect, vxs, hys, base_size, prof) + '\n', None))
            continue

        # kind == 'line'
        text = inline_line_tex(payload['spans'], base_size, prof, bars=[])
        if not text:
            continue
        plain = re.sub(r'\*\*', '', text)

        m = prof.TASK_RE.match(plain)
        if m and (not prof.task_needs_bold or payload['bold']):
            num = int(m.group(len(m.groups())))         # last group = task number
            if prof.name == 'word-omml':
                num = int(m.group(1)) * 100 + int(m.group(2))  # 11.1 -> 1101 (уникальный номер)
            cur = {'number': num, 'author': None, 'sections': [], 'variants': []}
            tasks.append(cur)
            variant = None
            new_section('statement', '\u0423\u0441\u043b\u043e\u0432\u0438\u0435', cur)
            rest = plain[m.end():].strip()
            if rest:
                section['parts'].append(('text', rest, None))
            continue
        if cur is None:
            continue

        if prof.variants and prof.VARIANT_RE:
            vm = prof.VARIANT_RE.match(plain)
            if vm and payload['bold']:
                variant = {'label': '\u0412-' + vm.group(1), 'sections': []}
                cur['variants'].append(variant)
                new_section('statement', '\u0423\u0441\u043b\u043e\u0432\u0438\u0435', variant)
                rest = plain[vm.end():].strip()
                if rest:
                    section['parts'].append(('text', rest, None))
                continue

        am = prof.AUTHOR_RE.match(plain.strip())
        if am and payload['italic']:
            cur['author'] = am.group(1)
            continue

        sm = prof.SECTION_RE.match(plain)
        if sm and payload['bold']:
            title = sm.group(1)
            body = plain[sm.end():].strip()
            base = title.split()[0]
            k2 = prof.classify_section(base)
            # решение/ответ варианта переключают контекст обратно на задачу-владельца варианта
            if k2 in ('solution', 'comment') and prof.variants:
                variant = None
            if k2 == 'sub' and section:
                section['parts'].append(('text', '**%s.** %s' % (title, body), None))
                continue
            new_section(k2, title, target())
            if body:
                section['parts'].append(('text', body, None))
            continue

        if section is None:
            new_section('statement', '\u0423\u0441\u043b\u043e\u0432\u0438\u0435', target())
        section['parts'].append(('text', text, None))

    # ---- assemble
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
        text = re.sub(r'\n?\$\$\n[A-Za-z](?:\s*[A-Za-z]){0,3}\n\$\$\n?', '\n', text)
        text = re.sub(r'(\w)-\s+([\u0430-\u044f\u0451])', r'\1\2', text)
        text = re.sub(r'\$([\u0410-\u044f\u0451])', r'$ \1', text)
        text = re.sub(r'([\u0410-\u044f\u0451])\$(?!\$)', r'\1 $', text)
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r' ?\n ?', '\n', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip(), figs

    def sections_to_obj(sections, target_obj):
        for sec in sections:
            body, figs = assemble(sec['parts'])
            target_obj.setdefault('figures', [])
            target_obj['figures'] += figs
            if sec['kind'] == 'statement':
                target_obj['statement_md'] = (target_obj.get('statement_md', '') + '\n' + body).strip()
            elif sec['kind'] == 'answer':
                target_obj['answer_md'] = body
            elif sec['kind'] == 'solution':
                target_obj.setdefault('solutions', []).append({'title': sec['title'], 'body_md': body})
            else:
                target_obj.setdefault('comments', []).append({'title': sec['title'], 'body_md': body})

    result = {'source': os.path.basename(pdf_path), 'profile': prof.name, 'base_size': base_size, 'tasks': []}
    for t in tasks:
        obj = {'number': t['number'], 'author': t['author'], 'statement_md': '',
               'answer_md': None, 'solutions': [], 'comments': [], 'figures': [],
               'meta': {}, 'verified': False}
        sections_to_obj(t['sections'], obj)
        if t['variants']:
            obj['variants'] = []
            for v in t['variants']:
                vobj = {'label': v['label'], 'statement_md': '', 'answer_md': None, 'figures': []}
                sections_to_obj(v['sections'], vobj)
                obj['figures'] += vobj.pop('figures', [])
                obj['variants'].append(vobj)
        result['tasks'].append(obj)

    with open(os.path.join(outdir, 'result.json'), 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


if __name__ == '__main__':
    import sys
    r = parse(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 'out')
    nv = sum(len(t.get('variants', [])) for t in r['tasks'])
    print('profile:', r['profile'], '| tasks:', len(r['tasks']), '| variants:', nv,
          '| figures:', sum(len(t['figures']) for t in r['tasks']))
