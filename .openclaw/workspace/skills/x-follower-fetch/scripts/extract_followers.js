/**
 * X (Twitter) Follower Extractor
 *
 * Run this in the browser console (or via browser.act evaluate) on:
 *   https://x.com/{username}/followers
 *
 * Returns JSON array of followers filtered by:
 *   - followers_count >= minFollowers  (default 1000)
 *   OR
 *   - bio contains IT professional keywords (engineer / PM / marketing)
 *
 * Parameters:
 *   minFollowers  {number}  Minimum follower count threshold (default 1000)
 *   maxScrolls    {number}  Max scroll attempts (default 30)
 *   limit         {number}  Stop after collecting this many raw users (0 = unlimited, default 0)
 *   includeITPros {boolean} Also include IT professionals regardless of follower count (default true)
 */

(async function extractFollowers(minFollowers = 1000, maxScrolls = 30, limit = 0, includeITPros = true) {
  const seen = new Map(); // screen_name -> user object

  // Keywords to identify IT professionals from bio/description
  const IT_KEYWORDS = [
    // Engineers
    'エンジニア', 'engineer', 'developer', 'dev ', 'software',
    'プログラマ', 'programmer', 'coder', 'frontend', 'backend',
    'フロントエンド', 'バックエンド', 'fullstack', 'フルスタック',
    'cto', 'テックリード', 'tech lead', 'sre', 'devops', 'インフラ',
    // Product
    'プロダクトマネージャ', 'プロダクトマネジャー', 'product manager',
    'プロダクト', ' pm ', '/#pm', 'ux', 'ui/ux', 'uxデザイン',
    // Marketing
    'マーケティング', 'マーケ', 'marketing', 'marketer', 'growth',
    'cmo', 'グロース', 'デジタルマーケ',
  ];

  function isITPro(description) {
    if (!description) return false;
    const lower = description.toLowerCase();
    return IT_KEYWORDS.some(kw => lower.includes(kw));
  }

  function getReactFiber(el) {
    const key = Object.keys(el).find(k =>
      k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance')
    );
    return key ? el[key] : null;
  }

  function scrapeVisible() {
    const cells = document.querySelectorAll('[data-testid="cellInnerDiv"]');
    cells.forEach(cell => {
      if (limit > 0 && seen.size >= limit) return;
      const fiber = getReactFiber(cell);
      if (!fiber) return;

      let node = fiber;
      let depth = 0;
      while (node && depth < 300) {
        try {
          const memoized = node.memoizedProps;

          // Path 1: userResult.result.legacy
          if (memoized?.userResult?.result?.legacy) {
            const legacy = memoized.userResult.result.legacy;
            if (legacy.screen_name && !seen.has(legacy.screen_name)) {
              seen.set(legacy.screen_name, {
                name: legacy.name,
                screen_name: legacy.screen_name,
                followers_count: legacy.followers_count,
                description: legacy.description || '',
              });
            }
          }

          // Path 2: user.legacy
          if (memoized?.user?.legacy) {
            const legacy = memoized.user.legacy;
            if (legacy.screen_name && !seen.has(legacy.screen_name)) {
              seen.set(legacy.screen_name, {
                name: legacy.name,
                screen_name: legacy.screen_name,
                followers_count: legacy.followers_count,
                description: legacy.description || '',
              });
            }
          }
        } catch (e) {}

        // DFS traversal
        if (node.child) {
          node = node.child;
        } else if (node.sibling) {
          node = node.sibling;
        } else {
          let ret = node.return;
          while (ret) {
            if (ret.sibling) { node = ret.sibling; break; }
            ret = ret.return;
          }
          if (!ret) break;
        }
        depth++;
      }
    });
  }

  // Scroll and collect
  for (let i = 0; i < maxScrolls; i++) {
    scrapeVisible();

    // Stop early if limit reached
    if (limit > 0 && seen.size >= limit) {
      console.log(`Limit of ${limit} users reached at scroll ${i}.`);
      break;
    }

    const prevCount = seen.size;
    window.scrollBy(0, window.innerHeight * 2);
    await new Promise(r => setTimeout(r, 1500));
    scrapeVisible();

    if (seen.size === prevCount && i > 3) {
      console.log(`No new users after scroll ${i}, stopping.`);
      break;
    }
    console.log(`Scroll ${i + 1}/${maxScrolls}: ${seen.size} users collected`);
  }

  const all = Array.from(seen.values());

  // Filter: high follower count OR IT professional
  const filtered = all.filter(u => {
    const highFollowers = u.followers_count >= minFollowers;
    const itPro = includeITPros && isITPro(u.description);
    return highFollowers || itPro;
  });

  // Tag why each user was included
  const tagged = filtered.map(u => ({
    ...u,
    reason: (() => {
      const tags = [];
      if (u.followers_count >= minFollowers) tags.push(`フォロワー${u.followers_count.toLocaleString()}`);
      if (includeITPros && isITPro(u.description)) tags.push('IT系');
      return tags.join(' / ');
    })(),
  }));

  // Sort by follower count desc
  tagged.sort((a, b) => b.followers_count - a.followers_count);

  console.log(`Total scraped: ${all.length}, matched: ${tagged.length}`);
  return JSON.stringify({
    total: all.length,
    matched: tagged.length,
    params: { minFollowers, limit, includeITPros },
    users: tagged,
  }, null, 2);
})(1000, 30, 0, true);
