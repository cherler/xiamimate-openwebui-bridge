(function() {
  if (document.getElementById('xm-nav')) return;

  var p = window.location.pathname;

  function isActive(prefix) {
    if (prefix === '/') {
      return p === '/' || p.startsWith('/c/') || p === '';
    }
    return p.startsWith(prefix);
  }

  function cls(prefix) {
    return isActive(prefix) ? ' class="xm-active"' : '';
  }

  // Detect login state from cookie or localStorage
  var logged = false;
  try {
    logged = document.cookie.split(';').some(function(ck) {
      var t = ck.trim();
      return t.indexOf('token=') === 0 && t.length > 7 && t !== 'token=' && t !== 'token=""';
    });
    if (!logged) {
      var stored = localStorage.getItem('token');
      logged = !!stored && stored !== '' && stored !== '""';
    }
  } catch(e) {}

  var el = document.createElement('div');
  el.id = 'xm-nav';
  el.innerHTML =
    '<div class="xm-links">' +
      '<a href="/" class="xm-brand">\uD83E\uDD90 \u867E\u5BC6\u5C0F\u52A9\u624B</a>' +
      '<a href="/"' + cls('/') + '>\u5BF9\u8BDD</a>' +
      '<a href="/portal/guide"' + cls('/portal/guide') + '>\u4F7F\u7528\u6307\u5357</a>' +
      '<a href="/portal/account"' + cls('/portal/account') + '>\u8D26\u6237\u7BA1\u7406</a>' +
      '<a href="/portal/products"' + cls('/portal/products') + '>\u8BA2\u9605\u4E0E\u5145\u503C</a>' +
    '</div>' +
    '<div class="xm-right">' +
      (logged ? '<button type="button" class="xm-btn" id="xm-signout">\u9000\u51FA\u767B\u5F55</button>' : '') +
    '</div>';

  document.body.prepend(el);

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
        try {
          var s = localStorage.getItem('token');
          t = !!s && s !== '' && s !== '""';
        } catch(e) {}
        if (t) {
          clearInterval(xmTokenPoll);
          // Small delay to let Open WebUI finish its post-login setup
          setTimeout(xmCheckVerification, 600);
        }
      }, 800);
      // Stop polling after 10 minutes
      setTimeout(function() { clearInterval(xmTokenPoll); }, 600000);
    }
  }
})();
