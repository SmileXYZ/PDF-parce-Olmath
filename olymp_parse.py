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
        r'\u041a\u0440\u0438\u0442\u0435\u0440\u0438\u0438[^\n]{0,90}?)\s*[.:)]')
    AUTHOR_RE = re.compile(r'^\(([\u0410-\u042f\u0401][^)]{2,60})\)$')

    math_as_image = False          # формулы кропаются картинками (MathType)
    merge_repeated = False         # повторный 'Задача N' = решение к той же задаче
    reject_mathy_figures = False   # не считать фигурой кластер, полный math-текста

    def classify_section(self, base):
        A = '\u041e\u0442\u0432\u0435\u0442'                      # Ответ
        R = ('\u0420\u0435\u0448\u0435\u043d\u0438\u0435', '\u0421\u043f\u043e\u0441\u043e\u0431',
             '\u0414\u043e\u043a\u0430\u0437\u0430\u0442\u0435\u043b\u044c\u0441\u0442\u0432\u043e')  # Решение/Способ/Доказательство
        K = '\u041a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0439'   # Комментарий
        KR = '\u041a\u0440\u0438\u0442\u0435\u0440\u0438\u0438'                    # Критерии
        if base == A: return 'answer'
        if base in R: return 'solution'
        if base == KR: return 'rubric'
        if base == K: return 'comment'
        return 'sub'

    def is_chrome(self, text, pno, page, lb=None):   # хедеры/футеры/номера страниц
        t = text.strip()
        if lb is not None and re.fullmatch(r'\d{1,3}', t) and \
                (lb.y1 < 70 or lb.y0 > page.rect.height - 60):
            return True
        return False

    # хук: профиль может перехватить элемент потока целиком (line/table)
    def handle_stream(self, ctx, kind, payload, plain=None):
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

    def is_chrome(self, text, pno, page, lb=None):
        if Profile.is_chrome(self, text, pno, page, lb):
            return True
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



class Kenguru(Profile):
    """«Кенгуру»: секции по классам, 'Задача N. Правильный ответ: X', решение телом."""
    name = "kenguru"
    GRADE_RE = re.compile(r'\u0420\u0435\u0448\u0435\u043d\u0438\u044f \u0437\u0430\u0434\u0430\u0447.*?\u00ab\u041a\u0435\u043d\u0433\u0443\u0440\u0443[^\u00bb]*\u00bb\.?\s*(\S[^\n]*?)\s*\u043a\u043b\u0430\u0441\u0441')
    POINTS_RE = re.compile(r'^\u0417\u0430\u0434\u0430\u0447\u0438 \u043d\u0430 (\d+)\s*\u0411\u0410\u041b\u041b')
    TASKANS_RE = re.compile(r'^\u0417\u0430\u0434\u0430\u0447\u0430\s+(\d+)\.\s*\u041f\u0440\u0430\u0432\u0438\u043b\u044c\u043d\u044b\u0439 \u043e\u0442\u0432\u0435\u0442:\s*(\S+)')

    def is_math_font(self, font):
        return font == 'CambriaMath'

    def handle_stream(self, ctx, kind, payload, plain=None):
        if kind != 'line':
            return False
        g = self.GRADE_RE.search(plain)
        if g:
            ctx.scope = ctx.scope + 1 if hasattr(ctx, 'scope') else 1
            ctx.grade = g.group(1).strip()
            return True
        p = self.POINTS_RE.match(plain)
        if p:
            ctx.points = int(p.group(1))
            return True
        m = self.TASKANS_RE.match(plain)
        if m and payload['bold']:
            n = int(m.group(1))
            scope = getattr(ctx, 'scope', 1)
            meta = {'grade': getattr(ctx, 'grade', None),
                    'points': getattr(ctx, 'points', None), 'orig_number': n}
            ctx.start_task(scope * 100 + n, meta=meta)
            ctx.cur['answer_md'] = m.group(2).strip().rstrip('.')
            ctx.new_section('solution', '\u0420\u0435\u0448\u0435\u043d\u0438\u0435')
            return True
        return False


