const BASE = 'https://to.zozo.jp';
const SHOP_ID = '3326';
const SCATEGORY_PID = '40469';
const SLACK_USERNAME = 'krähe 売上通知';
const SLACK_ICON = ':dress:';
const WEEKDAYS = '月火水木金土日'; // index 0=月 … 6=日

export interface Env {
  ZOZO_STATE: KVNamespace;
  BASIC_AUTH: string;
  PORTAL_LOGIN: string;
  SLACK_BOT_TOKEN: string;
  SLACK_CHANNEL: string;
}

// ── Cookie jar ──────────────────────────────────────────────────────────────

class CookieJar {
  private jar: Record<string, string> = {};

  absorb(headers: Headers) {
    headers.forEach((value, name) => {
      if (name.toLowerCase() !== 'set-cookie') return;
      const eq = value.indexOf('=');
      if (eq < 0) return;
      const k = value.slice(0, eq).trim();
      const semi = value.indexOf(';', eq);
      const v = semi > eq ? value.slice(eq + 1, semi) : value.slice(eq + 1);
      this.jar[k] = v.trim();
    });
  }

  header() {
    return Object.entries(this.jar).map(([k, v]) => `${k}=${v}`).join('; ');
  }
}

// ── HTTP fetch with manual redirect + cookie tracking ───────────────────────

async function fetchText(
  url: string,
  init: RequestInit,
  jar: CookieJar,
  hops = 6,
): Promise<string> {
  const headers = new Headers(init.headers as HeadersInit);
  const c = jar.header();
  if (c) headers.set('Cookie', c);

  const resp = await fetch(url, { ...init, headers, redirect: 'manual' });
  jar.absorb(resp.headers);

  if (resp.status >= 300 && resp.status < 400 && hops > 0) {
    const loc = resp.headers.get('Location');
    if (loc) {
      return fetchText(
        new URL(loc, url).toString(),
        { method: 'GET', headers: init.headers },
        jar,
        hops - 1,
      );
    }
  }

  const buf = await resp.arrayBuffer();
  const ct = resp.headers.get('content-type') ?? '';
  const cs = (ct.match(/charset=([^\s;]+)/i)?.[1] ?? '').toLowerCase().replace(/[^a-z0-9]/g, '');
  if (cs && (cs.includes('932') || cs.includes('sjis') || cs.includes('shiftjis'))) {
    return new TextDecoder('shift-jis').decode(buf);
  }
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(buf);
  } catch {
    return new TextDecoder('shift-jis').decode(buf);
  }
}

// ── Portal client ────────────────────────────────────────────────────────────

class Portal {
  private jar = new CookieJar();
  private auth: string;

  constructor(basicAuth: string) {
    const colon = basicAuth.indexOf(':');
    this.auth = `Basic ${btoa(basicAuth.slice(0, colon) + ':' + basicAuth.slice(colon + 1))}`;
  }

  private baseHeaders(): Record<string, string> {
    return { 'User-Agent': 'Mozilla/5.0', Authorization: this.auth };
  }

  private get(path: string) {
    return fetchText(BASE + path, { method: 'GET', headers: this.baseHeaders() }, this.jar);
  }

  private post(path: string, body: Record<string, string>) {
    return fetchText(BASE + path, {
      method: 'POST',
      headers: { ...this.baseHeaders(), 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams(body).toString(),
    }, this.jar);
  }

  async login(creds: string) {
    const html = await this.get('/to/');
    const m = html.match(/name="csrf_token"\s+value="([^"]+)"/);
    if (!m) throw new Error('[zozo] csrf_token not found');
    const colon = creds.indexOf(':');
    await this.post('/to/Default.asp', {
      c: 'Login', csrf_token: m[1], redirect_uri: '',
      'zozo-app-os': '', 'zozo-app-os-ver': '', 'zozo-app-name': '', 'zozo-app-ver': '',
      LoginName: creds.slice(0, colon), Password: creds.slice(colon + 1), TerminalID: '',
    });
  }

  async searchSummary(date: string) {
    await this.get('/to/Sales.asp?c=SalesSummary');
    return this.post('/to/Sales.asp', {
      c: 'Search', search: 'SEARCH',
      ShopID: SHOP_ID, SCategoryPID: SCATEGORY_PID, SCategoryID: '0',
      CustomerTypeID: '0', TypeCategoryID: '0', TypeID: '0',
      ViewType: '1', DailyFlag: '1', TeikibinCheck: '0', MallCheck: '0',
      TermFrom: date, TermTo: date,
      ViewList: '1', OldFlag: '0', HasOrderNumber: '0',
    });
  }

  getDetail(date: string) {
    return this.get(`/to/Sales.asp?c=SalesSummary_Detail&ReportDate=${date}&DetailDivision=1`);
  }
}

// ── HTML table parsing ───────────────────────────────────────────────────────

function stripTags(s: string) {
  return s.replace(/<[^>]+>/g, ' ')
    .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&nbsp;/g, ' ').replace(/\s+/g, ' ').trim();
}

function parseRows(html: string): string[][] {
  return html.split(/<\/tr>/i).map(chunk => {
    const cells: string[] = [];
    const re = /<t[dh][^>]*>([\s\S]*?)<\/t[dh]>/gi;
    let m;
    while ((m = re.exec(chunk)) !== null) cells.push(stripTags(m[1]));
    return cells;
  }).filter(r => r.length > 0);
}

interface Summary { total_qty: number; total_amount: number }

