"""Stage 1: clean a Remotive-style startup export into startups_clean.csv.

Dedupes by domain, scores each row, and builds email-candidate permutations from the
CEO name. Run once per source export; stages 2 and 3 read the output.

    python clean_list.py "900 Startups.csv" --out startups_clean.csv

The source is expected to carry its real header on the second row (--header 1), which
is how the Remotive sheet exports. Pass --header 0 for a plain CSV.
"""
import argparse
import pandas as pd, re, unicodedata
from urllib.parse import urlparse

ap = argparse.ArgumentParser(description=__doc__,
                             formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("infile", help="the raw startup list CSV")
ap.add_argument("--out", default="startups_clean.csv")
ap.add_argument("--header", type=int, default=1,
                help="0-indexed row holding the column names (default 1)")
args = ap.parse_args()

df = pd.read_csv(args.infile, header=args.header)
df = df.rename(columns={
    'Unnamed: 1': 'freshness',
    'Company name': 'company',
    'Nb. of \nEmployees': 'size',
    'What do they do (Verbatim - 10 words max.)   ': 'description',
    'Link to website': 'website',
    'HQ City': 'city',
    'HQ Country': 'country',
    'CEO name': 'ceo',
    'CEO Twitter': 'ceo_twitter',
    'Link to jobpage': 'jobpage',
})
cols = ['company','description','website','city','country','size','ceo','ceo_twitter','jobpage','freshness','Prov']
df = df[cols]
df = df[df['company'].notna() & df['website'].notna()].copy()

def domain(u):
    try:
        u = str(u).strip()
        if not u.startswith('http'): u = 'http://' + u
        h = urlparse(u).netloc.lower()
        return h[4:] if h.startswith('www.') else h
    except Exception as e:
        # The next line filters these out, so a silent failure here shrinks the
        # output with no trace. Every other stage prints its row counts.
        print(f'domain() failed on {u!r}: {type(e).__name__}: {e}')
        return None
df['domain'] = df['website'].map(domain)
df = df[df['domain'].notna() & (df['domain'] != '')]
df = df.drop_duplicates(subset='domain', keep='first')

def strip_accents(s):
    return ''.join(c for c in unicodedata.normalize('NFKD', s) if not unicodedata.combining(c))

def split_name(n):
    if not isinstance(n, str): return None, None
    n = strip_accents(n.strip())
    n = re.sub(r'[^A-Za-z \-\.]', '', n)
    parts = [p for p in n.split() if len(p) > 1 and '.' not in p]
    if len(parts) < 2: return (parts[0].lower(), None) if parts else (None, None)
    return parts[0].lower(), parts[-1].lower()

# Build both columns in one frame. `.apply(lambda n: pd.Series(...))` constructs a
# Series per row, which is the slow path and needless for a fixed-width tuple.
df[['ceo_first','ceo_last']] = pd.DataFrame(df['ceo'].map(split_name).tolist(),
                                            index=df.index,
                                            columns=['ceo_first','ceo_last'])

PATTERNS = ['{f}', '{f}.{l}', '{f}{l}', '{fi}{l}', '{fi}.{l}', '{f}_{l}']
def candidates(r):
    f, l, d = r['ceo_first'], r['ceo_last'], r['domain']
    if not isinstance(f, str) or not isinstance(d, str): return ''
    if not isinstance(l, str): l = None
    out = []
    for p in PATTERNS:
        if '{l}' in p and not l: continue
        out.append(p.format(f=f, l=l, fi=f[0]) + '@' + d)
    return '|'.join(dict.fromkeys(out))
df['email_candidates'] = df.apply(candidates, axis=1)

DEAD = r'(angel\.co|stackoverflow\.com/jobs|jobs\.github\.com|weworkremotely\.com/company)'
df['jobpage_stale'] = df['jobpage'].fillna('').astype(str).str.contains(DEAD, regex=True)

TECH = r'(ai\b|a\.i|machine learning|ml\b|data|api|developer|devtool|platform|saas|cloud|infrastructure|automation|analytics|software|engineering|app\b|apps\b|tech|blockchain|security|database|open source)'
df['tech_hit'] = df['description'].astype(str).str.lower().str.contains(TECH, regex=True)
SIZE_OK = {'11-50', '51-200', '1-10'}
df['size_ok'] = df['size'].astype(str).str.strip().isin(SIZE_OK)
df['fresh'] = df['freshness'].astype(str).str.lower().str.strip().eq('new')

df['score'] = (df['fresh'].astype(int) * 3 + df['tech_hit'].astype(int) * 2
               + df['size_ok'].astype(int) + df['jobpage'].notna().astype(int)
               - df['jobpage_stale'].astype(int) * 2)
df = df.sort_values(['score','company'], ascending=[False, True])

out = df[['company','domain','description','ceo','ceo_first','ceo_last','ceo_twitter',
          'city','country','size','jobpage','jobpage_stale','fresh','tech_hit','score','email_candidates','website']]
out.to_csv(args.out, index=False, encoding='utf-8')
print('wrote', args.out)
print('rows:', len(out))
print(out['score'].value_counts().sort_index(ascending=False).to_string())
print('with candidates:', (out['email_candidates']!='').sum())
print()
print(out.head(12)[['company','domain','description','score']].to_string())