class WordVG(WordOMML):
    """«Покори Воробьёвы горы»: критерии-таблицы -> варианты В-N -> ответы 'N-M.'."""
    name = "word-vg"
    task_needs_bold = False
    RUB_RE = re.compile(r'\u0417\u0430\u0434\u0430\u0447\u0430[^0-9]{0,8}(\d+)')
    VAR_RE = re.compile(r'^\u0412\u0430\u0440\u0438\u0430\u043d\u0442\s+\u0412-(\d+)')
    NUM_RE = re.compile(r'^(\d+)\.\s+(?=\S)')
    ANS_RE = re.compile(r'^(\d+)-(\d+)\.\s*')
    ANSSTART_RE = re.compile(r'^\u041e\u0442\u0432\u0435\u0442\u044b \u0438 \u0440\u0435\u0448\u0435\u043d\u0438\u044f')
    TASK_RE = re.compile(r'$^')                       # generic-обработку задач глушим

    def is_chrome(self, text, pno, page, lb=None):
        if Profile.is_chrome(self, text, pno, page, lb):
            return True
        t = text.strip()
        if t.startswith('\u041e\u043b\u0438\u043c\u043f\u0438\u0430\u0434\u0430 \u0448\u043a\u043e\u043b'):
            return True
        if t.startswith('\u041c\u0430\u0442\u0435\u043c\u0430\u0442\u0438\u043a\u0430.'):
            return True
        if t.startswith('\u0410\u043f\u0440\u0435\u043b\u044c ') or re.fullmatch(r'_{5,}', t):
            return True
        return False

    def handle_stream(self, ctx, kind, payload, plain=None):
        if kind == 'table':
            m = self.RUB_RE.search(payload)
            if m:
                ctx.ensure_task(int(m.group(1)))
                ctx.new_section('rubric', '\u041a\u0440\u0438\u0442\u0435\u0440\u0438\u0438')
                ctx.add_part(('text', '\n' + payload + '\n', None))
                return True
            return False
        if kind != 'line':
            return False
        v = self.VAR_RE.match(plain)
        if v:
            ctx.vg_var = '\u0412-' + v.group(1)
            ctx.vg_mode = 'statements'
            return True
        if self.ANSSTART_RE.match(plain):
            ctx.vg_mode = 'answers'
            ctx.vg_var = None
            return True
        mode = getattr(ctx, 'vg_mode', None)
        if mode == 'statements':
            m = self.NUM_RE.match(plain)
            if m:
                n = int(m.group(1))
                ctx.ensure_task(n)
                ctx.start_variant(ctx.vg_var, task_number=n)
                ctx.add_part(('text', plain[m.end():].strip(), None))
                return True
            if getattr(ctx, 'variant', None) is not None:
                ctx.add_part(('text', plain, None))
                return True
            return True                                # преамбула варианта — мимо
        if mode == 'answers':
            m = self.ANS_RE.match(plain)
            if m:
                n, k = int(m.group(1)), int(m.group(2))
                ctx.ensure_task(n)
                ctx.vg_ans = (n, k)
                rest = plain[m.end():].strip()
                am = re.match(r'\u041e\u0442\u0432\u0435\u0442[.:]\s*(.*)', rest)
                label = '\u0412-%d' % k
                v = ctx.find_variant(n, label) or ctx.start_variant(label, task_number=n)
                if am:
                    v['answer_md'] = am.group(1).strip()
                    ctx.section = None
                    if k == 1:
                        ctx.new_section('solution', '\u0420\u0435\u0448\u0435\u043d\u0438\u0435',
                                        into=ctx.cur)
                else:
                    ctx.new_section('solution', '\u0420\u0435\u0448\u0435\u043d\u0438\u0435',
                                    into=ctx.cur)
                    ctx.add_part(('text', rest, None))
                return True
            # тело решения после N-1 -> в текущую секцию задачи
            if ctx.section is not None:
                return False                            # generic допишет (в т.ч. Решение./display)
            return False
        return False


