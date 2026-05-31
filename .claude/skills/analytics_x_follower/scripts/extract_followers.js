// X (Twitter) followers extractor — DOM extraction版。
//
// 背景: 2026年の X は React fiber を DOM ノードに attach しない。さらに window.fetch /
// XMLHttpRequest を後から hook しても捕捉できない（X 側がページ初期化時に originalFetch を
// クロージャに抱え込むため）。よって React/network経由でのfollower_count取得は不可。
// DOM から取れる範囲（@handle / 名前 / bio / verified）に絞って収集する。
//
// 引数: (minFollowers, maxScrolls, limit, includeITPros)
//   - minFollowers は現状フィルタに使えない（DOMにフォロワー数が出ていない）ため、
//     `--no-it=false`時は全件マッチ扱い、IT判定併用時はIT系のみが reasonable な結果になる。
// 出力: window.__x_followers_result, window.__x_followers_done

(function (minFollowers, maxScrolls, limit, includeITPros) {
  if (window.__x_followers_running) return "already running";
  window.__x_followers_running = true;
  window.__x_followers_done = false;
  window.__x_followers_result = null;
  window.__x_followers_progress = { collected: 0, scrolls: 0, phase: "init" };

  const IT_KEYWORDS = [
    /engineer/i, /developer/i, /\bCTO\b/, /\bSRE\b/, /devops/i, /backend/i, /frontend/i,
    /full[- ]?stack/i, /software/i, /infrastructure/i, /platform/i, /machine learning/i,
    /\bML\b/, /\bAI\b/, /data scientist/i, /\bSWE\b/, /researcher/i, /research/i,
    /エンジニア/, /開発/, /プログラマ/, /研究/,
    /product manager/i, /\bPM\b/, /プロダクトマネージャ/, /プロダクトマネジャ/,
    /marketing/i, /marketer/i, /\bCMO\b/, /growth/i, /マーケティング/, /マーケター/,
    /founder/i, /\bCEO\b/, /\bCOO\b/, /entrepreneur/i, /起業/, /創業/, /代表/, /経営/,
    /designer/i, /デザイナー/, /UI\/UX/i,
    /investor/i, /\bVC\b/, /投資/, /キャピタリスト/,
  ];

  const collected = new Map();

  function extractFromCell(cell) {
    // @screen_name: UserAvatar-Container-<screen_name>
    let screen_name = null;
    const avatar = cell.querySelector('[data-testid^="UserAvatar-Container-"]');
    if (avatar) {
      const t = avatar.getAttribute("data-testid") || "";
      screen_name = t.replace(/^UserAvatar-Container-/, "") || null;
    }
    if (!screen_name) {
      // fallback: 最初の /<handle> アンカー
      const a = cell.querySelector('a[role="link"][href^="/"]');
      if (a) {
        const m = (a.getAttribute("href") || "").match(/^\/([A-Za-z0-9_]{1,15})$/);
        if (m) screen_name = m[1];
      }
    }
    if (!screen_name) return null;

    // 名前: 通常、handle と同じリンク領域の最初のテキストブロック
    let name = "";
    const nameNode = cell.querySelector('a[role="link"] span');
    if (nameNode) name = nameNode.textContent.trim();

    // bio: data-testid="UserDescription" もしくはセル末尾のテキストブロック
    let description = "";
    const bio = cell.querySelector('[data-testid="UserDescription"]');
    if (bio) {
      description = bio.textContent.trim();
    } else {
      // fallback: 全テキストから name と @handle を除いた残り
      const all = cell.innerText || "";
      description = all
        .split("\n")
        .map((s) => s.trim())
        .filter((s) => s && s !== name && s !== "@" + screen_name && s !== "Follow" && s !== "Following" && s !== "フォロー" && s !== "フォロー中" && s !== "フォローバック")
        .slice(2) // 最初の2行は name / @handle
        .join(" ");
    }

    // verified: SVG with aria-label hinting verified
    const verified = !!cell.querySelector('[data-testid="icon-verified"], svg[aria-label*="Verified"], svg[aria-label*="認証"]');

    return {
      screen_name,
      name,
      description,
      verified,
      url: `https://x.com/${screen_name}`,
    };
  }

  function collectVisible() {
    const cells = document.querySelectorAll('[data-testid="UserCell"]');
    cells.forEach((cell) => {
      const u = extractFromCell(cell);
      if (!u) return;
      if (collected.has(u.screen_name)) {
        // 既存にbioが空でこっちに有るなら更新
        const prev = collected.get(u.screen_name);
        if (!prev.description && u.description) prev.description = u.description;
        return;
      }
      collected.set(u.screen_name, u);
    });
    window.__x_followers_progress.collected = collected.size;
  }

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  (async () => {
    try {
      collectVisible();
      let stagnant = 0;
      for (let i = 0; i < maxScrolls; i++) {
        window.__x_followers_progress.scrolls = i;
        window.__x_followers_progress.phase = "scrolling";
        if (limit && collected.size >= limit) break;
        const prev = collected.size;
        window.scrollBy(0, window.innerHeight * 1.6);
        await sleep(950);
        collectVisible();
        if (collected.size === prev) {
          stagnant++;
          if (stagnant >= 5) break;
        } else {
          stagnant = 0;
        }
      }
      window.__x_followers_progress.phase = "filtering";

      let users = Array.from(collected.values());
      if (limit && users.length > limit) users = users.slice(0, limit);

      const matched = [];
      for (const u of users) {
        const reasons = [];
        if (includeITPros && u.description) {
          const hit = IT_KEYWORDS.find((re) => re.test(u.description));
          if (hit) reasons.push("IT/起業/投資/デザイン系");
        }
        if (u.verified) reasons.push("verified");
        // フォロワー数しきい値は不能（DOMに無い）
        if (reasons.length || !includeITPros) {
          matched.push({ ...u, reason: reasons.join(" / ") || "(no-filter)" });
        }
      }

      window.__x_followers_result = {
        total_collected: collected.size,
        matched_count: matched.length,
        params: { minFollowers, maxScrolls, limit, includeITPros },
        users: matched,
        all_users: Array.from(collected.values()),
        note: "follower_count はXの仕様変更でDOMから取れない。フィルタはbioキーワード+verifiedのみ。",
      };
      window.__x_followers_progress.phase = "done";
      window.__x_followers_done = true;
    } catch (e) {
      window.__x_followers_result = { error: String((e && e.stack) || e) };
      window.__x_followers_done = true;
    } finally {
      window.__x_followers_running = false;
    }
  })();

  return "started";
})(MIN_FOLLOWERS_PLACEHOLDER, MAX_SCROLLS_PLACEHOLDER, LIMIT_PLACEHOLDER, INCLUDE_IT_PLACEHOLDER);
