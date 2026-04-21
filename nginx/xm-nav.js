(function() {
  if (document.getElementById('xm-nav')) return;

  var p = window.location.pathname;
  if (p.indexOf('/admin/backoffice') === 0) return;
  var siteContactUrl = '/portal/api/public/site-contact-config';
  var defaultContact = {
    contact_email: '',
    feedback_url: 'https://my.feishu.cn/share/base/form/shrcnQVnRPvEuOGjz9ojf05tD1d',
    wechat_qr_base64: ''
  };

  function mailIcon() {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.75 6.75h16.5v10.5H3.75z"></path><path d="m4.5 7.5 7.5 6 7.5-6"></path></svg>';
  }

  function wechatIcon() {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9.2 5.5c-3.5 0-6.2 2.2-6.2 5.1 0 1.6.8 3 2.2 4l-.6 2.4 2.6-1.3c.6.1 1.3.2 2 .2 3.5 0 6.2-2.2 6.2-5.1S12.7 5.5 9.2 5.5Z"></path><path d="M15.5 10.2c3 0 5.5 1.9 5.5 4.5 0 1.3-.7 2.5-1.8 3.3l.5 2-2.2-1.1c-.6.1-1.2.2-1.9.2-3 0-5.5-1.9-5.5-4.5s2.5-4.4 5.4-4.4Z"></path><circle cx="7.2" cy="10.5" r=".8" fill="currentColor" stroke="none"></circle><circle cx="11.2" cy="10.5" r=".8" fill="currentColor" stroke="none"></circle><circle cx="13.8" cy="14.6" r=".8" fill="currentColor" stroke="none"></circle><circle cx="17.2" cy="14.6" r=".8" fill="currentColor" stroke="none"></circle></svg>';
  }

  function feedbackIcon() {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3.75 4.75 8v8L12 20.25 19.25 16V8L12 3.75Z"></path><path d="M12 7.75v5.25"></path><circle cx="12" cy="15.8" r=".9" fill="currentColor" stroke="none"></circle></svg>';
  }

  function isActive(prefix) {
    if (prefix === '/') {
      return p === '/' || p.startsWith('/c/') || p === '';
    }
    return p.startsWith(prefix);
  }

  function cls(prefix) {
    return isActive(prefix) ? ' class="xm-active"' : '';
  }

  function hasTokenCookie() {
    try {
      return document.cookie.split(';').some(function(ck) {
        var t = ck.trim();
        return t.indexOf('token=') === 0 && t.length > 7 && t !== 'token=' && t !== 'token=""';
      });
    } catch (e) {
      return false;
    }
  }

  function readStoredToken() {
    try {
      var stored = localStorage.getItem('token');
      if (!stored || stored === '""') return '';
      if (stored.charAt(0) === '"' && stored.charAt(stored.length - 1) === '"') {
        stored = stored.slice(1, -1);
      }
      return stored || '';
    } catch (e) {
      return '';
    }
  }

  function ensurePortalTokenCookie() {
    if (hasTokenCookie()) return true;
    var storedToken = readStoredToken();
    if (!storedToken) return false;
    document.cookie = 'token=' + storedToken + '; path=/; SameSite=Lax';
    return hasTokenCookie();
  }

  // Detect login state from cookie or localStorage, and sync a cookie for portal auth if needed.
  var logged = ensurePortalTokenCookie();
  if (!logged) {
    logged = !!readStoredToken();
  }
    var storedToken = readStoredToken();
    var portalLinkSuffix = storedToken ? ('?t=' + encodeURIComponent(storedToken)) : '';

  document.documentElement.classList.add('xm-nav-active');
  if (document.body) {
    document.body.classList.add('xm-nav-active');
  }

  var el = document.createElement('div');
  el.id = 'xm-nav';
  el.innerHTML =
    '<div class="xm-links">' +
      '<a href="/" class="xm-brand">\uD83E\uDD90 \u867E\u5BC6\u5C0F\u52A9\u624B</a>' +
      '<a href="/"' + cls('/') + '>\u5BF9\u8BDD</a>' +
      '<a href="/portal/guide"' + cls('/portal/guide') + '>\u4F7F\u7528\u6307\u5357</a>' +
        '<a href="/portal/account' + portalLinkSuffix + '"' + cls('/portal/account') + '>\u8D26\u6237\u7BA1\u7406</a>' +
        '<a href="/portal/products' + portalLinkSuffix + '"' + cls('/portal/products') + '>\u8BA2\u9605\u4E0E\u5145\u503C</a>' +
    '</div>' +
    '<div class="xm-right">' +
      '<a href="#" id="xm-email-link" class="xm-action-link" aria-label="\u90AE\u4EF6\u8054\u7CFB" title="\u90AE\u4EF6\u8054\u7CFB">' + mailIcon() + '<span class="xm-action-text">\u90AE\u4EF6</span></a>' +
      '<button type="button" class="xm-action-link xm-action-button" id="xm-wechat-trigger" aria-label="\u4F01\u5FAE\u8054\u7CFB" title="\u4F01\u5FAE\u8054\u7CFB">' + wechatIcon() + '<span class="xm-action-text">\u4F01\u5FAE</span></button>' +
      '<a href="' + defaultContact.feedback_url + '" id="xm-feedback-link" target="_blank" rel="noreferrer" class="xm-action-link" aria-label="\u610F\u89C1\u53CD\u9988" title="\u610F\u89C1\u53CD\u9988">' + feedbackIcon() + '<span class="xm-action-text">\u53CD\u9988</span></a>' +
      (logged ? '<button type="button" class="xm-btn" id="xm-signout">\u9000\u51FA\u767B\u5F55</button>' : '') +
    '</div>';

  document.body.prepend(el);

  var modal = document.createElement('div');
  modal.id = 'xm-contact-modal';
  modal.setAttribute('hidden', 'hidden');
  modal.innerHTML =
    '<div class="xm-contact-backdrop" data-close="1"></div>' +
    '<div class="xm-contact-card" role="dialog" aria-modal="true" aria-labelledby="xm-contact-title">' +
      '<div class="xm-contact-header">' +
        '<div>' +
          '<div class="xm-contact-title" id="xm-contact-title">\u4F01\u5FAE\u8054\u7CFB</div>' +
          '<div class="xm-contact-note">\u626B\u7801\u5373\u53EF\u6DFB\u52A0\u4F01\u5FAE\uFF0C\u8BF7\u4F7F\u7528\u4E0B\u65B9\u4E8C\u7EF4\u7801\u8054\u7CFB。</div>' +
        '</div>' +
        '<button type="button" class="xm-contact-close" id="xm-contact-close" aria-label="\u5173\u95ED">×</button>' +
      '</div>' +
      '<div class="xm-contact-qr-wrap">' +
        '<img class="xm-contact-qr" id="xm-contact-qr" src="" alt="\u4F01\u5FAE\u4E8C\u7EF4\u7801" hidden />' +
        '<div class="xm-contact-qr-fallback" id="xm-contact-qr-fallback" hidden>\u5F53\u524D\u672A\u627E\u5230\u4F01\u5FAE\u4E8C\u7EF4\u7801\u56FE\u7247\uFF0C\u8BF7\u8054\u7CFB\u7BA1\u7406\u5458\u4E0A\u4F20。</div>' +
      '</div>' +
    '</div>';
  document.body.appendChild(modal);

  function openWechatModal() {
    modal.hidden = false;
    document.documentElement.classList.add('xm-contact-open');
    document.body.classList.add('xm-contact-open');
  }

  function closeWechatModal() {
    modal.hidden = true;
    document.documentElement.classList.remove('xm-contact-open');
    document.body.classList.remove('xm-contact-open');
  }

  var wechatTrigger = document.getElementById('xm-wechat-trigger');
  var emailLink = document.getElementById('xm-email-link');
  var feedbackLink = document.getElementById('xm-feedback-link');
  var closeBtn = document.getElementById('xm-contact-close');
  var qrImg = document.getElementById('xm-contact-qr');
  var qrFallback = document.getElementById('xm-contact-qr-fallback');

  function renderWechatQr(contact) {
    var qr = contact && contact.wechat_qr_base64 ? String(contact.wechat_qr_base64) : '';
    if (!qrImg || !qrFallback) return;
    if (!qr) {
      qrImg.hidden = true;
      qrImg.removeAttribute('src');
      qrFallback.hidden = false;
      return;
    }
    qrImg.src = qr;
    qrImg.alt = '\u4F01\u5FAE\u4E8C\u7EF4\u7801';
    qrImg.hidden = false;
    qrFallback.hidden = true;
  }

  function applyContactConfig(contact) {
    if (!contact) return;
    if (emailLink) {
      emailLink.href = contact.contact_email ? ('mailto:' + String(contact.contact_email)) : '#';
    }
    if (feedbackLink) {
      feedbackLink.href = contact.feedback_url ? String(contact.feedback_url) : defaultContact.feedback_url;
    }
    renderWechatQr(contact);
  }

  async function refreshContactConfig() {
    try {
      var response = await fetch(siteContactUrl, {
        cache: 'no-store',
        credentials: 'same-origin'
      });
      if (!response.ok) return null;
      var payload = await response.json();
      var contact = payload && payload.data ? payload.data.contact : null;
      applyContactConfig(contact);
      return contact;
    } catch (error) {
      return null;
    }
  }

  if (wechatTrigger) {
    wechatTrigger.addEventListener('click', async function() {
      await refreshContactConfig();
      openWechatModal();
    });
  }
  if (emailLink) {
    emailLink.addEventListener('click', async function(event) {
      event.preventDefault();
      var contact = await refreshContactConfig();
      var target = contact && contact.contact_email ? ('mailto:' + String(contact.contact_email)) : (emailLink.getAttribute('href') || '#');
      if (target && target !== '#') {
        window.location.href = target;
      }
    });
  }
  if (feedbackLink) {
    feedbackLink.addEventListener('click', async function(event) {
      event.preventDefault();
      var contact = await refreshContactConfig();
      var target = contact && contact.feedback_url ? String(contact.feedback_url) : (feedbackLink.getAttribute('href') || '#');
      if (target && target !== '#') {
        window.open(target, '_blank', 'noopener,noreferrer');
      }
    });
  }
  if (closeBtn) {
    closeBtn.addEventListener('click', closeWechatModal);
  }
  modal.addEventListener('click', function(event) {
    if (event.target && event.target.getAttribute('data-close') === '1') {
      closeWechatModal();
    }
  });
  document.addEventListener('keydown', function(event) {
    if (event.key === 'Escape' && !modal.hidden) {
      closeWechatModal();
    }
  });
  if (qrImg && qrFallback) {
    qrImg.addEventListener('error', function() {
      qrImg.hidden = true;
      qrFallback.hidden = false;
    });
  }
  applyContactConfig(defaultContact);
  refreshContactConfig();

  // Signout handler
  var btn = document.getElementById('xm-signout');
  if (btn) {
    btn.addEventListener('click', function() {
      fetch('/api/v1/auths/signout', {
        method: 'GET',
        credentials: 'same-origin',
        cache: 'no-store'
      }).catch(function() {
        return null;
      }).finally(function() {
        // Clear cookies
        ['token', 'oui-session', 'oauth_id_token'].forEach(function(n) {
          document.cookie = n + '=; Max-Age=0; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/; SameSite=Lax';
        });
        // Clear localStorage auth state (Open WebUI persists token here)
        try { localStorage.removeItem('token'); } catch(e) {}
        window.location.href = '/';
      });
    });
  }

  // ── Verification gate redirect ──
  // If user is logged-in but unverified and on Open WebUI (not portal),
  // redirect to /portal/account for forced email verification.
  var isPortalPage = p.startsWith('/portal/');

  function xmCheckVerification() {
    fetch('/portal/api/account', { credentials: 'same-origin' })
      .then(function(r) { return r.ok ? r.json() : null; })
      .then(function(json) {
        if (json && json.success && json.data) {
          var iv = json.data.identity_verification || {};
          if (iv.email_verification_required_before_portal_use && !iv.email_verified) {
            window.location.href = '/portal/account';
          }
        }
      })
      .catch(function() {});
  }

  if (!isPortalPage) {
    if (logged) {
      // Already logged in on Open WebUI page — check verification now
      xmCheckVerification();
    } else {
      // Not logged in yet — watch for token to appear after login
      var xmTokenPoll = setInterval(function() {
        var t = false;
        t = !!readStoredToken();
        if (t) {
          clearInterval(xmTokenPoll);
          ensurePortalTokenCookie();
          // Small delay to let Open WebUI finish its post-login setup
          setTimeout(xmCheckVerification, 600);
        }
      }, 800);
      // Stop polling after 10 minutes
      setTimeout(function() { clearInterval(xmTokenPoll); }, 600000);
    }
  }
})();