class MathTypeImage(Profile):
    """Word+MathType (Физтех/Росатом): текстовый слой формул битый ->
    формулы кропаются картинками, текст остаётся текстом."""
    name = "mathtype-image"
    math_as_image = True
    merge_repeated = True
    reject_mathy_figures = True
    task_needs_bold = True
    TASK_RE = re.compile(r'^(\d+)\.\s+(?=\S)')
    FIZTEH_RE = re.compile(r'\u0412\u0430\u0440\u0438\u0430\u043d\u0442\s+([\d-]+)')

    MATH_FONTS = ('Symbol', 'SymbolMT', 'MT-Extra', 'MT Extra', 'CambriaMath',
                  'Euclid', 'MTSY', 'MTMI')

    def is_math_font(self, font):
        return any(font.startswith(f) for f in self.MATH_FONTS)

    _LATINISH = re.compile(r"[A-Za-z0-9\s.,'()=+\-\u00b1\u00b0\u00b7/\u0394\u03b1-\u03c9]{1,24}$")
    _GLUE = re.compile(r"[0-9\s.,()=+\-\u00b1\u00b0\u00b7/{}|\[\]]{1,12}$")

    def is_math_span(self, s):
        if self.is_math_font(s['font']):
            return True
        if any(0xE000 <= ord(c) <= 0xF8FF for c in s['text']):
            return True
        t = s['text'].strip()
        if t and 'Italic' in s['font'] and self._LATINISH.match(t) \
                and re.search(r'[A-Za-z\u0394\u03b1-\u03c9]', t):
            return True
        return False

    def glue_span(self, s):
        t = s['text'].strip()
        return bool(t) and bool(self._GLUE.match(t))

    def is_chrome(self, text, pno, page, lb=None):
        if Profile.is_chrome(self, text, pno, page, lb):
            return True
        t = text.strip()
        if t.startswith('\u0420\u0435\u0448\u0435\u043d\u0438\u044f \u0438 \u043a\u0440\u0438\u0442\u0435\u0440\u0438\u0438') and pno > 0:
            return True
        return False

    def handle_stream(self, ctx, kind, payload, plain=None):
        if kind == 'line':
            m = self.FIZTEH_RE.search(plain)
            if m and payload['bold']:
                ctx.scope = getattr(ctx, 'scope', 0) + 1
                ctx.scope_meta = {'variant': m.group(1)}
                return True
        return False

def detect(doc):
    prod = (doc.metadata.get('producer') or '').lower()
    fonts = set()
    cmex_codes = set()
    pua = 0
    text0 = ''
    has_bvariant = False
    has_anskey = False
    vr = re.compile(r'^(?:\u0412\u0430\u0440\u0438\u0430\u043d\u0442\s+)?\u0412-\d')
    ak = re.compile(r'^\d+-\d+\.')
    for pi, p in enumerate(doc):
        for b in p.get_text('dict')['blocks']:
            if b['type'] != 0:
                continue
            for l in b['lines']:
                txt = ''.join(s['text'] for s in l['spans']).strip()
                if pi == 0:
                    text0 += txt + '\n'
                if vr.match(txt):
                    has_bvariant = True
                if ak.match(txt):
                    has_anskey = True
                for s in l['spans']:
                    fonts.add(s['font'])
                    if s['font'] == 'CMEX10':
                        for ch in s['text']:
                            cmex_codes.add(ord(ch))
                    for c in s['text']:
                        if 0xE000 <= ord(c) <= 0xF8FF:
                            pua += 1
    if '\u041a\u0435\u043d\u0433\u0443\u0440\u0443' in text0:
        return Kenguru()
    latex = any(f[:2] in ('CM', 'MS') for f in fonts if f not in ('MT Extra',)) and \
            any(f.startswith('CM') for f in fonts)
    if latex:
        quartz = 'quartz' in prod or bool(cmex_codes & {0x2713, 0x25c6, 0x21e2})
        return LatexQuartz() if (quartz or has_bvariant) else LatexNative()
    mathtypey = pua > 30 or any(f.startswith(('Symbol', 'MT-Extra', 'MT Extra', 'MTSY', 'MTMI'))
                                 for f in fonts)
    cambria = any('cambriamath' in f.lower() for f in fonts)
    if mathtypey and pua > 30:
        return MathTypeImage()
    if cambria:
        return WordVG() if (has_bvariant and has_anskey) else WordOMML()
    if mathtypey:
        return MathTypeImage()
    return WordOMML() if any('cambria' in f.lower() or 'times' in f.lower() for f in fonts) else LatexNative()



