#!/usr/bin/env node
/**
 * slack-archiver/sync.js
 * Slackの全チャンネルメッセージをSQLite3に保存する
 * 中断しても続きから再開できる設計
 */

require('dotenv').config();
const Database = require('better-sqlite3');
const path = require('path');

const SLACK_TOKEN = process.env.SLACK_TOKEN;
const DB_PATH = process.env.DB_PATH || path.join(__dirname, 'messages.db');
const RATE_LIMIT_DELAY_MS = 1200; // Slack tier3: 50req/min

if (!SLACK_TOKEN) {
  console.error('❌ SLACK_TOKEN が設定されていない。.env を確認して');
  process.exit(1);
}

// CLI引数
const args = process.argv.slice(2);
const channelFilter = (() => {
  const idx = args.indexOf('--channel');
  return idx !== -1 ? args[idx + 1] : null;
})();
const limitFilter = (() => {
  const idx = args.indexOf('--limit');
  return idx !== -1 ? parseInt(args[idx + 1], 10) : null;
})();

// --- DB セットアップ ---
const db = new Database(DB_PATH);
db.pragma('journal_mode = WAL');

db.exec(`
  CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    name TEXT,
    type TEXT,
    is_member INTEGER DEFAULT 1,
    synced_until_ts TEXT,
    updated_at TEXT
  );

  CREATE TABLE IF NOT EXISTS messages (
    channel_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    user_id TEXT,
    text TEXT,
    subtype TEXT,
    thread_ts TEXT,
    reply_count INTEGER,
    raw_json TEXT,
    PRIMARY KEY (channel_id, ts)
  );

  CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT,
    channel_name TEXT,
    synced_at TEXT,
    messages_fetched INTEGER,
    status TEXT
  );

  CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id);
  CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
`);

// --- Slack API ヘルパー ---
async function slackAPI(method, params = {}) {
  const url = new URL(`https://slack.com/api/${method}`);
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null) url.searchParams.set(k, v);
  });

  const res = await fetch(url.toString(), {
    headers: { Authorization: `Bearer ${SLACK_TOKEN}` },
  });

  // レート制限対応
  if (res.status === 429) {
    const retryAfter = parseInt(res.headers.get('Retry-After') || '60', 10);
    console.log(`  ⏳ レート制限。${retryAfter}秒待機...`);
    await sleep(retryAfter * 1000);
    return slackAPI(method, params);
  }

  const json = await res.json();
  if (!json.ok) {
    throw new Error(`Slack API error [${method}]: ${json.error}`);
  }
  return json;
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// --- チャンネル一覧取得 ---
async function fetchAllChannels() {
  console.log('📋 チャンネル一覧を取得中...');
  const channels = [];
  let cursor = undefined;

  do {
    const res = await slackAPI('conversations.list', {
      types: 'public_channel,private_channel',
      exclude_archived: false,
      limit: 200,
      cursor,
    });

    for (const ch of res.channels) {
      if (ch.is_member) {
        channels.push(ch);
      }
    }

    cursor = res.response_metadata?.next_cursor;
    if (cursor) await sleep(500);
  } while (cursor);

  console.log(`✅ ${channels.length}チャンネル found\n`);
  return channels;
}

// --- チャンネル名を解決 ---
function channelDisplayName(ch) {
  if (ch.name) return `#${ch.name}`;
  if (ch.is_im) return `DM:${ch.user || ch.id}`;
  if (ch.is_mpim) return `MPIM:${ch.id}`;
  return ch.id;
}

