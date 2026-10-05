// LinkedIn Vault — adds "📥 Vault" to every post's action bar (Like / Comment / Repost / Send).
// LinkedIn changes its markup often, so every lookup has fallbacks.
(() => {
  const POST_SEL = [
    '[data-urn^="urn:li:activity"]', '[data-urn^="urn:li:share"]', '[data-urn^="urn:li:ugcPost"]',
    '[data-id^="urn:li:activity"]', '[data-id^="urn:li:share"]', '[data-id^="urn:li:ugcPost"]',
    '.feed-shared-update-v2'
  ].join(',');
  const TEXT_SEL = [
    '.update-components-text', '.feed-shared-inline-show-more-text', '.feed-shared-update-v2__description',
    '[data-test-id="main-feed-activity-card__commentary"]', '.update-components-update-v2__commentary'
  ].join(',');
  const SKIP_IMG = /profile-displayphoto|profile-framedphoto|company-logo|ghost|\/aero-v1\/|emoji|reaction/i;
  const URN_RE = /urn:li:(activity|share|ugcPost):\d{10,}/;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // ---------- locate action bars: the row containing the "Repost"/"Send" buttons
  function findActionBars() {
    const bars = new Set();
    document.querySelectorAll('.feed-shared-social-action-bar, .social-details-social-actions').forEach((b) => bars.add(b));
    document.querySelectorAll('button[aria-label*="Repost" i], button[aria-label^="Send" i]').forEach((btn) => {
      let el = btn.parentElement;
      for (let i = 0; i < 5 && el; i++, el = el.parentElement) {
        if (el.querySelectorAll('button').length >= 3) { bars.add(el); break; }
      }
    });
    return [...bars].filter((b) => !b.querySelector('.vault-btn') && !b.closest('.comments-comment-item, article.comments-comment-entity'));
  }

  function postFor(bar) {
    const p = bar.closest(POST_SEL);
    if (p) return p;
    // fallback: climb until we find a container that also holds an author link and some text
    let el = bar;
    for (let i = 0; i < 14 && el; i++, el = el.parentElement) {
      if (el.querySelector('a[href*="/in/"], a[href*="/company/"]') && (el.innerText || '').length > 80) return el;
    }
    return bar.parentElement;
  }

  function postUrn(post) {
    const attrs = [post.getAttribute('data-urn'), post.getAttribute('data-id')];
    for (const a of attrs) { const m = a && a.match(URN_RE); if (m) return m[0]; }
    const inner = post.querySelector('[data-urn*="urn:li:"], [data-id*="urn:li:"]');
    if (inner) { const m = (inner.getAttribute('data-urn') || inner.getAttribute('data-id') || '').match(URN_RE); if (m) return m[0]; }
    const link = post.querySelector('a[href*="urn:li:activity"], a[href*="/posts/"]');
    if (link) { const m = link.href.match(URN_RE); if (m) return m[0]; return link.href.split('?')[0]; }
    const m = location.href.match(URN_RE);
    return m ? m[0] : null;
  }

  async function expandText(post) {
    const more = [...post.querySelectorAll('button, span[role="button"]')].find((b) => {
      const t = (b.getAttribute('aria-label') || b.innerText || '').trim().toLowerCase();
      return t === '…more' || t === '...more' || t === 'more' || t.startsWith('see more') || t.includes('see more');
    });
    if (more) { more.click(); await sleep(400); }
  }

  function postText(post) {
    const el = post.querySelector(TEXT_SEL);
    if (el) return el.innerText.replace(/…\s*more$/i, '').trim();
    // fallback: the longest left-to-right text span inside the post
    let best = '';
    post.querySelectorAll('span[dir="ltr"], div[dir="ltr"]').forEach((s) => { if (s.innerText.length > best.length) best = s.innerText; });
    return best.trim();
  }

  function author(post) {
    const t = post.querySelector('.update-components-actor__title span[aria-hidden="true"], .update-components-actor__name span[aria-hidden="true"]');
    if (t) return t.innerText.trim();
    const a = post.querySelector('a[href*="/in/"], a[href*="/company/"]');
    return a ? (a.innerText || '').split('\n').map((s) => s.trim()).filter(Boolean)[0] || '' : '';
  }

  function headline(post) {
    const d = post.querySelector('.update-components-actor__description span[aria-hidden="true"]');
    return d ? d.innerText.trim() : '';
  }

  function bestSrc(img) {
    // prefer the largest candidate in srcset
    if (img.srcset) {
      const c = img.srcset.split(',').map((s) => s.trim().split(/\s+/)).sort((a, b) => parseInt(b[1] || 0) - parseInt(a[1] || 0));
      if (c[0] && c[0][0]) return c[0][0];
    }
    return img.currentSrc || img.src;
  }

  function images(post) {
    const out = [];
    post.querySelectorAll('img').forEach((img) => {
      const src = bestSrc(img);
      if (!src || !/media\.licdn\.com/.test(src) || SKIP_IMG.test(src)) return;
      const w = img.naturalWidth || img.width;
      if (w && w < 200) return;
      if (!out.includes(src)) out.push(src);
    });
    return out;
  }

  function documents(post) {
    const out = [];
    post.querySelectorAll('a[href$=".pdf"], a[href*=".pdf?"], [data-url*=".pdf"], iframe[src*="document"]').forEach((el) => {
      const u = el.href || el.getAttribute('data-url') || el.src;
      if (u && !out.includes(u)) out.push(u);
    });
    return out;
  }

  function links(post) {
    const out = [];
    post.querySelectorAll('a[href^="http"]').forEach((a) => {
      const h = a.href;
      if (/lnkd\.in|arxiv\.org|doi\.org|ieeexplore|\.pdf(\?|$)/i.test(h) || !/linkedin\.com/.test(h)) {
        if (!out.includes(h)) out.push(h);
      }
    });
    return out.slice(0, 30);
  }

  async function capture(post, btn) {
    btn.disabled = true; btn.textContent = '⏳ Saving';
    try {
      await expandText(post);
      const urn = postUrn(post);
      const url = urn && urn.startsWith('urn:') ? `https://www.linkedin.com/feed/update/${urn}/` : (urn || location.href);
      const payload = {
        url, text: postText(post), author: author(post), author_headline: headline(post),
        images: images(post), documents: documents(post), links: links(post), source: 'extension'
      };
      const res = await chrome.runtime.sendMessage({ type: 'capture', payload });
      if (res && res.ok) {
        btn.textContent = res.duplicate ? '✅ Already saved' : '✅ Saved';
        btn.classList.add('vault-ok');
      } else if (res && res.queued) {
        btn.textContent = '🕓 Queued (Pi offline)';
        btn.classList.add('vault-warn');
      } else {
        throw new Error((res && res.error) || 'unknown error');
      }
    } catch (e) {
      console.warn('[LinkedIn Vault]', e);
      btn.textContent = '⚠️ Retry'; btn.disabled = false; btn.classList.add('vault-warn');
    }
  }

  function addButtons() {
    for (const bar of findActionBars()) {
      const post = postFor(bar);
      if (!post || post.dataset.vaultBound) continue;
      post.dataset.vaultBound = '1';
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'vault-btn';
      btn.title = 'Save this post to your Vault (Pi)';
      btn.textContent = '📥 Vault';
      btn.addEventListener('click', (ev) => { ev.preventDefault(); ev.stopPropagation(); capture(post, btn); });
      bar.appendChild(btn);
    }
  }

  let t = null;
  new MutationObserver(() => { clearTimeout(t); t = setTimeout(addButtons, 400); })
    .observe(document.body, { childList: true, subtree: true });
  addButtons();
})();