# ================================================================ VALIDATOR
WHITELIST = set("""dfrac frac sqrt cdot times angle circ le ge leqslant geqslant ne pm mp to
Rightarrow Leftrightarrow leftrightarrow in infty triangle sum prod int partial ldots sim approx
equiv cup cap subset subseteq supset supseteq forall exists neg wedge vee perp parallel varnothing
nabla text operatorname left right begin end cases alpha beta gamma delta epsilon varepsilon zeta
eta theta vartheta iota kappa lambda mu nu xi pi varpi rho sigma tau upsilon phi varphi chi psi
omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega sin cos tan cot log ln lg min max
gcd lim exp deg arcsin arccos""".split())


def _math_ok(frag):
    for m in re.finditer(r'\\([a-zA-Z]+)', frag):
        if m.group(1) not in WHITELIST:
            return 'unknown \\%s' % m.group(1)
    depth = 0
    i = 0
    while i < len(frag):
        c = frag[i]
        if c == '\\' and i + 1 < len(frag):
            i += 2
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth < 0:
                return 'unbalanced }'
        elif c == '$':
            return 'nested $'
        i += 1
    if depth != 0:
        return 'unbalanced {'
    if frag.count(r'\left') != frag.count(r'\right'):
        return 'left/right mismatch'
    if frag.count(r'\begin{cases}') != frag.count(r'\end{cases}'):
        return 'cases mismatch'
    if re.search(r'\^\s*\^|_\s*_', frag):
        return 'double script'
    return None


def _math_fix(frag):
    def fix_cmd(m):
        w = m.group(1)
        return ('\\' + w) if w in WHITELIST else (r'\operatorname{%s}' % w)
    frag = re.sub(r'\\([a-zA-Z]+)', fix_cmd, frag)
    frag = re.sub(r'\\(?![a-zA-Z{}\\, ])', '', frag)
    depth = 0
    out = []
    i = 0
    while i < len(frag):
        c = frag[i]
        if c == '\\' and i + 1 < len(frag):
            out.append(frag[i:i + 2]); i += 2
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            if depth == 0:
                i += 1
                continue
            depth -= 1
        out.append(c); i += 1
    frag = ''.join(out) + '}' * depth
    dl = frag.count(r'\left') - frag.count(r'\right')
    if dl > 0:
        frag += r'\right.' * dl
    elif dl < 0:
        frag = r'\left.' * (-dl) + frag
    frag = re.sub(r'\^\s*\^', '^', frag)
    frag = re.sub(r'_\s*_', '_', frag)
    return frag.replace('$', '')