function parseSummary(html: string, date: string): Summary {
  for (const row of parseRows(html)) {
    if (row[0] === date && row.length >= 3) {
      const qty = parseInt(row[1].replace(/,/g, ''), 10);
      const amt = parseInt(row[2].replace(/,/g, ''), 10);
      if (!isNaN(qty)) return { total_qty: qty, total_amount: isNaN(amt) ? 0 : amt };
    }
  }
  return { total_qty: 0, total_amount: 0 };
}

interface Product { name: string; code: string; price_type: string; qty: number; amount: number }

function parseDetail(html: string): Product[] {
  return parseRows(html).flatMap(row => {
    if (row.length < 11 || row[1] !== 'krahe') return [];
    const qty = parseInt(row[9].replace(/,/g, ''), 10);
    const amt = parseInt(row[10].replace(/,/g, ''), 10);
    if (isNaN(qty) || qty <= 0 || isNaN(amt)) return [];
    return [{ name: row[4], code: row[3], price_type: row[8], qty, amount: amt }];
  }).sort((a, b) => b.amount - a.amount);
}

// ── State ────────────────────────────────────────────────────────────────────

interface State {
  date: string;
  total_qty: number;
  total_amount: number;
  products: Record<string, { qty: number; amount: number }>;
}

function toDict(ps: Product[]): Record<string, { qty: number; amount: number }> {
  const d: Record<string, { qty: number; amount: number }> = {};
  for (const p of ps) d[`${p.name}|${p.price_type}`] = { qty: p.qty, amount: p.amount };
  return d;
}

// ── Slack ────────────────────────────────────────────────────────────────────

async function postSlack(token: string, channel: string, text: string) {
  const r = await fetch('https://slack.com/api/chat.postMessage', {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ channel, text, username: SLACK_USERNAME, icon_emoji: SLACK_ICON }),
  });
  const data = await r.json() as { ok: boolean; error?: string };
  if (!data.ok) throw new Error(`Slack: ${data.error}`);
}

// ── Format ───────────────────────────────────────────────────────────────────

function jstToday(): string {
  const d = new Date(Date.now() + 9 * 3600_000);
  return `${d.getUTCFullYear()}/${String(d.getUTCMonth() + 1).padStart(2, '0')}/${String(d.getUTCDate()).padStart(2, '0')}`;
}

function weekday(dateStr: string): string {
  const [y, m, d] = dateStr.split('/').map(Number);
  const jsDay = new Date(Date.UTC(y, m - 1, d, 3)).getDay(); // noon JST
  return WEEKDAYS[(jsDay + 6) % 7];
}

function fmt(n: number) {
  return n.toLocaleString();
}

function formatMsg(date: string, dQty: number, dAmt: number, items: Product[], totQty: number, totAmt: number): string {
  const lines = [
    `*krähe 売上通知です！* ${date}(${weekday(date)})`,
    `新着 *+${dQty}点 +¥${fmt(dAmt)}*（税抜）`,
    `（本日合計: ${fmt(totQty)}点 ¥${fmt(totAmt)}）`,
    '',
  ];
  for (const p of items.slice(0, 15)) {
    lines.push(`• ${p.price_type === 'セール' ? '🏷' : ''}${p.name} \`${p.code}\` — ¥${fmt(p.amount)} (${p.qty}点)`);
  }
  return lines.join('\n');
}

// ── Main ─────────────────────────────────────────────────────────────────────

async function run(env: Env, dateOverride?: string) {
  const date = dateOverride
    ? `${dateOverride.slice(0, 4)}/${dateOverride.slice(5, 7)}/${dateOverride.slice(8, 10)}`
    : jstToday();

  const portal = new Portal(env.BASIC_AUTH);
  await portal.login(env.PORTAL_LOGIN);

  const summaryHtml = await portal.searchSummary(date); // establishes session
  const detailHtml = await portal.getDetail(date);

  const summary = parseSummary(summaryHtml, date);
  const products = parseDetail(detailHtml);
  const current = toDict(products);

  const raw = await env.ZOZO_STATE.get('state');
  const last: State | null = raw ? JSON.parse(raw) : null;
  const lastProds = last?.date === date ? last.products : {};

  const delta: Product[] = [];
  let dQty = 0, dAmt = 0;
  for (const p of products) {
    const key = `${p.name}|${p.price_type}`;
    const prev = lastProds[key]?.qty ?? 0;
    if (p.qty > prev) {
      const added = p.qty - prev;
      const unit = p.qty > 0 ? Math.round(p.amount / p.qty) : 0;
      delta.push({ ...p, qty: added, amount: unit * added });
      dQty += added;
      dAmt += unit * added;
    }
  }

  if (dQty > 0) {
    await postSlack(env.SLACK_BOT_TOKEN, env.SLACK_CHANNEL,
      formatMsg(date, dQty, dAmt, delta.sort((a, b) => b.amount - a.amount), summary.total_qty, summary.total_amount));
    console.log(`notified +${dQty}pts +¥${dAmt}`);
  } else {
    console.log(`no new sales for ${date} (total: ${summary.total_qty}pts)`);
  }

  await env.ZOZO_STATE.put('state', JSON.stringify({
    date, total_qty: summary.total_qty, total_amount: summary.total_amount, products: current,
  } satisfies State));
}

// ── Exports ──────────────────────────────────────────────────────────────────

export default {
  async scheduled(_e: ScheduledEvent, env: Env, _ctx: ExecutionContext) {
    await run(env);
  },
  async fetch(req: Request, env: Env, _ctx: ExecutionContext) {
    try {
      const url = new URL(req.url);
      const date = url.searchParams.get('date'); // YYYY-MM-DD for manual trigger
      await run(env, date ?? undefined);
      return new Response('OK');
    } catch (e) {
      console.error(e);
      return new Response(String(e), { status: 500 });
    }
  },
};