// --- メッセージ取得＆保存 ---
async function syncChannel(ch, index, total) {
  const displayName = channelDisplayName(ch);
  const existing = db.prepare('SELECT synced_until_ts FROM channels WHERE id = ?').get(ch.id);
  const oldest = existing?.synced_until_ts || undefined;

  console.log(`[${index}/${total}] ${displayName} (${ch.id}) ${oldest ? `→ ${oldest}以降` : '→ 全件取得'}`);

  let fetched = 0;
  let latestTs = null;
  let cursor = undefined;

  const insertMsg = db.prepare(`
    INSERT OR REPLACE INTO messages (channel_id, ts, user_id, text, subtype, thread_ts, reply_count, raw_json)
    VALUES (@channel_id, @ts, @user_id, @text, @subtype, @thread_ts, @reply_count, @raw_json)
  `);

  const insertMany = db.transaction((msgs) => {
    for (const m of msgs) insertMsg.run(m);
  });

  try {
    do {
      const params = {
        channel: ch.id,
        limit: 200,
        cursor,
        oldest, // これ以降だけ取る（再開時）
      };

      let res;
      try {
        res = await slackAPI('conversations.history', params);
      } catch (err) {
        if (err.message.includes('not_in_channel') || err.message.includes('channel_not_found')) {
          console.log(`  ⚠️ アクセス不可 (${err.message.split(':')[1]?.trim()}) → スキップ`);
          return 0;
        }
        throw err;
      }

      const msgs = res.messages || [];

      const rows = msgs.map((m) => ({
        channel_id: ch.id,
        ts: m.ts,
        user_id: m.user || m.bot_id || null,
        text: m.text || null,
        subtype: m.subtype || null,
        thread_ts: m.thread_ts || null,
        reply_count: m.reply_count || 0,
        raw_json: JSON.stringify(m),
      }));

      insertMany(rows);
      fetched += rows.length;

      // 最新tsを記録（messagesは新しい順に返ってくる）
      if (rows.length > 0 && !latestTs) {
        latestTs = rows[0].ts;
      }

      process.stdout.write(`  ${fetched} msgs fetched...\r`);

      cursor = res.response_metadata?.next_cursor;
      if (cursor) await sleep(300);
    } while (cursor);

    // チャンネル情報を更新
    db.prepare(`
      INSERT OR REPLACE INTO channels (id, name, type, is_member, synced_until_ts, updated_at)
      VALUES (?, ?, ?, 1, ?, ?)
    `).run(
      ch.id,
      ch.name || null,
      ch.is_im ? 'im' : ch.is_mpim ? 'mpim' : ch.is_private ? 'private' : 'public',
      latestTs || existing?.synced_until_ts || null,
      new Date().toISOString()
    );

    // ログ記録
    db.prepare(`
      INSERT INTO sync_log (channel_id, channel_name, synced_at, messages_fetched, status)
      VALUES (?, ?, ?, ?, 'ok')
    `).run(ch.id, ch.name || ch.id, new Date().toISOString(), fetched);

    console.log(`  ✅ ${fetched} msgs saved`);
    return fetched;
  } catch (err) {
    db.prepare(`
      INSERT INTO sync_log (channel_id, channel_name, synced_at, messages_fetched, status)
      VALUES (?, ?, ?, ?, ?)
    `).run(ch.id, ch.name || ch.id, new Date().toISOString(), fetched, `error: ${err.message}`);
    console.log(`  ❌ エラー: ${err.message}`);
    return fetched;
  }
}

// --- メイン ---
async function main() {
  const startTime = Date.now();
  console.log('🚀 Slack Archiver 開始');
  console.log(`📁 DB: ${DB_PATH}\n`);

  let channels = await fetchAllChannels();

  // フィルタ
  if (channelFilter) {
    channels = channels.filter(
      (ch) => ch.name === channelFilter || ch.id === channelFilter
    );
    if (channels.length === 0) {
      console.error(`❌ チャンネル "${channelFilter}" が見つからない`);
      process.exit(1);
    }
  }

  if (limitFilter) {
    channels = channels.slice(0, limitFilter);
    console.log(`🔢 先頭 ${limitFilter} チャンネルのみ処理\n`);
  }

  let totalMessages = 0;
  for (let i = 0; i < channels.length; i++) {
    const ch = channels[i];
    const count = await syncChannel(ch, i + 1, channels.length);
    totalMessages += count;
    if (i < channels.length - 1) await sleep(RATE_LIMIT_DELAY_MS);
  }

  const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
  console.log('\n═══════════════════════════════════');
  console.log('📊 完了サマリー');
  console.log(`  チャンネル数: ${channels.length}`);
  console.log(`  総メッセージ: ${totalMessages.toLocaleString()}`);
  console.log(`  所要時間: ${elapsed}秒`);
  console.log('═══════════════════════════════════');

  // DB統計
  const stats = db.prepare('SELECT COUNT(*) as cnt FROM messages').get();
  console.log(`\n💾 DB総レコード数: ${stats.cnt.toLocaleString()} messages`);

  db.close();
}

main().catch((err) => {
  console.error('❌ 致命的エラー:', err);
  db.close();
  process.exit(1);
});