def _demote(frag):
    t = re.sub(r'\\[a-zA-Z]+', ' ', frag)
    t = re.sub(r'[{}\^_\\]', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


def validate_md(md, warnings):
    if not md:
        return md

    def handle(m, display):
        frag = m.group(1).strip()
        if not frag:
            return ' '
        err = _math_ok(frag)
        if err is None:
            return m.group(0)
        fixed = _math_fix(frag)
        if _math_ok(fixed) is None:
            warnings.append({'was': frag[:80], 'fix': 'auto', 'err': err})
            return ('\n$$\n%s\n$$\n' % fixed) if display else ('$%s$' % fixed)
        warnings.append({'was': frag[:80], 'fix': 'demoted', 'err': err})
        return _demote(frag)

    md = re.sub(r'\$\$\s*([\s\S]*?)\s*\$\$', lambda m: handle(m, True), md)
    md = re.sub(r'(?<!\$)\$(?!\$)([^\$\n]+?)\$(?!\$)', lambda m: handle(m, False), md)
    md = re.sub(r'\n?\$\$\s*\$\$\n?', '\n', md)
    md = re.sub(r'\$(\s*)\$', ' ', md)
    return md


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


def detect_graphics(page, curves, draw_rects, img_rects, prof=None):
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
        tspans = [s for tb in page.get_text('dict', clip=c)['blocks'] if tb['type'] == 0
                  for tl in tb['lines'] for s in tl['spans'] if s['text'].strip()]
        n_text = len(tspans)
        if prof is not None and getattr(prof, 'reject_mathy_figures', False) and n_text >= 2:
            mathy = sum(1 for s in tspans if prof.is_math_span(s))
            if mathy >= n_text * 0.5:
                continue                        # это выключная формула, не рисунок
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
    rows = rows_split(units, base_size)
    parts = [units_to_tex(sorted(r, key=lambda u: u['x0']), base_size, prof) for r in rows]
    tex = ' '.join(p for p in parts if p)
    tex = re.sub(r'([=<>+\-]|\\[lg]e(?:qslant)?|\\Leftrightarrow)\s+\1(?![\w])', r'\1', tex)
    return re.sub(r'\s{2,}', ' ', tex).strip()


def render_region(spans, bars, base_size, prof):
    bars = sorted([b for b in bars if (b[1] - b[0]) > 3], key=lambda b: (b[1] - b[0]))
    units = [{'span': s, 'x0': s['bbox'][0], 'x1': s['bbox'][2],
              'cy': (s['bbox'][1] + s['bbox'][3]) / 2, 'h': s['bbox'][3] - s['bbox'][1]} for s in spans]
    # система уравнений: '{' слева от вертикального стека (>=2 строк) -> \begin{cases}
    has_brace = any(u.get('span') and u['span']['text'].strip() == '{' for u in units)
    tall = any(u.get('span') and u['span']['text'].strip() == '{' and u['h'] > base_size * 1.6
               for u in units)
    nrows0 = len(rows_split([u for u in units
                             if not (u.get('span') and u['span']['text'].strip() in '{}')],
                            base_size))
    cases = has_brace and (tall or nrows0 >= 2)
    if cases:
        units = [u for u in units
                 if not (u.get('span') and u['span']['text'].strip() in ('{', '}'))]
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
    if cases:
        rows = rows_split(units, base_size)
        body = r' \\ '.join(units_to_tex(sorted(r, key=lambda u: u['x0']), base_size, prof)
                              for r in rows if r)
        return r'\begin{cases} %s \end{cases}' % body
    return rows_to_tex(units, base_size, prof)


def rows_split(units, base_size):
    if not units:
        return []
    us = sorted(units, key=lambda u: u['cy'])
    rows, cur = [], [us[0]]
    for u in us[1:]:
        if u['cy'] - cur[-1]['cy'] > base_size * 0.8:
            rows.append(cur); cur = [u]
        else:
            cur.append(u)
    rows.append(cur)
    return rows


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

    stream, fig_no_holder = [], [0]
    for pno, page in enumerate(doc):
        h_bars, curves, draw_rects, img_rects = page_geometry(page)
        figures, tables = detect_graphics(page, curves, draw_rects, img_rects, prof)
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
                if prof.is_chrome(raw, pno, page, lb):
                    continue
                only_math = all(prof.is_math_span(s) or not s['text'].strip() for s in spans)
                lines.append({'y': lb.y0, 'x0': lb.x0, 'x1': lb.x1, 'y0': lb.y0, 'y1': lb.y1,
                              'spans': spans, 'math': only_math,
                              'centered': (lb.x0 - page.rect.x0 > 110) and (page.rect.x1 - lb.x1 > 110),
                              'bold': bool(spans[0]['flags'] & 16),
                              'italic': bool(spans[0]['flags'] & 2) and not prof.is_math_span(spans[0])})
        lines.sort(key=lambda L: (round(L['y']), L['x0']))

        if prof.math_as_image:
            mi = 0
            glue = getattr(prof, 'glue_span', None)

            def _mline(LL):
                joined = ''.join(s['text'] for s in LL['spans']).strip()
                if prof.TASK_RE.match(joined):
                    return False
                if LL['math']:
                    return True
                return bool(glue) and all(glue(s) or not s['text'].strip() for s in LL['spans'])

            i = 0
            while i < len(lines):
                L = lines[i]
                if _mline(L):
                    block = [L]; j = i + 1
                    while j < len(lines) and _mline(lines[j]) and \
                            lines[j]['y0'] - max(bb['y1'] for bb in block) < base_size * 1.6:
                        block.append(lines[j]); j += 1
                    x0 = min(bb['x0'] for bb in block) - 3; x1 = max(bb['x1'] for bb in block) + 3
                    y0 = min(bb['y0'] for bb in block) - 3; y1 = max(bb['y1'] for bb in block) + 3
                    mi += 1
                    fid = 'm%d_%d' % (pno + 1, mi)
                    rect = fitz.Rect(x0, y0, x1, y1) & page.rect
                    if rect.width > 4 and rect.height > 4:
                        page.get_pixmap(clip=rect, dpi=DPI).save(
                            os.path.join(outdir, 'figures', fid + '.png'))
                        stream.append((pno, y0, 'mimg',
                                       (fid, {'page': pno + 1,
                                              'bbox': [round(v, 1) for v in rect]})))
                    i = j
                    continue
                if any(prof.is_math_span(s) for s in L['spans']):
                    sp = L['spans']
                    marks = [prof.is_math_span(s) for s in sp]
                    glue = getattr(prof, 'glue_span', None)
                    if glue:
                        for _ in range(2):
                            for k, s in enumerate(sp):
                                if marks[k] or not glue(s):
                                    continue
                                if (k > 0 and marks[k - 1]) or (k + 1 < len(sp) and marks[k + 1]):
                                    marks[k] = True
                    parts_md, figs, run = [], [], []

                    def flushrun():
                        nonlocal mi
                        if not run:
                            return
                        rx0 = min(s['bbox'][0] for s in run) - 2
                        rx1 = max(s['bbox'][2] for s in run) + 2
                        ry0 = min(s['bbox'][1] for s in run) - 2
                        ry1 = max(s['bbox'][3] for s in run) + 2
                        mi += 1
                        fid = 'm%d_%d' % (pno + 1, mi)
                        rect = fitz.Rect(rx0, ry0, rx1, ry1) & page.rect
                        if rect.width > 4 and rect.height > 3:
                            page.get_pixmap(clip=rect, dpi=DPI).save(
                                os.path.join(outdir, 'figures', fid + '.png'))
                            parts_md.append(' {{FIG:%s}} ' % fid)
                            figs.append((fid, {'page': pno + 1,
                                               'bbox': [round(v, 1) for v in rect]}))
                        run.clear()

                    for k, s in enumerate(sp):
                        if marks[k]:
                            run.append(s)
                        elif not s['text'].strip():
                            if run:
                                run.append(s)
                            else:
                                parts_md.append(s['text'])
                        else:
                            flushrun()
                            t = prof.clean_text(s['text'])
                            if s['flags'] & 16 and t.strip():
                                t = '**' + t.strip() + '** '
                            parts_md.append(t)
                    flushrun()
                    L2 = dict(L)
                    L2['pre_md'] = re.sub(r'  +', ' ', ''.join(parts_md)).strip()
                    L2['figs'] = figs
                    stream.append((pno, L['y'], 'line', L2))
                else:
                    stream.append((pno, L['y'], 'line', L))
                i += 1
        else:
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

    # ---------------- сегментация ----------------
    class _Ctx:
        def __init__(self, prof):
            self.prof, self.tasks = prof, []
            self.cur = self.variant = self.section = None

        def _target(self):
            return self.variant if self.variant is not None else self.cur

        def new_section(self, kind, title, into=None):
            t = into if into is not None else self._target()
            if t is None:
                return None
            self.section = {'kind': kind, 'title': title, 'parts': []}
            t['sections'].append(self.section)
            return self.section

        def add_part(self, part):
            if self.section is None:
                self.new_section('statement', 'Условие')
            if self.section is not None:
                self.section['parts'].append(part)

        def start_task(self, number, meta=None, author=None):
            self.cur = {'number': number, 'author': author, 'sections': [],
                        'variants': [], 'meta_extra': dict(meta or {})}
            sm = getattr(self, 'scope_meta', None)
            if sm:
                self.cur['meta_extra'].update(sm)
            self.tasks.append(self.cur)
            self.variant = None
            self.section = None
            self.new_section('statement', 'Условие')
            return self.cur

        def ensure_task(self, number):
            for t in self.tasks:
                if t['number'] == number:
                    self.cur, self.variant, self.section = t, None, None
                    return t
            return self.start_task(number)

        def start_variant(self, label, task_number=None):
            if task_number is not None:
                self.ensure_task(task_number)
            if self.cur is None:
                return None
            v = {'label': label, 'sections': [], 'answer_md': None}
            self.cur['variants'].append(v)
            self.variant = v
            self.section = None
            self.new_section('statement', 'Условие')
            return v

        def find_variant(self, number, label):
            for t in self.tasks:
                if t['number'] == number:
                    for v in t['variants']:
                        if v['label'] == label:
                            self.cur, self.variant = t, v
                            return v
            return None

    ctx = _Ctx(prof)

    for pno, y, kind, payload in stream:
        if kind == 'mimg':
            if ctx.cur is None:
                continue
            fid, meta = payload
            ctx.add_part(('figure', fid, meta))
            continue
        if kind == 'display':
            if ctx.cur is None:
                continue
            ctx.add_part(('display', payload, None))
            continue
        if kind == 'figure':
            if ctx.cur is None:
                continue
            fig_no_holder[0] += 1
            fid = 't%d_f%d' % (ctx.cur['number'], fig_no_holder[0])
            rect = fitz.Rect(payload.x0 - 4, payload.y0 - 4,
                             payload.x1 + 4, payload.y1 + 4) & doc[pno].rect
            doc[pno].get_pixmap(clip=rect, dpi=DPI).save(
                os.path.join(outdir, 'figures', fid + '.png'))
            ctx.add_part(('figure', fid,
                          {'page': pno + 1, 'bbox': [round(v, 1) for v in payload]}))
            continue
        if kind == 'table':
            rect, vxs, hys = payload
            md = build_table_md(doc[pno], rect, vxs, hys, base_size, prof)
            if prof.handle_stream(ctx, 'table', md):
                continue
            if ctx.cur is None:
                continue
            ctx.add_part(('text', '\n' + md + '\n', None))
            continue

        # kind == 'line'
        text = payload.get('pre_md') if payload.get('pre_md') is not None \
            else inline_line_tex(payload['spans'], base_size, prof, bars=[])
        if not text:
            continue
        plain = re.sub(r'\*\*', '', text)
        figs = payload.get('figs') or []

        if prof.handle_stream(ctx, 'line', payload, plain):
            continue

        plain_t = re.sub(r'^(?:\s*\{\{FIG:[^}]+\}\}\s*)+', '', plain)
        m = prof.TASK_RE.match(plain_t)
        if m and prof.merge_repeated and ctx.section is not None \
                and ctx.section['kind'] == 'rubric':
            ctx.add_part(('text', text, None))
            for f in figs:
                ctx.add_part(('figref', f[0], f[1]))
            continue
        if m and (not prof.task_needs_bold or payload['bold']):
            if len(m.groups()) == 2:
                n = int(m.group(1)) * 100 + int(m.group(2))
            else:
                n = int(m.group(1)) + getattr(ctx, 'scope', 0) * 100
            exists = any(t['number'] == n for t in ctx.tasks)
            if prof.merge_repeated and exists:
                ctx.ensure_task(n)
                ctx.new_section('solution', 'Решение')
            else:
                ctx.start_task(n)
            rest = plain_t[m.end():].strip()
            if rest:
                ctx.add_part(('text', rest, None))
            for f in figs:
                ctx.add_part(('figref', f[0], f[1]))
            continue
        if ctx.cur is None:
            continue

        if prof.variants and prof.VARIANT_RE:
            vm = prof.VARIANT_RE.match(plain)
            if vm and payload['bold']:
                ctx.start_variant('В-' + vm.group(1))
                rest = plain[vm.end():].strip()
                if rest:
                    ctx.add_part(('text', rest, None))
                continue

        am = prof.AUTHOR_RE.match(plain.strip())
        if am and payload['italic']:
            ctx.cur['author'] = am.group(1)
            continue

        sm = prof.SECTION_RE.match(plain)
        if sm and (payload['bold'] or payload['italic']):
            title = sm.group(1)
            body = plain[sm.end():].strip()
            k2 = prof.classify_section(title.split()[0])
            if k2 in ('solution', 'comment', 'rubric') and prof.variants:
                ctx.variant = None
            if k2 == 'sub' and ctx.section:
                ctx.add_part(('text', '**%s.** %s' % (title, body), None))
                continue
            ctx.new_section(k2, title)
            if body:
                ctx.add_part(('text', body, None))
            for f in figs:
                ctx.add_part(('figref', f[0], f[1]))
            continue

        ctx.add_part(('text', text, None))
        for f in figs:
            ctx.add_part(('figref', f[0], f[1]))

    # ---------------- сборка ----------------
    def assemble(parts):
        md, figs = [], []
        for typ, a, b in parts:
            if typ == 'figure':
                md.append('\n{{FIG:%s}}\n' % a)
                figs.append({'id': a, **b, 'file': 'figures/%s.png' % a})
            elif typ == 'figref':
                figs.append({'id': a, **b, 'file': 'figures/%s.png' % a})
            elif typ == 'display':
                md.append('\n$$\n%s\n$$\n' % a)
            else:
                md.append(a)
        text = ' '.join(md)
        text = re.sub(r'\n?\$\$\n[A-Za-z](?:\s*[A-Za-z]){0,3}\n\$\$\n?', '\n', text)
        text = re.sub(r'(\w)-\s+([а-яё])', r'\1\2', text)
        text = re.sub(r'\$([А-яЁё])', r'$ \1', text)
        text = re.sub(r'([А-яЁё])\$(?!\$)', r'\1 $', text)
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
            elif sec['kind'] == 'rubric':
                target_obj['rubric_md'] = ((target_obj.get('rubric_md') or '') + '\n' + body).strip()
            elif sec['kind'] == 'solution':
                target_obj.setdefault('solutions', []).append({'title': sec['title'], 'body_md': body})
            else:
                target_obj.setdefault('comments', []).append({'title': sec['title'], 'body_md': body})

    result = {'source': os.path.basename(pdf_path), 'profile': prof.name,
              'base_size': base_size, 'tasks': []}
    for t in ctx.tasks:
        obj = {'number': t['number'], 'author': t['author'], 'statement_md': '',
               'answer_md': t.get('answer_md'), 'rubric_md': None,
               'solutions': [], 'comments': [], 'figures': [],
               'meta': dict(t.get('meta_extra') or {}), 'verified': False}
        sections_to_obj(t['sections'], obj)
        if t['variants']:
            obj['variants'] = []
            for v in t['variants']:
                vobj = {'label': v['label'], 'statement_md': '',
                        'answer_md': v.get('answer_md'), 'figures': []}
                sections_to_obj(v['sections'], vobj)
                obj['figures'] += vobj.pop('figures', [])
                obj['variants'].append(vobj)
        # валидация KaTeX-совместимости всех текстовых полей
        warn = []
        obj['statement_md'] = validate_md(obj['statement_md'], warn)
        obj['answer_md'] = validate_md(obj['answer_md'], warn)
        obj['rubric_md'] = validate_md(obj['rubric_md'], warn)
        for s in obj['solutions']:
            s['body_md'] = validate_md(s['body_md'], warn)
        for c in obj['comments']:
            c['body_md'] = validate_md(c['body_md'], warn)
        for v in obj.get('variants', []):
            v['statement_md'] = validate_md(v['statement_md'], warn)
            v['answer_md'] = validate_md(v['answer_md'], warn)
        if warn:
            obj['meta']['math_warnings'] = warn
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
